"""Manage the isolated administrator authentication database without exposing passwords."""
import argparse
import getpass
import json
from pathlib import Path
import sqlite3
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from nutrition.auth.admin_auth import (assign_role, authenticate, bootstrap_admin,
                                        change_password, create_admin, disable_admin,
                                        initialize_auth_database, list_admins,
                                        reset_password, verify_auth_database)


def password(prompt):
    """Read secrets from a TTY; tests may inject getpass without command-line exposure."""
    return getpass.getpass(prompt)


def actor(auth_db, username):
    return authenticate(auth_db, username, password("Administrator password: "))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    init = commands.add_parser("init")
    init.add_argument("--auth-db", required=True)
    bootstrap = commands.add_parser("bootstrap-admin")
    bootstrap.add_argument("--auth-db", required=True)
    bootstrap.add_argument("--username", required=True)
    auth = commands.add_parser("authenticate")
    auth.add_argument("--auth-db", required=True)
    auth.add_argument("--username", required=True)
    create = commands.add_parser("create-admin")
    create.add_argument("--auth-db", required=True)
    create.add_argument("--actor", required=True)
    create.add_argument("--username", required=True)
    create.add_argument("--role", required=True)
    role = commands.add_parser("assign-role")
    role.add_argument("--auth-db", required=True)
    role.add_argument("--actor", required=True)
    role.add_argument("--account-id", required=True)
    role.add_argument("--role", required=True)
    disable = commands.add_parser("disable-admin")
    disable.add_argument("--auth-db", required=True)
    disable.add_argument("--actor", required=True)
    disable.add_argument("--account-id", required=True)
    change = commands.add_parser("change-password")
    change.add_argument("--auth-db", required=True)
    change.add_argument("--username", required=True)
    reset = commands.add_parser("reset-password")
    reset.add_argument("--auth-db", required=True)
    reset.add_argument("--actor", required=True)
    reset.add_argument("--account-id", required=True)
    listing = commands.add_parser("list-admins")
    listing.add_argument("--auth-db", required=True)
    listing.add_argument("--actor", required=True)
    verify = commands.add_parser("verify")
    verify.add_argument("--auth-db", required=True)
    args = parser.parse_args()
    try:
        if args.command == "init":
            initialize_auth_database(args.auth_db)
            result = {"status": "initialized", "database": "admin-auth-v1"}
        elif args.command == "bootstrap-admin":
            first = password("New administrator password: ")
            if first != password("Repeat new password: "):
                raise ValueError("Passwords do not match")
            result = {"status": "created", "account_id": bootstrap_admin(args.auth_db, args.username, first)}
        elif args.command == "authenticate":
            principal = actor(args.auth_db, args.username)
            result = {"status": "authenticated", "account_id": principal.account_id,
                      "username": principal.username, "role": principal.role}
        elif args.command == "create-admin":
            principal = actor(args.auth_db, args.actor)
            new = password("New administrator password: ")
            if new != password("Repeat new password: "):
                raise ValueError("Passwords do not match")
            result = {"status": "created", "account_id": create_admin(
                args.auth_db, principal, args.username, new, args.role)}
        elif args.command == "assign-role":
            assign_role(args.auth_db, actor(args.auth_db, args.actor), args.account_id, args.role)
            result = {"status": "updated"}
        elif args.command == "disable-admin":
            disable_admin(args.auth_db, actor(args.auth_db, args.actor), args.account_id)
            result = {"status": "disabled"}
        elif args.command == "change-password":
            current = password("Current password: ")
            principal = authenticate(args.auth_db, args.username, current)
            new = password("New password: ")
            if new != password("Repeat new password: "):
                raise ValueError("Passwords do not match")
            change_password(args.auth_db, principal, current, new)
            result = {"status": "password_changed"}
        elif args.command == "reset-password":
            principal = actor(args.auth_db, args.actor)
            new = password("New password: ")
            if new != password("Repeat new password: "):
                raise ValueError("Passwords do not match")
            reset_password(args.auth_db, principal, args.account_id, new)
            result = {"status": "password_reset"}
        elif args.command == "list-admins":
            result = {"administrators": list_admins(args.auth_db, actor(args.auth_db, args.actor))}
        else:
            result = verify_auth_database(args.auth_db)
            if not result["valid"]:
                raise ValueError("Authentication audit verification failed")
    except (ValueError, TypeError, PermissionError, OSError, sqlite3.Error) as error:
        parser.exit(2, json.dumps({"status": "rejected", "error_type": type(error).__name__,
                                  "message": str(error)}, ensure_ascii=False) + "\n")
    print(json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2))


if __name__ == "__main__":
    main()
