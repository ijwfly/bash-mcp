#!/usr/bin/env python3
"""CLI utility to generate JWT tokens for bash-mcp authentication."""

import argparse
import re
import sys
import time

import jwt


def parse_ttl(ttl_str: str) -> int:
    """Parse a TTL string like '30d', '24h', '1h' into seconds."""
    match = re.fullmatch(r"(\d+)([dhms])", ttl_str)
    if not match:
        raise ValueError(f"Invalid TTL format: {ttl_str!r}. Use e.g. '30d', '24h', '3600s'.")
    value, unit = int(match.group(1)), match.group(2)
    multipliers = {"s": 1, "m": 60, "h": 3600, "d": 86400}
    return value * multipliers[unit]


def sanitize_username(user_id: str) -> str:
    """Convert arbitrary user_id to a valid Linux username."""
    cleaned = re.sub(r"[^a-z0-9]", "_", user_id.lower())[:28]
    return f"user_{cleaned}"


def main():
    parser = argparse.ArgumentParser(description="Generate a JWT token for bash-mcp")
    parser.add_argument("--user-id", required=True, help="User identifier (e.g. email, UUID)")
    parser.add_argument("--secret", required=True, help="JWT secret (must match JWT_SECRET in the container)")
    parser.add_argument("--ttl", default=None, help="Token TTL, e.g. '30d', '24h', '1h'")
    args = parser.parse_args()

    linux_user = sanitize_username(args.user_id)
    now = int(time.time())
    payload = {"user_id": args.user_id, "sub": linux_user, "iat": now}

    if args.ttl:
        payload["exp"] = now + parse_ttl(args.ttl)

    token = jwt.encode(payload, args.secret, algorithm="HS256")
    print(token)
    print(f"linux_user: {linux_user}", file=sys.stderr)


if __name__ == "__main__":
    main()
