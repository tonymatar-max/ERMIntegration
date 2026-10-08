"""Restore employee hourly rates (recovered snapshot, 2026-09-23).

Run on the server from the app folder with its venv Python:
    & "C:\\ERMProject\\.venv\\Scripts\\python.exe" restore_employee_rates.py --list
    & "C:\\ERMProject\\.venv\\Scripts\\python.exe" restore_employee_rates.py --apply

--list  shows what's currently stored vs. what this script would set.
--apply writes the rates below into the employees table (upsert by user id).
It only sets a rate that is missing or zero unless you pass --force, so it
won't overwrite a newer rate someone has re-entered since the data loss.
"""
import argparse

from app import db, settings_store

# user_id -> (name, hourly_rate)  — recovered from the 2026-09-23 snapshot.
RATES = {
    3252: ("Viswanathan, Balakumar", 11.824),
    4193: ("Akbar, Zeeshan", 152.38),
    4194: ("Patil, Suhas", 98.35),
    4195: ("Youssef, Tarek", 23.88),
    4196: ("Sharaf, Ashraf", 36.25),
    4197: ("Jain, Akshant", 21.56),
    4198: ("Krishna, Vamsi", 98.19),
    4200: ("Kassem, Mohamed", 7.474),
    4201: ("Loganathan, Abhilash", 3.885),
    4202: ("Saade, Maroun", 16.0),
    4203: ("Iskandarani, Toufic", 30.05),
    4204: ("Touma, Hani", 119.16),
    4205: ("Jaafar, Ali", 17.36),
    4206: ("Majeed, Waqas", 32.17),
    4207: ("Talha, Muhamad", 32.17),
    4208: ("Khaliq, Tayyaba", 19.54),
    4209: ("Ghorayeb, Karl", 9.71),
    4211: ("Khowise, Mazen", 27.43),
    4212: ("Ajram, Rawad", 22.09),
    4213: ("Naseem, Umar", 27.99),
    4249: ("Kumar, Ranjith", 124.97),
    4250: ("Halabi, Wael", 31.04),
    4254: ("Veerapandian, Ganeshram", 8.218),
}


def main():
    ap = argparse.ArgumentParser(description="Restore recovered employee rates.")
    ap.add_argument("--apply", action="store_true", help="Write the rates (otherwise dry-run).")
    ap.add_argument("--list", action="store_true", help="Show current vs. recovered and exit.")
    ap.add_argument("--force", action="store_true", help="Overwrite rates that already have a non-zero value.")
    args = ap.parse_args()

    db.init_db()
    current = settings_store.get_employee_rates()  # user_id -> rate

    print(f"{'ID':<6}{'Name':<28}{'Current':>10}{'Recovered':>12}  Action")
    to_write = []
    for uid, (name, rate) in sorted(RATES.items(), key=lambda kv: kv[1][0]):
        cur = current.get(uid)
        has_rate = cur not in (None, 0, 0.0)
        if has_rate and not args.force:
            action = "keep (has rate)"
        else:
            action = "SET"
            to_write.append((uid, name, rate))
        cur_str = "-" if cur is None else f"{cur:g}"
        print(f"{uid:<6}{name:<28}{cur_str:>10}{rate:>12g}  {action}")

    if args.list or not args.apply:
        print(f"\n{len(to_write)} rate(s) would be set. Re-run with --apply to write them"
              + (" (use --force to overwrite existing)." if not args.force else "."))
        return

    for uid, name, rate in to_write:
        settings_store.set_employee_rate(uid, name, rate)
    print(f"\nDone: wrote {len(to_write)} rate(s).")


if __name__ == "__main__":
    main()
