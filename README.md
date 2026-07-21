# bash-mcp

A Docker container with an MCP server that gives AI agents the ability to execute bash commands in an isolated environment.

## Features

- **bash_exec** — execute arbitrary bash commands with configurable timeout
- **read_file** / **write_file** / **edit_file** — direct file operations without shell escaping hassles, contained to the user's workspace
- **Files API** — REST upload/download/list/delete of artifacts in the user's workspace
- **JWT authentication** — HMAC-SHA256 JWT auth with per-user OS-level workspace isolation and a server-side user registry (revocation supported)
- **Admin API** — HTTP endpoints to create, list and revoke users (opt-in)
- **Resource limits** — per-user process/memory limits (fork-bomb protection) plus container-level limits
- **Base image** — extend with `FROM bash-mcp:latest`
- **Preinstalled tools** — curl, wget, jq, git, python3, and more

## Quick Start

```bash
cp .env.example .env    # set JWT_SECRET (and optionally ADMIN_TOKEN)
docker compose up -d --build
```

The server starts at `http://localhost:8080/mcp` (Streamable HTTP). A health check is at `http://localhost:8080/health`.

### Creating Users & Tokens

Users must be registered before their tokens are accepted. The CLI registers the user and prints a token in one step:

```bash
docker compose exec bash-mcp python3 /opt/server/generate_token.py --user-id alice
docker compose exec bash-mcp python3 /opt/server/generate_token.py --user-id alice --ttl 24h
docker compose exec bash-mcp python3 /opt/server/generate_token.py --user-id alice --no-expiry
```

Inside the container the secret is taken from the `JWT_SECRET` env var. From the host, pass `--secret` and `--registry workspace/.users.json` explicitly.

Or use the admin API (requires `ENABLE_ADMIN_API=true` and `ADMIN_TOKEN`):

```bash
# Create a user and get a token
curl -s -X POST http://localhost:8080/admin/users \
  -H "Authorization: Bearer $ADMIN_TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"user_id": "alice", "ttl": "30d"}'

# List users
curl -s http://localhost:8080/admin/users -H "Authorization: Bearer $ADMIN_TOKEN"

# Revoke a user (all their tokens stop working immediately)
curl -s -X DELETE "http://localhost:8080/admin/users/alice?delete_workspace=false" \
  -H "Authorization: Bearer $ADMIN_TOKEN"

# Issue a fresh token for an existing user
curl -s -X POST http://localhost:8080/admin/users/alice/token \
  -H "Authorization: Bearer $ADMIN_TOKEN" \
  -H 'Content-Type: application/json' -d '{"ttl": "7d"}'
```

Each user gets an isolated workspace at `/workspace/user_<sanitized_id>` (mode 700) and commands run under a dedicated OS user with process/memory limits.

### Using the MCP Server

```bash
curl -s -X POST http://localhost:8080/mcp \
  -H 'Content-Type: application/json' \
  -H 'Accept: application/json, text/event-stream' \
  -H 'Authorization: Bearer <token>' \
  -d '{"jsonrpc":"2.0","method":"tools/call","params":{"name":"bash_exec","arguments":{"command":"whoami"}},"id":1}'
```

### Files API (artifacts)

Upload and download files in the user's workspace over plain HTTP (same JWT):

```bash
# Upload (streams, any size; parent dirs created automatically)
curl -T report.pdf -H "Authorization: Bearer <token>" \
  http://localhost:8080/files/artifacts/report.pdf

# Download
curl -H "Authorization: Bearer <token>" \
  http://localhost:8080/files/artifacts/report.pdf -o report.pdf

# List a directory (workspace root: /files or /files/)
curl -H "Authorization: Bearer <token>" http://localhost:8080/files/artifacts

# Delete a file
curl -X DELETE -H "Authorization: Bearer <token>" \
  http://localhost:8080/files/artifacts/report.pdf
```

Paths are always relative to the user's workspace; anything resolving outside it is rejected.

### Connecting to an MCP Client

Claude Desktop, Cursor, and other MCP clients:

```json
{
  "mcpServers": {
    "bash-mcp": {
      "url": "http://localhost:8080/mcp",
      "transport": "streamable-http"
    }
  }
}
```

## Environment Variables

| Variable | Default | Description |
|---|---|---|
| `MCP_PORT` | `8080` | Server port |
| `BASH_TIMEOUT_MAX` | `300` | Maximum bash_exec timeout (sec) |
| `JWT_SECRET` | *(empty)* | HMAC-SHA256 secret for JWT auth. If unset, all requests return 403 unless `ALLOW_NO_AUTH` is set |
| `ALLOW_NO_AUTH` | `false` | Set to `true` to allow running without `JWT_SECRET` (open access, runs as root — local experiments only) |
| `ENABLE_FILE_TOOLS` | `true` | Set to `false` to expose only `bash_exec` |
| `ENABLE_ADMIN_API` | `false` | Enable `/admin/*` endpoints (also requires `ADMIN_TOKEN`) |
| `ADMIN_TOKEN` | *(empty)* | Static bearer token for the admin API |
| `USER_MAX_PROCS` | `256` | Max processes per user (pam_limits on sudo sessions) |
| `USER_MAX_MEM_MB` | `4096` | Max virtual memory per user process, MB |
| `CONTAINER_MEM_LIMIT` | `2g` | Container memory limit (docker-compose) |
| `CONTAINER_CPUS` | `2` | Container CPU limit |
| `CONTAINER_PIDS_LIMIT` | `512` | Container PID limit |

For non-public deployments, bind the port to loopback in `docker-compose.yml`: `"127.0.0.1:8080:8080"`.

## User Registry

Registered users live in `/workspace/.users.json` (persisted via the bind mount). A token whose `sub` is not an active registry entry is rejected with 401 — revoking a user invalidates all their outstanding tokens at once. On startup the server re-provisions OS users for every registry entry, so registered users survive container rebuilds. On first start with a pre-existing workspace, existing `user_*` directories are imported into the registry automatically.

## JWT Token Format

Payload:

```json
{
  "user_id": "alice@corp.com",
  "sub": "user_alice_corp_com",
  "iat": 1710000000,
  "exp": 1712592000
}
```

- `user_id` (required) — external user identifier (email, UUID, etc.) for readability
- `sub` (required) — authoritative internal Linux username, computed at token creation time; must match an active registry entry
- `iat` — issued-at timestamp
- `exp` — expiration timestamp (tokens default to a 30-day TTL; use `--no-expiry` to omit)

## Extending the Image

```dockerfile
FROM bash-mcp:latest

RUN apt-get update && apt-get install -y postgresql-client
```

## End-to-End Tests

```bash
./test.sh
```

Builds the image, starts containers in every auth mode, and exercises authentication, the user registry, the admin API, the files API, path containment, resource limits and restart reconciliation.
