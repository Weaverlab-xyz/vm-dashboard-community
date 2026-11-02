"""Command line for the database layer: ``python -m web_dashboard.database <command>``."""
import sys

from . import create_admin_user, init_db


def main(argv):
    if len(argv) < 2:
        print("Usage:")
        print("  python -m web_dashboard.database init                          # Initialize database")
        print("  python -m web_dashboard.database create-user <username> <password> [workgroups]")
        return 1

    command = argv[1]

    if command == "init":
        init_db()
    elif command == "create-user":
        if len(argv) < 4:
            print("Usage: python -m web_dashboard.database create-user <username> <password> [Hydra,Weaverlab]")
            return 1

        username = argv[2]
        password = argv[3]
        workgroups = argv[4].split(",") if len(argv) > 4 else None

        create_admin_user(username, password, workgroups)
    else:
        print(f"Unknown command: {command}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
