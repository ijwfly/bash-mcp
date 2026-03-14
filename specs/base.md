# bash-mcp — Base Docker Container with MCP Server

## Goal

A minimal self-hosted Docker image that:
- Exposes an MCP server with a single `bash_exec` tool
- Serves as a base image for extension (`FROM bash-mcp`)
- Comes with `ocli` (openapi-to-cli) preinstalled for HTTP API onboarding (https://github.com/EvilFreelancer/openapi-to-cli)
- Supports ocli profile provisioning via volume mount or env vars

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
- `python3`, `pip`, `nodejs`, `npm`
- `htop`, `net-tools`, `iputils-ping`, `dnsutils`
- `ocli` via `npm install -g openapi-to-cli`
- MCP server dependencies from `requirements.txt`

**Working directory:** `/workspace` (volume mount point)

**User:** unprivileged `mcpuser` (uid 1000)

**ENTRYPOINT:** `entrypoint.sh` → then starts the MCP server on port 8080

---

## ocli Profile Provisioning

Two methods are supported and can be used simultaneously.

### Method A — Volume with profiles.ini (preferred)

Mount a ready-made profiles file directly:
```yaml
volumes:
  - ./ocli-profiles.ini:/home/mcpuser/.ocli/profiles.ini:ro
```

Format of `ocli-profiles.ini`:
```ini
[myapi]
api_base_url = http://my-service:3000
api_bearer_token = secret
openapi_spec_source = http://my-service:3000/openapi.json

[payments]
api_base_url = http://payments-svc:4000
api_bearer_token = secret2
openapi_spec_source = http://payments-svc:4000/openapi.json
```

On startup, `entrypoint.sh` runs `ocli profiles refresh` for each profile to download and cache the specs.

### Method B — Environment Variables (onboarding at startup)

If `profiles.ini` is not mounted, `entrypoint.sh` looks for variables of the form:
```
OCLI_PROFILE_{NAME}_BASE_URL
OCLI_PROFILE_{NAME}_SPEC
OCLI_PROFILE_{NAME}_TOKEN        # optional
OCLI_PROFILE_{NAME}_BASIC_AUTH   # optional
OCLI_PROFILE_{NAME}_INCLUDE      # optional, comma-separated
OCLI_PROFILE_{NAME}_EXCLUDE      # optional, comma-separated
```

Example:
```yaml
environment:
  - OCLI_PROFILE_MYAPI_BASE_URL=http://my-service:3000
  - OCLI_PROFILE_MYAPI_SPEC=http://my-service:3000/openapi.json
  - OCLI_PROFILE_MYAPI_TOKEN=secret
  - OCLI_PROFILE_PAYMENTS_BASE_URL=http://payments-svc:4000
  - OCLI_PROFILE_PAYMENTS_SPEC=http://payments-svc:4000/openapi.json
  - OCLI_PROFILE_PAYMENTS_TOKEN=secret2
```

`entrypoint.sh` groups variables by `{NAME}` and runs `ocli profiles add` for each.

### entrypoint.sh Logic
```
1. If ~/.ocli/profiles.ini exists (volume mounted):
     → ocli profiles refresh for each profile (download/update specs)
   Otherwise:
     → collect profiles from OCLI_PROFILE_* env vars
     → ocli profiles add for each
2. Start MCP server
```

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
      - ./ocli-profiles.ini:/home/mcpuser/.ocli/profiles.ini:ro  # method A
    environment:
      - MCP_PORT=8080
      - BASH_TIMEOUT_MAX=300
      # method B (if profiles.ini is not mounted):
      # - OCLI_PROFILE_MYAPI_BASE_URL=http://my-service:3000
      # - OCLI_PROFILE_MYAPI_SPEC=http://my-service:3000/openapi.json
      # - OCLI_PROFILE_MYAPI_TOKEN=secret
```

---

## Usage Scenarios

### Agent calling APIs via ocli
```bash
# Agent via bash_exec:
bash_exec("ocli commands --query 'create user'")
bash_exec("ocli myapi_users_post --name Alice --email alice@example.com")
bash_exec("ocli payments_invoices_get --limit 10 | jq '.items[]'")
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
