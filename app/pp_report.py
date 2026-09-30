"""Monthly PP (Earned Revenue) report — a hosted equivalent of the manual
'Earned Revenue Mon YY - Kuwait_Workings.xlsx' workbook.

Ported from that workbook's 'Aug26' sheet, row-3 formulas (columns G..AH):
everything is computed from Estimated/Spent/Risk (already in the cached
Redmine project data), one shared 'daily rate' per month, and exactly two
genuinely manual per-project inputs the workbook also required by hand:
Total Project Revenue (often hand-typed, sometimes a sum of SAP orders)
and free-text Notes.

'Previous Accumulated', which the workbook carries forward by hand from
the prior month's 'Accumulated Till Current Month' column, is automated
here: locking a month freezes that month's computed rows, and the next
month reads a project's frozen 'accumulated' as its own previous_accumulated.
"""

import calendar
import csv
import json
import math
import os
from datetime import date, datetime, timezone

from . import db, settings_store

FIRST_MONTH_DAILY_RATE_DEFAULT = 259.59  # workbook's "Daily Rate with Shared Service"

# The one month this app has no Redmine-tracked history for — its figures
# come from a hand-maintained opening_balance.csv at the site root instead
# of being computed from Estimated/Spent, so the accumulated-earnings
# chain has somewhere to start from. See load_opening_balance().
OPENING_BALANCE_MONTH = "2026-07"


def current_month_str():
    return date.today().strftime("%Y-%m")


def previous_month_str(month: str) -> str:
    year, mon = (int(x) for x in month.split("-"))
    if mon == 1:
        return f"{year - 1}-12"
    return f"{year}-{mon - 1:02d}"


def next_month_str(month: str) -> str:
    year, mon = (int(x) for x in month.split("-"))
    if mon == 12:
        return f"{year + 1}-01"
    return f"{year}-{mon + 1:02d}"


def next_month_to_lock() -> str:
    """The earliest month not yet locked, walking forward from
    OPENING_BALANCE_MONTH — where the PP Report should default to opening,
    since that's the next thing actually needing attention (not necessarily
    today's calendar month, if locking has fallen behind). Capped well
    beyond any realistic backlog so a pathological all-locked state can't
    loop forever."""
    month = OPENING_BALANCE_MONTH
    for _ in range(1000):
        if not is_locked(month):
            return month
        month = next_month_str(month)
    return month


def format_month_label(month: str) -> str:
    """'2026-08' -> '2026-Aug' — used for the "not closed yet" hint so it
    reads naturally rather than as the raw 'YYYY-MM' value."""
    year, mon = (int(x) for x in month.split("-"))
    return f"{year}-{calendar.month_abbr[mon]}"


def month_end_str(month: str) -> str:
    """Last calendar day of `month` ('YYYY-MM'), as an ISO date string."""
    year, mon = (int(x) for x in month.split("-"))
    last_day = calendar.monthrange(year, mon)[1]
    return date(year, mon, last_day).isoformat()


def spent_through_month(timespent: list, month: str) -> dict:
    """project_id -> sum of hours logged on or before the last day of
    `month`, from the cached raw time-entry list (settings_store.load_cache
    'timespent'). ISO 'YYYY-MM-DD' strings compare correctly with plain
    string comparison, so no date parsing is needed.

    Deduplicates by each entry's Redmine time-entry id — defensive against
    the cached list ever containing the same entry twice (e.g. from two
    overlapping Redmine fetches racing each other), which otherwise
    silently doubles every total."""
    cutoff = month_end_str(month)
    totals = {}
    seen_entry_ids = set()
    for entry in timespent or []:
        entry_id = entry.get("id")
        if entry_id is not None:
            if entry_id in seen_entry_ids:
                continue
            seen_entry_ids.add(entry_id)

        spent_on = (entry.get("spent_on") or "").strip()
        if not spent_on or spent_on > cutoff:
            continue
        project_id = entry.get("project_id")
        if project_id is None:
            continue
        totals[project_id] = totals.get(project_id, 0.0) + to_float(entry.get("hours"))
    return totals


def hours_by_project_and_consultant(timespent: list, month: str) -> dict:
    """project_id -> {user_id: hours logged on or before the last day of
    `month`} — same cutoff/dedup rules as spent_through_month() above, just
    grouped by consultant too. Used to cost a project's Cost till Date from
    each consultant's own hourly rate (see Employees admin screen) rather
    than one flat country Daily Rate — see build_report's non-opening-
    balance branch."""
    cutoff = month_end_str(month)
    by_project = {}
    seen_entry_ids = set()
    for entry in timespent or []:
        entry_id = entry.get("id")
        if entry_id is not None:
            if entry_id in seen_entry_ids:
                continue
            seen_entry_ids.add(entry_id)

        spent_on = (entry.get("spent_on") or "").strip()
        if not spent_on or spent_on > cutoff:
            continue
        project_id = entry.get("project_id")
        user_id = entry.get("user_id")
        if project_id is None or user_id is None:
            continue
        by_user = by_project.setdefault(project_id, {})
        by_user[user_id] = by_user.get(user_id, 0.0) + to_float(entry.get("hours"))
    return by_project


def consultant_cost_till_date(project_id, hours_by_project_user: dict, employee_rates: dict, fallback_hourly_rate: float) -> float:
    """Sums (hours logged * that consultant's own hourly rate) for one
    project — a consultant with no saved rate yet (Employees admin screen)
    falls back to the project's country Daily Rate converted to hourly
    (rate/8), same basis the old flat calculation used, so nobody's hours
    are silently costed at zero just because a rate hasn't been entered."""
    hours_by_user = hours_by_project_user.get(project_id, {})
    total = 0.0
    for user_id, hours in hours_by_user.items():
        rate = employee_rates.get(user_id)
        if not rate:
            rate = fallback_hourly_rate
        total += hours * rate
    return total


def parse_revenue_input(text: str):
    """Accepts a plain number or a '+'-separated sum (e.g. '19500+4900'),
    matching how the original workbook's Total Project Revenue cells were
    often hand-typed. Returns None for blank input, raises ValueError for
    anything else unparseable."""
    text = (text or "").strip()
    if not text:
        return None
    if "+" in text:
        parts = [p.strip() for p in text.split("+")]
        return sum(float(p) for p in parts if p)
    return float(text)


def _safe_div(numerator, denominator):
    if not denominator:
        return 0.0
    return numerator / denominator


def _profitability_fields(estimated, spent, total_revenue, accumulated, fully_earned, rate, completion_override=None, cost_till_date=None):
    """The Sales/Cost/Profitability columns shared by compute_row() and
    compute_row_from_fixed_accumulated() — driven by `rate`, the Daily Rate
    of the project's *country* (see the Countries admin screen /
    settings_store.list_countries), not the month's single shared rate.

    `completion_override` lets Project Completion be hand-edited (see
    save_override) — when given, it's used in place of the formula-derived
    value for Expected Profitability, and reported back as 'completion' so
    the UI always shows exactly what's in effect. 'computedCompletion' is
    the formula value regardless, so the UI can show it as a placeholder/
    reference even when overridden.

    `cost_till_date`, when given, overrides the default rate/8*h calculation
    with a consultant-by-consultant figure (see consultant_cost_till_date) —
    used by compute_row() for ordinary months, where actual per-consultant
    time entries exist. compute_row_from_fixed_accumulated() (the opening-
    balance month, which has no Redmine-tracked history to attribute hours
    to a consultant) never passes this, so it keeps the flat country-rate
    calculation unchanged. 'Sales Sold/Expected Profit' stay on the flat
    country rate regardless — they price *estimated* (sold) hours, which
    aren't tied to any specific consultant."""
    g, h, k = estimated or 0.0, spent or 0.0, total_revenue or 0.0

    sales_sold_profit = (1 - _safe_div(rate * g / 8, k)) if k else 0.0
    sales_expected_profit = k * sales_sold_profit
    project_time_profitability = _safe_div(g - h, g)
    cost_till_date = (rate / 8 * h) if cost_till_date is None else cost_till_date
    profit_till_date = k - cost_till_date
    amount_profitability = _safe_div(profit_till_date, k)
    computed_completion = 1.0 if fully_earned else _safe_div(accumulated, k)
    completion = computed_completion if completion_override is None else completion_override
    expected_profitability = _safe_div(k - _safe_div(cost_till_date, completion), k) if completion else 0.0

    return {
        "salesSoldProfit": sales_sold_profit,
        "salesExpectedProfit": sales_expected_profit,
        "projectTimeProfitability": project_time_profitability,
        "costTillDate": cost_till_date,
        "profitTillDate": profit_till_date,
        "amountProfitability": amount_profitability,
        "completion": completion,
        "computedCompletion": computed_completion,
        "completionOverridden": completion_override is not None,
        "expectedProfitability": expected_profitability,
    }


def compute_row(estimated, spent, risk, total_revenue, previous_accumulated, daily_rate, completion_override=None, cost_till_date=None):
    """Recreate columns I, L, N, P, Q, R, AA-AG from the workbook's Aug26
    sheet for one project. `total_revenue` may be None (not yet entered by
    the user) — in that case every dependent figure is left at 0/blank
    rather than guessed. `daily_rate` here is the project's *country* rate
    (resolved by the caller — see build_report), not necessarily the
    month's shared rate. `cost_till_date`, when given, is a consultant-by-
    consultant costing that overrides the default flat daily_rate/8*spent
    calculation — see _profitability_fields."""
    g, h = estimated or 0.0, spent or 0.0
    j = risk or 0.0
    k = total_revenue
    m = previous_accumulated or 0.0

    remaining = g - h

    if k is None:
        return {
            "remaining": remaining, "totalRevenue": None, "previousAccumulated": m,
            "accumulated": 0.0, "amountToBeTaken": 0.0, "fullyEarned": False,
            "yetToEarn": 0.0, "remainingPct": 0.0, "salesSoldProfit": 0.0,
            "salesExpectedProfit": 0.0, "projectTimeProfitability": 0.0, "costTillDate": 0.0,
            "profitTillDate": 0.0, "amountProfitability": 0.0,
            "completion": completion_override or 0.0, "computedCompletion": 0.0,
            "completionOverridden": completion_override is not None, "expectedProfitability": 0.0,
        }

    # Both branches of the original IF($J3<0, ..., ...) compute the same
    # expression — kept as a single branch here rather than reproducing
    # the redundant Excel formula.
    ratio_amount = _safe_div(h - j, g) * k
    accumulated = k if (h - j) >= g else ratio_amount
    accumulated = math.ceil(accumulated)

    amount_to_be_taken = accumulated - m
    fully_earned = (k - accumulated) < 1
    yet_to_earn = k - accumulated
    remaining_pct = _safe_div(yet_to_earn, k)

    profitability = _profitability_fields(g, h, k, accumulated, fully_earned, daily_rate, completion_override, cost_till_date=cost_till_date)

    return {
        "remaining": remaining,
        "totalRevenue": k,
        "previousAccumulated": m,
        "accumulated": accumulated,
        "amountToBeTaken": amount_to_be_taken,
        "fullyEarned": fully_earned,
        "yetToEarn": yet_to_earn,
        "remainingPct": remaining_pct,
        **profitability,
    }


def compute_row_from_fixed_accumulated(accumulated, total_revenue, previous_accumulated, daily_rate, spent, amount_to_be_taken, estimated=0.0, completion_override=None):
    """Like compute_row(), but for a project whose Accumulated is a given,
    fixed figure (the opening-balance CSV — see load_opening_balance())
    rather than something derived from Estimated/Spent/Risk. Reuses the
    same dependent-metric relationships as compute_row()'s second half,
    just starting from `accumulated` directly instead of computing it via
    the ratio formula. `amount_to_be_taken` is trusted as given (the CSV's
    own column) rather than recomputed as accumulated - previous_accumulated,
    since opening-balance figures may not reconcile to the exact cent."""
    k = total_revenue or 0.0
    h = spent or 0.0
    m = previous_accumulated or 0.0

    fully_earned = (k - accumulated) < 1
    yet_to_earn = k - accumulated
    remaining_pct = _safe_div(yet_to_earn, k)

    profitability = _profitability_fields(estimated, h, k, accumulated, fully_earned, daily_rate, completion_override)

    return {
        "totalRevenue": k,
        "previousAccumulated": m,
        "accumulated": accumulated,
        "amountToBeTaken": amount_to_be_taken,
        "fullyEarned": fully_earned,
        "yetToEarn": yet_to_earn,
        "remainingPct": remaining_pct,
        **profitability,
    }


# ---------------------------------------------------------------------------
# Storage
# ---------------------------------------------------------------------------

def get_month_meta(month: str):
    with db.get_db() as conn:
        row = conn.execute(
            "SELECT daily_rate, locked, locked_snapshot_json, reopened_by, reopened_on FROM pp_report_months WHERE month = ?",
            (month,),
        ).fetchone()
        if row:
            return {
                "daily_rate": row["daily_rate"], "locked": bool(row["locked"]),
                "snapshot": json.loads(row["locked_snapshot_json"]) if row["locked_snapshot_json"] else None,
                "reopened_by": row["reopened_by"], "reopened_on": row["reopened_on"],
            }

        # No row yet for this month — default the rate to whatever the
        # most recent earlier month used, or the workbook's original
        # default if nobody has ever used the report before.
        prev = conn.execute(
            "SELECT daily_rate FROM pp_report_months WHERE month < ? ORDER BY month DESC LIMIT 1",
            (month,),
        ).fetchone()
        rate = prev["daily_rate"] if prev else FIRST_MONTH_DAILY_RATE_DEFAULT
        return {"daily_rate": rate, "locked": False, "snapshot": None, "reopened_by": None, "reopened_on": None}


def is_locked(month: str) -> bool:
    return get_month_meta(month)["locked"]


def any_month_locked_after(month: str) -> bool:
    """True if some chronologically later month is currently locked. Used
    to enforce that months lock/unlock strictly in order — otherwise a
    later month's frozen 'Previous Accumulated' could end up based on
    figures this month later changes underneath it."""
    with db.get_db() as conn:
        row = conn.execute(
            "SELECT 1 FROM pp_report_months WHERE locked = 1 AND month > ? LIMIT 1",
            (month,),
        ).fetchone()
        return row is not None


def check_lock_order(month: str) -> str | None:
    """Returns an error message if `month` may not be locked right now
    (out of order), else None. Locking must proceed strictly in
    chronological order — every earlier month locked first (except
    OPENING_BALANCE_MONTH, which has no true 'previous' month to require) —
    and never while a later month is already locked, since that later
    month's Previous Accumulated may have already been frozen from this
    month's pre-edit figures."""
    if month != OPENING_BALANCE_MONTH:
        prev_month = previous_month_str(month)
        if not is_locked(prev_month):
            return f"Lock {format_month_label(prev_month)} first — months must be locked in order so Previous Accumulated stays accurate."
    if any_month_locked_after(month):
        return "A later month is already locked — reopen it first before (re-)locking this one, to keep months in order."
    return None


def check_unlock_order(month: str) -> str | None:
    """Returns an error message if `month` may not be reopened right now,
    else None. Months must be reopened in reverse chronological order —
    the most recently locked month first — for the same reason locking
    must go forward in order."""
    if any_month_locked_after(month):
        return "Reopen the most recently locked month first — months must be reopened in reverse order."
    return None


def check_delete_order(month: str) -> str | None:
    """Returns an error message if this month's data may not be deleted
    right now, else None. Same rule as reopening (deleting is a strict
    superset of reopening, plus wiping the record outright) — refuses
    while any later month is still locked, since that later month's
    Previous Accumulated etc. may depend on this month's current, about-
    to-be-erased state."""
    if any_month_locked_after(month):
        return "A later month is already locked — reopen it first before deleting this month's data."
    return None


def list_report_months():
    """Every month with any PP Report state at all — locked at some point
    (pp_report_months) or just a saved draft (pp_report_overrides) even if
    never locked — for the admin 'PP Report Data' cleanup screen. Ordered
    newest first, since that's usually the more interesting end."""
    with db.get_db() as conn:
        rows = conn.execute(
            "SELECT month FROM pp_report_months UNION SELECT month FROM pp_report_overrides ORDER BY month DESC"
        ).fetchall()
        result = []
        for row in rows:
            month = row["month"]
            meta = get_month_meta(month)
            draft_count = conn.execute(
                "SELECT COUNT(*) AS c FROM pp_report_overrides WHERE month = ?", (month,)
            ).fetchone()["c"]
            result.append({
                "month": month,
                "label": format_month_label(month),
                "locked": meta["locked"],
                "daily_rate": meta["daily_rate"],
                "reopened_by": meta["reopened_by"],
                "reopened_on": meta["reopened_on"],
                "draft_row_count": draft_count,
            })
        return result


def delete_month_data(month: str):
    """Wipes a month's PP Report state outright — its locked snapshot (if
    any) and every draft override — so the next visit to that month starts
    completely fresh, as if it had never been touched. Callers must check
    check_delete_order() first."""
    with db.get_db() as conn:
        conn.execute("DELETE FROM pp_report_months WHERE month = ?", (month,))
        conn.execute("DELETE FROM pp_report_overrides WHERE month = ?", (month,))
        conn.execute("DELETE FROM ksa_import WHERE month = ?", (month,))


# ---------------------------------------------------------------------------
# KSA — read from the monthly PP Report workbook (not tracked in Redmine)
# ---------------------------------------------------------------------------

KSA_COUNTRY = "KSA"
# Every KSA project is managed by the same person (they are not in Redmine, so
# there is no per-project manager to read) — shown in the PP Report's PM column
# and offered as a Project Manager filter option on the Dashboard's Time Analysis.
KSA_MANAGER_NAME = "Al Saadi, Ahmad"
KSA_MANAGER_KEY = "ksa"


def parse_ksa_workbook(file_bytes: bytes, month: str) -> list:
    """Reads the Saudi Arabia rows out of the monthly PP Report workbook's
    month tab (the invoicing-style export with 'SAP Company', '*Earnings',
    the prior-month column and 'Diff' — see the 'Aug'/'July' tabs).
    Mapping: *Earnings -> Accumulated, the previous month's tab '*Earnings'
    (else the column right after it, named after the previous month) ->
    Previous Accumulated, Accumulated - Previous -> Amount to Take, Initial
    Revenue -> Total Revenue. The tab is picked by the
    target month's abbreviation ('2026-08' -> 'Aug'), falling back to the
    only matching tab. Raises ValueError with a user-facing message."""
    import io

    import openpyxl

    year, mon = (int(x) for x in month.split("-"))
    wanted = calendar.month_abbr[mon].lower()

    wb = openpyxl.load_workbook(io.BytesIO(file_bytes), data_only=True, read_only=True)
    candidates = []
    for ws in wb.worksheets:
        header = next(ws.iter_rows(min_row=1, max_row=1, values_only=True), None)
        if not header:
            continue
        names = [str(x).strip() if x is not None else "" for x in header]
        if "SAP Company" in names and "*Earnings" in names and "Diff" in names:
            candidates.append((ws, names))
    if not candidates:
        raise ValueError("No sheet with 'SAP Company', '*Earnings' and 'Diff' columns found in this workbook.")

    chosen = next(((ws, n) for ws, n in candidates if ws.title.strip().lower().startswith(wanted)), None)
    if chosen is None:
        if len(candidates) == 1:
            chosen = candidates[0]
        else:
            raise ValueError(
                f"Couldn't tell which tab is {format_month_label(month)} — found: "
                + ", ".join(ws.title for ws, _ in candidates) + f". Name the tab '{calendar.month_abbr[mon]}'."
            )
    ws, names = chosen
    idx = {n: i for i, n in enumerate(names) if n}

    # Previous Accumulated comes from the PREVIOUS month's tab ('*Earnings'
    # there) when the workbook has it — the month tab's own previous-month
    # column and 'Diff' are lookups that break for some projects (e.g. a
    # project whose earlier earnings show as 0 there), which overstates the
    # month's earning. Falls back to that column if there's no previous tab.
    prev_abbr = calendar.month_abbr[int(previous_month_str(month).split("-")[1])].lower()
    prev_tab = next(((pws, pn) for pws, pn in candidates if pws is not ws and pws.title.strip().lower().startswith(prev_abbr)), None)
    prev_earnings = None
    prev_amounts = {}  # last month's own Amount to Take = its *Earnings less the column after it
    if prev_tab is not None:
        pws, pn = prev_tab
        pidx = {n: i for i, n in enumerate(pn) if n}
        if "Project ID" in pidx:
            prev_earnings = {}
            for prow in pws.iter_rows(min_row=2, values_only=True):
                if str(prow[pidx["SAP Company"]] or "").strip().lower() not in ("saudi arabia", "ksa"):
                    continue
                try:
                    pid_ = int(float(prow[pidx["Project ID"]]))
                except (TypeError, ValueError):
                    continue
                earn_ = to_float(prow[pidx["*Earnings"]])
                prev_earnings[pid_] = earn_
                if pidx["*Earnings"] + 1 < len(prow):
                    prev_amounts[pid_] = earn_ - to_float(prow[pidx["*Earnings"] + 1])

    def col(*options):
        for o in options:
            if o in idx:
                return idx[o]
        return None

    c_id, c_company = idx.get("Project ID"), idx["SAP Company"]
    c_earn, c_diff = idx["*Earnings"], idx["Diff"]
    c_prev = c_earn + 1  # the previous month's earnings column, headed by that month's name
    c_code, c_name = col("Project Code"), col("Project Name")
    c_status, c_author = col("Project Status"), col("Project Author")
    c_spent, c_est = col("Project Spent Time", "Logged Spent Time"), col("*Est Sold Time", "Sold Time", "PM Est Time")
    c_rev, c_prog = col("Initial Revenue", "Revenue"), col("*Project Progress %")
    if c_id is None:
        raise ValueError("The month tab has no 'Project ID' column.")

    def cell(row, i):
        return row[i] if i is not None and i < len(row) else None

    result = []
    for row in ws.iter_rows(min_row=2, values_only=True):
        company = str(cell(row, c_company) or "").strip().lower()
        if company not in ("saudi arabia", "ksa"):
            continue
        try:
            excel_id = int(float(cell(row, c_id)))
        except (TypeError, ValueError):
            continue
        status = str(cell(row, c_status) or "").strip()
        progress = cell(row, c_prog)
        accumulated = to_float(cell(row, c_earn))
        previous = prev_earnings.get(excel_id, 0.0) if prev_earnings is not None else to_float(cell(row, c_prev))
        result.append({
            "excel_project_id": excel_id,
            "project_code": str(cell(row, c_code) or "").strip(),
            "name": str(cell(row, c_name) or "").strip(),
            "status": "Active" if status.lower() == "open" else status,
            "author": str(cell(row, c_author) or "").strip(),
            "est_hours": to_float(cell(row, c_est)),
            "spent_hours": to_float(cell(row, c_spent)),
            "total_revenue": to_float(cell(row, c_rev)),
            "accumulated": accumulated,
            "previous_accumulated": previous,
            "amount_to_take": accumulated - previous,
            "previous_amount_to_take": prev_amounts.get(excel_id, 0.0) if prev_earnings is not None else None,
            "progress": (to_float(progress) / 100.0) if progress not in (None, "") else None,
        })
    if not result:
        raise ValueError("The month tab has no Saudi Arabia rows.")
    return result


def save_ksa_rows(month: str, rows: list):
    """Replaces every stored KSA row for `month` with `rows` (a fresh upload
    always supersedes the previous one for that month)."""
    with db.get_db() as conn:
        conn.execute("DELETE FROM ksa_import WHERE month = ?", (month,))
        for r in rows:
            conn.execute(
                """
                INSERT INTO ksa_import (month, excel_project_id, project_code, name, status, author, est_hours,
                    spent_hours, total_revenue, accumulated, previous_accumulated, amount_to_take, progress, previous_amount_to_take)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (month, r["excel_project_id"], r["project_code"], r["name"], r["status"], r["author"], r["est_hours"],
                 r["spent_hours"], r["total_revenue"], r["accumulated"], r["previous_accumulated"], r["amount_to_take"], r["progress"], r.get("previous_amount_to_take")),
            )


def delete_ksa_rows(month: str):
    with db.get_db() as conn:
        conn.execute("DELETE FROM ksa_import WHERE month = ?", (month,))


def get_ksa_rows(month: str) -> list:
    with db.get_db() as conn:
        return [dict(r) for r in conn.execute(
            "SELECT * FROM ksa_import WHERE month = ? ORDER BY excel_project_id", (month,)
        ).fetchall()]


def list_ksa_months() -> list:
    with db.get_db() as conn:
        rows = conn.execute(
            "SELECT month, COUNT(*) AS c, MAX(updated_on) AS updated_on, SUM(amount_to_take) AS total "
            "FROM ksa_import GROUP BY month ORDER BY month DESC"
        ).fetchall()
        return [
            {"month": r["month"], "label": format_month_label(r["month"]), "count": r["c"],
             "updated_on": r["updated_on"], "amount_to_take": r["total"] or 0.0,
             "locked": is_locked(r["month"])}
            for r in rows
        ]


# --- KSA timesheet (Intra export) ------------------------------------------

def parse_ksa_timesheet(file_bytes: bytes, month: str) -> list:
    """Reads KSA time entries out of an uploaded timesheet workbook. Two
    layouts are accepted:

    * The current "Spent time" export — header row (which may sit a few rows
      below a title) with 'Rapports code (PEP)', 'Date', 'Project', 'Spent
      time', 'User', 'Comment'. The Rapports code is the project code and
      'Project' is its name.
    * The older "Intra" export — 'Date', 'User', 'Project' (which there *is*
      the code), 'HoursBase100'/'Hours', and optional 'UserDivision'/'Division'.

    Rows are KSA when a Division column says 'KSA' — so a KSA-only file works
    too (no Division column → every row kept). A trailing 'Total:' row (no
    date) is ignored. Only rows dated inside `month` are kept. Raises
    ValueError with a user-facing message."""
    import datetime
    import io

    import openpyxl

    HOURS_COLS = ("Spent time", "HoursBase100", "Hours")
    CODE_COLS = ("Rapports code (PEP)", "Rapports code (PEP", "Rapports code")

    def header_index(cand_names):
        nm = {n: i for i, n in enumerate(cand_names) if n}
        has_code = any(c in nm for c in CODE_COLS) or "Project" in nm
        if "Date" in nm and "User" in nm and has_code and any(h in nm for h in HOURS_COLS):
            return nm
        return None

    wb = openpyxl.load_workbook(io.BytesIO(file_bytes), data_only=True, read_only=True)
    ws, idx, data_rows = None, None, None
    for cand in wb.worksheets:
        rows = list(cand.iter_rows(values_only=True))
        # The header isn't always row 1 (the "Spent time" export has a title
        # and blank rows above it) — scan the first several rows for it.
        for ri, row in enumerate(rows[:15]):
            cand_names = [str(x).strip() if x is not None else "" for x in row]
            nm = header_index(cand_names)
            if nm is not None:
                ws, idx, data_rows = cand, nm, rows[ri + 1:]
                break
        if ws is not None:
            break
    if ws is None:
        raise ValueError(
            "Couldn't find the timesheet header — expected a row with 'Date', "
            "'User', a project/Rapports code column and a 'Spent time' (or "
            "'Hours') column. Is this the Spent time / Intra export?")

    c_date, c_user = idx["Date"], idx["User"]
    c_hours = next(idx[h] for h in HOURS_COLS if h in idx)
    c_code = next((idx[c] for c in CODE_COLS if c in idx), None)
    if c_code is not None:
        c_name = idx.get("Project")          # new layout: Project is the name
    else:
        c_code, c_name = idx.get("Project"), None  # old layout: Project is the code
    c_desc = idx.get("Comment", idx.get("Description"))
    c_div = [idx[k] for k in ("UserDivision", "Division") if k in idx]

    def cell(row, i):
        return row[i] if i is not None and i < len(row) else None

    def to_date(v):
        if isinstance(v, datetime.datetime):
            return v.date()
        if isinstance(v, datetime.date):
            return v
        text = str(v or "").strip()
        for fmt in ("%d/%m/%Y", "%Y-%m-%d", "%d-%m-%Y"):
            try:
                return datetime.datetime.strptime(text[:10], fmt).date()
            except ValueError:
                continue
        return None

    def to_hours(v):
        if isinstance(v, (int, float)):
            return float(v)
        text = str(v or "").strip().replace(",", ".")
        if ":" in text:
            h, _, m = text.partition(":")
            try:
                return int(h) + int(m) / 60.0
            except ValueError:
                return 0.0
        return to_float(text)

    all_rows = [r for r in data_rows if cell(r, c_date) and cell(r, c_user)]

    def is_ksa(r):
        return any(str(cell(r, c) or "").strip().lower() == "ksa" for c in c_div)

    if any(is_ksa(r) for r in all_rows):
        all_rows = [r for r in all_rows if is_ksa(r)]

    result = []
    for r in all_rows:
        d = to_date(cell(r, c_date))
        if d is None or d.strftime("%Y-%m") != month:
            continue
        hours = to_hours(cell(r, c_hours))
        if hours <= 0:
            continue
        result.append({
            "spent_on": d.isoformat(),
            "user_name": " ".join(str(cell(r, c_user)).split()),
            "project_code": str(cell(r, c_code) or "").strip(),
            "project_name": str(cell(r, c_name) or "").strip() if c_name is not None else "",
            "description": str(cell(r, c_desc) or "").strip(),
            "hours": hours,
        })
    if not result:
        raise ValueError(f"No KSA time entries dated in {format_month_label(month)} were found in this file.")
    return result


def save_ksa_timesheet(month: str, rows: list):
    """Replaces the stored KSA time entries for `month`."""
    with db.get_db() as conn:
        conn.execute("DELETE FROM ksa_timesheet WHERE month = ?", (month,))
        conn.executemany(
            "INSERT INTO ksa_timesheet (month, spent_on, user_name, project_code, project_name, description, hours) VALUES (?, ?, ?, ?, ?, ?, ?)",
            [(month, r["spent_on"], r["user_name"], r["project_code"], r.get("project_name", ""), r["description"], r["hours"]) for r in rows],
        )


def delete_ksa_timesheet(month: str):
    with db.get_db() as conn:
        conn.execute("DELETE FROM ksa_timesheet WHERE month = ?", (month,))


def list_ksa_timesheet_months() -> list:
    with db.get_db() as conn:
        rows = conn.execute(
            "SELECT month, COUNT(*) AS c, COUNT(DISTINCT user_name) AS people, SUM(hours) AS hours FROM ksa_timesheet GROUP BY month ORDER BY month DESC"
        ).fetchall()
        return [{"month": r["month"], "label": format_month_label(r["month"]), "entries": r["c"], "people": r["people"], "hours": r["hours"] or 0.0} for r in rows]


def _name_tokens(text: str) -> set:
    import re
    return {t.lower() for t in re.sub(r"[^A-Za-z ]", " ", text or "").split() if t}


def _resolve_ksa_user_ids(names, roster: list) -> dict:
    """timesheet name -> (user_id, display name). A KSA consultant who also
    has a Redmine account (found by name against `roster`, tolerating word
    order and small spelling differences) keeps that Redmine id, so their
    KSA hours add to their existing utilization and hourly rate. Anyone
    else gets a stable app-only negative id (ksa_person), reused if the
    timesheet later spells their name a little differently."""
    import difflib

    roster_tokens = [(u, _name_tokens(u["name"].replace(",", " "))) for u in roster if u["id"] > 0]
    resolved = {}
    with db.get_db() as conn:
        known = {r["name"]: r["user_id"] for r in conn.execute("SELECT name, user_id FROM ksa_person").fetchall()}
        for name in names:
            toks = _name_tokens(name)
            match = next((u for u, rt in roster_tokens if rt and rt <= toks), None)
            if match is None:
                key = " ".join(sorted(toks))
                scored = [(difflib.SequenceMatcher(None, key, " ".join(sorted(rt))).ratio(), u) for u, rt in roster_tokens if rt]
                if scored:
                    best = max(scored, key=lambda x: x[0])
                    if best[0] >= 0.88:
                        match = best[1]
            if match is not None:
                resolved[name] = (match["id"], match["name"])
                continue
            if name in known:
                resolved[name] = (known[name], name)
                continue
            key = " ".join(sorted(toks))
            similar = next((uid for other, uid in known.items() if difflib.SequenceMatcher(None, key, " ".join(sorted(_name_tokens(other)))).ratio() >= 0.85), None)
            uid = similar if similar is not None else (min(list(known.values()) + [-100000]) - 1)
            conn.execute("INSERT OR IGNORE INTO ksa_person (name, user_id) VALUES (?, ?)", (name, uid))
            known[name] = uid
            resolved[name] = (uid, name)
    return resolved


def ksa_timesheet_entries(roster: list) -> list:
    """Every stored KSA time entry, shaped like a cached Redmine time entry
    (so Utilization / Resource Planning / cost-by-consultant read them
    unchanged). Only entries whose project code is a KSA project from the
    uploaded workbook become project time (project_id = -excel id); leave,
    support, sales and other-country codes are not project hours and are
    left out."""
    with db.get_db() as conn:
        rows = [dict(r) for r in conn.execute("SELECT * FROM ksa_timesheet ORDER BY spent_on").fetchall()]
        if not rows:
            return []
        codes = {}
        for r in conn.execute("SELECT project_code, excel_project_id, name FROM ksa_import ORDER BY month ASC").fetchall():
            if r["project_code"]:
                codes[r["project_code"]] = (r["excel_project_id"], r["name"])
    people = _resolve_ksa_user_ids(sorted({r["user_name"] for r in rows}), roster)
    entries = []
    for r in rows:
        project = codes.get(r["project_code"])
        if project is None:
            continue
        user_id, user_name = people[r["user_name"]]
        entries.append({
            "id": None, "project_id": -project[0], "project_name": project[1], "issue_id": None,
            "user_id": user_id, "user_name": user_name, "activity_name": None,
            "hours": r["hours"], "spent_on": r["spent_on"], "comments": r["description"],
        })
    return entries


def ksa_synthetic_project_id(code: str) -> int:
    """Stable synthetic project id for a KSA timesheet code that doesn't match
    an uploaded KSA project (crc32 is deterministic across runs, unlike hash());
    far below real -excel ids so it can't collide with a matched KSA project."""
    import zlib
    return -(9000000 + (zlib.crc32((code or "").encode("utf-8")) % 1000000))


def ksa_timesheet_entries_all(roster: list) -> list:
    """Like ksa_timesheet_entries, but includes EVERY stored KSA time entry so
    the Time Analysis tab can show all logged hours. Rows whose code matches an
    uploaded KSA project become that project (project_id = -excel id); unmatched
    codes (leave, support, sales, …) become a synthetic project named after the
    code so they're still visible and groupable. Each entry carries its
    project_code so callers can apply the admin's "Excluded KSA codes" list as a
    show/hide toggle rather than dropping the hours here."""
    with db.get_db() as conn:
        rows = [dict(r) for r in conn.execute("SELECT * FROM ksa_timesheet ORDER BY spent_on").fetchall()]
        if not rows:
            return []
        codes = {}
        for r in conn.execute("SELECT project_code, excel_project_id, name FROM ksa_import ORDER BY month ASC").fetchall():
            if r["project_code"]:
                codes[r["project_code"]] = (r["excel_project_id"], r["name"])
    people = _resolve_ksa_user_ids(sorted({r["user_name"] for r in rows}), roster)
    entries = []
    for r in rows:
        code = (r["project_code"] or "").strip()
        matched = codes.get(code)
        if matched is not None:
            project_id, project_name = -matched[0], matched[1]
        else:
            # Unmatched code (leave/support/other): use the real project name
            # from the timesheet when it has one, else fall back to the code.
            project_id = ksa_synthetic_project_id(code)
            project_name = (r.get("project_name") or "").strip() or code or "(no code)"
        user_id, user_name = people[r["user_name"]]
        entries.append({
            "id": None, "project_id": project_id, "project_name": project_name, "issue_id": None,
            "user_id": user_id, "user_name": user_name, "activity_name": None,
            "hours": r["hours"], "spent_on": r["spent_on"], "comments": r["description"],
            "project_code": code,
        })
    return entries


def ksa_timesheet_skipped(month: str) -> list:
    """(project_code, hours) for this month's stored entries that are NOT
    KSA project time (leave, support, sales, other countries' projects)."""
    with db.get_db() as conn:
        known = {r["project_code"] for r in conn.execute("SELECT project_code FROM ksa_import").fetchall() if r["project_code"]}
        rows = conn.execute("SELECT project_code, SUM(hours) AS h FROM ksa_timesheet WHERE month = ? GROUP BY project_code ORDER BY h DESC", (month,)).fetchall()
    return [(r["project_code"], r["h"]) for r in rows if r["project_code"] not in known]


def ksa_projects() -> list:
    """The KSA projects as Redmine-shaped project dicts (negative ids) —
    from the most recent upload of each — so Resource Planning / Utilization
    can list them next to the Redmine projects."""
    with db.get_db() as conn:
        rows = conn.execute("SELECT * FROM ksa_import ORDER BY month ASC").fetchall()
    latest = {}
    for r in rows:
        latest[r["excel_project_id"]] = r
    return [
        {"id": -r["excel_project_id"], "name": r["name"], "country": KSA_COUNTRY, "rapportCode": r["project_code"],
         "est": r["est_hours"], "spent": r["spent_hours"], "isTM": False, "status": r["status"],
         "managerId": 0, "managerName": KSA_MANAGER_NAME, "risk": "", "orderAmount": r["total_revenue"]}
        for r in latest.values()
    ]


def with_ksa(projects: list, timespent: list):
    """(projects + KSA projects, timespent + KSA time entries) — the view
    Resource Planning and the Utilization Report work from."""
    roster = settings_store.distinct_timesheet_users(timespent)
    return list(projects) + ksa_projects(), list(timespent or []) + ksa_timesheet_entries(roster)


def roster_with_ksa(timespent: list) -> list:
    """Redmine consultants plus KSA-only ones (app-only negative ids), for
    the Employees rate screen and Resource Planning's assignee list."""
    base = settings_store.distinct_timesheet_users(timespent)
    return settings_store.distinct_timesheet_users(list(timespent or []) + ksa_timesheet_entries(base))


def refresh_ksa_in_locked_snapshot(month: str):
    """For a LOCKED month, rewrites only the KSA rows (excelSource) inside
    the frozen snapshot from the current ksa_import / ksa_timesheet data,
    leaving every other row exactly as it was locked — so KSA can be loaded
    or corrected without reopening the month (which would recompute the
    other countries from today's Redmine data). Returns the number of KSA
    rows now in the snapshot, or None if the month isn't locked."""
    meta = get_month_meta(month)
    if not meta["locked"] or meta["snapshot"] is None:
        return None
    rows = [r for r in meta["snapshot"] if not r.get("excelSource")]
    timespent, _ = settings_store.load_cache("timespent")
    roster = settings_store.distinct_timesheet_users(timespent)
    ksa_rate = {c["name"]: c["daily_rate"] for c in settings_store.list_countries()}.get(KSA_COUNTRY, meta["daily_rate"])
    ksa_hours = hours_by_project_and_consultant(ksa_timesheet_entries(roster), month)
    ksa_rows = ksa_report_rows(month, ksa_rate, ksa_hours, settings_store.get_employee_rates())
    with db.get_db() as conn:
        conn.execute("UPDATE pp_report_months SET locked_snapshot_json = ? WHERE month = ?", (json.dumps(rows + ksa_rows), month))
    return len(ksa_rows)


def _ksa_timesheet_names() -> dict:
    """project_code -> project name, from the stored KSA timesheets (the
    "Spent time" export carries names; the older Intra format doesn't). Used
    only as a fallback name source. Most recent non-empty name per code wins."""
    names = {}
    with db.get_db() as conn:
        for r in conn.execute(
            "SELECT project_code, project_name FROM ksa_timesheet "
            "WHERE project_name != '' ORDER BY month ASC"
        ).fetchall():
            code = (r["project_code"] or "").strip()
            if code:
                names[code] = r["project_name"]
    return names


def ksa_report_rows(month: str, rate: float, ksa_hours: dict = None, employee_rates: dict = None) -> list:
    """The stored KSA rows for `month`, shaped like build_report()'s own
    rows so the PP Report table, totals, Country summary and exports pick
    them up unchanged. Ids are negative (-excel id) so they can never
    collide with a Redmine project id; 'displayId' is what the UI shows.
    Everything here is read-only ('excelSource'): the figures come straight
    from the workbook, not from Estimated/Spent formulas. When KSA timesheets
    have been uploaded (`ksa_hours`: project -> {consultant: hours} through
    this month), Cost till Date is priced per consultant exactly like the
    Redmine rows (see consultant_cost_till_date) — note it only covers the
    months whose timesheets were uploaded — and the profit columns follow;
    otherwise those stay 0. Sales Sold/Expected Profit stay 0 unless a KSA
    Daily Rate is configured (Countries screen)."""
    rows = []
    # Last month's Amount to Take (Summary "Earning last month", trend arrow):
    # the previous month's own uploaded KSA figure — so a month's "this
    # month" and the next month's "last month" always agree. Deliberately
    # blank when that month hasn't been loaded: what the current workbook's
    # previous-month tab implies is unreliable (its own previous-month
    # column has broken lookups — e.g. it gave 102,738 for a June that was
    # really 36,708.68), and a wrong figure is worse than none. Load the
    # earlier month (tools/load_ksa.py --month YYYY-MM --workbook ...) to fill it.
    prior_rows = get_ksa_rows(previous_month_str(month))
    prev_import = {r["excel_project_id"]: r["amount_to_take"] for r in prior_rows}
    month_rows = get_ksa_rows(month)
    # The "Spent time" timesheet carries each KSA project's real name, keyed by
    # code — used only to fill in a workbook row that has no name of its own, so
    # existing (and locked-month) rows are never renamed.
    ts_names = _ksa_timesheet_names()
    carried_over = False
    if not month_rows and prior_rows:
        # This month's KSA workbook hasn't been imported yet: carry last
        # month's projects forward with nothing earned so far (Amount to
        # Take 0, Accumulated unchanged), so the report and Summary still
        # list KSA — this month 0, last month the previous figure — until
        # the real month is imported and replaces these lines.
        carried_over = True
        month_rows = [dict(r, previous_accumulated=r["accumulated"], amount_to_take=0.0) for r in prior_rows]
    for r in month_rows:
        k, acc = r["total_revenue"], r["accumulated"]
        last_month = prev_import.get(r["excel_project_id"], 0.0) if prev_import else None
        fully_earned = k > 0 and (k - acc) < 1
        computed_completion = r["progress"] if r["progress"] is not None else (1.0 if fully_earned else _safe_div(acc, k))
        profitability = {
            "salesSoldProfit": 0.0, "salesExpectedProfit": 0.0, "projectTimeProfitability": 0.0,
            "costTillDate": 0.0, "profitTillDate": 0.0, "amountProfitability": 0.0, "expectedProfitability": 0.0,
        }
        if ksa_hours:
            cost = consultant_cost_till_date(-r["excel_project_id"], ksa_hours, employee_rates or {}, rate / 8)
            full = _profitability_fields(r["est_hours"], r["spent_hours"], k, acc, fully_earned, rate,
                                         completion_override=computed_completion, cost_till_date=cost)
            profitability = {key: full[key] for key in profitability}
            if rate <= 0:
                profitability["salesSoldProfit"] = 0.0
                profitability["salesExpectedProfit"] = 0.0
        rows.append({
            "projectId": -r["excel_project_id"],
            "displayId": f"KSA-{r['excel_project_id']}",
            "excelSource": True,
            "carriedOver": carried_over,
            "name": r["name"] or ts_names.get((r["project_code"] or "").strip(), ""),
            "country": KSA_COUNTRY,
            "managerId": 0,
            "managerName": KSA_MANAGER_NAME,
            "rapportCode": r["project_code"],
            "status": r["status"],
            "estimated": r["est_hours"],
            "spent": r["spent_hours"],
            "risk": None,
            "notes": "",
            "dailyRate": rate,
            "previousAmountToBeTaken": last_month,
            "previousEstimated": None,
            "previousSpent": None,
            "previousTotalRevenue": None,
            "previousCompletion": None,
            "fromOpeningBalance": False,
            "remaining": r["est_hours"] - r["spent_hours"],
            "totalRevenue": k,
            "previousAccumulated": r["previous_accumulated"],
            "accumulated": acc,
            "amountToBeTaken": r["amount_to_take"],
            "fullyEarned": fully_earned,
            "yetToEarn": k - acc,
            "remainingPct": _safe_div(k - acc, k),
            **profitability,
            "completion": computed_completion,
            "computedCompletion": computed_completion,
            "completionOverridden": False,
        })
    return rows


def set_daily_rate(month: str, daily_rate: float):
    with db.get_db() as conn:
        conn.execute(
            """
            INSERT INTO pp_report_months (month, daily_rate, updated_on)
            VALUES (?, ?, datetime('now'))
            ON CONFLICT(month) DO UPDATE SET daily_rate = excluded.daily_rate, updated_on = datetime('now')
            """,
            (month, daily_rate),
        )


def _get_previous_snapshot_field_map(month: str, field: str):
    """project_id -> that field's frozen value from the prior month's
    locked snapshot, or {} if that month was never locked (or doesn't
    exist yet)."""
    prev_month = previous_month_str(month)
    meta = get_month_meta(prev_month)
    if not meta["locked"] or not meta["snapshot"]:
        return {}
    return {row["projectId"]: row.get(field) for row in meta["snapshot"]}


def get_previous_accumulated_map(month: str):
    """project_id -> frozen 'accumulated' figure from the prior month's
    locked snapshot, or {} if that month was never locked (or doesn't
    exist yet) — in which case every project's previous_accumulated is 0,
    same as a brand new workbook column with nothing carried forward."""
    return _get_previous_snapshot_field_map(month, "accumulated")


def get_previous_amount_to_take_map(month: str):
    """project_id -> frozen 'amountToBeTaken' figure from the prior
    month's locked snapshot, or {} if that month was never locked — used
    only to show the up/down trend indicator, never in any calculation."""
    return _get_previous_snapshot_field_map(month, "amountToBeTaken")


def get_previous_estimated_map(month: str):
    """project_id -> frozen 'estimated' figure from the prior month's
    locked snapshot — used only to flag when Estimated changed month to
    month (unusual for a project's scope to shift), never in any
    calculation."""
    return _get_previous_snapshot_field_map(month, "estimated")


def get_previous_spent_map(month: str):
    """project_id -> frozen 'spent' figure from the prior month's locked
    snapshot — used only to flag a month-over-month decrease in logged
    hours (unusual — spent should only grow), never in any calculation."""
    return _get_previous_snapshot_field_map(month, "spent")


def get_previous_total_revenue_map(month: str):
    """project_id -> frozen 'totalRevenue' figure from the prior month's
    locked snapshot — used only to flag when Total Revenue changed month
    to month, never in any calculation."""
    return _get_previous_snapshot_field_map(month, "totalRevenue")


def get_overrides(month: str):
    with db.get_db() as conn:
        rows = conn.execute(
            "SELECT project_id, total_revenue, risk, notes, completion_override FROM pp_report_overrides WHERE month = ?",
            (month,),
        ).fetchall()
        return {
            r["project_id"]: {
                "total_revenue": r["total_revenue"], "risk": r["risk"], "notes": r["notes"],
                "completion_override": r["completion_override"],
            }
            for r in rows
        }


def save_override(month: str, project_id: int, total_revenue, risk, notes: str, completion_override=None):
    with db.get_db() as conn:
        conn.execute(
            """
            INSERT INTO pp_report_overrides (month, project_id, total_revenue, risk, notes, completion_override, updated_on)
            VALUES (?, ?, ?, ?, ?, ?, datetime('now'))
            ON CONFLICT(month, project_id) DO UPDATE SET
                total_revenue = excluded.total_revenue, risk = excluded.risk, notes = excluded.notes,
                completion_override = excluded.completion_override, updated_on = datetime('now')
            """,
            (month, project_id, total_revenue, risk, notes, completion_override),
        )


def clear_overrides(month: str):
    """Deletes this month's draft overrides outright. NOT called
    automatically by lock_month() (its drafts are deliberately left in
    place — see that function) — this is here for an explicit "discard my
    draft" action, if one is ever added."""
    with db.get_db() as conn:
        conn.execute("DELETE FROM pp_report_overrides WHERE month = ?", (month,))


def lock_month(month: str, snapshot_rows: list):
    """Deliberately does NOT clear this month's draft overrides (unlike
    older versions of this function) — while locked, the frozen snapshot is
    all that's shown regardless (build_report's locked branch never reads
    overrides), so the dormant draft is harmless. Keeping it means that if
    this month is ever reopened later, Total Revenue/Risk/Notes/Project
    Completion come back exactly as they were without retyping anything,
    while every COMPUTED figure (Accumulated, Amount to Take, etc.) is
    still freshly recalculated from whatever's currently cached from
    Redmine at that point — never the stale frozen numbers."""
    with db.get_db() as conn:
        conn.execute(
            """
            INSERT INTO pp_report_months (month, locked, locked_snapshot_json, reopened_by, reopened_on, updated_on)
            VALUES (?, 1, ?, NULL, NULL, datetime('now'))
            ON CONFLICT(month) DO UPDATE SET
                locked = 1, locked_snapshot_json = excluded.locked_snapshot_json,
                reopened_by = NULL, reopened_on = NULL, updated_on = datetime('now')
            """,
            (month, json.dumps(snapshot_rows)),
        )


def unlock_month(month: str, username: str = ""):
    """`username` (who reopened it) and the current time are recorded so
    the NEXT month's page can warn that its Prev. Accumulated (and other
    previous-month-derived figures) are provisional until this month is
    relocked — see build_report's previous_*_map lookups, which read
    straight from whatever's currently locked/unlocked, live, every time."""
    with db.get_db() as conn:
        conn.execute(
            "UPDATE pp_report_months SET locked = 0, reopened_by = ?, reopened_on = ?, updated_on = datetime('now') WHERE month = ?",
            (username, datetime.now(timezone.utc).isoformat(), month),
        )


def default_total_revenue(project: dict) -> float:
    """Total Project Revenue starts out equal to the project's SAP Order
    amount — the same figure shown as 'Order amount' on the Dashboard
    (already computed there for T&M vs fixed-scope projects in
    redmine_client.build_report). Still overridable per project per month
    (see pp_report_overrides) for cases like a revenue figure that's
    actually the sum of several SAP orders."""
    return to_float(project.get("orderAmount"), default=0.0)


def _opening_balance_csv_path() -> str:
    override = os.environ.get("ERM_OPENING_BALANCE_CSV_PATH")
    if override:
        return override
    # Site root — one level up from this app/ package, so the file sits
    # right next to README.md/run.ps1 for easy hand-editing.
    return os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "opening_balance.csv")


def load_opening_balance() -> dict:
    """Reads opening_balance.csv (site root — see _opening_balance_csv_path)
    if present, keyed by project id. Used only for OPENING_BALANCE_MONTH,
    since that's the one month this app has no Redmine-tracked history
    for — its Risk/Total Revenue/Accumulated/Amount to Take are hand-typed
    into the CSV rather than computed. Missing file or a row with a
    non-numeric id is silently skipped rather than failing the report."""
    path = _opening_balance_csv_path()
    if not os.path.isfile(path):
        return {}

    result = {}
    with open(path, newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        field_map = {name: name.strip().lower().replace(" ", "_") for name in (reader.fieldnames or [])}
        for raw_row in reader:
            row = {field_map[k]: v for k, v in raw_row.items() if k in field_map}
            try:
                project_id = int(float(row.get("id")))
            except (TypeError, ValueError):
                continue
            result[project_id] = {
                "risk": to_float(row.get("risk"), default=0.0),
                "total_revenue": to_float(row.get("total_revenue"), default=0.0),
                "prev_accumulated": to_float(row.get("prev_accumulated"), default=0.0),
                "accumulated": to_float(row.get("accumulated"), default=0.0),
                "amount_to_take": to_float(row.get("amount_to_take"), default=0.0),
            }
    return result


def build_report(month: str, projects: list, timespent: list = None):
    """Assemble the full report for a month: either the frozen shared
    snapshot (if locked) or a live computation over the given cached
    project list (settings_store.load_cache('projects')) and cached raw
    time entries (settings_store.load_cache('timespent'))."""
    meta = get_month_meta(month)
    if meta["locked"] and meta["snapshot"] is not None:
        rows = meta["snapshot"]
        for r in rows:
            r.setdefault("previousAmountToBeTaken", None)  # backward-compat with snapshots locked before this field existed
            r.setdefault("fromOpeningBalance", False)  # backward-compat with snapshots locked before this field existed
            r.setdefault("previousEstimated", None)  # backward-compat with snapshots locked before this field existed
            r.setdefault("previousSpent", None)  # backward-compat with snapshots locked before this field existed
            r.setdefault("previousTotalRevenue", None)  # backward-compat with snapshots locked before this field existed
            r.setdefault("dailyRate", meta["daily_rate"])  # backward-compat with snapshots locked before per-country rates existed
            r.setdefault("salesSoldProfit", 0.0)  # backward-compat, ditto
            r.setdefault("salesExpectedProfit", 0.0)  # backward-compat, ditto
            r.setdefault("projectTimeProfitability", 0.0)  # backward-compat, ditto
            r.setdefault("costTillDate", 0.0)  # backward-compat, ditto
            r.setdefault("amountProfitability", 0.0)  # backward-compat, ditto
            r.setdefault("computedCompletion", r.get("completion", 0.0))  # backward-compat, ditto
            r.setdefault("completionOverridden", False)  # backward-compat, ditto
            r.setdefault("expectedProfitability", 0.0)  # backward-compat, ditto
            r.setdefault("previousCompletion", None)  # backward-compat, ditto
            r.setdefault("managerId", 0)  # backward-compat with snapshots locked before the PM column existed
            r.setdefault("managerName", "")  # backward-compat, ditto
        return {"locked": True, "daily_rate": meta["daily_rate"], "rows": rows}

    overrides = get_overrides(month)
    previous_map = get_previous_accumulated_map(month)
    previous_amount_map = get_previous_amount_to_take_map(month)
    previous_estimated_map = get_previous_estimated_map(month)
    previous_spent_map = get_previous_spent_map(month)
    previous_total_revenue_map = get_previous_total_revenue_map(month)
    # "When a month is closed, the following month should copy these fields
    # from previous month" (Project Completion, Notes) — pre-fills this
    # month's draft with last month's frozen values whenever this month has
    # no draft of its own yet; still fully editable from there. Project
    # Completion is only carried forward as a live override when it was
    # itself a manual override last month — a purely computed completion
    # isn't copied, since it should keep recomputing fresh each month.
    # previous_completion_map is still exposed per-row (see 'previousCompletion'
    # below) regardless of whether it was overridden, purely so the UI's
    # Auto/Manual toggle can offer "last month's value" as a starting point
    # when a user switches a row to Manual by hand.
    previous_completion_map = _get_previous_snapshot_field_map(month, "completion")
    previous_completion_overridden_map = _get_previous_snapshot_field_map(month, "completionOverridden")
    previous_notes_map = _get_previous_snapshot_field_map(month, "notes")
    spent_map = spent_through_month(timespent or [], month)
    hours_by_project_user = hours_by_project_and_consultant(timespent or [], month)
    employee_rates = settings_store.get_employee_rates()
    daily_rate = meta["daily_rate"]
    opening_balance = load_opening_balance() if month == OPENING_BALANCE_MONTH else {}

    # Admin-configured project ids to leave out of the PP Report entirely
    # (see the "PP Report Data" admin screen) — excluded from both the live
    # Redmine-cached project list and the opening-balance CSV, so they
    # never appear in rows, totals, the country summary, or the export.
    # Only affects months computed live; a month's already-locked snapshot
    # is frozen as-is regardless of later exclusion-list changes.
    excluded_ids = set(settings_store.get_pp_report_excluded_project_ids())
    if excluded_ids:
        projects = [p for p in projects if p["id"] not in excluded_ids]
        opening_balance = {pid: ob for pid, ob in opening_balance.items() if pid not in excluded_ids}

    # Same idea, by Project Manager instead of by individual project id —
    # every project managed by an excluded PM is left out entirely.
    excluded_manager_ids = set(settings_store.get_pp_report_excluded_manager_ids())
    if excluded_manager_ids:
        excluded_by_manager = {p["id"] for p in projects if (p.get("managerId") or 0) in excluded_manager_ids}
        projects = [p for p in projects if (p.get("managerId") or 0) not in excluded_manager_ids]
        opening_balance = {pid: ob for pid, ob in opening_balance.items() if pid not in excluded_by_manager}
    # Daily Rate per country (Countries admin screen) — falls back to the
    # month's own shared rate for a project whose country isn't set or
    # isn't configured there, so this never breaks a project without one.
    country_rates = {c["name"]: c["daily_rate"] for c in settings_store.list_countries()}
    # Case-insensitive lookup onto each configured country's canonical
    # spelling (e.g. "LEBANON"/"lebanon" -> "Lebanon") — Redmine's country
    # custom field isn't case-normalized, and without this, differently-
    # cased spellings of the same country silently split into separate
    # rate lookups, filter options, and Summary tab rows.
    canonical_country_by_lower = {name.lower(): name for name in country_rates}

    rows = []
    seen_ids = set()
    for p in projects:
        seen_ids.add(p["id"])
        override = overrides.get(p["id"])
        ob = opening_balance.get(p["id"])
        raw_country = p.get("country")
        country = canonical_country_by_lower.get(raw_country.lower(), raw_country) if raw_country else raw_country
        rate = country_rates.get(country, daily_rate)

        # Spent is cumulative hours logged through the end of the selected
        # month, not the all-time total from the last Redmine refresh —
        # falls back to the cached all-time figure only if no time-entry
        # data has ever been fetched at all.
        spent = spent_map.get(p["id"], to_float(p.get("spent")) if not timespent else 0.0)
        previous_completion = previous_completion_map.get(p["id"])

        # Whether this project is STILL opening-balance-driven this month —
        # NOT simply "no override row exists". Save draft/Lock's auto-save
        # always resubmit every row's current Total Revenue/Risk verbatim,
        # even rows nobody touched (see save-draft's own docstring: "the
        # whole visible table, not just changed rows") — so an override row
        # existing at all doesn't mean the user actually changed anything
        # away from the CSV. Only a REAL change to Total Revenue or Risk
        # away from the CSV's own values should graduate a project to the
        # normal Estimated/Spent-computed formulas below; editing just
        # Project Completion/Notes must NOT silently swap out Total
        # Revenue/Accumulated/Amount to Take for different (wrong) figures.
        revenue_changed_from_csv = (
            ob is not None and override is not None and override["total_revenue"] is not None
            and abs(override["total_revenue"] - ob["total_revenue"]) > 0.005
        )
        risk_changed_from_csv = (
            ob is not None and override is not None and override["risk"] is not None
            and abs(override["risk"] - ob["risk"]) > 0.005
        )

        if ob is not None and not revenue_changed_from_csv and not risk_changed_from_csv:
            # Still opening-balance driven this month — Total Revenue/
            # Accumulated/Amount to Take come straight from the CSV rather
            # than being computed, since this app has no Redmine history to
            # derive them from. Project Completion/Notes remain overridable
            # on top without affecting this. A genuine edit to Total
            # Revenue or Risk (caught above) is what "graduates" a project
            # to the normal formula-driven behavior in the else branch.
            risk = ob["risk"]
            total_revenue = ob["total_revenue"]
            notes = override["notes"] if override is not None else ""
            completion_override = override["completion_override"] if override is not None else None
            estimated = p.get("est")
            from_opening_balance = True
            computed = compute_row_from_fixed_accumulated(
                ob["accumulated"], total_revenue, ob["prev_accumulated"], rate, spent, ob["amount_to_take"],
                estimated=estimated, completion_override=completion_override,
            )
        else:
            from_opening_balance = False
            if override is not None:
                total_revenue = override["total_revenue"]
                # A saved draft with risk left blank still means "use the
                # Redmine value", same as when there's no draft at all — risk
                # is only truly overridden when the draft explicitly set a
                # number, unlike total_revenue where None is a valid explicit
                # "no revenue yet" choice.
                risk = to_float(p.get("risk")) if override["risk"] is None else override["risk"]
                notes = override["notes"]
                completion_override = override["completion_override"]
            else:
                total_revenue = default_total_revenue(p)
                risk = to_float(p.get("risk"))
                notes = None
                completion_override = None

            # Notes/Completion carry forward from last month independently of
            # whether a draft already exists this month for OTHER fields
            # (e.g. an old blank Save-draft click that only ever touched
            # Risk/Total Revenue) — otherwise that stale draft permanently
            # blocks this carry-forward, exactly like it does for the
            # opening-balance CSV above. Each field only falls back when IT
            # SPECIFICALLY hasn't been set this month: notes is blank, or
            # completion_override is still None (never manually set).
            if not notes:
                notes = previous_notes_map.get(p["id"], "")
            if completion_override is None:
                completion_override = previous_completion if previous_completion_overridden_map.get(p["id"]) else None

            # T&M projects have no fixed scope — Estimated must always equal
            # this month's Spent (not the all-time figure frozen in the cache
            # at the last Redmine refresh), so every dependent formula below
            # (Remaining, Accumulated, etc.) sees a project that's always
            # exactly "on budget" for whatever's been logged so far.
            estimated = spent if p.get("isTM") else p.get("est")
            project_cost_till_date = consultant_cost_till_date(p["id"], hours_by_project_user, employee_rates, rate / 8)
            computed = compute_row(
                estimated, spent, risk, total_revenue, previous_map.get(p["id"], 0.0), rate,
                completion_override=completion_override, cost_till_date=project_cost_till_date,
            )

        rows.append({
            "projectId": p["id"],
            "name": p.get("name"),
            "country": country,
            "managerId": p.get("managerId", 0),
            "managerName": p.get("managerName", ""),
            "rapportCode": p.get("rapportCode"),
            "status": p.get("status"),
            "estimated": estimated,
            "spent": spent,
            "risk": risk,
            "notes": notes,
            "dailyRate": rate,
            "previousAmountToBeTaken": previous_amount_map.get(p["id"]),
            "previousEstimated": previous_estimated_map.get(p["id"]),
            "previousSpent": previous_spent_map.get(p["id"]),
            "previousTotalRevenue": previous_total_revenue_map.get(p["id"]),
            "previousCompletion": previous_completion,
            "fromOpeningBalance": from_opening_balance,
            **computed,
        })

    # Opening-balance rows for project ids no longer in the current
    # Redmine cache (renamed, closed & excluded, etc.) — shown anyway with
    # only what the CSV has, per how this was scoped: never silently drop
    # an opening balance just because Redmine's project list has moved on.
    for project_id, ob in opening_balance.items():
        if project_id in seen_ids:
            continue
        computed = compute_row_from_fixed_accumulated(
            ob["accumulated"], ob["total_revenue"], ob["prev_accumulated"], daily_rate, 0.0, ob["amount_to_take"]
        )
        rows.append({
            "projectId": project_id,
            "name": f"(opening balance only — project #{project_id} not in current Redmine data)",
            "country": None,
            "managerId": 0,
            "managerName": "",
            "rapportCode": None,
            "status": None,
            "estimated": 0.0,
            "spent": 0.0,
            "risk": ob["risk"],
            "notes": "",
            "dailyRate": daily_rate,
            "previousAmountToBeTaken": previous_amount_map.get(project_id),
            "previousEstimated": previous_estimated_map.get(project_id),
            "previousSpent": previous_spent_map.get(project_id),
            "previousTotalRevenue": previous_total_revenue_map.get(project_id),
            "previousCompletion": previous_completion_map.get(project_id),
            "fromOpeningBalance": True,
            **computed,
        })

    # KSA isn't in Redmine — its rows come from the workbook upload (see
    # parse_ksa_workbook). Only live months read them here; a locked month
    # returns its frozen snapshot above, which already contains them.
    ksa_hours = hours_by_project_and_consultant(ksa_timesheet_entries(settings_store.distinct_timesheet_users(timespent)), month)
    rows.extend(ksa_report_rows(month, country_rates.get(KSA_COUNTRY, daily_rate), ksa_hours, employee_rates))

    return {"locked": False, "daily_rate": daily_rate, "rows": rows}


def build_workbook(month: str, daily_rate: float, rows: list):
    """Render the report as an .xlsx workbook (one sheet, same columns as
    the on-screen table plus a totals row) — returns raw bytes."""
    import io

    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Font, PatternFill
    from openpyxl.utils import get_column_letter

    wb = Workbook()
    ws = wb.active
    ws.title = month

    headers = [
        "ID", "Project", "Country", "PM", "Rapport Code", "Estimated", "Spent", "Risk", "Total Revenue",
        "Prev. Accumulated", "Accumulated", "Amount to Take", "Fully Earned",
        "Yet to Earn", "Remaining %", "Sales Sold Profit %", "Sales Expected Profit",
        "Project Time Profitability %", "Cost till Date", "Profit till Date",
        "Project Amount Profitability %", "Project Completion %", "Expected Profitability %", "Notes",
    ]
    header_fill = PatternFill(start_color="F4F6F8", end_color="F4F6F8", fill_type="solid")
    header_font = Font(bold=True, size=10)

    ws.append([f"Daily rate: {daily_rate}"])
    ws.append([])
    header_row_idx = 3
    ws.append(headers)
    for col_idx in range(1, len(headers) + 1):
        cell = ws.cell(row=header_row_idx, column=col_idx)
        cell.fill = header_fill
        cell.font = header_font
        cell.alignment = Alignment(horizontal="center")

    for row in rows:
        ws.append([
            row.get("projectId"),
            row.get("name"),
            row.get("country"),
            row.get("managerName"),
            row.get("rapportCode"),
            row.get("estimated") or 0,
            row.get("spent") or 0,
            row.get("risk") or 0,
            row.get("totalRevenue") or 0,
            row.get("previousAccumulated") or 0,
            row.get("accumulated") or 0,
            row.get("amountToBeTaken") or 0,
            "Yes" if row.get("fullyEarned") else "No",
            row.get("yetToEarn") or 0,
            round((row.get("remainingPct") or 0) * 100, 1),
            round((row.get("salesSoldProfit") or 0) * 100, 1),
            row.get("salesExpectedProfit") or 0,
            round((row.get("projectTimeProfitability") or 0) * 100, 1),
            row.get("costTillDate") or 0,
            row.get("profitTillDate") or 0,
            round((row.get("amountProfitability") or 0) * 100, 1),
            round((row.get("completion") or 0) * 100, 0),
            round((row.get("expectedProfitability") or 0) * 100, 1),
            row.get("notes") or "",
        ])

    totals = totals_for(rows)
    totals_row = [
        "", "TOTAL", "", "", "", "", "", "", round(totals["totalRevenue"], 2), round(totals["previousAccumulated"], 2),
        round(totals["accumulated"], 2), round(totals["amountToBeTaken"], 2), "", "", "", "", "", "", "", "", "", "", "", "",
    ]
    ws.append(totals_row)
    last_row = ws.max_row
    for col_idx in range(1, len(headers) + 1):
        ws.cell(row=last_row, column=col_idx).font = Font(bold=True)

    ws.freeze_panes = ws.cell(row=header_row_idx + 1, column=1)
    widths = [8, 32, 12, 16, 20, 11, 11, 9, 14, 16, 13, 14, 12, 12, 12, 14, 14, 16, 12, 14, 16, 14, 14, 30]
    for i, width in enumerate(widths, start=1):
        ws.column_dimensions[get_column_letter(i)].width = width

    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def totals_for(rows: list) -> dict:
    """Sum of Total Revenue / Prev. Accumulated / Accumulated / Amount to
    be Taken across a set of report rows — shown as header stat tiles."""
    return {
        "totalRevenue": sum((r.get("totalRevenue") or 0) for r in rows),
        "previousAccumulated": sum((r.get("previousAccumulated") or 0) for r in rows),
        "accumulated": sum((r.get("accumulated") or 0) for r in rows),
        "amountToBeTaken": sum((r.get("amountToBeTaken") or 0) for r in rows),
    }


def country_summary_for(rows: list) -> list:
    """Country-wise earning breakdown for the Summary tab — deliberately
    unfiltered (always every row in the report, ignoring whatever the
    main table's Country dropdown/chips currently show). 'Earning this
    month'/'last month' are amountToBeTaken/previousAmountToBeTaken
    summed per country; 'Earning %' is each country's share of this
    month's total earning across every country combined."""
    by_country = {}
    for r in rows:
        country = r.get("country") or "Unspecified"
        entry = by_country.setdefault(country, {"country": country, "thisMonth": 0.0, "lastMonth": 0.0})
        entry["thisMonth"] += r.get("amountToBeTaken") or 0
        entry["lastMonth"] += r.get("previousAmountToBeTaken") or 0

    total_this_month = sum(entry["thisMonth"] for entry in by_country.values())
    summary = list(by_country.values())
    for entry in summary:
        entry["pct"] = (entry["thisMonth"] / total_this_month * 100) if total_this_month else 0.0
    summary.sort(key=lambda e: e["thisMonth"], reverse=True)
    return summary


def country_project_breakdown_for(rows: list) -> list:
    """Country -> its projects' Amount to Take ('this month' earning,
    same figure as country_summary_for's 'thisMonth') for the Summary
    tab's country/project drill-down. Projects with nothing earned this
    month (amountToBeTaken == 0) are left out entirely — same idea as
    the Report tab's own 'Earned this month' filter chip — since a long
    list of zero rows would otherwise bury the ones that actually moved.
    Each country's remaining projects are sorted by amount descending,
    and each country carries its own subtotal; countries are sorted by
    subtotal descending, same ordering as the country-only summary
    above it. A country whose every project nets to zero this month
    (e.g. from projects with equal and opposite adjustments) is left out
    too, rather than showing an empty subtotal-only row."""
    by_country = {}
    for r in rows:
        amount = r.get("amountToBeTaken") or 0.0
        if amount == 0:
            continue
        country = r.get("country") or "Unspecified"
        entry = by_country.setdefault(country, {"country": country, "projects": [], "subtotal": 0.0})
        entry["projects"].append({"name": r.get("name"), "rapportCode": r.get("rapportCode"), "amountToBeTaken": amount})
        entry["subtotal"] += amount

    result = [entry for entry in by_country.values() if entry["subtotal"] != 0]
    for entry in result:
        entry["projects"].sort(key=lambda p: p["amountToBeTaken"], reverse=True)
    result.sort(key=lambda e: e["subtotal"], reverse=True)
    return result


def build_country_project_workbook(month: str, breakdown: list, grand_total: float) -> bytes:
    """Renders country_project_breakdown_for()'s output as a flat .xlsx
    — one row per project (with its country repeated, so it filters/
    pivots cleanly in Excel), a bold subtotal row per country, and a
    grand total row at the end."""
    import io

    from openpyxl import Workbook
    from openpyxl.styles import Font
    from openpyxl.utils import get_column_letter

    wb = Workbook()
    ws = wb.active
    ws.title = "Country x Project"

    ws.append(["Country", "Project", "Rapport Code", "Amount to Take"])
    bold = Font(bold=True)
    for cell in ws[1]:
        cell.font = bold

    for entry in breakdown:
        for p in entry["projects"]:
            ws.append([entry["country"], p["name"], p["rapportCode"], p["amountToBeTaken"]])
        ws.append([f"{entry['country']} TOTAL", "", "", entry["subtotal"]])
        for cell in ws[ws.max_row]:
            cell.font = bold

    ws.append(["GRAND TOTAL", "", "", grand_total])
    for cell in ws[ws.max_row]:
        cell.font = bold

    for row_idx in range(2, ws.max_row + 1):
        ws[f"D{row_idx}"].number_format = "#,##0.00"
    widths = [16, 45, 20, 16]
    for i, width in enumerate(widths, start=1):
        ws.column_dimensions[get_column_letter(i)].width = width
    ws.freeze_panes = "A2"

    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


_PIE_PALETTE = [
    "#0f6e6e", "#b8792e", "#3d7a4f", "#b4482f", "#5b6ee1",
    "#a15bd1", "#c2554a", "#3f8fbf", "#8a8f2e", "#6c757d",
]


def pie_slices_for(country_summary: list) -> list:
    """SVG pie-slice path data for the country-wise 'Earning this month'
    breakdown — computed server-side (plain trig, no charting library)
    since the Summary tab is a static, unfiltered snapshot of the
    current report; matches this app's existing no-dependency approach
    to charts (see the Dashboard's inline-SVG bar charts)."""
    total = sum(e["thisMonth"] for e in country_summary if e["thisMonth"] > 0)
    slices = []
    if total <= 0:
        return slices

    cx, cy, r = 100, 100, 90
    angle = -90.0  # start at 12 o'clock, sweep clockwise
    positive = [e for e in country_summary if e["thisMonth"] > 0]
    for i, entry in enumerate(positive):
        fraction = entry["thisMonth"] / total
        sweep = fraction * 360.0
        color = _PIE_PALETTE[i % len(_PIE_PALETTE)]

        if fraction >= 0.9999 and len(positive) == 1:
            # A single 100% slice can't be drawn as one arc (degenerate
            # start==end point) — split it into two half-circle arcs.
            path = f"M {cx - r},{cy} A {r},{r} 0 1 1 {cx + r},{cy} A {r},{r} 0 1 1 {cx - r},{cy} Z"
        else:
            start_rad = math.radians(angle)
            end_rad = math.radians(angle + sweep)
            x1, y1 = cx + r * math.cos(start_rad), cy + r * math.sin(start_rad)
            x2, y2 = cx + r * math.cos(end_rad), cy + r * math.sin(end_rad)
            large_arc = 1 if sweep > 180 else 0
            path = f"M {cx},{cy} L {x1:.2f},{y1:.2f} A {r},{r} 0 {large_arc} 1 {x2:.2f},{y2:.2f} Z"

        slices.append({"country": entry["country"], "pct": fraction * 100, "path": path, "color": color})
        angle += sweep
    return slices


def performance_rows_for(rows: list) -> list:
    """'Projects Performance' view — the layout of the old manual
    'Projects Performance MENA' workbook (Country/Project, Performance to
    date / of which YTD / Performance to Go, each split into
    Revenue/Margin/Margin%, plus Completion, Project total and Sales
    Expected Profit). Reuses this report's own computed fields rather than
    re-deriving anything: 'to date' Revenue/Margin map onto Accumulated/
    Profit till Date, 'to go' is the remainder against Total Revenue, and
    Margin% follows the same relationships the original workbook used.
    Only revenue-bearing rows are included (no Total Revenue entered yet
    means nothing to show here) — filtering further (by country, margin
    threshold, etc.) is left to the caller/UI, not baked in here, since
    which projects belong in a given month's report is a judgment call,
    not a fixed rule."""
    out = []
    for r in rows:
        total_revenue = r.get("totalRevenue") or 0.0
        if not total_revenue:
            continue
        rev_to_date = r.get("accumulated") or 0.0
        margin_to_date = r.get("profitTillDate") or 0.0
        completion = r.get("completion") or 0.0
        sales_expected_profit = r.get("salesExpectedProfit") or 0.0

        margin_pct_to_date = _safe_div(margin_to_date * completion, rev_to_date)
        rev_ytd = rev_to_date
        margin_ytd = margin_to_date
        margin_pct_ytd = _safe_div(margin_ytd, rev_ytd)
        rev_to_go = total_revenue - rev_to_date
        margin_to_go = -(1 - completion) * sales_expected_profit
        ratio = _safe_div(rev_to_go, rev_to_date)
        margin_pct_to_go = margin_pct_ytd if ratio == 0 else ratio

        out.append({
            "country": r.get("country") or "",
            "project": r.get("name") or "",
            "revToDate": rev_to_date, "marginToDate": margin_to_date, "marginPctToDate": margin_pct_to_date,
            "revYtd": rev_ytd, "marginYtd": margin_ytd, "marginPctYtd": margin_pct_ytd,
            "revToGo": rev_to_go, "marginToGo": margin_to_go, "marginPctToGo": margin_pct_to_go,
            "completion": completion, "projectTotal": total_revenue, "salesExpectProfit": sales_expected_profit,
        })

    country_order = {"UAE": 0, "Kuwait": 1, "KSA": 2, "Lebanon": 3, "": 4}
    out.sort(key=lambda r: (country_order.get(r["country"], 5), r["project"]))
    return out


def build_performance_workbook(month: str, performance_rows: list) -> bytes:
    """Renders performance_rows_for()'s output as an .xlsx, matching the
    old manual 'Projects Performance MENA' workbook's column layout
    exactly (two header rows, grouped Revenue/Margin/Margin% triples)."""
    import io

    from openpyxl import Workbook
    from openpyxl.styles import Font
    from openpyxl.utils import get_column_letter

    wb = Workbook()
    ws = wb.active
    ws.title = month

    row1 = ["Country", "Project", "Performance to date", None, None,
            "of which performance YTD", None, None,
            "Performance to Go", None, None,
            "Insights", None, None, None, None, None,
            "Completion", "Project total", "Sales expect Profit"]
    row2 = [None, None, "Revenue", "Margin", "Margin%",
            "Revenue", "Margin", "Margin%",
            "Revenue", "Margin", "Margin%",
            None, None, None, None, None, None, None, None, None]
    ws.append(row1)
    ws.append(row2)
    bold = Font(bold=True)
    for cell in ws[1]:
        cell.font = bold
    for cell in ws[2]:
        cell.font = bold

    for r in performance_rows:
        ws.append([
            r["country"], r["project"],
            r["revToDate"], r["marginToDate"], r["marginPctToDate"],
            r["revYtd"], r["marginYtd"], r["marginPctYtd"],
            r["revToGo"], r["marginToGo"], r["marginPctToGo"],
            None, None, None, None, None, None,
            r["completion"], r["projectTotal"], r["salesExpectProfit"],
        ])

    for col_letter in ("E", "H", "K", "R"):
        for row_idx in range(3, ws.max_row + 1):
            ws[f"{col_letter}{row_idx}"].number_format = "0.0%"
    for col_letter in ("C", "D", "F", "G", "I", "J", "S", "T"):
        for row_idx in range(3, ws.max_row + 1):
            ws[f"{col_letter}{row_idx}"].number_format = "#,##0.00"

    widths = [10, 55, 12, 14, 10, 12, 14, 10, 12, 14, 10, 8, 8, 8, 8, 8, 8, 11, 14, 16]
    for i, width in enumerate(widths, start=1):
        ws.column_dimensions[get_column_letter(i)].width = width
    ws.freeze_panes = "A3"
    ws.auto_filter.ref = f"A2:T{ws.max_row}"

    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def profitability_rows_for(rows: list) -> list:
    """'Project Profitability' view — the layout of the old manual
    'Project Profatibility MonYY.xlsx' workbook's Data sheet. Every column
    here already exists on the report row EXCEPT the four computed below
    (Total estimated cost, Remaining completion %, Remaining Cost, Total
    margin by end of project), whose formulas were reverse-engineered by
    matching real numbers from the July 2026 workbook against this app's
    existing fields:
      totalEstimatedCost = dailyRate/8 * estimated   (same ratio compute_row's
          own costTillDate uses, just against Estimated instead of Spent)
      remainingCompletionPct = 1 - completion
      remainingCost = remainingCompletionPct * totalEstimatedCost
      totalMarginByEndOfProject = profitTillDate + salesExpectedProfit * remainingCompletionPct
    Uses the project's *country* Daily Rate (row['dailyRate'], resolved by
    build_report) — still the shared per-country rate today, not a
    per-consultant one (see the employees table/admin screen for that
    future direction)."""
    out = []
    for r in rows:
        estimated = r.get("estimated") or 0.0
        rate = r.get("dailyRate") or 0.0
        completion = r.get("completion") or 0.0
        profit_till_date = r.get("profitTillDate") or 0.0
        sales_expected_profit = r.get("salesExpectedProfit") or 0.0

        total_estimated_cost = rate / 8 * estimated
        remaining_completion_pct = 1 - completion
        remaining_cost = remaining_completion_pct * total_estimated_cost
        total_margin_by_end = profit_till_date + sales_expected_profit * remaining_completion_pct

        out.append({
            "country": r.get("country") or "",
            "rapportCode": r.get("rapportCode") or "",
            "name": r.get("name") or "",
            "estimated": estimated,
            "spent": r.get("spent") or 0.0,
            "totalRevenue": r.get("totalRevenue") or 0.0,
            "totalEarning": r.get("accumulated") or 0.0,
            "salesSoldProfit": r.get("salesSoldProfit") or 0.0,
            "salesExpectedProfit": sales_expected_profit,
            "costTillDate": r.get("costTillDate") or 0.0,
            "profitTillDate": profit_till_date,
            "profitabilityPct": r.get("amountProfitability") or 0.0,
            "completion": completion,
            "expectedProfitability": r.get("expectedProfitability") or 0.0,
            "notes": r.get("notes") or "",
            "totalEstimatedCost": total_estimated_cost,
            "remainingCompletionPct": remaining_completion_pct,
            "remainingCost": remaining_cost,
            "totalMarginByEndOfProject": total_margin_by_end,
        })

    country_order = {"UAE": 0, "Kuwait": 1, "KSA": 2, "Lebanon": 3, "": 4}
    out.sort(key=lambda r: (country_order.get(r["country"], 5), r["name"]))
    return out


def profitability_country_summary_for(profitability_rows: list) -> list:
    """Country-wise Cost/Profit/Earning totals for the Summary sheet/tab —
    mirrors the old workbook's pivot: Sum of Cost till date, Sum of Profit
    till date, Sum of Total Earning, and Profit % (= profit / earning) per
    country, plus a Grand Total row."""
    by_country = {}
    for r in profitability_rows:
        country = r["country"] or "Unspecified"
        entry = by_country.setdefault(country, {"country": country, "cost": 0.0, "profit": 0.0, "earning": 0.0})
        entry["cost"] += r["costTillDate"]
        entry["profit"] += r["profitTillDate"]
        entry["earning"] += r["totalEarning"]

    summary = list(by_country.values())
    for entry in summary:
        entry["profitPct"] = _safe_div(entry["profit"], entry["earning"])
    summary.sort(key=lambda e: e["country"])

    grand = {
        "country": "Grand Total",
        "cost": sum(e["cost"] for e in summary),
        "profit": sum(e["profit"] for e in summary),
        "earning": sum(e["earning"] for e in summary),
    }
    grand["profitPct"] = _safe_div(grand["profit"], grand["earning"])
    summary.append(grand)
    return summary


def build_profitability_workbook(month: str, profitability_rows: list, country_summary: list, countries: list) -> bytes:
    """Renders profitability_rows_for()'s output as an .xlsx with the old
    workbook's three sheets: Data, Summary, and Rate (a plain readout of
    the Countries admin table, for reference)."""
    import io

    from openpyxl import Workbook
    from openpyxl.styles import Font
    from openpyxl.utils import get_column_letter

    wb = Workbook()
    data_ws = wb.active
    data_ws.title = "Data"

    headers = [
        "Sap Company", "Rapports code (PEP)", "Name", "Estimated", "Spent", "Total Project Revenue",
        "Total Earning", "Sales sold Profit", "Sales expected profit", "Cost till date", "Profit till date",
        f"Project Profitability as of {format_month_label(month)} %", "Project Completion",
        "Expected Profitability By end of project", "Notes", "Total estimated cost",
        "Remaining completion %", "Remaining Cost", "Total margin by end of project",
    ]
    data_ws.append(headers)
    bold = Font(bold=True)
    for cell in data_ws[1]:
        cell.font = bold

    for r in profitability_rows:
        data_ws.append([
            r["country"], r["rapportCode"], r["name"], r["estimated"], r["spent"], r["totalRevenue"],
            r["totalEarning"], r["salesSoldProfit"], r["salesExpectedProfit"], r["costTillDate"], r["profitTillDate"],
            r["profitabilityPct"], r["completion"], r["expectedProfitability"], r["notes"],
            r["totalEstimatedCost"], r["remainingCompletionPct"], r["remainingCost"], r["totalMarginByEndOfProject"],
        ])

    for col_letter in ("H", "L", "M", "N", "Q"):
        for row_idx in range(2, data_ws.max_row + 1):
            data_ws[f"{col_letter}{row_idx}"].number_format = "0.0%"
    widths = [10, 22, 45, 10, 10, 14, 12, 12, 14, 12, 12, 14, 12, 16, 30, 14, 14, 12, 16]
    for i, width in enumerate(widths, start=1):
        data_ws.column_dimensions[get_column_letter(i)].width = width
    data_ws.freeze_panes = "A2"
    data_ws.auto_filter.ref = f"A1:S{data_ws.max_row}"

    summary_ws = wb.create_sheet("Summary")
    summary_ws.append(["Row Labels", "Sum of Cost till date", "Sum of Profit till date", "Sum of Total Earning",
                        f"Profit % as of end {format_month_label(month)}"])
    for cell in summary_ws[1]:
        cell.font = bold
    for entry in country_summary:
        summary_ws.append([entry["country"], entry["cost"], entry["profit"], entry["earning"], entry["profitPct"]])
    for row_idx in range(2, summary_ws.max_row + 1):
        summary_ws[f"E{row_idx}"].number_format = "0.0%"
    for i, width in enumerate([14, 20, 20, 18, 18], start=1):
        summary_ws.column_dimensions[get_column_letter(i)].width = width

    rate_ws = wb.create_sheet("Rate")
    rate_ws.append(["Country", "Daily Rate", "Currency"])
    for cell in rate_ws[1]:
        cell.font = bold
    for c in countries:
        rate_ws.append([c["name"], c["daily_rate"], c["currency"]])
    for i, width in enumerate([16, 12, 10], start=1):
        rate_ws.column_dimensions[get_column_letter(i)].width = width

    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def to_float(val, default=0.0):
    if val in (None, ""):
        return default
    try:
        return float(val)
    except (TypeError, ValueError):
        return default
