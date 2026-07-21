#!/usr/bin/env python3
"""CLI to register a bash-mcp user and generate a JWT token for them.

By default this both registers the user in the registry (so the server will
accept their tokens) and prints a token. Typical usage:

Inside the container (secret and registry path come from the environment):
    docker compose exec bash-mcp python3 /opt/server/generate_token.py --user-id alice

From the host (registry lives in the ./workspace bind mount):
    python3 server/generate_token.py --user-id alice --secret "$JWT_SECRET" \
        --registry workspace/.users.json
"""

import argparse
import os
import sys

import registry


def main():
    parser = argparse.ArgumentParser(
        description="Register a bash-mcp user and generate a JWT token")
    parser.add_argument("--user-id", required=True,
                        help="User identifier (e.g. email, UUID)")
    parser.add_argument("--secret", default=os.environ.get("JWT_SECRET", ""),
                        help="JWT secret (default: JWT_SECRET env var)")
    parser.add_argument("--ttl", default="30d",
                        help="Token TTL, e.g. '30d', '24h', '1h' (default: 30d)")
    parser.add_argument("--no-expiry", action="store_true",
                        help="Issue a token without expiration (overrides --ttl)")
    parser.add_argument("--registry", default=None,
                        help="Path to the user registry JSON "
                             "(default: REGISTRY_PATH env var or /workspace/.users.json)")
    parser.add_argument("--no-register", action="store_true",
                        help="Only generate a token, do not touch the registry")
    parser.add_argument("--reactivate", action="store_true",
                        help="Re-activate the user if they were revoked")
    args = parser.parse_args()

    if not args.secret:
        parser.error("--secret is required (or set the JWT_SECRET env var)")

    linux_user = registry.sanitize_username(args.user_id)

    if not args.no_register:
        try:
            registry.add_user(args.user_id, path=args.registry,
                              reactivate=args.reactivate)
        except registry.UserRevoked as e:
            sys.exit(f"error: {e}. Re-run with --reactivate or use the admin API.")
        except registry.UserCollision as e:
            sys.exit(f"error: {e}")
        except registry.RegistryError as e:
            sys.exit(f"error: {e}")

        # Inside the container we can provision the OS user right away;
        # on the host it happens at server startup (reconcile) instead.
        if os.path.exists("/opt/server/main.py") and os.geteuid() == 0:
            sys.path.insert(0, "/opt/server")
            from common import ensure_user
            ensure_user(linux_user)

    ttl_seconds = None if args.no_expiry else registry.parse_ttl(args.ttl)
    token = registry.make_token(args.user_id, linux_user, args.secret, ttl_seconds)
    print(token)
    print(f"linux_user: {linux_user}", file=sys.stderr)


if __name__ == "__main__":
    main()
