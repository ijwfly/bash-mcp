import asyncio
import os
import subprocess
from contextvars import ContextVar

import jwt
from mcp.server.fastmcp import FastMCP

BASH_TIMEOUT_MAX = int(os.environ.get("BASH_TIMEOUT_MAX", "300"))
PORT = int(os.environ.get("MCP_PORT", "8080"))
JWT_SECRET = os.environ.get("JWT_SECRET", "")
ALLOW_NO_AUTH = os.environ.get("ALLOW_NO_AUTH", "").lower() in ("1", "true", "yes")

current_user: ContextVar[str] = ContextVar("current_user")

mcp = FastMCP(
    "bash-mcp",
    stateless_http=True,
    host="0.0.0.0",
    port=PORT,
)


def ensure_user(linux_user: str) -> None:
    """Create OS user and workspace directory if they don't exist yet."""
    workspace = f"/workspace/{linux_user}"
    if os.path.isdir(workspace):
        return
    # Create OS user (ignore error if already exists)
    subprocess.run(
        ["useradd", "-m", "-s", "/bin/bash", linux_user],
        check=False,
        capture_output=True,
    )
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
# Tool
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
    )

    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        return {
            "stdout": stdout.decode(errors="replace"),
            "stderr": stderr.decode(errors="replace"),
            "exit_code": proc.returncode,
        }
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        return {
            "stdout": "",
            "stderr": f"Command timed out after {timeout} seconds",
            "exit_code": -1,
        }


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import uvicorn

    app = mcp.streamable_http_app()
    app = JWTAuthMiddleware(app)

    uvicorn.run(app, host="0.0.0.0", port=PORT)
