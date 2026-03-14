# bash-mcp — Architecture and Guide

## What Is It

bash-mcp is a self-hosted Docker container that provides an MCP server (Model Context Protocol) with a single `bash_exec` tool. It allows AI agents to execute arbitrary bash commands inside an isolated container, with optional JWT-based multi-user authentication and workspace isolation.

---

## Project Structure

```
bash-mcp/
├── Dockerfile              # Image based on Ubuntu 24.04
├── docker-compose.yml      # Run configuration
├── entrypoint.sh           # Server startup script
├── server/
│   ├── main.py             # MCP server (FastMCP + JWT middleware + uvicorn)
│   ├── generate_token.py   # CLI for JWT token generation
│   └── requirements.txt    # Python dependencies
├── workspace/              # Working directory (mounted as volume)
├── test.sh                 # End-to-end test script
└── specs/
    ├── base.md             # Original specification
    └── architecture.md     # This file
```

---

## Architecture

### Overview

```
┌──────────────────────────────────────────────────────────────┐
│  Docker container (Ubuntu 24.04, root)                       │
│                                                              │
│  ┌────────────────────────────────────────────────────────┐  │
│  │  entrypoint.sh                                         │  │
│  │  exec python3 /opt/server/main.py                      │  │
│  └────────────────────────────────────────────────────────┘  │
│                     │                                        │
│                     ▼                                        │
│  ┌────────────────────────────────────────────────────────┐  │
│  │  JWTAuthMiddleware (ASGI)                              │  │
│  │  ├── JWT_SECRET not set → 403 (or pass if ALLOW_NO_AUTH)│  │
│  │  ├── Verify Bearer token (HMAC-SHA256)                 │  │
│  │  ├── Extract user_id → sanitize → ContextVar           │  │
│  │  └── Missing/invalid/expired → 401                     │  │
│  └────────────────────────────────────────────────────────┘  │
│                     │                                        │
│                     ▼                                        │
│  ┌────────────────────────────────────────────────────────┐  │
│  │  MCP server (FastMCP)                                  │  │
│  │  Transport: Streamable HTTP on :8080/mcp               │  │
│  │  Mode: stateless (each request is independent)         │  │
│  │                                                        │  │
│  │  ┌────────────────────────────────────────────────┐    │  │
│  │  │  bash_exec(command, timeout)                   │    │  │
│  │  │  ├── No auth: bash -c "$cmd", cwd=/workspace   │    │  │
│  │  │  └── Auth: sudo -u $user bash -c "$cmd"        │    │  │
│  │  │          cwd=/workspace/$user                   │    │  │
│  │  └────────────────────────────────────────────────┘    │  │
│  └────────────────────────────────────────────────────────┘  │
│                                                              │
│  /workspace/                                                 │
│  ├── user_alice/  ← isolated workspace (auth mode)           │
│  ├── user_bob/    ← isolated workspace (auth mode)           │
│  └── ...          ← or single shared workspace (no-auth)     │
│                                                              │
│  Lazy user creation: useradd + mkdir + chown on first request│
└──────────────────────────────────────────────────────────────┘
         ▲
         │ HTTP POST :8080/mcp
         │ Authorization: Bearer <jwt>
         │ (JSON-RPC 2.0)
         │
   AI agent / client
```

### Technology Stack

| Component | Technology |
|---|---|
| MCP SDK | `mcp[cli]` (Python, official Anthropic SDK) |
| Framework | FastMCP — high-level wrapper over MCP SDK |
| Transport | Streamable HTTP (JSON-RPC 2.0 over HTTP) |
| HTTP server | uvicorn (ASGI) |
| Authentication | PyJWT (HMAC-SHA256) |
| Command execution | `asyncio.create_subprocess_exec` |
| User isolation | Linux users + `sudo -u` |
| Base image | Ubuntu 24.04 |

### MCP Server (`server/main.py`)

The server is built on FastMCP in stateless mode — each HTTP request is handled independently, with no sessions. This simplifies scaling and eliminates state leaks.

**Authentication middleware (`JWTAuthMiddleware`):**
- Pure ASGI middleware wrapping the FastMCP app
- If `JWT_SECRET` is not set — rejects with 403 (set `ALLOW_NO_AUTH=true` for open access)
- Extracts `Authorization: Bearer <token>` header
- Verifies JWT with HMAC-SHA256
- Extracts `user_id`, sanitizes to Linux username, stores in `ContextVar`
- Returns 401 on missing/invalid/expired tokens

**`bash_exec` tool:**
- Accepts `command` (string) and `timeout` (int, default 30 sec)
- Timeout is clamped to `[1, BASH_TIMEOUT_MAX]` (BASH_TIMEOUT_MAX defaults to 300)
- In auth mode: runs `sudo -u <linux_user> bash -c "..."` with `cwd=/workspace/<linux_user>`
- In no-auth mode: runs `bash -c "..."` with `cwd=/workspace`
- On first request per user: creates OS account and workspace directory (lazy provisioning)
- On timeout, the process is killed via `proc.kill()`
- Returns `{stdout, stderr, exit_code}`

**`sanitize_username(user_id)`:**
- Lowercase → replace `[^a-z0-9]` with `_` → truncate to 28 chars → prefix `user_`
- Max 32 chars total (Linux username limit)

### Token Generation (`server/generate_token.py`)

CLI utility for generating JWT tokens:
```bash
python3 server/generate_token.py --user-id alice --secret mysecret
python3 server/generate_token.py --user-id alice --secret mysecret --ttl 30d
```

### Entrypoint (`entrypoint.sh`)

A minimal startup script that launches the MCP server via `exec` (process replacement for proper signal forwarding).

### Docker Image (`Dockerfile`)

Multi-layer build optimized for caching:

```
Layer 1: apt-get install (system packages + sudo)   ← rarely changes
Layer 2: sudoers config                              ← never changes
Layer 3: pip install -r requirements.txt             ← changes on dependency updates
Layer 4: COPY server/ + entrypoint.sh                ← changes on code updates
Layer 5: mkdir /workspace                            ← never changes
```

**Preinstalled tools:**
- Core: `bash`, `curl`, `wget`, `jq`, `git`
- Editors: `vim`, `nano`
- Languages: `python3`, `pip`
- Networking: `net-tools`, `iputils-ping`, `dnsutils`
- Monitoring: `htop`
- System: `sudo`

---

## Usage

### Quick Start (No Auth)

```bash
# Build and run
docker compose up -d --build

# Verify the server is running
curl -s -X POST http://localhost:8080/mcp \
  -H 'Content-Type: application/json' \
  -H 'Accept: application/json, text/event-stream' \
  -d '{
    "jsonrpc": "2.0",
    "method": "initialize",
    "params": {
      "protocolVersion": "2025-03-26",
      "capabilities": {},
      "clientInfo": {"name": "test", "version": "1.0"}
    },
    "id": 1
  }'
```

### With JWT Authentication

```bash
# Start with JWT_SECRET
JWT_SECRET=mysecret docker compose up -d --build

# Generate a token
TOKEN=$(python3 server/generate_token.py --user-id alice --secret mysecret)

# Execute a command
curl -s -X POST http://localhost:8080/mcp \
  -H 'Content-Type: application/json' \
  -H 'Accept: application/json, text/event-stream' \
  -H "Authorization: Bearer $TOKEN" \
  -d '{
    "jsonrpc": "2.0",
    "method": "tools/call",
    "params": {
      "name": "bash_exec",
      "arguments": {"command": "whoami && pwd"}
    },
    "id": 2
  }'
```

### Connecting to an AI Agent

bash-mcp works as a remote MCP server. To connect it to Claude Desktop, Cursor, or any other MCP client, specify the URL `http://<host>:8080/mcp` with Streamable HTTP transport.

Example MCP client configuration:
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

---

## Environment Variables

| Variable | Default | Description |
|---|---|---|
| `MCP_PORT` | `8080` | MCP server port |
| `BASH_TIMEOUT_MAX` | `300` | Maximum allowed timeout for bash_exec (seconds) |
| `JWT_SECRET` | *(empty)* | HMAC-SHA256 secret for JWT auth. If unset, all requests return 403 unless `ALLOW_NO_AUTH` is set |
| `ALLOW_NO_AUTH` | `false` | Set to `true` to allow running without `JWT_SECRET` (open access, no auth) |

---

## Extending the Image

bash-mcp is designed as a base image. To add your own software:

```dockerfile
FROM bash-mcp:latest

RUN apt-get update && apt-get install -y postgresql-client redis-tools
```

Or add Python packages:
```dockerfile
FROM bash-mcp:latest

COPY extra-requirements.txt /tmp/
RUN pip3 install --break-system-packages -r /tmp/extra-requirements.txt
```

---

## Security

- In auth mode: commands run under isolated per-user OS accounts via `sudo -u`
- In no-auth mode: commands run as root (container provides isolation)
- JWT tokens are verified with HMAC-SHA256 (symmetric secret)
- Workspace isolation: each authenticated user has a separate `/workspace/<username>` directory
- Isolation is provided by the Docker container
- Timeout limits command execution time (max 300 sec by default)

---

## Limitations

- **Stateless execution:** each `bash_exec` call spawns a new process. Environment variables, `cd`, and other state are not preserved between calls. Use `&&` or write a script within a single call for command chains
- **No authentication UI:** tokens must be generated via CLI and distributed manually
- **Root in container:** the server process runs as root to support `sudo -u` for user isolation. Security is provided by container boundaries, not OS-level privilege separation
