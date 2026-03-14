#!/usr/bin/env bash
set -euo pipefail

PROFILES_INI="$HOME/.ocli/profiles.ini"

if [ -f "$PROFILES_INI" ]; then
    echo "[entrypoint] Found profiles.ini, refreshing specs..."
    # Extract section names [profile_name] from ini file
    profile_names=$(grep -oP '^\[\K[^\]]+' "$PROFILES_INI" || true)
    for profile in $profile_names; do
        echo "[entrypoint] Refreshing profile: $profile"
        ocli spec refresh --profile "$profile" || echo "[entrypoint] Warning: failed to refresh profile $profile"
    done
else
    echo "[entrypoint] No profiles.ini found, checking env vars..."
    # Extract unique profile names from OCLI_PROFILE_{NAME}_* env vars
    profile_names=$(env | grep -oP '^OCLI_PROFILE_\K[^_]+' | sort -u || true)
    for name in $profile_names; do
        profile=$(echo "$name" | tr '[:upper:]' '[:lower:]')
        echo "[entrypoint] Adding profile from env: $profile"

        base_url_var="OCLI_PROFILE_${name}_BASE_URL"
        spec_var="OCLI_PROFILE_${name}_SPEC"
        token_var="OCLI_PROFILE_${name}_TOKEN"
        basic_auth_var="OCLI_PROFILE_${name}_BASIC_AUTH"
        include_var="OCLI_PROFILE_${name}_INCLUDE"
        exclude_var="OCLI_PROFILE_${name}_EXCLUDE"

        cmd=(ocli profiles add --name "$profile")
        cmd+=(--api-base-url "${!base_url_var}")
        cmd+=(--openapi-spec-source "${!spec_var}")

        if [ -n "${!token_var:-}" ]; then
            cmd+=(--api-bearer-token "${!token_var}")
        fi
        if [ -n "${!basic_auth_var:-}" ]; then
            cmd+=(--api-basic-auth "${!basic_auth_var}")
        fi
        if [ -n "${!include_var:-}" ]; then
            cmd+=(--include "${!include_var}")
        fi
        if [ -n "${!exclude_var:-}" ]; then
            cmd+=(--exclude "${!exclude_var}")
        fi

        "${cmd[@]}" || echo "[entrypoint] Warning: failed to add profile $profile"
    done
fi

echo "[entrypoint] Starting MCP server..."
exec python3 /opt/server/main.py
