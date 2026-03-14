#!/usr/bin/env bash
set -euo pipefail

echo "[entrypoint] Starting MCP server..."
exec python3 /opt/server/main.py
