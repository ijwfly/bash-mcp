# bash-mcp

A Docker container with an MCP server that gives AI agents the ability to execute bash commands in an isolated environment.

## Features

- **bash_exec** — execute arbitrary bash commands with configurable timeout
- **read_file** / **write_file** / **edit_file** — direct file operations without shell escaping hassles
- **JWT authentication** — optional HMAC-SHA256 JWT auth with per-user workspace isolation
- **Base image** — extend with `FROM bash-mcp:latest`
- **Preinstalled tools** — curl, wget, jq, git, python3, and more

## Quick Start

```bash
docker compose up -d --build
```

The server will start at `http://localhost:8080/mcp` (Streamable HTTP).

### With Authentication

1. Set `JWT_SECRET` in `docker-compose.yml` or pass via environment:

```bash
JWT_SECRET=your-secret docker compose up -d --build
```

2. Generate a token:

```bash
python3 server/generate_token.py --user-id alice --secret your-secret
python3 server/generate_token.py --user-id alice --secret your-secret --ttl 30d
```

3. Use the token in requests:

```bash
curl -s -X POST http://localhost:8080/mcp \
  -H 'Content-Type: application/json' \
  -H 'Accept: application/json, text/event-stream' \
  -H 'Authorization: Bearer <token>' \
  -d '{"jsonrpc":"2.0","method":"tools/call","params":{"name":"bash_exec","arguments":{"command":"whoami"}},"id":1}'
```

Each user gets an isolated workspace at `/workspace/user_<sanitized_id>` and commands run under a dedicated OS user.

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

### Testing with curl

```bash
# Execute a command (no-auth mode, requires ALLOW_NO_AUTH=true)
curl -s -X POST http://localhost:8080/mcp \
  -H 'Content-Type: application/json' \
  -H 'Accept: application/json, text/event-stream' \
  -d '{"jsonrpc":"2.0","method":"tools/call","params":{"name":"bash_exec","arguments":{"command":"echo hello"}},"id":1}'
```

## Environment Variables

| Variable | Default | Description |
|---|---|---|
| `MCP_PORT` | `8080` | Server port |
| `BASH_TIMEOUT_MAX` | `300` | Maximum bash_exec timeout (sec) |
| `JWT_SECRET` | *(empty)* | HMAC-SHA256 secret for JWT auth. If unset, all requests return 403 unless `ALLOW_NO_AUTH` is set |
| `ALLOW_NO_AUTH` | `false` | Set to `true` to allow running without `JWT_SECRET` (open access, no auth) |

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
- `sub` (required) — authoritative internal Linux username, computed by `generate_token.py` at token creation time
- `iat` — issued-at timestamp
- `exp` (optional) — expiration timestamp

## Token Generation

```bash
# Permanent token
python3 server/generate_token.py --user-id alice --secret mysecret

# Token with TTL
python3 server/generate_token.py --user-id alice --secret mysecret --ttl 30d
python3 server/generate_token.py --user-id alice --secret mysecret --ttl 24h
```

## Extending the Image

```dockerfile
FROM bash-mcp:latest

RUN apt-get update && apt-get install -y postgresql-client
```

## End-to-End Tests

```bash
./test.sh
```

Builds the image, starts the container with JWT auth, runs authentication and isolation tests, then cleans up.
