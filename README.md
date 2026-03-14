# bash-mcp

A Docker container with an MCP server that gives AI agents the ability to execute bash commands in an isolated environment. Comes with `ocli` preinstalled for interacting with HTTP APIs via OpenAPI specifications.

## Features

- **bash_exec** — execute arbitrary bash commands with configurable timeout
- **ocli** — CLI access to any HTTP API via OpenAPI spec (profiles configured at startup)
- **Base image** — extend with `FROM bash-mcp:latest`
- **Preinstalled tools** — curl, wget, jq, git, python3, nodejs, and more

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

## Configuring ocli Profiles

### Method A — profiles.ini file

Create `ocli-profiles.ini`:

```ini
[myapi]
api_base_url = http://my-service:3000
api_bearer_token = secret
openapi_spec_source = http://my-service:3000/openapi.json
```

Uncomment the volume in `docker-compose.yml`:

```yaml
volumes:
  - ./workspace:/workspace
  - ./ocli-profiles.ini:/home/mcpuser/.ocli/profiles.ini:ro
```

### Method B — Environment variables

```yaml
environment:
  - OCLI_PROFILE_MYAPI_BASE_URL=http://my-service:3000
  - OCLI_PROFILE_MYAPI_SPEC=http://my-service:3000/openapi.json
  - OCLI_PROFILE_MYAPI_TOKEN=secret
```

Once configured, the agent can call APIs:

```bash
bash_exec("ocli commands --query 'create user'")
bash_exec("ocli myapi_users_post --name Alice --email alice@example.com")
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
