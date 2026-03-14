# bash-mcp — Base Docker Container with MCP Server

## Goal

A minimal self-hosted Docker image that:
- Exposes an MCP server with a single `bash_exec` tool
- Serves as a base image for extension (`FROM bash-mcp`)
- Optionally provides JWT-based multi-user authentication with workspace isolation

---

## Repository Structure
```
bash-mcp/
├── Dockerfile
├── docker-compose.yml
├── entrypoint.sh
├── server/
│   ├── main.py
│   ├── generate_token.py
│   └── requirements.txt
├── test.sh
└── README.md
```

---

## MCP Server

**Transport:** Streamable HTTP (`:8080/mcp`)

**Stack:** Python, `fastmcp`, `asyncio.subprocess`, `uvicorn`

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

## Authentication

**Mode:** Optional, controlled by `JWT_SECRET` environment variable.

When `JWT_SECRET` is set:
- All requests must include `Authorization: Bearer <jwt>` header
- JWT is verified with HMAC-SHA256
- `user_id` claim is extracted and sanitized to a Linux username
- Each user gets an isolated OS account and workspace directory
- Commands execute via `sudo -u <linux_user>`

When `JWT_SECRET` is not set:
- All requests are rejected with 403 (safe default)
- Set `ALLOW_NO_AUTH=true` to explicitly enable open access (commands run as root in `/workspace`)

### JWT Payload

```json
{
  "user_id": "alice@corp.com",
  "iat": 1710000000,
  "exp": 1712592000
}
```

- `user_id` (required) — arbitrary user identifier
- `exp` (optional) — expiration timestamp

### Username Sanitization

`user_id` → Linux username via `sanitize_username()`:
1. Lowercase
2. Replace non-alphanumeric chars with `_`
3. Truncate to 28 chars
4. Prefix with `user_`

Examples:
- `Alice@Corp` → `user_alice_corp`
- `bob` → `user_bob`
- `user@test.com` → `user_user_test_com`

### Token Generation

```bash
python3 server/generate_token.py --user-id alice --secret mysecret
python3 server/generate_token.py --user-id alice --secret mysecret --ttl 30d
```

---

## Dockerfile

**Base:** `ubuntu:24.04`

**Preinstalled packages:**
- `bash`, `curl`, `wget`, `jq`, `git`, `vim`, `nano`
- `python3`, `pip`
- `htop`, `net-tools`, `iputils-ping`, `dnsutils`
- `sudo`
- MCP server dependencies from `requirements.txt`

**Working directory:** `/workspace` (volume mount point)

**User:** root (required for `sudo -u` and lazy user creation)

**ENTRYPOINT:** `entrypoint.sh` → starts the MCP server on port 8080

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
      # - JWT_SECRET=your-secret-here
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

### Multi-user with authentication
```bash
# Generate tokens
TOKEN=$(python3 server/generate_token.py --user-id alice --secret mysecret)

# Use with curl
curl -X POST http://localhost:8080/mcp \
  -H "Authorization: Bearer $TOKEN" \
  -H 'Content-Type: application/json' \
  -H 'Accept: application/json, text/event-stream' \
  -d '{"jsonrpc":"2.0","method":"tools/call","params":{"name":"bash_exec","arguments":{"command":"whoami"}},"id":1}'
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
- Stateful bash sessions — not implemented; use `&&` or a script within a single call for stateful scenarios
- `users.json` or user database — users are determined from JWT, OS accounts created lazily
