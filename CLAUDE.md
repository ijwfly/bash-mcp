# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What This Is

bash-mcp is a Docker container providing an MCP server (Streamable HTTP, JSON-RPC 2.0) that gives AI agents bash execution and file operations inside an isolated container, with JWT-based multi-user authentication, a server-side user registry, an admin API, and a REST files API for artifacts.

## Build & Test

```bash
docker compose build              # build the image
./test.sh                         # run all integration tests (builds, starts containers, cleans workspace, tears down)
```

There are no unit tests — all testing is integration via `test.sh`, which spins up Docker containers, generates JWT tokens via the in-container CLI, and exercises every endpoint. `test.sh` cleans `workspace/user_*` and `workspace/.users.json` itself at the start and between phases.

To run manually:

```bash
JWT_SECRET=secret ENABLE_ADMIN_API=true ADMIN_TOKEN=admin docker compose up -d  # full auth mode
ALLOW_NO_AUTH=true docker compose up -d                                         # open mode (root, local only)
ENABLE_FILE_TOOLS=false ALLOW_NO_AUTH=true docker compose up -d                 # bash-only mode
```

Create users + tokens: `docker compose exec bash-mcp python3 /opt/server/generate_token.py --user-id alice [--ttl 30d | --no-expiry]` (registers the user in the registry and provisions the OS user in one step). From the host add `--secret <secret> --registry workspace/.users.json`.

## Architecture

### Request Flow

```
HTTP → JWTAuthMiddleware (ASGI) ─→ /mcp (FastMCP, stateless) → tool handler
                                ─→ /files/* (Starlette routes, same JWT)
                                ─→ /admin/* (exempt from JWT; ADMIN_TOKEN check in handlers)
                                ─→ /health (no auth)
```

- Auth middleware is pure ASGI wrapping the Starlette app; uses `contextvars.ContextVar` to pass the authenticated user to tools and files handlers.
- Three auth modes: JWT enforced (JWT_SECRET set), open access (ALLOW_NO_AUTH=true), locked down (neither — 403).
- In JWT mode the decoded `sub` must match `^user_[a-z0-9_]{1,27}$` AND be an active (non-revoked) entry in the user registry — otherwise 401.

### User Registry

`server/registry.py` — JSON file at `/workspace/.users.json` (bind-mounted, persists), root-owned 600. Mutations use an fcntl file lock (`.users.json.lock`) because both the server and the CLI (docker exec) write it; reads are cached by mtime. Revocation = `revoked: true`; all the user's tokens stop working on the next request.

`startup_reconcile()` in `main.py` runs at boot: re-provisions OS users for every active registry entry (fixes the stale-bind-mount / "unknown user" problem), and on first start imports pre-existing `workspace/user_*` dirs into the registry (upgrade path).

### User Isolation

Each registered user maps to a Linux user (`user_<sanitized>`, max 32 chars total) in the `mcpusers` group, workspace `/workspace/<user>` chmod 700.

- `bash_exec`: runs `sudo -u <user> bash -c "..."` with cwd `/workspace/<user>`; returns `cwd` in the result to orient agents.
- File tools and files API: delegate to `file_helper.py` via `sudo -u <user>` — JSON over stdin/stdout, or `--stream-read`/`--stream-write` raw-byte modes for HTTP streaming.
- Path containment: `resolve_path()` in `common.py` realpath-resolves every path and rejects anything outside the caller's workspace (raises `PathOutsideWorkspace`). Applies to file tools AND the files API; `bash_exec` is intentionally unrestricted bash — cross-user access there is blocked by Linux permissions only.
- Resource limits: `entrypoint.sh` renders `/etc/security/limits.d/bash-mcp.conf` (`@mcpusers` nproc/as/nofile from `USER_MAX_PROCS`/`USER_MAX_MEM_MB`); applied to sudo sessions via pam_limits (line ensured in `/etc/pam.d/sudo` by the Dockerfile). Container-level `mem_limit`/`cpus`/`pids_limit` in docker-compose.
- No-auth mode: everything runs as root, `/workspace` is the base; path containment still applies to file tools/files API.

### Process Group Management

All subprocesses use `preexec_fn=os.setsid`; on timeout `kill_process_group()` (common.py) kills the entire tree (sudo → bash → children).

### Conditional Registration

- File tools (`read_file`, `write_file`, `edit_file`): registered via `mcp.tool()()` based on `ENABLE_FILE_TOOLS`.
- Admin routes: only added by `build_routes()` (http_api.py) when `ENABLE_ADMIN_API=true` AND `ADMIN_TOKEN` is set — otherwise 404.

## Key Files

- `server/main.py` — MCP tools, JWT middleware, startup reconciliation, app assembly
- `server/common.py` — shared config/env, logger, `ensure_user()`, `resolve_path()` (containment), helper subprocess plumbing
- `server/registry.py` — user registry (fcntl-locked JSON), `sanitize_username()`, `parse_ttl()`, `make_token()`; host-compatible (no server deps)
- `server/http_api.py` — `/health`, `/files/*` (streaming via helper subprocess), `/admin/*`
- `server/file_helper.py` — file ops under sudo: JSON protocol (read/write/edit/stat/list/delete) + `--stream-read`/`--stream-write`
- `server/generate_token.py` — CLI: registers user + issues token (in-container: secret from env; host: `--secret`/`--registry`)
- `Dockerfile` — sudoers, `mcpusers` group, pam_limits; `COPY server/ /opt/server/`
- `entrypoint.sh` — renders limits.d config, starts server
- `test.sh` — integration suite (auth, registry, admin API, files API, containment, limits, reconciliation)

## Environment Variables

| Variable | Default | Effect |
|---|---|---|
| `JWT_SECRET` | empty | Set to enable JWT auth |
| `ALLOW_NO_AUTH` | false | Skip auth when JWT_SECRET is empty (root, local only) |
| `ENABLE_FILE_TOOLS` | true | Set `false` to expose only `bash_exec` |
| `ENABLE_ADMIN_API` | false | Enable `/admin/*` (also needs `ADMIN_TOKEN`) |
| `ADMIN_TOKEN` | empty | Static bearer token for the admin API |
| `BASH_TIMEOUT_MAX` | 300 | Upper bound for bash_exec timeout parameter |
| `USER_MAX_PROCS` | 256 | Per-user nproc limit (pam_limits) |
| `USER_MAX_MEM_MB` | 4096 | Per-user virtual memory limit, MB |
| `MCP_PORT` | 8080 | Server listen port |

See `.env.example` for the full annotated list including container-level limits.

## Important Patterns

- **Stateless execution**: each `bash_exec` spawns a fresh shell — no state persists between calls.
- **`ensure_user()` checks OS user via `id` command**, not workspace directory existence (bind mounts persist dirs across container restarts). `chown -R` runs only on first provision or owner mismatch.
- **File helper protocol**: `{"op": "read|write|edit|stat|list|delete", ...}` on stdin → JSON on stdout; `--stream-read`/`--stream-write` argv modes move raw bytes for the files API (so root never opens user files directly — no TOCTOU).
- **Containment errors**: tools return `{"error": "Path outside workspace: ..."}`; files API returns HTTP 400. Cross-user reads/writes via `bash_exec` are blocked by 700 permissions (not enforced on macOS Docker bind mounts — test 24 is skipped there).
- **Registry is the source of auth truth**: a validly-signed token is useless unless its `sub` is an active registry entry. Anyone holding `JWT_SECRET` can mint tokens, so treat the secret as root-equivalent.
