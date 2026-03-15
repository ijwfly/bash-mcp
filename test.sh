#!/usr/bin/env bash
set -euo pipefail

# ─── Helpers ──────────────────────────────────────────────────────────────
RED='\033[0;31m'; GREEN='\033[0;32m'; NC='\033[0m'
pass() { echo -e "${GREEN}PASS${NC}: $1"; }
fail() { echo -e "${RED}FAIL${NC}: $1"; exit 1; }

MCP_URL="http://localhost:8080/mcp"
SECRET="test-secret-bash-mcp-jwt-$(date +%s)"

# Build a JSON-RPC request for bash_exec
mcp_call() {
  local cmd="$1"
  local token="${2:-}"
  local auth_header=""
  [ -n "$token" ] && auth_header="-H \"Authorization: Bearer $token\""
  eval curl -sf -X POST "$MCP_URL" \
    -H "'Content-Type: application/json'" \
    -H "'Accept: application/json, text/event-stream'" \
    $auth_header \
    -d "'$(printf '{"jsonrpc":"2.0","method":"tools/call","params":{"name":"bash_exec","arguments":{"command":"%s"}},"id":1}' "$cmd")'"
}

# curl that does NOT fail on HTTP errors (returns body + status code)
mcp_raw() {
  local cmd="$1"
  local token="${2:-}"
  local auth_header=""
  [ -n "$token" ] && auth_header="-H \"Authorization: Bearer $token\""
  eval curl -s -o /dev/null -w "%{http_code}" -X POST "$MCP_URL" \
    -H "'Content-Type: application/json'" \
    -H "'Accept: application/json, text/event-stream'" \
    $auth_header \
    -d "'$(printf '{"jsonrpc":"2.0","method":"tools/call","params":{"name":"bash_exec","arguments":{"command":"%s"}},"id":1}' "$cmd")'"
}

extract_stdout() {
  # Parse SSE response: last "data:" line contains JSON-RPC result
  echo "$1" | grep '^data:' | tail -1 | sed 's/^data://' | python3 -c "
import sys, json
resp = json.load(sys.stdin)
content = resp.get('result', {}).get('content', [{}])
text = content[0].get('text', '{}') if content else '{}'
parsed = json.loads(text)
print(parsed.get('stdout', '').strip())
"
}

# ─── Setup ────────────────────────────────────────────────────────────────
echo "=== Building image ==="
docker compose build --quiet

# ─── Test 0: No JWT_SECRET, no ALLOW_NO_AUTH → 403 ────────────────────────
echo ""
echo "--- Test 0: No JWT_SECRET, no ALLOW_NO_AUTH → 403 ---"
docker compose up -d
sleep 3
status=$(mcp_raw "whoami")
docker compose down --volumes
[ "$status" = "403" ] && pass "No config returns 403" || fail "Expected 403, got $status"

# ─── Start with JWT_SECRET ────────────────────────────────────────────────
echo ""
echo "=== Starting container with JWT_SECRET ==="
JWT_SECRET=$SECRET docker compose up -d
sleep 3

echo "=== Generating tokens ==="
TOKEN_ALICE=$(python3 server/generate_token.py --user-id alice --secret "$SECRET")
TOKEN_BOB=$(python3 server/generate_token.py --user-id bob --secret "$SECRET")
TOKEN_EXPIRED=$(python3 server/generate_token.py --user-id alice --secret "$SECRET" --ttl 1s)
sleep 2  # wait for expiration

# ─── Auth Tests ───────────────────────────────────────────────────────────

# Test 1: No token → 401
echo ""
echo "--- Test 1: No token → 401 ---"
status=$(mcp_raw "whoami")
[ "$status" = "401" ] && pass "No token returns 401" || fail "Expected 401, got $status"

# Test 2: Invalid token → 401
echo "--- Test 2: Invalid token → 401 ---"
status=$(mcp_raw "whoami" "invalid-token")
[ "$status" = "401" ] && pass "Invalid token returns 401" || fail "Expected 401, got $status"

# Test 3: Expired token → 401
echo "--- Test 3: Expired token → 401 ---"
status=$(mcp_raw "whoami" "$TOKEN_EXPIRED")
[ "$status" = "401" ] && pass "Expired token returns 401" || fail "Expected 401, got $status"

# Test 4: Alice — whoami
echo "--- Test 4: Alice whoami → user_alice ---"
resp=$(mcp_call "whoami" "$TOKEN_ALICE")
whoami_result=$(extract_stdout "$resp")
[ "$whoami_result" = "user_alice" ] && pass "Alice whoami = user_alice" || fail "Expected user_alice, got '$whoami_result'"

# Test 5: Alice — pwd
echo "--- Test 5: Alice pwd → /workspace/user_alice ---"
resp=$(mcp_call "pwd" "$TOKEN_ALICE")
pwd_result=$(extract_stdout "$resp")
[ "$pwd_result" = "/workspace/user_alice" ] && pass "Alice pwd = /workspace/user_alice" || fail "Expected /workspace/user_alice, got '$pwd_result'"

# Test 6: Bob — whoami
echo "--- Test 6: Bob whoami → user_bob ---"
resp=$(mcp_call "whoami" "$TOKEN_BOB")
whoami_result=$(extract_stdout "$resp")
[ "$whoami_result" = "user_bob" ] && pass "Bob whoami = user_bob" || fail "Expected user_bob, got '$whoami_result'"

# Test 7: Workspace isolation
echo "--- Test 7: Workspace isolation ---"
mcp_call "echo secret_data > test_isolation.txt" "$TOKEN_ALICE" > /dev/null
resp=$(mcp_call "cat test_isolation.txt 2>&1 || echo FILE_NOT_FOUND" "$TOKEN_BOB")
bob_result=$(extract_stdout "$resp")
echo "$bob_result" | grep -q "FILE_NOT_FOUND\|No such file" \
  && pass "Bob cannot see Alice's file" \
  || fail "Bob saw Alice's file: '$bob_result'"

# ─── File Tools Tests ────────────────────────────────────────────────────

# Generic helper: call any MCP tool
mcp_tool_call() {
  local tool="$1"
  local args_json="$2"
  local token="${3:-}"
  local auth_header=""
  [ -n "$token" ] && auth_header="-H \"Authorization: Bearer $token\""
  eval curl -sf -X POST "$MCP_URL" \
    -H "'Content-Type: application/json'" \
    -H "'Accept: application/json, text/event-stream'" \
    $auth_header \
    -d "'$(printf '{"jsonrpc":"2.0","method":"tools/call","params":{"name":"%s","arguments":%s},"id":1}' "$tool" "$args_json")'"
}

extract_field() {
  # Extract a field from the SSE JSON-RPC result's text content
  local field="$1"
  echo "$2" | grep '^data:' | tail -1 | sed 's/^data://' | python3 -c "
import sys, json
resp = json.load(sys.stdin)
content = resp.get('result', {}).get('content', [{}])
text = content[0].get('text', '{}') if content else '{}'
parsed = json.loads(text)
print(parsed.get('$field', ''))
"
}

# Test 8: write_file + read_file round-trip
echo "--- Test 8: write_file + read_file round-trip ---"
resp=$(mcp_tool_call "write_file" '{"path":"test_rw.txt","content":"hello from write_file\nsecond line\n"}' "$TOKEN_ALICE")
status_val=$(extract_field "status" "$resp")
[ "$status_val" = "ok" ] || fail "write_file failed: $resp"
resp=$(mcp_tool_call "read_file" '{"path":"test_rw.txt"}' "$TOKEN_ALICE")
content_val=$(extract_field "content" "$resp")
echo "$content_val" | grep -q "hello from write_file" \
  && pass "write_file + read_file round-trip" \
  || fail "read_file content mismatch: '$content_val'"

# Test 9: edit_file
echo "--- Test 9: edit_file ---"
resp=$(mcp_tool_call "edit_file" '{"path":"test_rw.txt","old_text":"hello from write_file","new_text":"EDITED"}' "$TOKEN_ALICE")
status_val=$(extract_field "status" "$resp")
[ "$status_val" = "ok" ] || fail "edit_file failed: $resp"
resp=$(mcp_tool_call "read_file" '{"path":"test_rw.txt"}' "$TOKEN_ALICE")
content_val=$(extract_field "content" "$resp")
echo "$content_val" | grep -q "EDITED" \
  && pass "edit_file replaced text" \
  || fail "edit_file content mismatch: '$content_val'"

# Test 10: file isolation — Alice's file not visible to Bob
echo "--- Test 10: File tool isolation ---"
mcp_tool_call "write_file" '{"path":"secret_file.txt","content":"alice secret"}' "$TOKEN_ALICE" > /dev/null
resp=$(mcp_tool_call "read_file" '{"path":"secret_file.txt"}' "$TOKEN_BOB")
error_val=$(extract_field "error" "$resp")
echo "$error_val" | grep -q "not found\|No such file" \
  && pass "Bob cannot read Alice's file via read_file" \
  || fail "Bob saw Alice's file: '$resp'"

# Test 11: Alice writes to Bob's workspace via absolute path → permission denied
echo "--- Test 11: File tool cross-user write blocked ---"
resp=$(mcp_tool_call "write_file" '{"path":"/workspace/user_bob/hacked.txt","content":"pwned"}' "$TOKEN_ALICE")
error_val=$(extract_field "error" "$resp")
echo "$error_val" | grep -qi "permission denied\|Permission denied" \
  && pass "Alice cannot write to Bob's workspace" \
  || fail "Expected permission denied, got: '$error_val' (full: $resp)"

docker compose down --volumes

# ─── Test 12: ALLOW_NO_AUTH mode ──────────────────────────────────────────
echo ""
echo "--- Test 12: ALLOW_NO_AUTH=true → open access ---"
ALLOW_NO_AUTH=true docker compose up -d
sleep 3
resp=$(mcp_call "echo ok")
result=$(extract_stdout "$resp")
docker compose down --volumes
[ "$result" = "ok" ] && pass "ALLOW_NO_AUTH allows open access" || fail "Expected 'ok', got '$result'"

# ─── Done ─────────────────────────────────────────────────────────────────
echo ""
echo -e "${GREEN}All tests passed!${NC}"
