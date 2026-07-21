import asyncio
import json
import os
import time

import jwt
from mcp.server.fastmcp import FastMCP

import registry
from common import (
    ALLOW_NO_AUTH,
    BASH_TIMEOUT_MAX,
    ENABLE_FILE_TOOLS,
    JWT_SECRET,
    PORT,
    WORKSPACE_ROOT,
    PathOutsideWorkspace,
    current_user,
    ensure_user,
    get_current_user,
    kill_process_group,
    logger,
    resolve_path,
    run_file_op,
    workspace_for,
)
from http_api import build_routes

mcp = FastMCP(
    "bash-mcp",
    stateless_http=True,
    host="0.0.0.0",
    port=PORT,
)


# ---------------------------------------------------------------------------
# ASGI auth middleware
# ---------------------------------------------------------------------------

# Paths that bypass JWT auth: /health is public, /admin/* is guarded by
# ADMIN_TOKEN inside its handlers (http_api.py).
AUTH_EXEMPT_PREFIXES = ("/health", "/admin")


class JWTAuthMiddleware:
    """Pure ASGI middleware that verifies JWT Bearer tokens.

    Behaviour:
    - ``JWT_SECRET`` is set → tokens are verified (normal mode); the ``sub``
      claim must be a valid linux username AND an active user in the registry.
    - ``JWT_SECRET`` is empty and ``ALLOW_NO_AUTH=true`` → open access, no auth.
    - ``JWT_SECRET`` is empty and ``ALLOW_NO_AUTH`` is not set → all requests
      are rejected with 403 (safe default).
    """

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            # lifespan or websocket — pass through
            await self.app(scope, receive, send)
            return

        path = scope.get("path", "")
        if path == "/health" or any(
            path == p or path.startswith(p + "/") for p in AUTH_EXEMPT_PREFIXES
        ):
            await self.app(scope, receive, send)
            return

        if not JWT_SECRET:
            if ALLOW_NO_AUTH:
                await self.app(scope, receive, send)
                return
            await self._send_403(send, "Server has no JWT_SECRET configured. "
                                       "Set JWT_SECRET or ALLOW_NO_AUTH=true")
            return

        # Extract Authorization header
        headers = dict(scope.get("headers", []))
        auth_value = headers.get(b"authorization", b"").decode()

        if not auth_value.startswith("Bearer "):
            await self._send_401(send, "Missing or invalid Authorization header")
            return

        token = auth_value[7:]

        try:
            payload = jwt.decode(token, JWT_SECRET, algorithms=["HS256"])
        except jwt.ExpiredSignatureError:
            logger.warning("auth failed reason=expired path=%s", path)
            await self._send_401(send, "Token expired")
            return
        except jwt.InvalidTokenError:
            logger.warning("auth failed reason=invalid path=%s", path)
            await self._send_401(send, "Invalid token")
            return

        linux_user = payload.get("sub")
        if not linux_user:
            await self._send_401(send, "Token missing sub claim")
            return
        if not registry.LINUX_USER_RE.match(linux_user):
            logger.warning("auth failed reason=bad_sub sub=%r path=%s", linux_user, path)
            await self._send_401(send, "Invalid sub claim")
            return
        try:
            if not registry.is_active(linux_user):
                logger.warning("auth failed reason=unknown_or_revoked user=%s path=%s",
                               linux_user, path)
                await self._send_401(send, "Unknown or revoked user")
                return
        except registry.RegistryError as e:
            logger.error("registry unavailable: %s", e)
            await self._send_error(send, 503, "User registry unavailable")
            return

        ctx_token = current_user.set(linux_user)
        try:
            await self.app(scope, receive, send)
        finally:
            current_user.reset(ctx_token)

    @staticmethod
    async def _send_error(send, status: int, detail: str):
        body = json.dumps({"error": detail}).encode()
        await send({
            "type": "http.response.start",
            "status": status,
            "headers": [
                [b"content-type", b"application/json"],
                [b"content-length", str(len(body)).encode()],
            ],
        })
        await send({
            "type": "http.response.body",
            "body": body,
        })

    @classmethod
    async def _send_401(cls, send, detail: str):
        await cls._send_error(send, 401, detail)

    @classmethod
    async def _send_403(cls, send, detail: str):
        await cls._send_error(send, 403, detail)


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------

@mcp.tool()
async def bash_exec(command: str, timeout: int = 30) -> dict:
    """Executes a bash command in a subprocess. Each call is stateless —
    a new subshell is spawned every time, so state does not persist
    between calls (no shared environment variables, working directory
    resets on each invocation). Chain dependent commands with && or
    write a script if you need stateful execution.

    Work only inside your workspace directory — the ``cwd`` returned in the
    result. Files outside it are not writable, are not covered by the file
    tools or the artifacts API, and may disappear at any time."""
    timeout = max(1, min(timeout, BASH_TIMEOUT_MAX))

    linux_user = get_current_user()

    if linux_user:
        ensure_user(linux_user)
        cwd = workspace_for(linux_user)
        cmd = ["sudo", "-u", linux_user, "bash", "-c", command]
    else:
        cwd = WORKSPACE_ROOT
        cmd = ["bash", "-c", command]

    started = time.monotonic()
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=cwd,
        preexec_fn=os.setsid,
    )

    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        logger.info(
            "bash_exec user=%s exit=%s duration=%.2fs command=%r",
            linux_user, proc.returncode, time.monotonic() - started, command[:200],
        )
        return {
            "stdout": stdout.decode(errors="replace"),
            "stderr": stderr.decode(errors="replace"),
            "exit_code": proc.returncode,
            "cwd": cwd,
        }
    except asyncio.TimeoutError:
        # Kill entire process group (sudo + bash + all children)
        kill_process_group(proc)
        try:
            await asyncio.wait_for(proc.wait(), timeout=5)
        except asyncio.TimeoutError:
            pass
        logger.info(
            "bash_exec user=%s exit=timeout duration=%.2fs command=%r",
            linux_user, time.monotonic() - started, command[:200],
        )
        return {
            "stdout": "",
            "stderr": f"Command timed out after {timeout} seconds",
            "exit_code": -1,
            "cwd": cwd,
        }


async def _file_tool(op_name: str, path: str, payload: dict) -> dict:
    try:
        resolved, linux_user = resolve_path(path)
    except PathOutsideWorkspace as e:
        return {"error": str(e)}
    result = await run_file_op(linux_user, {**payload, "op": op_name, "path": resolved})
    logger.info("%s user=%s path=%s ok=%s", op_name, linux_user, resolved,
                "error" not in result)
    return result


async def read_file(path: str, limit: int = 0) -> dict:
    """Read the contents of a file. Paths are resolved relative to your
    workspace directory; paths outside the workspace are rejected.
    Use ``limit`` to return only the first N lines."""
    return await _file_tool("read", path, {"limit": limit})


async def write_file(path: str, content: str) -> dict:
    """Write content to a file, creating parent directories as needed.
    Paths are resolved relative to your workspace directory; paths outside
    the workspace are rejected."""
    return await _file_tool("write", path, {"content": content})


async def edit_file(path: str, old_text: str, new_text: str) -> dict:
    """Replace an exact occurrence of ``old_text`` with ``new_text`` in a file.
    ``old_text`` must appear exactly once; otherwise an error is returned.
    Paths are resolved relative to your workspace directory; paths outside
    the workspace are rejected."""
    return await _file_tool("edit", path, {"old_text": old_text, "new_text": new_text})


if ENABLE_FILE_TOOLS:
    mcp.tool()(read_file)
    mcp.tool()(write_file)
    mcp.tool()(edit_file)


# ---------------------------------------------------------------------------
# Startup reconciliation
# ---------------------------------------------------------------------------

def startup_reconcile() -> None:
    """Re-provision OS users for everyone in the registry.

    The workspace is a bind mount, so user dirs persist across container
    restarts while OS users do not — recreate them up front so `sudo -u`
    works immediately. On the very first start with a pre-existing workspace
    (upgrade path), import existing user_* dirs into the registry.
    """
    if not JWT_SECRET:
        return
    try:
        users = registry.list_users()
        if not users and os.path.isdir(WORKSPACE_ROOT):
            for name in sorted(os.listdir(WORKSPACE_ROOT)):
                if (registry.LINUX_USER_RE.match(name)
                        and os.path.isdir(os.path.join(WORKSPACE_ROOT, name))):
                    registry.add_user(name[len("user_"):])
                    logger.info("migrated existing workspace dir=%s into registry", name)
            users = registry.list_users()
        for user in users:
            if not user.get("revoked"):
                ensure_user(user["linux_user"])
    except registry.RegistryError as e:
        logger.error("startup reconcile failed: %s", e)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import uvicorn

    startup_reconcile()

    app = mcp.streamable_http_app()
    app.router.routes.extend(build_routes())
    app = JWTAuthMiddleware(app)

    uvicorn.run(app, host="0.0.0.0", port=PORT)
