"""
User management CLI for Disco Validator.
Run this script to create, delete, or list users.

Usage:
    python create_user.py add <username> <password>
    python create_user.py delete <username>
    python create_user.py list
    python create_user.py password <username> <new_password>
"""

import sys
from database import init_db
from auth import init_auth_db, create_user, delete_user, list_users, change_password


def main():
    init_db()
    init_auth_db()

    args = sys.argv[1:]

    if not args:
        print(__doc__)
        return

    cmd = args[0]

    if cmd == "add":
        if len(args) < 3:
            print("Usage: python create_user.py add <username> <password>")
            return
        username, password = args[1], args[2]
        try:
            create_user(username, password)
            print(f"✅ User '{username}' created successfully.")
        except ValueError as e:
            print(f"❌ {e}")

    elif cmd == "delete":
        if len(args) < 2:
            print("Usage: python create_user.py delete <username>")
            return
        username = args[1]
        delete_user(username)
        print(f"✅ User '{username}' deleted.")

    elif cmd == "list":
        users = list_users()
        if not users:
            print("No users found.")
        else:
            print(f"\n{'USERNAME':<20} {'CREATED AT'}")
            print("-" * 50)
            for u in users:
                print(f"{u['username']:<20} {u['created_at']}")

    elif cmd == "password":
        if len(args) < 3:
            print("Usage: python create_user.py password <username> <new_password>")
            return
        username, new_password = args[1], args[2]
        change_password(username, new_password)
        print(f"✅ Password updated for '{username}'.")

    else:
        print(f"Unknown command: {cmd}")
        print(__doc__)


if __name__ == "__main__":
    main()
