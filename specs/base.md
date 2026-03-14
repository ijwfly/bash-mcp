# bash-mcp — Base Docker Container with MCP Server

## Goal

A minimal self-hosted Docker image that:
- Exposes an MCP server with a single `bash_exec` tool
- Serves as a base image for extension (`FROM bash-mcp`)

---

## Repository Structure
```
bash-mcp/
├── Dockerfile
├── docker-compose.yml
├── entrypoint.sh
├── server/
│   ├── main.py
│   └── requirements.txt
└── README.md
```

---

## MCP Server

**Transport:** Streamable HTTP (`:8080/mcp`) + SSE (`:8080/sse`) for compatibility

**Stack:** Python, `fastmcp`, `asyncio.subprocess`

### Tool: `bash_exec`

**Description:**
Executes a bash command in a subprocess. Each call is stateless —
a new subshell is spawned every time, so state does not persist
between calls (no shared environment variables, working directory
resets to `/workspace` on each invocation). Chain dependent commands
with `&&` or write a script if you need stateful execution.

**Parameters:**

| Parameter | Type   | Required | Default | Description                          |
|-----------|--------|----------|---------|--------------------------------------|
| `command` | string | yes      | —       | Bash command to execute              |
| `timeout` | int    | no       | 30      | Timeout in seconds, max 300          |

**Returns:**
```json
{
  "stdout": "string",
  "stderr": "string",
  "exit_code": 0
}
```

---

## Dockerfile

**Base:** `ubuntu:24.04`

**Preinstalled packages:**
- `bash`, `curl`, `wget`, `jq`, `git`, `vim`, `nano`
- `python3`, `pip`
- `htop`, `net-tools`, `iputils-ping`, `dnsutils`
- MCP server dependencies from `requirements.txt`

**Working directory:** `/workspace` (volume mount point)

**User:** unprivileged `mcpuser`

**ENTRYPOINT:** `entrypoint.sh` → then starts the MCP server on port 8080

---

## docker-compose.yml
```yaml
services:
  bash-mcp:
    build: .
    ports:
      - "8080:8080"
    volumes:
      - ./workspace:/workspace
    environment:
      - MCP_PORT=8080
      - BASH_TIMEOUT_MAX=300
```

---

## Usage Scenarios

### Agent executing commands
```bash
# Agent via bash_exec:
bash_exec("ls -la /workspace")
bash_exec("curl -s https://api.example.com/data | jq .")
bash_exec("python3 -c 'print(2 + 2)'")
```

### Extending the image
```dockerfile
FROM bash-mcp:latest

# Add your own software
RUN apt-get install -y postgresql-client redis-tools
```

---

## Out of Scope

- Command whitelist — not needed, security is provided by container isolation
- AppArmor / Firejail — overkill
- MCP server authentication — not implemented; add on top via reverse proxy if needed
- Stateful bash sessions — not implemented; use `&&` or a script within a single call for stateful scenarios
