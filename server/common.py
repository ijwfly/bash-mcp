"""Shared config, logging, user provisioning and path resolution.

Imported by main.py (tools + middleware) and http_api.py (admin/files routes).
"""

import asyncio
import json
import logging
import os
import pwd
import signal
import subprocess
from contextvars import ContextVar

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("bash-mcp")

BASH_TIMEOUT_MAX = int(os.environ.get("BASH_TIMEOUT_MAX", "300"))
PORT = int(os.environ.get("MCP_PORT", "8080"))
JWT_SECRET = os.environ.get("JWT_SECRET", "")
ALLOW_NO_AUTH = os.environ.get("ALLOW_NO_AUTH", "").lower() in ("1", "true", "yes")
ENABLE_FILE_TOOLS = os.environ.get("ENABLE_FILE_TOOLS", "").lower() not in ("0", "false", "no")
ENABLE_ADMIN_API = os.environ.get("ENABLE_ADMIN_API", "").lower() in ("1", "true", "yes")
ADMIN_TOKEN = os.environ.get("ADMIN_TOKEN", "")

WORKSPACE_ROOT = "/workspace"
HELPER_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "file_helper.py")

current_user: ContextVar[str] = ContextVar("current_user")


class PathOutsideWorkspace(Exception):
    pass


def get_current_user() -> str | None:
    try:
        return current_user.get()
    except LookupError:
        return None


def workspace_for(linux_user: str | None) -> str:
    return f"{WORKSPACE_ROOT}/{linux_user}" if linux_user else WORKSPACE_ROOT


def ensure_user(linux_user: str) -> None:
    """Create OS user and workspace directory if they don't exist yet."""
    # Check for the OS user, not the directory — the workspace dir may
    # persist across container restarts via a bind mount.
    r = subprocess.run(["id", linux_user], capture_output=True)
    created = r.returncode != 0
    if created:
        subprocess.run(
            ["useradd", "-m", "-s", "/bin/bash", "-G", "mcpusers", linux_user],
            check=False,
            capture_output=True,
        )
        logger.info("provisioned linux user=%s", linux_user)
    workspace = workspace_for(linux_user)
    os.makedirs(workspace, exist_ok=True)
    try:
        os.chmod(workspace, 0o700)
    except OSError:
        pass  # bind mounts on some hosts (macOS) may not support chmod
    try:
        uid = pwd.getpwnam(linux_user).pw_uid
        if os.stat(workspace).st_uid != uid:
            subprocess.run(["chown", "-R", f"{linux_user}:{linux_user}", workspace], check=False)
    except (KeyError, OSError):
        pass


def resolve_path(path: str) -> tuple[str, str | None]:
    """Resolve a tool/API path against the caller's workspace.

    Returns (absolute_path, linux_user | None). Raises PathOutsideWorkspace if
    the resolved path (symlinks included) escapes the workspace.
    """
    linux_user = get_current_user()

    if linux_user:
        ensure_user(linux_user)
    base = workspace_for(linux_user)

    if not os.path.isabs(path):
        path = os.path.join(base, path)
    resolved = os.path.realpath(path)

    if resolved != base and not resolved.startswith(base + os.sep):
        raise PathOutsideWorkspace(
            f"Path outside workspace: {path!r} (workspace is {base})"
        )
    return resolved, linux_user


def helper_cmd(linux_user: str | None, *args: str) -> list[str]:
    """Build the file_helper.py invocation, under sudo when a user is set."""
    base = ["python3", HELPER_PATH, *args]
    if linux_user:
        return ["sudo", "-u", linux_user, *base]
    return base


def kill_process_group(proc) -> None:
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        proc.kill()


async def run_file_op(linux_user: str | None, payload: dict, timeout: int = 30) -> dict:
    """Run a JSON file operation via file_helper.py (as linux_user, or root)."""
    proc = await asyncio.create_subprocess_exec(
        *helper_cmd(linux_user),
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        preexec_fn=os.setsid,
    )
    stdin_data = json.dumps(payload).encode()
    try:
        stdout, stderr = await asyncio.wait_for(
            proc.communicate(input=stdin_data), timeout=timeout
        )
    except asyncio.TimeoutError:
        kill_process_group(proc)
        try:
            await asyncio.wait_for(proc.wait(), timeout=5)
        except asyncio.TimeoutError:
            pass
        return {"error": "File operation timed out"}
    if proc.returncode != 0:
        return {"error": stderr.decode(errors="replace").strip() or "Helper failed"}
    return json.loads(stdout.decode())
