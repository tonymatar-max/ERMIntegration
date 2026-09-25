"""Load KSA data straight into the ERM database — no need to reopen a locked
PP Report month.

    python tools\\load_ksa.py --month 2026-08 --workbook "Aug_2026_-_PP_Report_v1.0.xlsx" --timesheet "Aug 2026 Time Sheet.xlsx"

  --month      the PP Report month (YYYY-MM) the files are for
  --workbook   the monthly PP Report workbook (its month tab + the previous
               month's tab, e.g. 'Aug' and 'July') — optional
  --timesheet  the Intra timesheet export (only KSA rows are read) — optional
  --db         database file (default: data\\erm.db next to this project)
  --dry-run    read and report only, change nothing

What it does (same as the upload panel on /admin/pp-report-months):
  * backs the database up first (data\\backups\\erm_<timestamp>.db)
  * replaces that month's KSA workbook rows / KSA timesheet hours
  * if the month is LOCKED, rewrites only the KSA lines inside the frozen
    snapshot — every other country's figures are left exactly as locked

Run it from the project folder with the project's Python (the .venv), e.g.
    .venv\\Scripts\\python.exe tools\\load_ksa.py --month 2026-08 --workbook ... --timesheet ...
Stopping the app first isn't required.
"""

import argparse
import datetime
import os
import re
import sqlite3
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def main():
    ap = argparse.ArgumentParser(description="Load KSA workbook / timesheet data into the ERM database.")
    ap.add_argument("--month", required=True, help="PP Report month, YYYY-MM")
    ap.add_argument("--workbook", help="monthly PP Report workbook (.xlsx)")
    ap.add_argument("--timesheet", help="Intra timesheet export (.xlsx)")
    ap.add_argument("--db", help="database file (default data\\erm.db)")
    ap.add_argument("--dry-run", action="store_true", help="report only, change nothing")
    args = ap.parse_args()

    if not re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", args.month):
        sys.exit("--month must look like 2026-08")
    if not args.workbook and not args.timesheet:
        sys.exit("Give --workbook and/or --timesheet.")
    for label, path in (("workbook", args.workbook), ("timesheet", args.timesheet)):
        if path and not os.path.isfile(path):
            sys.exit(f"{label} file not found: {path}")

    # Must be set before app.db is imported (it reads the path at import time).
    if args.db:
        os.environ["ERM_DB_PATH"] = os.path.abspath(args.db)
    sys.path.insert(0, ROOT)
    from app import db, pp_report  # noqa: E402

    if not os.path.isfile(db.DB_PATH):
        sys.exit(f"Database not found: {db.DB_PATH}")
    print(f"Database: {db.DB_PATH}")

    workbook_rows = timesheet_rows = None
    try:
        if args.workbook:
            with open(args.workbook, "rb") as f:
                workbook_rows = pp_report.parse_ksa_workbook(f.read(), args.month)
            total = sum(r["amount_to_take"] for r in workbook_rows)
            print(f"Workbook : {len(workbook_rows)} KSA projects, Amount to Take {total:,.2f}")
        if args.timesheet:
            with open(args.timesheet, "rb") as f:
                timesheet_rows = pp_report.parse_ksa_timesheet(f.read(), args.month)
            hours = sum(r["hours"] for r in timesheet_rows)
            print(f"Timesheet: {len(timesheet_rows)} KSA entries, {hours:,.1f} h, {len({r['user_name'] for r in timesheet_rows})} people")
    except ValueError as e:
        sys.exit(f"Could not read the file: {e}")

    if args.dry_run:
        print("Dry run — nothing changed.")
        return

    backups = os.path.join(os.path.dirname(db.DB_PATH), "backups")
    os.makedirs(backups, exist_ok=True)
    backup_path = os.path.join(backups, f"erm_{datetime.datetime.now():%Y%m%d_%H%M%S}.db")
    src = sqlite3.connect(db.DB_PATH)
    dst = sqlite3.connect(backup_path)
    with dst:
        src.backup(dst)
    src.close()
    dst.close()
    print(f"Backup   : {backup_path}")

    db.init_db()  # creates the KSA tables if this database predates them

    if workbook_rows is not None:
        pp_report.save_ksa_rows(args.month, workbook_rows)
    if timesheet_rows is not None:
        pp_report.save_ksa_timesheet(args.month, timesheet_rows)
        skipped = pp_report.ksa_timesheet_skipped(args.month)
        if skipped:
            print("Not KSA project time (left out): " + ", ".join(f"{c or '(no code)'} {h:,.1f}h" for c, h in skipped))

    merged = pp_report.refresh_ksa_in_locked_snapshot(args.month)
    if merged is None:
        print(f"{args.month} is open: the PP Report picks the KSA data up live.")
    else:
        print(f"{args.month} is locked: updated {merged} KSA lines in the frozen snapshot; other countries untouched.")
    print("Done.")


if __name__ == "__main__":
    main()
