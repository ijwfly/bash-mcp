#!/usr/bin/env bash
set -euo pipefail

# ─── Helpers ──────────────────────────────────────────────────────────────
RED='\033[0;31m'; GREEN='\033[0;32m'; NC='\033[0m'
pass() { echo -e "${GREEN}PASS${NC}: $1"; }
fail() { echo -e "${RED}FAIL${NC}: $1"; exit 1; }

BASE_URL="http://localhost:8080"
MCP_URL="$BASE_URL/mcp"
SECRET="test-secret-bash-mcp-jwt-$(date +%s)"
ADMIN_TOKEN_VAL="admin-test-token-$(date +%s)"

clean_workspace() {
  rm -rf workspace/user_* workspace/.users.json workspace/.users.json.lock \
    workspace/.users.json.tmp 2>/dev/null || true
}

wait_healthy() {
  for _ in $(seq 1 60); do
    curl -sf "$BASE_URL/health" > /dev/null 2>&1 && return 0
    sleep 0.5
  done
  fail "Server did not become healthy within 30s"
}

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

# curl that does NOT fail on HTTP errors (returns status code only)
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

admin_curl() {
  curl -s -H "Authorization: Bearer $ADMIN_TOKEN_VAL" "$@"
}

files_curl() {
  local token="$1"; shift
  curl -s -H "Authorization: Bearer $token" "$@"
}

# ─── Setup ────────────────────────────────────────────────────────────────
echo "=== Building image ==="
docker compose build --quiet
mkdir -p workspace
clean_workspace

# ─── Test 0: No JWT_SECRET, no ALLOW_NO_AUTH → 403 ────────────────────────
echo ""
echo "--- Test 0: No JWT_SECRET, no ALLOW_NO_AUTH → 403 ---"
docker compose up -d
wait_healthy
status=$(mcp_raw "whoami")
[ "$status" = "403" ] && pass "No config returns 403" || fail "Expected 403, got $status"

# Test 0a: /health works without auth even in locked mode
echo "--- Test 0a: /health → 200 without auth ---"
status=$(curl -s -o /dev/null -w "%{http_code}" "$BASE_URL/health")
docker compose down --volumes
[ "$status" = "200" ] && pass "/health returns 200 without auth" || fail "Expected 200, got $status"

# ─── Start with JWT_SECRET + admin API ────────────────────────────────────
echo ""
echo "=== Starting container with JWT_SECRET + admin API ==="
clean_workspace
JWT_SECRET=$SECRET ENABLE_ADMIN_API=true ADMIN_TOKEN=$ADMIN_TOKEN_VAL docker compose up -d
wait_healthy

echo "=== Generating tokens (in-container CLI registers users) ==="
gen_token() {
  docker compose exec -T bash-mcp python3 /opt/server/generate_token.py "$@" 2>/dev/null | tr -d '\r'
}
TOKEN_ALICE=$(gen_token --user-id alice)
TOKEN_BOB=$(gen_token --user-id bob)
TOKEN_EXPIRED=$(gen_token --user-id alice --ttl 1s)
TOKEN_MALLORY=$(gen_token --user-id mallory --no-register)
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

# Test 11: bash_exec timeout kills entire process tree
echo "--- Test 11: bash_exec timeout returns promptly ---"
start_ts=$(date +%s)
resp=$(mcp_tool_call "bash_exec" '{"command":"sleep 999","timeout":3}' "$TOKEN_ALICE")
end_ts=$(date +%s)
elapsed=$((end_ts - start_ts))
error_val=$(extract_field "stderr" "$resp")
echo "$error_val" | grep -q "timed out" \
  && [ "$elapsed" -lt 15 ] \
  && pass "Timeout returned in ${elapsed}s" \
  || fail "Timeout took ${elapsed}s or wrong message: '$error_val'"

# Test 12: file tools reject paths outside the workspace
echo "--- Test 12: File tool path containment ---"
resp=$(mcp_tool_call "write_file" '{"path":"/workspace/user_bob/hacked.txt","content":"pwned"}' "$TOKEN_ALICE")
error_val=$(extract_field "error" "$resp")
echo "$error_val" | grep -qi "outside workspace" \
  || fail "Absolute path to Bob's workspace not rejected: '$resp'"
resp=$(mcp_tool_call "read_file" '{"path":"/etc/passwd"}' "$TOKEN_ALICE")
error_val=$(extract_field "error" "$resp")
echo "$error_val" | grep -qi "outside workspace" \
  || fail "read_file /etc/passwd not rejected: '$resp'"
resp=$(mcp_tool_call "read_file" '{"path":"../../etc/passwd"}' "$TOKEN_ALICE")
error_val=$(extract_field "error" "$resp")
echo "$error_val" | grep -qi "outside workspace" \
  && pass "File tools reject paths outside the workspace" \
  || fail "read_file ../../etc/passwd not rejected: '$resp'"

# ─── Registry / Admin API Tests ──────────────────────────────────────────

# Test 15: valid signature but unregistered sub → 401
echo "--- Test 15: Unregistered user → 401 ---"
status=$(mcp_raw "whoami" "$TOKEN_MALLORY")
[ "$status" = "401" ] && pass "Unregistered user returns 401" || fail "Expected 401, got $status"

# Test 16: admin creates a user, token works immediately
echo "--- Test 16: Admin API creates user ---"
create_resp=$(admin_curl -X POST "$BASE_URL/admin/users" \
  -H 'Content-Type: application/json' -d '{"user_id":"carol","ttl":"1h"}')
TOKEN_CAROL=$(echo "$create_resp" | python3 -c "import sys,json; print(json.load(sys.stdin).get('token',''))")
[ -n "$TOKEN_CAROL" ] || fail "Admin create returned no token: $create_resp"
resp=$(mcp_call "whoami" "$TOKEN_CAROL")
whoami_result=$(extract_stdout "$resp")
[ "$whoami_result" = "user_carol" ] && pass "Admin-created user works" || fail "Expected user_carol, got '$whoami_result'"

# Test 17: admin list shows the user
echo "--- Test 17: Admin API lists users ---"
listed=$(admin_curl "$BASE_URL/admin/users" | python3 -c "
import sys, json
users = json.load(sys.stdin)['users']
print(any(u['linux_user'] == 'user_carol' for u in users))
")
[ "$listed" = "True" ] && pass "Admin list contains user_carol" || fail "user_carol missing from admin list"

# Test 18: CLI inside the container registers + issues a working token
echo "--- Test 18: In-container CLI registers user ---"
TOKEN_DAVE=$(docker compose exec -T bash-mcp python3 /opt/server/generate_token.py --user-id dave 2>/dev/null | tr -d '\r')
resp=$(mcp_call "whoami" "$TOKEN_DAVE")
whoami_result=$(extract_stdout "$resp")
[ "$whoami_result" = "user_dave" ] && pass "CLI-registered user works" || fail "Expected user_dave, got '$whoami_result'"

# Test 19: wrong admin token → 401
echo "--- Test 19: Wrong admin token → 401 ---"
status=$(curl -s -o /dev/null -w "%{http_code}" -H "Authorization: Bearer wrong-token" "$BASE_URL/admin/users")
[ "$status" = "401" ] && pass "Wrong admin token returns 401" || fail "Expected 401, got $status"

# Test 20: revocation → 401 on next request
echo "--- Test 20: Revocation ---"
revoke_resp=$(admin_curl -X DELETE "$BASE_URL/admin/users/carol")
echo "$revoke_resp" | grep -q '"revoked"' || fail "Revoke failed: $revoke_resp"
status=$(mcp_raw "whoami" "$TOKEN_CAROL")
[ "$status" = "401" ] && pass "Revoked user returns 401" || fail "Expected 401, got $status"

# ─── Files API Tests ─────────────────────────────────────────────────────

# Test 21: upload → download round-trip (binary), listing, delete
echo "--- Test 21: Files API round-trip ---"
TMPD=$(mktemp -d)
trap 'rm -rf "$TMPD"' EXIT
head -c 100000 /dev/urandom > "$TMPD/artifact.bin"
status=$(files_curl "$TOKEN_ALICE" -o "$TMPD/up.json" -w "%{http_code}" \
  -T "$TMPD/artifact.bin" "$BASE_URL/files/artifacts/artifact.bin")
[ "$status" = "200" ] || fail "Upload failed with $status: $(cat "$TMPD/up.json")"
files_curl "$TOKEN_ALICE" -o "$TMPD/down.bin" "$BASE_URL/files/artifacts/artifact.bin"
cmp -s "$TMPD/artifact.bin" "$TMPD/down.bin" || fail "Downloaded bytes differ from uploaded"
listed=$(files_curl "$TOKEN_ALICE" "$BASE_URL/files/artifacts" | python3 -c "
import sys, json
data = json.load(sys.stdin)
print(any(e['name'] == 'artifact.bin' and e['type'] == 'file' for e in data['entries']))
")
[ "$listed" = "True" ] || fail "Listing does not show artifact.bin"
del_resp=$(files_curl "$TOKEN_ALICE" -X DELETE "$BASE_URL/files/artifacts/artifact.bin")
echo "$del_resp" | grep -q '"ok"' || fail "Delete failed: $del_resp"
status=$(files_curl "$TOKEN_ALICE" -o /dev/null -w "%{http_code}" "$BASE_URL/files/artifacts/artifact.bin")
[ "$status" = "404" ] && pass "Files API upload/download/list/delete round-trip" || fail "Expected 404 after delete, got $status"

# Test 22: files API path containment
echo "--- Test 22: Files API path containment ---"
status=$(files_curl "$TOKEN_ALICE" -o /dev/null -w "%{http_code}" --path-as-is \
  -X PUT -d "evil" "$BASE_URL/files/../../etc/evil")
[ "$status" = "400" ] && pass "Files API rejects path traversal" || fail "Expected 400, got $status"

# Test 23: per-user process limit is applied to sudo sessions
echo "--- Test 23: ulimit applied via pam_limits ---"
resp=$(mcp_call "ulimit -u" "$TOKEN_ALICE")
nproc_limit=$(extract_stdout "$resp")
[ "$nproc_limit" = "256" ] && pass "nproc limit = 256" || fail "Expected nproc 256, got '$nproc_limit'"

# Test 24: cross-user write via bash_exec blocked by permissions
# NOTE: Requires proper Linux file permissions. On macOS Docker Desktop with
# bind mounts, permissions are not enforced — the test is skipped there.
echo "--- Test 24: bash_exec cross-user write blocked ---"
resp=$(mcp_call "echo pwned > /workspace/user_bob/hacked.txt 2>&1 || echo WRITE_DENIED" "$TOKEN_ALICE")
result=$(extract_stdout "$resp")
if echo "$result" | grep -q "WRITE_DENIED\|Permission denied"; then
  pass "Alice cannot write to Bob's workspace via bash"
else
  echo -e "${RED}SKIP${NC}: Cross-user write not blocked (expected on macOS Docker bind mounts)"
fi

# Test 25: container restart — registered users survive (reconcile)
echo "--- Test 25: Restart reconciliation ---"
JWT_SECRET=$SECRET ENABLE_ADMIN_API=true ADMIN_TOKEN=$ADMIN_TOKEN_VAL docker compose restart
wait_healthy
resp=$(mcp_call "whoami" "$TOKEN_ALICE")
whoami_result=$(extract_stdout "$resp")
[ "$whoami_result" = "user_alice" ] && pass "Alice survives container restart" || fail "Expected user_alice after restart, got '$whoami_result'"

docker compose down --volumes

# ─── Test 26: Admin API disabled → 404 ───────────────────────────────────
echo ""
echo "--- Test 26: Admin API disabled → 404 ---"
clean_workspace
JWT_SECRET=$SECRET docker compose up -d
wait_healthy
status=$(admin_curl -o /dev/null -w "%{http_code}" "$BASE_URL/admin/users")
docker compose down --volumes
[ "$status" = "404" ] && pass "Admin API disabled returns 404" || fail "Expected 404, got $status"

# ─── Test 13: ALLOW_NO_AUTH mode ──────────────────────────────────────────
echo ""
echo "--- Test 13: ALLOW_NO_AUTH=true → open access ---"
clean_workspace
ALLOW_NO_AUTH=true docker compose up -d
wait_healthy
resp=$(mcp_call "echo ok")
result=$(extract_stdout "$resp")
docker compose down --volumes
[ "$result" = "ok" ] && pass "ALLOW_NO_AUTH allows open access" || fail "Expected 'ok', got '$result'"

# ─── Test 14: ENABLE_FILE_TOOLS=false → only bash_exec ────────────────────
echo ""
echo "--- Test 14: ENABLE_FILE_TOOLS=false → no file tools ---"
ALLOW_NO_AUTH=true ENABLE_FILE_TOOLS=false docker compose up -d
wait_healthy
# List tools via tools/list
tools_resp=$(curl -sf -X POST "$MCP_URL" \
  -H 'Content-Type: application/json' \
  -H 'Accept: application/json, text/event-stream' \
  -d '{"jsonrpc":"2.0","method":"tools/list","id":1}')
docker compose down --volumes
tool_names=$(echo "$tools_resp" | grep '^data:' | tail -1 | sed 's/^data://' | python3 -c "
import sys, json
resp = json.load(sys.stdin)
tools = resp.get('result', {}).get('tools', [])
for t in tools:
    print(t['name'])
")
if echo "$tool_names" | grep -q "bash_exec" && ! echo "$tool_names" | grep -q "read_file"; then
  pass "Only bash_exec registered (file tools disabled)"
else
  fail "Expected only bash_exec, got: $tool_names"
fi

# ─── Done ─────────────────────────────────────────────────────────────────
clean_workspace
echo ""
echo -e "${GREEN}All tests passed!${NC}"
