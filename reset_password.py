"""Reset a user's password (or list users) directly against the database.

Run this ON THE SERVER, from the app's folder, with its Python environment —
it uses the same app/data/erm.db the live service uses.

List all accounts:
    python reset_password.py --list

Reset a password (by username or by id):
    python reset_password.py --user tony --password "NewPass123"
    python reset_password.py --id 4 --password "NewPass123"

Notes:
- The new password must be at least 8 characters.
- The change is immediate; no service restart is needed (auth reads the DB
  live). If a login still fails right after, hard-refresh the browser so an
  old session cookie isn't reused.
"""
import argparse
import sys

from app import auth, db


def main():
    ap = argparse.ArgumentParser(description="Reset an ERM user's password.")
    ap.add_argument("--list", action="store_true", help="List all users and exit.")
    ap.add_argument("--user", help="Username to reset.")
    ap.add_argument("--id", type=int, help="User id to reset (alternative to --user).")
    ap.add_argument("--password", help="The new password (min 8 chars).")
    args = ap.parse_args()

    db.init_db()

    with db.get_db() as conn:
        users = conn.execute("SELECT id, username, is_admin, email FROM users ORDER BY id").fetchall()

    if args.list or (not args.user and args.id is None):
        print("Users:")
        for u in users:
            print(f"  id={u['id']:<3} {u['username']:<20} "
                  f"{'admin' if u['is_admin'] else 'user ':<5} {u['email'] or ''}")
        if args.list:
            return
        print("\nTo reset: python reset_password.py --user <name> --password <newpass>")
        return

    if not args.password:
        print("ERROR: --password is required to reset.", file=sys.stderr)
        sys.exit(2)
    if len(args.password) < 8:
        print("ERROR: password must be at least 8 characters.", file=sys.stderr)
        sys.exit(2)

    target = None
    for u in users:
        if (args.id is not None and u["id"] == args.id) or \
           (args.user and u["username"].lower() == args.user.lower()):
            target = u
            break
    if target is None:
        print(f"ERROR: no user matched {args.user or args.id!r}.", file=sys.stderr)
        sys.exit(1)

    auth.set_password(target["id"], args.password)
    print(f"OK: password reset for {target['username']} (id={target['id']}).")


if __name__ == "__main__":
    main()
