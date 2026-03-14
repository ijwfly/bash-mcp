# bash-mcp — Architecture and Guide

## What Is It

bash-mcp is a self-hosted Docker container that provides an MCP server (Model Context Protocol) with a single `bash_exec` tool. It allows AI agents to execute arbitrary bash commands inside an isolated container.

---

## Project Structure

```
bash-mcp/
├── Dockerfile              # Image based on Ubuntu 24.04
├── docker-compose.yml      # Run configuration
├── entrypoint.sh           # Server startup script
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
│  │  exec python3 /opt/server/main.py              │  │
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

A minimal startup script that launches the MCP server via `exec` (process replacement for proper signal forwarding).

### Docker Image (`Dockerfile`)

Multi-layer build optimized for caching:

```
Layer 1: apt-get install (system packages)       ← rarely changes
Layer 2: useradd mcpuser                         ← never changes
Layer 3: pip install -r requirements.txt         ← changes on dependency updates
Layer 4: COPY server/ + entrypoint.sh            ← changes on code updates
Layer 5: mkdir /workspace                        ← never changes
```

**Preinstalled tools:**
- Core: `bash`, `curl`, `wget`, `jq`, `git`
- Editors: `vim`, `nano`
- Languages: `python3`, `pip`
- Networking: `net-tools`, `iputils-ping`, `dnsutils`
- Monitoring: `htop`

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

## Environment Variables

| Variable | Default | Description |
|---|---|---|
| `MCP_PORT` | `8080` | MCP server port |
| `BASH_TIMEOUT_MAX` | `300` | Maximum allowed timeout for bash_exec (seconds) |

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
