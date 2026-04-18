#!/usr/bin/env python3
"""Kittyhack API token management CLI.

Usage:
    python tools/api_token.py create <label>
    python tools/api_token.py list
    python tools/api_token.py revoke <token_id>

Run from the kittyhack project root so the token file (`api_tokens.json`)
lands next to `config.ini`.
"""

from __future__ import annotations

import argparse
import os
import sys

# Allow running the script directly from the repo root
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from src.api import create_token, list_tokens, revoke_token, TOKEN_FILE  # noqa: E402


def cmd_create(label: str) -> int:
    clear, record = create_token(label)
    print("Token created. STORE IT NOW — it will not be shown again:")
    print()
    print(f"    {clear}")
    print()
    print(f"id:         {record['id']}")
    print(f"label:      {record['label']}")
    print(f"created_at: {record['created_at']}")
    print(f"storage:    {TOKEN_FILE}")
    print()
    print("Use it in requests, either via header:")
    print(f"    curl -H 'Authorization: Bearer {clear}' http://<kittyhack-host>/api/v1/status")
    print("or — for URL-only clients like Stream Deck — as a query parameter:")
    print(f"    http://<kittyhack-host>/api/v1/door/open?token={clear}")
    return 0


def cmd_list() -> int:
    tokens = list_tokens()
    if not tokens:
        print("(no tokens)")
        return 0
    fmt = "{:<18} {:<25} {:<25} {}"
    print(fmt.format("id", "created_at", "last_used_at", "label"))
    print("-" * 90)
    for t in tokens:
        print(fmt.format(
            t.get("id", "?"),
            t.get("created_at", "?"),
            t.get("last_used_at") or "never",
            t.get("label", "?"),
        ))
    return 0


def cmd_revoke(token_id: str) -> int:
    if revoke_token(token_id):
        print(f"Revoked token {token_id}")
        return 0
    print(f"No token found with id {token_id}", file=sys.stderr)
    return 1


def main() -> int:
    parser = argparse.ArgumentParser(description="Kittyhack API token management")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_create = sub.add_parser("create", help="Create a new API token")
    p_create.add_argument("label", help="human-readable label (e.g. 'home-assistant')")

    sub.add_parser("list", help="List all tokens (metadata only)")

    p_revoke = sub.add_parser("revoke", help="Revoke a token by id")
    p_revoke.add_argument("token_id")

    args = parser.parse_args()

    if args.cmd == "create":
        return cmd_create(args.label)
    if args.cmd == "list":
        return cmd_list()
    if args.cmd == "revoke":
        return cmd_revoke(args.token_id)
    parser.print_help()
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
