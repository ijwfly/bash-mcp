# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What This Is

bash-mcp is a Docker container providing an MCP server (Streamable HTTP, JSON-RPC 2.0) that gives AI agents bash execution and file operations inside an isolated container, with optional JWT-based multi-user authentication.

## Build & Test

```bash
docker compose build              # build the image
rm -rf workspace/user_*           # clean leftover user dirs BEFORE running tests
./test.sh                         # run all 14 integration tests (builds, starts containers, tears down)
```

There are no unit tests — all testing is integration via `test.sh` which spins up Docker containers, generates JWT tokens, and exercises every endpoint.

**Important:** `docker-compose.yml` uses a bind mount (`./workspace:/workspace`), so user workspace directories (`workspace/user_*`) persist on the host between runs. `docker compose down --volumes` does NOT clean them. If stale dirs remain, `ensure_user()` may find the directory but the OS user won't exist inside a fresh container, causing `sudo -u` to fail with "unknown user". Always `rm -rf workspace/user_*` before running tests.

To run manually:

```bash
JWT_SECRET=secret docker compose up -d                          # auth mode
ALLOW_NO_AUTH=true docker compose up -d                         # open mode
ENABLE_FILE_TOOLS=false ALLOW_NO_AUTH=true docker compose up -d # bash-only mode
```

Generate tokens: `python3 server/generate_token.py --user-id alice --secret secret [--ttl 30d]`

## Architecture

### Request Flow

```
HTTP POST /mcp → JWTAuthMiddleware (ASGI) → FastMCP (stateless) → tool handler
```

- Auth middleware is pure ASGI wrapping FastMCP, uses `contextvars.ContextVar` to pass authenticated user identity to tools.
- Three auth modes: JWT enforced (JWT_SECRET set), open access (ALLOW_NO_AUTH=true), locked down (neither — 403).

### User Isolation

In auth mode, each JWT `sub` claim maps to a Linux user (`user_<sanitized>`). Users are lazily provisioned via `useradd` on first request.

- `bash_exec`: runs `sudo -u <user> bash -c "..."` with cwd `/workspace/<user>`
- File tools: delegate to `file_helper.py` via `sudo -u <user> python3 /opt/server/file_helper.py` — JSON over stdin/stdout protocol
- No-auth mode: everything runs as root, `/workspace` is cwd

### Process Group Management

All subprocesses use `preexec_fn=os.setsid` to create a new process group. On timeout, `os.killpg()` kills the entire tree (sudo → bash → children). This prevents zombie processes when commands launched via sudo hang.

### Conditional Tool Registration

File tools (`read_file`, `write_file`, `edit_file`) are defined as plain functions and conditionally registered via `mcp.tool()()` based on `ENABLE_FILE_TOOLS` env var.

## Key Files

- `server/main.py` — MCP server: auth middleware, all tool handlers, subprocess management
- `server/file_helper.py` — Standalone script for file ops, invoked as subprocess under sudo
- `server/generate_token.py` — CLI for JWT token generation with `sanitize_username()` logic
- `Dockerfile` — Multi-layer build; `COPY server/ /opt/server/` picks up all server files
- `test.sh` — Integration test suite (auth, isolation, file ops, timeouts, tool registration)

## Environment Variables

| Variable | Default | Effect |
|---|---|---|
| `JWT_SECRET` | empty | Set to enable JWT auth |
| `ALLOW_NO_AUTH` | false | Skip auth when JWT_SECRET is empty |
| `ENABLE_FILE_TOOLS` | true | Set `false` to expose only `bash_exec` |
| `BASH_TIMEOUT_MAX` | 300 | Upper bound for bash_exec timeout parameter |
| `MCP_PORT` | 8080 | Server listen port |

## Important Patterns

- **Stateless execution**: each `bash_exec` spawns a fresh shell — no state persists between calls.
- **`ensure_user()` checks OS user via `id` command**, not workspace directory existence (bind mounts persist dirs across container restarts).
- **File helper protocol**: `{"op": "read|write|edit", ...}` on stdin → `{"content": ...}` or `{"error": ...}` on stdout.
- **`_resolve_path()`** resolves relative paths against user workspace and returns `(abs_path, linux_user | None)` — all tools use this to determine auth context.
