# bash-mcp — Architecture and Guide

## What Is It

bash-mcp is a self-hosted Docker container that provides an MCP server (Model Context Protocol) with a single `bash_exec` tool. It allows AI agents to execute arbitrary bash commands inside an isolated container. Comes with `ocli` (openapi-to-cli) preinstalled for interacting with HTTP APIs from the command line.

---

## Project Structure

```
bash-mcp/
├── Dockerfile              # Image based on Ubuntu 24.04
├── docker-compose.yml      # Run configuration
├── entrypoint.sh           # ocli profile initialization + server startup
├── server/
│   ├── main.py             # MCP server (FastMCP + asyncio)
│   └── requirements.txt    # Python dependencies
├── workspace/              # Working directory (mounted as volume)
└── specs/
    ├── base.md             # Original specification
    └── architecture.md     # This file
```

---

## Architecture

### Overview

```
┌──────────────────────────────────────────────────────┐
│  Docker container (Ubuntu 24.04)                     │
│                                                      │
│  ┌────────────────────────────────────────────────┐  │
│  │  entrypoint.sh                                 │  │
│  │  1. Set up ocli profiles                       │  │
│  │  2. exec python3 /opt/server/main.py           │  │
│  └────────────────────────────────────────────────┘  │
│                     │                                │
│                     ▼                                │
│  ┌────────────────────────────────────────────────┐  │
│  │  MCP server (FastMCP)                          │  │
│  │  Transport: Streamable HTTP on :8080/mcp       │  │
│  │  Mode: stateless (each request is independent) │  │
│  │                                                │  │
│  │  ┌──────────────────────────────────────────┐  │  │
│  │  │  bash_exec(command, timeout)             │  │  │
│  │  │  → asyncio.create_subprocess_exec        │  │  │
│  │  │  → bash -c "$command"                    │  │  │
│  │  │  → cwd=/workspace                        │  │  │
│  │  │  → timeout via asyncio.wait_for          │  │  │
│  │  └──────────────────────────────────────────┘  │  │
│  └────────────────────────────────────────────────┘  │
│                                                      │
│  /workspace  ← volume mount (persistent data)       │
│  ~/.ocli/    ← ocli profiles                         │
│                                                      │
│  User: mcpuser (unprivileged)                        │
└──────────────────────────────────────────────────────┘
         ▲
         │ HTTP POST :8080/mcp
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
| Command execution | `asyncio.create_subprocess_exec` |
| Base image | Ubuntu 24.04 |
| API CLI | `ocli` (openapi-to-cli) via npm |

### MCP Server (`server/main.py`)

The server is built on FastMCP in stateless mode — each HTTP request is handled independently, with no sessions. This simplifies scaling and eliminates state leaks.

**`bash_exec` tool:**
- Accepts `command` (string) and `timeout` (int, default 30 sec)
- Timeout is clamped to `[1, BASH_TIMEOUT_MAX]` (BASH_TIMEOUT_MAX defaults to 300)
- Each call spawns a new `bash -c "..."` process with `cwd=/workspace`
- State is not preserved between calls (environment variables, current directory)
- On timeout, the process is killed via `proc.kill()`
- Returns `{stdout, stderr, exit_code}`

### Entrypoint (`entrypoint.sh`)

Initialization script that runs on container startup:

1. **If `~/.ocli/profiles.ini` is mounted** (method A):
   - Parses section names `[profile_name]` from the INI file
   - Runs `ocli spec refresh --profile <name>` for each profile
   - This downloads and caches OpenAPI specifications

2. **If the file does not exist** (method B):
   - Looks for env variables matching `OCLI_PROFILE_{NAME}_*`
   - Groups by `{NAME}`, converts to lowercase
   - Runs `ocli profiles add` with the corresponding flags for each group

3. Starts the MCP server via `exec` (process replacement for proper signal forwarding)

### Docker Image (`Dockerfile`)

Multi-layer build optimized for caching:

```
Layer 1: apt-get install (system packages)       ← rarely changes
Layer 2: npm install -g openapi-to-cli           ← rarely changes
Layer 3: useradd mcpuser                         ← never changes
Layer 4: pip install -r requirements.txt         ← changes on dependency updates
Layer 5: COPY server/ + entrypoint.sh            ← changes on code updates
Layer 6: mkdir /workspace, ~/.ocli               ← never changes
```

**Preinstalled tools:**
- Core: `bash`, `curl`, `wget`, `jq`, `git`
- Editors: `vim`, `nano`
- Languages: `python3`, `pip`, `nodejs`, `npm`
- Networking: `net-tools`, `iputils-ping`, `dnsutils`
- Monitoring: `htop`
- API CLI: `ocli`

---

## Usage

### Quick Start

```bash
# Clone the repository
git clone <repo-url> bash-mcp
cd bash-mcp

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

### Calling bash_exec

```bash
curl -s -X POST http://localhost:8080/mcp \
  -H 'Content-Type: application/json' \
  -H 'Accept: application/json, text/event-stream' \
  -d '{
    "jsonrpc": "2.0",
    "method": "tools/call",
    "params": {
      "name": "bash_exec",
      "arguments": {"command": "echo hello world"}
    },
    "id": 2
  }'
```

Response (SSE format):
```
event: message
data: {"jsonrpc":"2.0","id":2,"result":{"content":[{"type":"text","text":"{\"stdout\":\"hello world\\n\",\"stderr\":\"\",\"exit_code\":0}"}],"isError":false}}
```

### Calling with a Timeout

```bash
# Command will be terminated after 2 seconds
curl -s -X POST http://localhost:8080/mcp \
  -H 'Content-Type: application/json' \
  -H 'Accept: application/json, text/event-stream' \
  -d '{
    "jsonrpc": "2.0",
    "method": "tools/call",
    "params": {
      "name": "bash_exec",
      "arguments": {"command": "sleep 60", "timeout": 2}
    },
    "id": 3
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

## Configuring ocli Profiles

### Method A — Volume with profiles.ini (recommended)

Create an `ocli-profiles.ini` file:
```ini
[myapi]
api_base_url = http://my-service:3000
api_bearer_token = secret-token
openapi_spec_source = http://my-service:3000/openapi.json

[payments]
api_base_url = http://payments-svc:4000
api_bearer_token = another-token
openapi_spec_source = http://payments-svc:4000/openapi.json
```

Uncomment the line in `docker-compose.yml`:
```yaml
volumes:
  - ./workspace:/workspace
  - ./ocli-profiles.ini:/home/mcpuser/.ocli/profiles.ini:ro
```

On container startup, `entrypoint.sh` will automatically download and cache OpenAPI specifications for each profile.

### Method B — Environment Variables

Uncomment and fill in `docker-compose.yml`:
```yaml
environment:
  - MCP_PORT=8080
  - BASH_TIMEOUT_MAX=300
  - OCLI_PROFILE_MYAPI_BASE_URL=http://my-service:3000
  - OCLI_PROFILE_MYAPI_SPEC=http://my-service:3000/openapi.json
  - OCLI_PROFILE_MYAPI_TOKEN=secret-token
```

**Available variables for each profile `{NAME}`:**

| Variable | Required | Description |
|---|---|---|
| `OCLI_PROFILE_{NAME}_BASE_URL` | yes | API base URL |
| `OCLI_PROFILE_{NAME}_SPEC` | yes | URL or path to the OpenAPI specification |
| `OCLI_PROFILE_{NAME}_TOKEN` | no | Bearer token for authorization |
| `OCLI_PROFILE_{NAME}_BASIC_AUTH` | no | Basic Auth credentials |
| `OCLI_PROFILE_{NAME}_INCLUDE` | no | Include only these endpoints (comma-separated) |
| `OCLI_PROFILE_{NAME}_EXCLUDE` | no | Exclude these endpoints (comma-separated) |

`{NAME}` is specified in uppercase in the variables, but the profile itself is created in lowercase.

### Using ocli via bash_exec

Once profiles are configured, the agent can interact with APIs:
```bash
# List available commands
bash_exec("ocli commands --query 'create user'")

# Call an endpoint
bash_exec("ocli myapi_users_post --name Alice --email alice@example.com")

# Fetch data and process with jq
bash_exec("ocli payments_invoices_get --limit 10 | jq '.items[]'")
```

---

## Environment Variables

| Variable | Default | Description |
|---|---|---|
| `MCP_PORT` | `8080` | MCP server port |
| `BASH_TIMEOUT_MAX` | `300` | Maximum allowed timeout for bash_exec (seconds) |
| `OCLI_PROFILE_{NAME}_*` | — | ocli profile settings (see above) |

---

## Extending the Image

bash-mcp is designed as a base image. To add your own software:

```dockerfile
FROM bash-mcp:latest

USER root
RUN apt-get update && apt-get install -y postgresql-client redis-tools
USER mcpuser
```

Or add Python packages:
```dockerfile
FROM bash-mcp:latest

USER root
COPY extra-requirements.txt /tmp/
RUN pip3 install --break-system-packages -r /tmp/extra-requirements.txt
USER mcpuser
```

---

## Security

- Commands run under the unprivileged `mcpuser` user
- Isolation is provided by the Docker container
- No command whitelist — security through isolation
- MCP server authentication is not implemented; add a reverse proxy (nginx, traefik) in front of the container if needed
- Timeout limits command execution time (max 300 sec by default)

---

## Limitations

- **Stateless execution:** each `bash_exec` call spawns a new process. Environment variables, `cd`, and other state are not preserved between calls. Use `&&` or write a script within a single call for command chains
- **Streamable HTTP only:** SSE transport is not connected (due to Starlette lifespan conflicts with dual mount). Can be switched to `mcp.run(transport="sse")` if needed
- **No authentication:** the MCP endpoint is open. Do not expose to the public internet without a reverse proxy with authorization
