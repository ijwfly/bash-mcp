import asyncio
import json
import os
import signal
import subprocess
from contextvars import ContextVar

import jwt
from mcp.server.fastmcp import FastMCP

BASH_TIMEOUT_MAX = int(os.environ.get("BASH_TIMEOUT_MAX", "300"))
PORT = int(os.environ.get("MCP_PORT", "8080"))
JWT_SECRET = os.environ.get("JWT_SECRET", "")
ALLOW_NO_AUTH = os.environ.get("ALLOW_NO_AUTH", "").lower() in ("1", "true", "yes")
ENABLE_FILE_TOOLS = os.environ.get("ENABLE_FILE_TOOLS", "").lower() not in ("0", "false", "no")

current_user: ContextVar[str] = ContextVar("current_user")

mcp = FastMCP(
    "bash-mcp",
    stateless_http=True,
    host="0.0.0.0",
    port=PORT,
)


def ensure_user(linux_user: str) -> None:
    """Create OS user and workspace directory if they don't exist yet."""
    # Check for the OS user, not the directory — the workspace dir may
    # persist across container restarts via a bind mount.
    r = subprocess.run(["id", linux_user], capture_output=True)
    if r.returncode != 0:
        subprocess.run(
            ["useradd", "-m", "-s", "/bin/bash", linux_user],
            check=False,
            capture_output=True,
        )
    workspace = f"/workspace/{linux_user}"
    os.makedirs(workspace, exist_ok=True)
    subprocess.run(["chown", "-R", f"{linux_user}:{linux_user}", workspace], check=False)


# ---------------------------------------------------------------------------
# ASGI auth middleware
# ---------------------------------------------------------------------------

class JWTAuthMiddleware:
    """Pure ASGI middleware that verifies JWT Bearer tokens.

    Behaviour:
    - ``JWT_SECRET`` is set → tokens are verified (normal mode).
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
            await self._send_401(send, "Token expired")
            return
        except jwt.InvalidTokenError:
            await self._send_401(send, "Invalid token")
            return

        linux_user = payload.get("sub")
        if not linux_user:
            await self._send_401(send, "Token missing sub claim")
            return
        ctx_token = current_user.set(linux_user)
        try:
            await self.app(scope, receive, send)
        finally:
            current_user.reset(ctx_token)

    @staticmethod
    async def _send_error(send, status: int, detail: str):
        import json as _json

        body = _json.dumps({"error": detail}).encode()
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
# Helpers
# ---------------------------------------------------------------------------

def _resolve_path(path: str) -> tuple[str, str | None]:
    """Return (absolute_path, linux_user | None)."""
    try:
        linux_user = current_user.get()
    except LookupError:
        linux_user = None

    if linux_user:
        ensure_user(linux_user)
        cwd = f"/workspace/{linux_user}"
    else:
        cwd = "/workspace"

    if not os.path.isabs(path):
        path = os.path.join(cwd, path)
    path = os.path.realpath(path)

    return path, linux_user


async def _run_file_op(linux_user: str, payload: dict) -> dict:
    """Run a file operation as linux_user via the file_helper.py subprocess."""
    cmd = ["sudo", "-u", linux_user, "python3", "/opt/server/file_helper.py"]
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        preexec_fn=os.setsid,
    )
    stdin_data = json.dumps(payload).encode()
    try:
        stdout, stderr = await asyncio.wait_for(
            proc.communicate(input=stdin_data), timeout=30
        )
    except asyncio.TimeoutError:
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            proc.kill()
        try:
            await asyncio.wait_for(proc.wait(), timeout=5)
        except asyncio.TimeoutError:
            pass
        return {"error": "File operation timed out"}
    if proc.returncode != 0:
        return {"error": stderr.decode(errors="replace").strip() or "Helper failed"}
    return json.loads(stdout.decode())


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------

@mcp.tool()
async def bash_exec(command: str, timeout: int = 30) -> dict:
    """Executes a bash command in a subprocess. Each call is stateless —
    a new subshell is spawned every time, so state does not persist
    between calls (no shared environment variables, working directory
    resets to /workspace on each invocation). Chain dependent commands
    with && or write a script if you need stateful execution."""
    timeout = max(1, min(timeout, BASH_TIMEOUT_MAX))

    # Determine user context
    try:
        linux_user = current_user.get()
    except LookupError:
        linux_user = None

    if linux_user:
        ensure_user(linux_user)
        cwd = f"/workspace/{linux_user}"
        cmd = ["sudo", "-u", linux_user, "bash", "-c", command]
    else:
        cwd = "/workspace"
        cmd = ["bash", "-c", command]

    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=cwd,
        preexec_fn=os.setsid,
    )

    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        return {
            "stdout": stdout.decode(errors="replace"),
            "stderr": stderr.decode(errors="replace"),
            "exit_code": proc.returncode,
        }
    except asyncio.TimeoutError:
        # Kill entire process group (sudo + bash + all children)
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            proc.kill()
        try:
            await asyncio.wait_for(proc.wait(), timeout=5)
        except asyncio.TimeoutError:
            pass
        return {
            "stdout": "",
            "stderr": f"Command timed out after {timeout} seconds",
            "exit_code": -1,
        }


async def read_file(path: str, limit: int = 0) -> dict:
    """Read the contents of a file. Paths are resolved relative to the
    user's workspace directory. Use ``limit`` to return only the first N lines."""
    resolved, linux_user = _resolve_path(path)

    if linux_user:
        return await _run_file_op(linux_user, {"op": "read", "path": resolved, "limit": limit})

    try:
        with open(resolved, "r", encoding="utf-8", errors="replace") as f:
            content = f.read()
    except FileNotFoundError:
        return {"error": f"File not found: {resolved}"}
    except IsADirectoryError:
        return {"error": f"Is a directory: {resolved}"}
    except PermissionError:
        return {"error": f"Permission denied: {resolved}"}

    lines = content.splitlines(keepends=True)
    if limit > 0:
        lines = lines[:limit]
        content = "".join(lines)

    return {
        "content": content,
        "size": os.path.getsize(resolved),
        "lines": len(lines),
    }


async def write_file(path: str, content: str) -> dict:
    """Write content to a file, creating parent directories as needed.
    Paths are resolved relative to the user's workspace directory."""
    resolved, linux_user = _resolve_path(path)

    if linux_user:
        return await _run_file_op(linux_user, {"op": "write", "path": resolved, "content": content})

    try:
        parent = os.path.dirname(resolved)
        os.makedirs(parent, exist_ok=True)
        with open(resolved, "w", encoding="utf-8") as f:
            f.write(content)
    except PermissionError:
        return {"error": f"Permission denied: {resolved}"}
    except IsADirectoryError:
        return {"error": f"Is a directory: {resolved}"}

    return {
        "status": "ok",
        "size": os.path.getsize(resolved),
        "path": resolved,
    }


async def edit_file(path: str, old_text: str, new_text: str) -> dict:
    """Replace an exact occurrence of ``old_text`` with ``new_text`` in a file.
    ``old_text`` must appear exactly once; otherwise an error is returned.
    Paths are resolved relative to the user's workspace directory."""
    resolved, linux_user = _resolve_path(path)

    if linux_user:
        return await _run_file_op(linux_user, {
            "op": "edit", "path": resolved,
            "old_text": old_text, "new_text": new_text,
        })

    try:
        with open(resolved, "r", encoding="utf-8", errors="replace") as f:
            content = f.read()
    except FileNotFoundError:
        return {"error": f"File not found: {resolved}"}
    except IsADirectoryError:
        return {"error": f"Is a directory: {resolved}"}
    except PermissionError:
        return {"error": f"Permission denied: {resolved}"}

    count = content.count(old_text)
    if count == 0:
        return {"error": "old_text not found"}
    if count > 1:
        return {"error": f"old_text found {count} times, must be unique"}

    new_content = content.replace(old_text, new_text, 1)

    try:
        with open(resolved, "w", encoding="utf-8") as f:
            f.write(new_content)
    except PermissionError:
        return {"error": f"Permission denied: {resolved}"}

    return {"status": "ok", "replacements": 1}


if ENABLE_FILE_TOOLS:
    mcp.tool()(read_file)
    mcp.tool()(write_file)
    mcp.tool()(edit_file)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import uvicorn

    app = mcp.streamable_http_app()
    app = JWTAuthMiddleware(app)

    uvicorn.run(app, host="0.0.0.0", port=PORT)
