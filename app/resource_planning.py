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


def clear_all_plan() -> dict:
    """Wipe every saved planning override so the plan reverts to the pure
    auto-generated state: planned-hour cells (resource_plan), manual
    reassignments (resource_plan_assignee) and split rows
    (resource_plan_extra_assignee). Returns the row counts removed."""
    with db.get_db() as conn:
        counts = {}
        for table in ("resource_plan", "resource_plan_assignee", "resource_plan_extra_assignee"):
            counts[table] = conn.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()["n"]
            conn.execute(f"DELETE FROM {table}")
    return counts


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


def apply_imported_plan(cells: list, uid_name: dict, months: list) -> dict:
    """Persist an imported plan AND make every imported (project, consultant)
    pair actually appear on the grid.

    The grid shows one assignee per project (its reassignment override, else the
    most-recent logger, else the PM) plus any registered split rows. So saving
    hours for an arbitrary consultant isn't enough — if they aren't that
    project's assignee, their row never renders (and never exports). This
    registers them: per project, the consultant with the most imported hours
    becomes the assignee, and every other imported consultant on that project is
    added as a split row.

    Every window month is written for each imported pair (0 where the file had
    no value), so a project reassigned to a new consultant doesn't auto-fill the
    months the file didn't mention — the imported plan is taken literally.
    `cells` is a parse_resource_plan_workbook() result; `uid_name` maps user id
    -> display name. Returns a small stats dict."""
    from collections import defaultdict
    per = defaultdict(lambda: defaultdict(lambda: {m: 0.0 for m in months}))  # pid -> uid -> {month: hours}
    for c in cells:
        if c["month"] in months:
            per[c["project_id"]][c["user_id"]][c["month"]] = c["hours"]

    # Persist every window month for each imported pair (0 where unspecified),
    # so the reassigned/normal row never auto-fills a blank month.
    full_cells = [
        {"project_id": pid, "user_id": uid, "month": m, "hours": monthly[m]}
        for pid, users in per.items() for uid, monthly in users.items() for m in months
    ]
    save_planned_hours_batch(full_cells)

    assignees = splits = 0
    for pid, users in per.items():
        # Primary = whoever has the most imported hours (stable tie-break).
        primary = max(users, key=lambda u: (sum(users[u].values()), -u))
        save_assignee_override(pid, primary, uid_name.get(primary, ""))
        assignees += 1
        for uid, monthly in users.items():
            if uid == primary:
                continue
            add_extra_assignee(pid, uid, uid_name.get(uid, ""), monthly)
            splits += 1
    return {"projects": len(per), "assignees": assignees, "splits": splits}


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
        is_tm = bool(p.get("isTM"))
        estimated = p.get("est") or 0.0
        spent = p.get("spent") or 0.0
        remaining = estimated - spent
        # Open projects stay on the plan even once their estimate is used up
        # (remaining <= 0): the team may still need to log more time against
        # them, so they must remain plannable. Nothing is auto-filled for them
        # (the capacity-fill loop below allocates 0 when remaining <= 0), but
        # the row appears so hours can be entered by hand. T&M projects are
        # kept too (they used to be dropped as "always remaining == 0"); they
        # have no fixed-budget cap, so they're shown but never flagged
        # over-budget — see overBudget below. Only Closed projects are left out.

        assignee = assignee_overrides.get(p["id"]) or _most_recent_assignee(p["id"], timespent)
        if assignee is None:
            # Fall back to the project manager when no one has logged time yet
            mgr_id = p.get("managerId")
            mgr_name = p.get("managerName") or ""
            if mgr_id:
                assignee = {"id": mgr_id, "name": mgr_name}
            else:
                continue

        plannable_projects[p["id"]] = {"name": p.get("name"), "rapportCode": p.get("rapportCode"), "country": p.get("country"), "remaining": remaining, "est": estimated, "spent": spent, "isTM": is_tm}
        by_user.setdefault(assignee["id"], {"userName": assignee["name"], "projects": []})["projects"].append({
            "projectId": p["id"], "projectName": p.get("name"), "rapportCode": p.get("rapportCode"),
            "country": p.get("country"), "remaining": remaining, "est": estimated, "spent": spent, "isTM": is_tm,
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
                "est": pr["est"],
                "spent": pr["spent"],
                "isTM": pr["isTM"],
                "overBudget": (not pr["isTM"]) and pr["est"] > 0 and pr["spent"] >= pr["est"],
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
                "est": project["est"],
                "spent": project["spent"],
                "isTM": project["isTM"],
                "overBudget": (not project["isTM"]) and project["est"] > 0 and project["spent"] >= project["est"],
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


# ---------------------------------------------------------------------------
# Locking a month's plan as a baseline, to compare against actual hours later
# ---------------------------------------------------------------------------

def lock_month(month: str, rows: list, locked_by: str = "") -> int:
    """Freeze the current plan's `month` column as a baseline snapshot — one
    entry per (project, consultant) with a non-zero planned figure that month.
    `rows` is a build_plan() result. Re-locking replaces the snapshot. Returns
    the number of planned entries captured."""
    import json
    snap = []
    for r in rows:
        planned = float(r.get("monthly", {}).get(month) or 0)
        if planned <= 0:
            continue
        snap.append({
            "projectId": r["projectId"], "projectName": r.get("projectName"),
            "rapportCode": r.get("rapportCode"), "country": r.get("country"),
            "userId": r["userId"], "userName": r.get("userName"),
            "planned": planned,
        })
    payload = json.dumps({"rows": snap})
    with db.get_db() as conn:
        conn.execute(
            """
            INSERT INTO resource_plan_lock (month, snapshot_json, locked_on, locked_by)
            VALUES (?, ?, datetime('now'), ?)
            ON CONFLICT(month) DO UPDATE SET
                snapshot_json = excluded.snapshot_json, locked_on = datetime('now'),
                locked_by = excluded.locked_by
            """,
            (month, payload, locked_by or ""),
        )
    return len(snap)


def unlock_month(month: str):
    with db.get_db() as conn:
        conn.execute("DELETE FROM resource_plan_lock WHERE month = ?", (month,))


def get_locked_months() -> list:
    """All locked months (newest first) with their metadata and entry count."""
    import json
    with db.get_db() as conn:
        rows = conn.execute(
            "SELECT month, snapshot_json, locked_on, locked_by FROM resource_plan_lock ORDER BY month DESC"
        ).fetchall()
    out = []
    for r in rows:
        try:
            n = len(json.loads(r["snapshot_json"]).get("rows", []))
        except (ValueError, TypeError):
            n = 0
        out.append({"month": r["month"], "label": format_month_label(r["month"]),
                    "locked_on": r["locked_on"], "locked_by": r["locked_by"], "entries": n})
    return out


def get_lock(month: str) -> dict | None:
    import json
    with db.get_db() as conn:
        r = conn.execute(
            "SELECT month, snapshot_json, locked_on, locked_by FROM resource_plan_lock WHERE month = ?",
            (month,),
        ).fetchone()
    if not r:
        return None
    try:
        data = json.loads(r["snapshot_json"])
    except (ValueError, TypeError):
        data = {"rows": []}
    return {"month": r["month"], "label": format_month_label(r["month"]),
            "locked_on": r["locked_on"], "locked_by": r["locked_by"],
            "rows": data.get("rows", [])}


def build_lock_comparison(month: str, projects: list, timespent: list) -> dict | None:
    """Compare the locked plan for `month` against the actual hours logged that
    month, per consultant. Returns None if the month isn't locked. Each row:
    planned (from the lock), actual (from timespent), variance and utilization;
    plus firm-wide totals."""
    lock = get_lock(month)
    if lock is None:
        return None

    planned_by_user = {}
    planned_detail = {}  # (userId, projectId) -> planned
    for s in lock["rows"]:
        planned_by_user[s["userId"]] = planned_by_user.get(s["userId"], 0.0) + float(s["planned"] or 0)
        planned_detail[(s["userId"], s["projectId"])] = {
            "projectName": s.get("projectName"), "rapportCode": s.get("rapportCode"),
            "userName": s.get("userName"), "planned": float(s["planned"] or 0), "actual": 0.0,
        }

    actual_rows = actual_hours_for_months(projects, timespent, [month])
    actual_by_user = {}
    names = {}
    for a in actual_rows:
        uid = a["userId"]
        act = float(a["monthly"].get(month) or 0)
        actual_by_user[uid] = actual_by_user.get(uid, 0.0) + act
        names[uid] = a.get("userName") or names.get(uid, "")
        key = (uid, a["projectId"])
        if key in planned_detail:
            planned_detail[key]["actual"] += act
        else:
            planned_detail[key] = {"projectName": a.get("projectName"), "rapportCode": a.get("rapportCode"),
                                   "userName": a.get("userName"), "planned": 0.0, "actual": act}

    cap = capacity_hours(month)
    user_ids = set(planned_by_user) | set(actual_by_user)
    rows = []
    for uid in user_ids:
        planned = round(planned_by_user.get(uid, 0.0), 1)
        actual = round(actual_by_user.get(uid, 0.0), 1)
        name = names.get(uid) or next((s["userName"] for s in lock["rows"] if s["userId"] == uid), "") or f"User #{uid}"
        rows.append({
            "userId": uid, "userName": name,
            "planned": planned, "actual": actual, "variance": round(actual - planned, 1),
            "planned_pct": round(100 * planned / cap) if cap else 0,
            "actual_pct": round(100 * actual / cap) if cap else 0,
        })
    rows.sort(key=lambda r: r["userName"].lower())

    detail = []
    for (uid, pid), d in planned_detail.items():
        detail.append({**d, "userId": uid, "projectId": pid,
                       "planned": round(d["planned"], 1), "actual": round(d["actual"], 1),
                       "variance": round(d["actual"] - d["planned"], 1)})
    detail.sort(key=lambda d: ((d["userName"] or "").lower(), (d["projectName"] or "").lower()))

    totals = {
        "planned": round(sum(r["planned"] for r in rows), 1),
        "actual": round(sum(r["actual"] for r in rows), 1),
    }
    totals["variance"] = round(totals["actual"] - totals["planned"], 1)
    return {"month": month, "label": lock["label"], "locked_on": lock["locked_on"],
            "locked_by": lock["locked_by"], "capacity": cap,
            "rows": rows, "detail": detail, "totals": totals}


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
    # Project ID / User ID are carried (as the first two columns) so the file
    # can be edited and re-imported with exact matching — see
    # parse_resource_plan_workbook. Edit only the month columns; leave the ids,
    # names and header row intact.
    detail_ws.append(["Project ID", "User ID", "Consultant", "Project", "Rapport Code", "Remaining Est. Hours"] + month_labels)
    for cell in detail_ws[1]:
        cell.font = bold
    for r in rows:
        detail_ws.append([r["projectId"], r["userId"], r["userName"], r["projectName"], r["rapportCode"], r["remaining"]] + [r["monthly"].get(m, 0.0) for m in months])
    widths = [10, 9, 22, 45, 20, 16] + [14] * len(months)
    for i, width in enumerate(widths, start=1):
        detail_ws.column_dimensions[get_column_letter(i)].width = width
    detail_ws.freeze_panes = "A2"
    detail_ws.auto_filter.ref = f"A1:{get_column_letter(6 + len(months))}{detail_ws.max_row}"

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


class ImportError_(Exception):
    """Raised by parse_resource_plan_workbook when the file can't be read."""


def _norm_text(v) -> str:
    return " ".join(str(v or "").strip().lower().split())


_MONTH_ABBR = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], start=1)}


def _match_header_month(value, months: list, label_to_month: dict):
    """Resolve one spreadsheet header cell to a planning month key, tolerating
    the various shapes real files use: the exact export label ('Oct 2026'), a
    real Excel date cell (datetime), or a bare month name ('Oct', 'October').
    A bare month name maps to the single window month with that month number
    (unambiguous because the window is <= 12 months). Returns the month key
    ('YYYY-MM') or None."""
    import datetime as _dt

    if isinstance(value, (_dt.datetime, _dt.date)):
        key = f"{value.year:04d}-{value.month:02d}"
        return key if key in months else None

    s = str(value or "").strip()
    if not s:
        return None
    if s in label_to_month:
        return label_to_month[s]

    # Bare month name / abbreviation (no year) -> the window month with that
    # month number, if exactly one.
    abbr = s[:3].lower()
    mnum = _MONTH_ABBR.get(abbr)
    if mnum:
        hits = [m for m in months if int(m[5:7]) == mnum]
        if len(hits) == 1:
            return hits[0]
    return None


def parse_resource_plan_workbook(file_bytes: bytes, months: list,
                                 project_by_code: dict = None,
                                 project_by_name: dict = None,
                                 user_by_name: dict = None) -> tuple:
    """Read an exported Resource Planning .xlsx back into planned-hours cells.

    Reads the 'Detail' sheet (as produced by build_resource_plan_workbook) and
    supports both export formats:

      * New: 'Project ID' + 'User ID' columns → exact matching.
      * Legacy / hand-edited: no id columns, matched instead by 'Rapport Code'
        (falling back to 'Project' name) and the 'Consultant' name — using the
        resolver maps passed in (all keyed by _norm_text): project_by_code and
        project_by_name map to a project id, user_by_name maps a consultant
        name to a Redmine user id.

    Only month columns whose header label matches one of `months` are imported,
    so a file exported for a different window still imports the overlapping
    months. Rows that can't be resolved to both a project and a user are
    skipped and counted.

    Returns (cells, stats); stats = {rows, cells, skipped, unresolved_project,
    unresolved_user, months_matched, mode}."""
    import io

    from openpyxl import load_workbook

    try:
        wb = load_workbook(io.BytesIO(file_bytes), data_only=True, read_only=True)
    except Exception as e:
        raise ImportError_(f"Couldn't open the file as an Excel workbook: {e}")

    ws = wb["Detail"] if "Detail" in wb.sheetnames else wb.active
    rows_iter = ws.iter_rows(values_only=True)
    try:
        header = next(rows_iter)
    except StopIteration:
        raise ImportError_("The sheet is empty.")

    label_to_month = {format_month_label(m): m for m in months}
    # Accept common header spellings so hand-built sheets import too.
    ALIASES = {
        "project id": "Project ID", "user id": "User ID",
        "rapport code": "Rapport Code", "project code": "Rapport Code", "code": "Rapport Code",
        "project": "Project", "project name": "Project",
        "consultant": "Consultant", "resource": "Consultant", "ressource": "Consultant",
    }
    col_month = {}
    cols = {}  # canonical column name -> index
    for idx, h in enumerate(header):
        canon = ALIASES.get(_norm_text(h))
        if canon and canon not in cols:
            cols[canon] = idx
            continue
        m = _match_header_month(h, months, label_to_month)
        if m is not None and idx not in col_month:
            col_month[idx] = m

    if not col_month:
        raise ImportError_(
            "None of the month columns in the file match the current planning "
            "window (" + ", ".join(label_to_month) + "). The month headers can "
            "be 'Oct 2026', a date, or just 'Oct'.")

    id_mode = "Project ID" in cols and "User ID" in cols
    if not id_mode:
        # Legacy / hand-edited file — need something to match on.
        if "Consultant" not in cols or ("Rapport Code" not in cols and "Project" not in cols):
            raise ImportError_(
                "This sheet needs either 'Project ID' + 'User ID' columns, or a "
                "consultant column (Consultant/Resource) plus a code column "
                "(Rapport Code/Project Code) or a Project name column to match "
                "on. Found columns: " + ", ".join(str(h) for h in header if h) + ".")
        project_by_code = project_by_code or {}
        project_by_name = project_by_name or {}
        user_by_name = user_by_name or {}

    def _to_int(v):
        try:
            return int(float(v))
        except (TypeError, ValueError):
            return None

    def _to_hours(v):
        if v in (None, ""):
            return 0.0
        try:
            return max(0.0, float(v))
        except (TypeError, ValueError):
            return 0.0

    def _cell(raw, name):
        idx = cols.get(name)
        return raw[idx] if idx is not None and idx < len(raw) else None

    cells = []
    seen_rows = skipped = unresolved_project = unresolved_user = 0
    for raw in rows_iter:
        if raw is None or not any(c not in (None, "") for c in raw):
            continue

        if id_mode:
            pid = _to_int(_cell(raw, "Project ID"))
            uid = _to_int(_cell(raw, "User ID"))
        else:
            code = _norm_text(_cell(raw, "Rapport Code"))
            name = _norm_text(_cell(raw, "Project"))
            pid = (project_by_code.get(code) if code else None)
            if pid is None and name:
                pid = project_by_name.get(name)
            uid = user_by_name.get(_norm_text(_cell(raw, "Consultant")))

        if pid is None:
            unresolved_project += 1; skipped += 1; continue
        if uid is None:
            unresolved_user += 1; skipped += 1; continue

        seen_rows += 1
        for col, month in col_month.items():
            hours = _to_hours(raw[col]) if col < len(raw) else 0.0
            cells.append({"project_id": pid, "user_id": uid, "month": month, "hours": hours})

    wb.close()
    return cells, {"rows": seen_rows, "cells": len(cells), "skipped": skipped,
                   "unresolved_project": unresolved_project,
                   "unresolved_user": unresolved_user,
                   "months_matched": len(col_month),
                   "mode": "id" if id_mode else "name"}
