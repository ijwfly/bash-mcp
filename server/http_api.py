"""HTTP routes mounted next to /mcp: health check, files API, admin API.

Auth model:
- /health           — no auth (exempted in JWTAuthMiddleware).
- /files/*          — JWT auth via JWTAuthMiddleware (current_user contextvar);
                      in no-auth mode operates as root rooted at /workspace.
- /admin/*          — exempted from JWT middleware; guarded by ADMIN_TOKEN here.
                      Routes are only registered when ENABLE_ADMIN_API=true and
                      ADMIN_TOKEN is set.
"""

import asyncio
import json
import os
import secrets
import shutil

from starlette.responses import JSONResponse, StreamingResponse
from starlette.routing import Route

import registry
from common import (
    ADMIN_TOKEN,
    ENABLE_ADMIN_API,
    JWT_SECRET,
    PathOutsideWorkspace,
    ensure_user,
    get_current_user,
    helper_cmd,
    kill_process_group,
    logger,
    resolve_path,
    run_file_op,
)

CHUNK_SIZE = 64 * 1024
STREAM_TIMEOUT = 600  # generous bound so a hung sudo/helper cannot leak forever


# ---------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------

async def health(request):
    return JSONResponse({"status": "ok"})


# ---------------------------------------------------------------------------
# Files API
# ---------------------------------------------------------------------------

def _error_status(error: str) -> int:
    lowered = error.lower()
    if "permission denied" in lowered:
        return 403
    if "not found" in lowered:
        return 404
    return 400


def _resolve_or_error(rel_path: str):
    """Returns (abs_path, linux_user, None) or (None, None, error_response)."""
    try:
        abs_path, linux_user = resolve_path(rel_path)
        return abs_path, linux_user, None
    except PathOutsideWorkspace as e:
        return None, None, JSONResponse({"error": str(e)}, status_code=400)


async def files_get(request):
    rel_path = request.path_params.get("path", "")
    abs_path, linux_user, err = _resolve_or_error(rel_path)
    if err:
        return err

    st = await run_file_op(linux_user, {"op": "stat", "path": abs_path})
    if "error" in st:
        return JSONResponse({"error": st["error"]}, status_code=_error_status(st["error"]))
    if st["type"] == "missing":
        return JSONResponse({"error": f"Not found: {rel_path or '/'}"}, status_code=404)

    if st["type"] == "dir":
        result = await run_file_op(linux_user, {"op": "list", "path": abs_path})
        if "error" in result:
            return JSONResponse({"error": result["error"]}, status_code=_error_status(result["error"]))
        logger.info("files list user=%s path=%s", linux_user, abs_path)
        return JSONResponse({"path": rel_path or "/", "entries": result["entries"]})

    return await _stream_download(linux_user, abs_path)


async def _stream_download(linux_user, abs_path):
    proc = await asyncio.create_subprocess_exec(
        *helper_cmd(linux_user, "--stream-read", abs_path),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        preexec_fn=os.setsid,
    )
    first = await proc.stdout.read(CHUNK_SIZE)
    if not first:
        stderr = (await proc.stderr.read()).decode(errors="replace").strip()
        await proc.wait()
        if proc.returncode != 0:
            return JSONResponse(
                {"error": stderr or "read failed"},
                status_code=_error_status(stderr),
            )

    async def body():
        try:
            if first:
                yield first
            while True:
                chunk = await proc.stdout.read(CHUNK_SIZE)
                if not chunk:
                    break
                yield chunk
            await proc.wait()
        finally:
            if proc.returncode is None:
                kill_process_group(proc)

    filename = os.path.basename(abs_path).replace('"', "")
    logger.info("files download user=%s path=%s", linux_user, abs_path)
    return StreamingResponse(
        body(),
        media_type="application/octet-stream",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


async def files_put(request):
    rel_path = request.path_params.get("path", "")
    if not rel_path or rel_path.endswith("/"):
        return JSONResponse({"error": "Upload path must name a file"}, status_code=400)
    abs_path, linux_user, err = _resolve_or_error(rel_path)
    if err:
        return err

    proc = await asyncio.create_subprocess_exec(
        *helper_cmd(linux_user, "--stream-write", abs_path),
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        preexec_fn=os.setsid,
    )
    try:
        async for chunk in request.stream():
            if chunk:
                proc.stdin.write(chunk)
                await proc.stdin.drain()
        proc.stdin.close()
    except (BrokenPipeError, ConnectionResetError):
        pass  # helper died early — its stderr explains why

    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=STREAM_TIMEOUT)
    except asyncio.TimeoutError:
        kill_process_group(proc)
        return JSONResponse({"error": "Upload timed out"}, status_code=500)

    if proc.returncode != 0:
        error = stderr.decode(errors="replace").strip() or "write failed"
        return JSONResponse({"error": error}, status_code=_error_status(error))

    result = json.loads(stdout.decode())
    logger.info("files upload user=%s path=%s size=%s", linux_user, abs_path, result.get("size"))
    return JSONResponse(result)


async def files_delete(request):
    rel_path = request.path_params.get("path", "")
    if not rel_path:
        return JSONResponse({"error": "Delete path must name a file"}, status_code=400)
    abs_path, linux_user, err = _resolve_or_error(rel_path)
    if err:
        return err

    result = await run_file_op(linux_user, {"op": "delete", "path": abs_path})
    if "error" in result:
        return JSONResponse({"error": result["error"]}, status_code=_error_status(result["error"]))
    logger.info("files delete user=%s path=%s", linux_user, abs_path)
    return JSONResponse(result)


# ---------------------------------------------------------------------------
# Admin API
# ---------------------------------------------------------------------------

def _check_admin(request):
    auth = request.headers.get("authorization", "")
    token = auth[7:] if auth.startswith("Bearer ") else ""
    if not token or not secrets.compare_digest(token, ADMIN_TOKEN):
        logger.warning("admin auth failed path=%s", request.url.path)
        return JSONResponse({"error": "Invalid admin token"}, status_code=401)
    return None


def _parse_ttl_arg(ttl) -> tuple[int | None, JSONResponse | None]:
    """Returns (ttl_seconds | None for no expiry, error_response | None)."""
    if ttl in (None, ""):
        ttl = "30d"
    if ttl == "none":
        return None, None
    try:
        return registry.parse_ttl(ttl), None
    except ValueError as e:
        return None, JSONResponse({"error": str(e)}, status_code=400)


def _issue_token(user_id: str, linux_user: str, ttl_seconds: int | None) -> dict:
    token = registry.make_token(user_id, linux_user, JWT_SECRET, ttl_seconds)
    return {
        "user_id": user_id,
        "linux_user": linux_user,
        "token": token,
        "expires_in": ttl_seconds,
    }


async def admin_create_user(request):
    if err := _check_admin(request):
        return err
    if not JWT_SECRET:
        return JSONResponse(
            {"error": "Admin API requires JWT_SECRET to issue tokens"}, status_code=400
        )
    try:
        body = await request.json()
    except json.JSONDecodeError:
        return JSONResponse({"error": "Invalid JSON body"}, status_code=400)
    user_id = body.get("user_id")
    if not user_id or not isinstance(user_id, str):
        return JSONResponse({"error": "user_id is required"}, status_code=400)
    ttl_seconds, err = _parse_ttl_arg(body.get("ttl"))
    if err:
        return err

    try:
        linux_user = registry.add_user(user_id, reactivate=bool(body.get("reactivate")))
    except registry.UserCollision as e:
        return JSONResponse({"error": str(e)}, status_code=409)
    except registry.UserRevoked as e:
        return JSONResponse(
            {"error": f"{e}. Pass \"reactivate\": true to re-activate."}, status_code=409
        )
    ensure_user(linux_user)
    logger.info("admin created user_id=%s linux_user=%s", user_id, linux_user)
    return JSONResponse(_issue_token(user_id, linux_user, ttl_seconds), status_code=201)


async def admin_list_users(request):
    if err := _check_admin(request):
        return err
    return JSONResponse({"users": registry.list_users()})


async def admin_revoke_user(request):
    if err := _check_admin(request):
        return err
    user_id = request.path_params["user_id"]
    try:
        linux_user = registry.revoke_user(user_id)
    except KeyError:
        return JSONResponse({"error": f"Unknown user: {user_id}"}, status_code=404)

    deleted_workspace = False
    if request.query_params.get("delete_workspace", "").lower() in ("1", "true", "yes"):
        workspace = f"/workspace/{linux_user}"
        if registry.LINUX_USER_RE.match(linux_user) and os.path.isdir(workspace):
            shutil.rmtree(workspace, ignore_errors=True)
            deleted_workspace = True
    logger.info("admin revoked user=%s deleted_workspace=%s", linux_user, deleted_workspace)
    return JSONResponse({
        "status": "revoked",
        "linux_user": linux_user,
        "deleted_workspace": deleted_workspace,
    })


async def admin_issue_token(request):
    if err := _check_admin(request):
        return err
    if not JWT_SECRET:
        return JSONResponse(
            {"error": "Admin API requires JWT_SECRET to issue tokens"}, status_code=400
        )
    user_id = request.path_params["user_id"]
    entry = registry.find(user_id)
    if entry is None:
        return JSONResponse({"error": f"Unknown user: {user_id}"}, status_code=404)
    if entry.get("revoked"):
        return JSONResponse({"error": f"User is revoked: {user_id}"}, status_code=409)
    try:
        body = await request.json()
    except json.JSONDecodeError:
        body = {}
    ttl_seconds, err = _parse_ttl_arg(body.get("ttl"))
    if err:
        return err
    logger.info("admin issued token user=%s", entry["linux_user"])
    return JSONResponse(_issue_token(entry["user_id"], entry["linux_user"], ttl_seconds))


# ---------------------------------------------------------------------------
# Route assembly
# ---------------------------------------------------------------------------

def build_routes() -> list[Route]:
    routes = [
        Route("/health", health, methods=["GET"]),
        Route("/files", files_get, methods=["GET"]),
        Route("/files/{path:path}", files_get, methods=["GET"]),
        Route("/files/{path:path}", files_put, methods=["PUT"]),
        Route("/files/{path:path}", files_delete, methods=["DELETE"]),
    ]
    if ENABLE_ADMIN_API and ADMIN_TOKEN:
        routes += [
            Route("/admin/users", admin_create_user, methods=["POST"]),
            Route("/admin/users", admin_list_users, methods=["GET"]),
            Route("/admin/users/{user_id}", admin_revoke_user, methods=["DELETE"]),
            Route("/admin/users/{user_id}/token", admin_issue_token, methods=["POST"]),
        ]
        logger.info("admin API enabled")
    return routes
