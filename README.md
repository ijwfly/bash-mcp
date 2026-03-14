# bash-mcp

A Docker container with an MCP server that gives AI agents the ability to execute bash commands in an isolated environment.

## Features

- **bash_exec** — execute arbitrary bash commands with configurable timeout
- **Base image** — extend with `FROM bash-mcp:latest`
- **Preinstalled tools** — curl, wget, jq, git, python3, and more

## Quick Start

```bash
docker compose up -d --build
```

The server will start at `http://localhost:8080/mcp` (Streamable HTTP).

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
# Execute a command
curl -s -X POST http://localhost:8080/mcp \
  -H 'Content-Type: application/json' \
  -H 'Accept: application/json, text/event-stream' \
  -d '{"jsonrpc":"2.0","method":"tools/call","params":{"name":"bash_exec","arguments":{"command":"echo hello"}},"id":1}'

# With timeout (will be terminated after 5 sec)
curl -s -X POST http://localhost:8080/mcp \
  -H 'Content-Type: application/json' \
  -H 'Accept: application/json, text/event-stream' \
  -d '{"jsonrpc":"2.0","method":"tools/call","params":{"name":"bash_exec","arguments":{"command":"sleep 60","timeout":5}},"id":1}'
```

## Environment Variables

| Variable | Default | Description |
|---|---|---|
| `MCP_PORT` | `8080` | Server port |
| `BASH_TIMEOUT_MAX` | `300` | Maximum bash_exec timeout (sec) |

## Extending the Image

```dockerfile
FROM bash-mcp:latest

USER root
RUN apt-get update && apt-get install -y postgresql-client
USER mcpuser
```
