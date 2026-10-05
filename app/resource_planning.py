"""Resource Planning — a rolling 6-month view of planned hours per
consultant per project, and each consultant's capacity/utilization
against it.

Planned hours are auto-filled once, then live as editable overrides
(resource_plan table) exactly like pp_report_overrides overrides
pp_report's own computed defaults:

  - A project's *remaining* estimated hours (Estimated - Spent, floored
    at 0 — T&M projects always have Estimated == Spent, so they never
    appear here) are split evenly across the next 3 calendar months
    starting this month.
  - The whole remaining amount defaults onto whichever consultant logged
    that project's most recent (by spent_on date) cached time entry —
    the simplest available proxy for "who's actually working on this",
    since Redmine's own project data has no per-consultant assignment
    or allocation percentage to read instead.
  - Every cell is independently overridable from there (reassign to a
    different consultant, change the hours, zero it out) via
    save_planned_hours() — the auto-fill is only ever a starting point,
    never recomputed over a saved override.

Capacity is a fixed, simple basis (per the product decision, not derived
from Redmine): each weekday (Mon-Fri) in the month counts as 8 hours,
with no holiday calendar.
"""

import calendar
from datetime import date

from . import db
from .pp_report import to_float


def current_month_str() -> str:
    return date.today().strftime("%Y-%m")


def next_month_str(month: str) -> str:
    year, mon = (int(x) for x in month.split("-"))
    if mon == 12:
        return f"{year + 1}-01"
    return f"{year}-{mon + 1:02d}"


def previous_month_str(month: str) -> str:
    year, mon = (int(x) for x in month.split("-"))
    if mon == 1:
        return f"{year - 1}-12"
    return f"{year}-{mon - 1:02d}"


def plan_months(start_month: str = None, count: int = 6) -> list:
    """The rolling window of months this view covers — defaults to this
    month plus the next five (6 months total)."""
    month = start_month or current_month_str()
    months = []
    for _ in range(count):
        months.append(month)
        month = next_month_str(month)
    return months


def format_month_label(month: str) -> str:
    year, mon = (int(x) for x in month.split("-"))
    return f"{calendar.month_abbr[mon]} {year}"


def working_days_in_month(month: str) -> int:
    """Count of Monday-Friday calendar days in `month` ('YYYY-MM')."""
    year, mon = (int(x) for x in month.split("-"))
    _, days_in_month = calendar.monthrange(year, mon)
    return sum(1 for day in range(1, days_in_month + 1) if date(year, mon, day).weekday() < 5)


def capacity_hours(month: str) -> float:
    """Flat 8-hours-per-weekday capacity for `month` — no holiday
    calendar, per the product decision behind this feature."""
    return working_days_in_month(month) * 8.0


def _most_recent_assignee(project_id: int, timespent: list) -> dict | None:
    """{'id':, 'name':} of whoever logged the latest (by spent_on)
    cached time entry on `project_id`, or None if it has none."""
    best = None
    for entry in timespent or []:
        if entry.get("project_id") != project_id:
            continue
        spent_on = (entry.get("spent_on") or "").strip()
        if not spent_on:
            continue
        if best is None or spent_on > best["spent_on"]:
            best = {"spent_on": spent_on, "id": entry.get("user_id"), "name": entry.get("user_name") or ""}
    return {"id": best["id"], "name": best["name"]} if best and best["id"] is not None else None


def get_overrides() -> dict:
    """(project_id, user_id, month) -> planned_hours, for every saved
    override."""
    with db.get_db() as conn:
        rows = conn.execute("SELECT project_id, redmine_user_id, month, planned_hours FROM resource_plan").fetchall()
        return {(r["project_id"], r["redmine_user_id"], r["month"]): r["planned_hours"] for r in rows}


def get_assignee_overrides() -> dict:
    """project_id -> {'id':, 'name':} for every project manually
    reassigned to a specific consultant (see resource_plan_assignee's own
    comment in db.py) — takes precedence over _most_recent_assignee()'s
    guess in build_plan()."""
    with db.get_db() as conn:
        rows = conn.execute("SELECT project_id, redmine_user_id, redmine_user_name FROM resource_plan_assignee").fetchall()
        return {r["project_id"]: {"id": r["redmine_user_id"], "name": r["redmine_user_name"]} for r in rows}


def save_assignee_override(project_id: int, user_id: int, user_name: str):
    with db.get_db() as conn:
        conn.execute(
            """
            INSERT INTO resource_plan_assignee (project_id, redmine_user_id, redmine_user_name, updated_on)
            VALUES (?, ?, ?, datetime('now'))
            ON CONFLICT(project_id) DO UPDATE SET
                redmine_user_id = excluded.redmine_user_id, redmine_user_name = excluded.redmine_user_name,
                updated_on = datetime('now')
            """,
            (project_id, user_id, user_name.strip()),
        )


def get_extra_assignees() -> dict:
    """project_id -> [{'id':, 'name':}, ...] for every extra, manually-
    added planning row on top of a project's normal one (see
    resource_plan_extra_assignee's own comment in db.py)."""
    with db.get_db() as conn:
        rows = conn.execute("SELECT project_id, redmine_user_id, redmine_user_name FROM resource_plan_extra_assignee").fetchall()
    by_project = {}
    for r in rows:
        by_project.setdefault(r["project_id"], []).append({"id": r["redmine_user_id"], "name": r["redmine_user_name"]})
    return by_project


def add_extra_assignee(project_id: int, user_id: int, user_name: str, monthly_hours: dict):
    """Adds project_id's extra planning row for `user_id`, pre-filled
    with `monthly_hours` ({'YYYY-MM': hours}) — normally a straight copy
    of whatever the project's normal row was showing at the moment the
    Split button was clicked, so the two rows start identical and the
    user can then manually rebalance the numbers between them."""
    with db.get_db() as conn:
        conn.execute(
            """
            INSERT INTO resource_plan_extra_assignee (project_id, redmine_user_id, redmine_user_name, updated_on)
            VALUES (?, ?, ?, datetime('now'))
            ON CONFLICT(project_id, redmine_user_id) DO UPDATE SET
                redmine_user_name = excluded.redmine_user_name, updated_on = datetime('now')
            """,
            (project_id, user_id, user_name.strip()),
        )
    for month, hours in monthly_hours.items():
        save_planned_hours(project_id, user_id, month, hours)


def remove_extra_assignee(project_id: int, user_id: int):
    """Removes project_id's extra planning row for `user_id` and its
    saved hour cells, so it doesn't linger as orphaned data."""
    with db.get_db() as conn:
        conn.execute(
            "DELETE FROM resource_plan_extra_assignee WHERE project_id = ? AND redmine_user_id = ?",
            (project_id, user_id),
        )
        conn.execute(
            "DELETE FROM resource_plan WHERE project_id = ? AND redmine_user_id = ?",
            (project_id, user_id),
        )


def save_planned_hours(project_id: int, user_id: int, month: str, hours: float):
    with db.get_db() as conn:
        conn.execute(
            """
            INSERT INTO resource_plan (project_id, redmine_user_id, month, planned_hours, updated_on)
            VALUES (?, ?, ?, ?, datetime('now'))
            ON CONFLICT(project_id, redmine_user_id, month) DO UPDATE SET
                planned_hours = excluded.planned_hours, updated_on = datetime('now')
            """,
            (project_id, user_id, month, hours),
        )


def save_planned_hours_batch(cells: list):
    """Saves every {'project_id':, 'user_id':, 'month':, 'hours':} in
    `cells` in one connection/transaction — the Save button's "save
    everything currently on screen" action, instead of one request per
    cell."""
    with db.get_db() as conn:
        for c in cells:
            conn.execute(
                """
                INSERT INTO resource_plan (project_id, redmine_user_id, month, planned_hours, updated_on)
                VALUES (?, ?, ?, ?, datetime('now'))
                ON CONFLICT(project_id, redmine_user_id, month) DO UPDATE SET
                    planned_hours = excluded.planned_hours, updated_on = datetime('now')
                """,
                (c["project_id"], c["user_id"], c["month"], c["hours"]),
            )


def build_plan(projects: list, timespent: list, months: list = None) -> list:
    """One row per (project, assigned consultant) with a planned_hours
    figure for each month in `months` (defaults to plan_months(), now 6
    months) — auto-filled per this module's docstring, with any saved
    override (get_overrides) taking precedence over the auto-filled
    default on a per-cell basis. Closed projects and projects with no
    remaining estimated hours are left out.  The assignee is chosen as:
    (1) a manual reassignment override, (2) whoever logged the most
    recent timesheet entry, or (3) the project manager.  Only projects
    with none of these three are skipped.

    Auto-fill is capacity-aware per consultant: rather than splitting
    each project's remaining hours evenly across every month regardless
    of how much that piles onto one person, every one of a consultant's
    projects is queued up (in project-name order, for a stable, readable
    result) and poured month by month into whatever capacity that
    consultant has left that month, spilling into the next month once a
    month fills up — so no month is *pushed* over capacity by the
    auto-fill itself. A consultant whose total workload across every
    project still exceeds their capacity across the whole window will
    still show over capacity in the last month(s) — that's a genuine
    overallocation signal, not a splitting artifact, and it's exactly
    what should prompt reassigning some of their projects by hand.

    A project can also have one or more extra, manually-added planning
    rows (see the Split button / add_extra_assignee) — each is just a
    second (or third...) consultant against the same project, with its
    own independently-editable planned hours (get_overrides), starting
    as a straight copy of the project's normal row at the moment it was
    split off. Extra rows are never auto-filled — only the project's one
    normal row goes through the capacity-fill queue below."""
    months = months or plan_months()
    overrides = get_overrides()
    assignee_overrides = get_assignee_overrides()
    extra_assignees = get_extra_assignees()

    # First pass: remaining hours + assignee for every plannable
    # project's *normal* row, grouped by consultant so each person's
    # queue can be filled in order.
    by_user = {}
    plannable_projects = {}
    for p in projects:
        if p.get("status") == "Closed":
            continue  # nothing left to plan for a project that's already closed
        if p.get("isTM"):
            continue  # always remaining == 0 — see module docstring
        estimated = p.get("est") or 0.0
        spent = p.get("spent") or 0.0
        remaining = estimated - spent
        if remaining <= 0:
            continue

        assignee = assignee_overrides.get(p["id"]) or _most_recent_assignee(p["id"], timespent)
        if assignee is None:
            # Fall back to the project manager when no one has logged time yet
            mgr_id = p.get("managerId")
            mgr_name = p.get("managerName") or ""
            if mgr_id:
                assignee = {"id": mgr_id, "name": mgr_name}
            else:
                continue

        plannable_projects[p["id"]] = {"name": p.get("name"), "rapportCode": p.get("rapportCode"), "country": p.get("country"), "remaining": remaining}
        by_user.setdefault(assignee["id"], {"userName": assignee["name"], "projects": []})["projects"].append({
            "projectId": p["id"], "projectName": p.get("name"), "rapportCode": p.get("rapportCode"),
            "country": p.get("country"), "remaining": remaining,
        })

    rows = []
    for user_id, user_data in by_user.items():
        user_data["projects"].sort(key=lambda pr: pr["projectName"] or "")
        month_capacity_left = {month: capacity_hours(month) for month in months}

        for pr in user_data["projects"]:
            to_allocate = pr["remaining"]
            monthly = {month: 0.0 for month in months}
            for month in months:
                if to_allocate <= 0:
                    break
                take = min(to_allocate, month_capacity_left[month])
                if take <= 0:
                    continue
                monthly[month] = take
                month_capacity_left[month] -= take
                to_allocate -= take
            if to_allocate > 0:
                # Genuinely more work than this whole window has capacity
                # for — dump the rest on the final month rather than
                # silently dropping it, so the overallocation is visible
                # instead of hidden.
                monthly[months[-1]] += to_allocate

            for month in months:
                override = overrides.get((pr["projectId"], user_id, month))
                if override is not None:
                    monthly[month] = override

            rows.append({
                "projectId": pr["projectId"],
                "projectName": pr["projectName"],
                "rapportCode": pr["rapportCode"],
                "country": pr["country"],
                "userId": user_id,
                "userName": user_data["userName"],
                "remaining": pr["remaining"],
                "monthly": monthly,
                "isSplit": pr["projectId"] in extra_assignees,
                "isExtraRow": False,
                "reassigned": pr["projectId"] in assignee_overrides,
            })

    # Second pass: one purely-manual row per extra assignee — never
    # auto-filled, just whatever's been saved in `overrides` (starting as
    # a copy of the normal row, made at the moment Split was clicked).
    for project_id, extras in extra_assignees.items():
        project = plannable_projects.get(project_id)
        if project is None:
            continue  # project closed/fully earned/etc. since the split was made
        for extra in extras:
            monthly = {month: (overrides.get((project_id, extra["id"], month)) or 0.0) for month in months}
            rows.append({
                "projectId": project_id,
                "projectName": project["name"],
                "rapportCode": project["rapportCode"],
                "country": project["country"],
                "userId": extra["id"],
                "userName": extra["name"],
                "remaining": project["remaining"],
                "monthly": monthly,
                "isSplit": True,
                "isExtraRow": True,
                "reassigned": False,
            })

    # Grouped by consultant (each person's own project list together),
    # not by project — a split project's two rows can therefore land far
    # apart when they belong to different consultants; the Split button
    # and reassign-on-a-split-row both compensate for that by scrolling
    # to and flashing the affected rows after the page reloads (see the
    # template's 'rp-jump-to-project' sessionStorage handoff).
    rows.sort(key=lambda r: (r["userName"].lower(), (r["projectName"] or "").lower()))
    return rows


def month_range(from_month: str, to_month: str, max_months: int = 24) -> list:
    """Every month from `from_month` to `to_month` inclusive. Swaps the
    two if given backwards, and caps the span so a mistyped year can't
    silently build a multi-century table."""
    if from_month > to_month:
        from_month, to_month = to_month, from_month
    months = [from_month]
    while months[-1] != to_month and len(months) < max_months:
        months.append(next_month_str(months[-1]))
    return months


def actual_hours_for_month(projects: list, timespent: list, month: str) -> list:
    """Actual hours logged in `month` ('YYYY-MM'), grouped by consultant
    and project — the historical counterpart to build_plan()'s forward-
    looking planned hours. One row per (consultant, project) that had any
    time logged that month."""
    return actual_hours_for_months(projects, timespent, [month])


def actual_hours_for_months(projects: list, timespent: list, months: list) -> list:
    """Like actual_hours_for_month(), but across several months at once —
    one row per (consultant, project) with an hours figure for each month
    in `months` (0 where nothing was logged that month)."""
    project_names = {p["id"]: {"name": p.get("name"), "rapportCode": p.get("rapportCode"), "country": p.get("country")} for p in projects}
    month_set = set(months)

    totals = {}
    for entry in timespent or []:
        spent_on = (entry.get("spent_on") or "").strip()
        entry_month = spent_on[:7]
        if entry_month not in month_set:
            continue
        user_id = entry.get("user_id")
        project_id = entry.get("project_id")
        if user_id is None or project_id is None:
            continue
        key = (user_id, project_id)
        if key not in totals:
            totals[key] = {
                "userId": user_id, "userName": entry.get("user_name") or "",
                "projectId": project_id,
                "projectName": (project_names.get(project_id) or {}).get("name") or f"Project #{project_id}",
                "rapportCode": (project_names.get(project_id) or {}).get("rapportCode"),
                "country": (project_names.get(project_id) or {}).get("country"),
                "monthly": {m: 0.0 for m in months},
                "hours": 0.0,
            }
        hours = to_float(entry.get("hours"))
        totals[key]["monthly"][entry_month] += hours
        totals[key]["hours"] += hours

    rows = list(totals.values())
    rows.sort(key=lambda r: (r["userName"].lower(), r["projectName"] or ""))
    return rows


def actual_consultant_summary(rows: list, months: list) -> list:
    """Per-consultant total actual hours for each month in `months`, plus
    capacity and utilization % per month — the historical counterpart to
    consultant_summary(). `rows` must come from actual_hours_for_months()
    with the same `months` list (each row's 'monthly' dict is summed)."""
    by_user = {}
    for r in rows:
        entry = by_user.setdefault(r["userId"], {"userId": r["userId"], "userName": r["userName"], "monthly": {m: 0.0 for m in months}})
        for month in months:
            entry["monthly"][month] += r["monthly"].get(month, 0.0)

    capacities = {month: capacity_hours(month) for month in months}
    summary = []
    for entry in by_user.values():
        utilization = {}
        for month in months:
            cap = capacities[month]
            utilization[month] = (entry["monthly"][month] / cap) if cap else 0.0
        summary.append({**entry, "capacity": capacities, "utilization": utilization})
    summary.sort(key=lambda e: e["userName"].lower())
    return summary


def build_utilization_workbook(months: list, rows: list, summary: list) -> bytes:
    """Renders the Utilization Report as an .xlsx: a 'Summary' sheet
    (consultant x month actual/capacity/utilization%) and a 'Detail'
    sheet (consultant x project x month actual hours)."""
    import io

    from openpyxl import Workbook
    from openpyxl.styles import Font
    from openpyxl.utils import get_column_letter

    month_labels = [format_month_label(m) for m in months]
    wb = Workbook()

    summary_ws = wb.active
    summary_ws.title = "Summary"
    header = ["Consultant"]
    for label in month_labels:
        header += [f"{label} Actual", f"{label} Capacity", f"{label} Utilization %"]
    summary_ws.append(header)
    bold = Font(bold=True)
    for cell in summary_ws[1]:
        cell.font = bold

    for entry in summary:
        row = [entry["userName"]]
        for month in months:
            row += [entry["monthly"][month], entry["capacity"][month], entry["utilization"][month]]
        summary_ws.append(row)

    for i, month in enumerate(months):
        col = get_column_letter(4 + i * 3)
        for row_idx in range(2, summary_ws.max_row + 1):
            summary_ws[f"{col}{row_idx}"].number_format = "0.0%"
    summary_ws.column_dimensions["A"].width = 24
    for i in range(2, 2 + len(months) * 3):
        summary_ws.column_dimensions[get_column_letter(i)].width = 16
    summary_ws.freeze_panes = "A2"

    detail_ws = wb.create_sheet("Detail")
    detail_ws.append(["Consultant", "Project", "Rapport Code", "Country"] + month_labels)
    for cell in detail_ws[1]:
        cell.font = bold
    for r in rows:
        detail_ws.append([r["userName"], r["projectName"], r["rapportCode"], r["country"]] + [r["monthly"].get(m, 0.0) for m in months])
    widths = [22, 45, 20, 12] + [14] * len(months)
    for i, width in enumerate(widths, start=1):
        detail_ws.column_dimensions[get_column_letter(i)].width = width
    detail_ws.freeze_panes = "A2"
    detail_ws.auto_filter.ref = f"A1:{get_column_letter(4 + len(months))}{detail_ws.max_row}"

    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def build_resource_plan_workbook(months: list, rows: list, summary: list) -> bytes:
    """Renders the Resource Planning page as an .xlsx: a 'Summary' sheet
    (consultant x month planned/capacity/utilization%) and a 'Detail'
    sheet (consultant x project x month planned hours plus remaining
    estimated hours)."""
    import io

    from openpyxl import Workbook
    from openpyxl.styles import Font
    from openpyxl.utils import get_column_letter

    month_labels = [format_month_label(m) for m in months]
    wb = Workbook()

    summary_ws = wb.active
    summary_ws.title = "Summary"
    header = ["Consultant"]
    for label in month_labels:
        header += [f"{label} Planned", f"{label} Capacity", f"{label} Utilization %"]
    summary_ws.append(header)
    bold = Font(bold=True)
    for cell in summary_ws[1]:
        cell.font = bold

    for entry in summary:
        row = [entry["userName"]]
        for month in months:
            row += [entry["monthly"][month], entry["capacity"][month], entry["utilization"][month]]
        summary_ws.append(row)

    for i, month in enumerate(months):
        col = get_column_letter(4 + i * 3)
        for row_idx in range(2, summary_ws.max_row + 1):
            summary_ws[f"{col}{row_idx}"].number_format = "0.0%"
    summary_ws.column_dimensions["A"].width = 24
    for i in range(2, 2 + len(months) * 3):
        summary_ws.column_dimensions[get_column_letter(i)].width = 16
    summary_ws.freeze_panes = "A2"

    detail_ws = wb.create_sheet("Detail")
    detail_ws.append(["Consultant", "Project", "Rapport Code", "Remaining Est. Hours"] + month_labels)
    for cell in detail_ws[1]:
        cell.font = bold
    for r in rows:
        detail_ws.append([r["userName"], r["projectName"], r["rapportCode"], r["remaining"]] + [r["monthly"].get(m, 0.0) for m in months])
    widths = [22, 45, 20, 16] + [14] * len(months)
    for i, width in enumerate(widths, start=1):
        detail_ws.column_dimensions[get_column_letter(i)].width = width
    detail_ws.freeze_panes = "A2"
    detail_ws.auto_filter.ref = f"A1:{get_column_letter(4 + len(months))}{detail_ws.max_row}"

    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def consultant_summary(rows: list, months: list) -> list:
    """Per-consultant totals for each month, plus capacity and
    utilization % — the roll-up shown at the top of the Resource
    Planning page."""
    by_user = {}
    for r in rows:
        entry = by_user.setdefault(r["userId"], {"userId": r["userId"], "userName": r["userName"], "monthly": {m: 0.0 for m in months}})
        for month in months:
            entry["monthly"][month] += r["monthly"].get(month, 0.0)

    capacities = {month: capacity_hours(month) for month in months}
    summary = []
    for entry in by_user.values():
        utilization = {}
        for month in months:
            planned = entry["monthly"][month]
            cap = capacities[month]
            utilization[month] = (planned / cap) if cap else 0.0
        summary.append({**entry, "capacity": capacities, "utilization": utilization})

    summary.sort(key=lambda e: e["userName"].lower())
    return summary
