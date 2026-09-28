import json
from datetime import datetime, timezone

from cryptography.fernet import InvalidToken

from . import crypto, db
from .logging_config import logger


def get_app_settings() -> dict:
    """The single shared Redmine URL/API key/query id every user's fetches
    now use — replaces what used to be per-user redmine_settings."""
    with db.get_db() as conn:
        row = conn.execute("SELECT * FROM app_settings WHERE id = 1").fetchone()
        if not row:
            return {"redmine_url": "", "api_key": "", "all_statuses_query_id": ""}
        api_key = ""
        if row["api_key_encrypted"]:
            try:
                api_key = crypto.decrypt(row["api_key_encrypted"])
            except InvalidToken:
                # The app secret (data/secret.key) that encrypted this no
                # longer matches what's loaded now — most likely that file
                # was lost/replaced since. There's no way to recover the
                # old value; treat it as "not set" rather than crashing
                # every page (App Settings, the scheduler, every refresh)
                # so an admin can at least load App Settings and re-enter
                # the key.
                logger.error(
                    "Stored Redmine API key could not be decrypted (app secret changed or was lost) — "
                    "treating it as unset. Re-enter the API key in App Settings."
                )
        return {
            "redmine_url": row["redmine_url"],
            "api_key": api_key,
            "all_statuses_query_id": row["all_statuses_query_id"],
        }


def get_auto_refresh_settings() -> dict:
    """The admin-configured auto-refresh schedule (single shared row, same
    as the rest of app_settings) — see app/scheduler.py for how this is
    interpreted. Times are all in the SERVER's local time zone (the
    schedule has to run consistently regardless of which admin's browser
    configured it)."""
    with db.get_db() as conn:
        row = conn.execute(
            "SELECT auto_refresh_mode, auto_refresh_interval_minutes, auto_refresh_time, "
            "auto_refresh_weekday, auto_refresh_day_of_month FROM app_settings WHERE id = 1"
        ).fetchone()
        if not row:
            return {
                "mode": "off", "interval_minutes": None, "time": None,
                "weekday": None, "day_of_month": None,
            }
        return {
            "mode": row["auto_refresh_mode"] or "off",
            "interval_minutes": row["auto_refresh_interval_minutes"],
            "time": row["auto_refresh_time"],
            "weekday": row["auto_refresh_weekday"],
            "day_of_month": row["auto_refresh_day_of_month"],
        }


def save_auto_refresh_settings(mode: str, interval_minutes, time_str, weekday, day_of_month):
    with db.get_db() as conn:
        conn.execute(
            """
            INSERT INTO app_settings (id, auto_refresh_mode, auto_refresh_interval_minutes, auto_refresh_time, auto_refresh_weekday, auto_refresh_day_of_month, updated_on)
            VALUES (1, ?, ?, ?, ?, ?, datetime('now'))
            ON CONFLICT(id) DO UPDATE SET
                auto_refresh_mode = excluded.auto_refresh_mode,
                auto_refresh_interval_minutes = excluded.auto_refresh_interval_minutes,
                auto_refresh_time = excluded.auto_refresh_time,
                auto_refresh_weekday = excluded.auto_refresh_weekday,
                auto_refresh_day_of_month = excluded.auto_refresh_day_of_month,
                updated_on = datetime('now')
            """,
            (mode, interval_minutes, time_str, weekday, day_of_month),
        )


def get_pp_report_excluded_project_ids_raw() -> str:
    """The raw comma-separated text as last saved (for redisplaying in the
    admin textbox) — use get_pp_report_excluded_project_ids() instead for
    actually filtering the PP Report."""
    with db.get_db() as conn:
        row = conn.execute("SELECT pp_report_excluded_project_ids FROM app_settings WHERE id = 1").fetchone()
        return row["pp_report_excluded_project_ids"] if row and row["pp_report_excluded_project_ids"] else ""


def get_pp_report_excluded_project_ids() -> list:
    """Redmine project ids to leave out of the PP Report entirely —
    parsed from the saved comma-separated text, silently skipping any
    non-numeric entry (already filtered out at save time, but defensive
    either way)."""
    raw = get_pp_report_excluded_project_ids_raw()
    ids = []
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        try:
            ids.append(int(part))
        except ValueError:
            continue
    return ids


def save_pp_report_excluded_project_ids(normalized_csv: str):
    with db.get_db() as conn:
        conn.execute(
            """
            INSERT INTO app_settings (id, pp_report_excluded_project_ids, updated_on)
            VALUES (1, ?, datetime('now'))
            ON CONFLICT(id) DO UPDATE SET
                pp_report_excluded_project_ids = excluded.pp_report_excluded_project_ids, updated_on = datetime('now')
            """,
            (normalized_csv,),
        )


def get_ksa_excluded_codes_raw() -> str:
    """Raw comma-separated KSA timesheet codes to treat as non-project time
    (leave, support, sales, …) — for redisplaying in the admin textbox."""
    with db.get_db() as conn:
        row = conn.execute("SELECT ksa_excluded_codes FROM app_settings WHERE id = 1").fetchone()
        return row["ksa_excluded_codes"] if row and row["ksa_excluded_codes"] else ""


def get_ksa_excluded_codes() -> set:
    """Set of KSA timesheet project codes (upper-cased) to exclude from Time
    Analysis — leave/support/sales etc. Parsed from the saved text."""
    raw = get_ksa_excluded_codes_raw()
    return {part.strip().upper() for part in raw.split(",") if part.strip()}


def save_ksa_excluded_codes(normalized_csv: str):
    with db.get_db() as conn:
        conn.execute(
            """
            INSERT INTO app_settings (id, ksa_excluded_codes, updated_on)
            VALUES (1, ?, datetime('now'))
            ON CONFLICT(id) DO UPDATE SET
                ksa_excluded_codes = excluded.ksa_excluded_codes, updated_on = datetime('now')
            """,
            (normalized_csv,),
        )


def get_pp_report_excluded_manager_ids_raw() -> str:
    """The raw comma-separated text as last saved (for redisplaying in the
    admin textbox) — use get_pp_report_excluded_manager_ids() instead for
    actually filtering the PP Report."""
    with db.get_db() as conn:
        row = conn.execute("SELECT pp_report_excluded_manager_ids FROM app_settings WHERE id = 1").fetchone()
        return row["pp_report_excluded_manager_ids"] if row and row["pp_report_excluded_manager_ids"] else ""


def get_pp_report_excluded_manager_ids() -> list:
    """Redmine Project Manager ids whose projects are left out of the PP
    Report entirely — parsed from the saved comma-separated text, silently
    skipping any non-numeric entry (already filtered out at save time, but
    defensive either way)."""
    raw = get_pp_report_excluded_manager_ids_raw()
    ids = []
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        try:
            ids.append(int(part))
        except ValueError:
            continue
    return ids


def save_pp_report_excluded_manager_ids(normalized_csv: str):
    with db.get_db() as conn:
        conn.execute(
            """
            INSERT INTO app_settings (id, pp_report_excluded_manager_ids, updated_on)
            VALUES (1, ?, datetime('now'))
            ON CONFLICT(id) DO UPDATE SET
                pp_report_excluded_manager_ids = excluded.pp_report_excluded_manager_ids, updated_on = datetime('now')
            """,
            (normalized_csv,),
        )


def save_app_settings(redmine_url: str, api_key: str, all_statuses_query_id: str):
    encrypted = crypto.encrypt(api_key) if api_key else ""
    with db.get_db() as conn:
        conn.execute(
            """
            INSERT INTO app_settings (id, redmine_url, api_key_encrypted, all_statuses_query_id, updated_on)
            VALUES (1, ?, ?, ?, datetime('now'))
            ON CONFLICT(id) DO UPDATE SET
                redmine_url = excluded.redmine_url,
                api_key_encrypted = CASE WHEN excluded.api_key_encrypted = '' THEN app_settings.api_key_encrypted ELSE excluded.api_key_encrypted END,
                all_statuses_query_id = excluded.all_statuses_query_id,
                updated_on = datetime('now')
            """,
            (redmine_url.rstrip("/"), encrypted, all_statuses_query_id),
        )


def get_user_prefs(user_id: int) -> dict:
    with db.get_db() as conn:
        row = conn.execute("SELECT my_team_ids, manager_id FROM user_prefs WHERE user_id = ?", (user_id,)).fetchone()
        if not row:
            return {"my_team_ids": "", "manager_id": ""}
        return {"my_team_ids": row["my_team_ids"], "manager_id": row["manager_id"]}


def save_user_prefs(user_id: int, my_team_ids: str, manager_id: str):
    with db.get_db() as conn:
        conn.execute(
            """
            INSERT INTO user_prefs (user_id, my_team_ids, manager_id, updated_on)
            VALUES (?, ?, ?, datetime('now'))
            ON CONFLICT(user_id) DO UPDATE SET
                my_team_ids = excluded.my_team_ids, manager_id = excluded.manager_id, updated_on = datetime('now')
            """,
            (user_id, my_team_ids.strip(), manager_id.strip()),
        )


def save_my_team(user_id: int, my_team_ids: str):
    """Self-service half of user_prefs — leaves manager_id untouched, since
    that's now admin-assigned only (see save_user_manager / /admin/users)."""
    with db.get_db() as conn:
        conn.execute(
            """
            INSERT INTO user_prefs (user_id, my_team_ids, updated_on)
            VALUES (?, ?, datetime('now'))
            ON CONFLICT(user_id) DO UPDATE SET
                my_team_ids = excluded.my_team_ids, updated_on = datetime('now')
            """,
            (user_id, my_team_ids.strip()),
        )


def save_user_manager(user_id: int, manager_id: str):
    """Admin-assigned Project Manager restriction for one user (see
    /admin/users) — leaves my_team_ids untouched. An empty manager_id means
    unrestricted (sees every project, same as today's behavior)."""
    with db.get_db() as conn:
        conn.execute(
            """
            INSERT INTO user_prefs (user_id, manager_id, updated_on)
            VALUES (?, ?, datetime('now'))
            ON CONFLICT(user_id) DO UPDATE SET
                manager_id = excluded.manager_id, updated_on = datetime('now')
            """,
            (user_id, manager_id.strip()),
        )


def save_cache(kind: str, data: list):
    """Shared cache — the last Redmine fetch, visible identically to
    every user (no more per-user copies)."""
    with db.get_db() as conn:
        conn.execute(
            """
            INSERT INTO fetch_cache (kind, data_json, fetched_on)
            VALUES (?, ?, ?)
            ON CONFLICT(kind) DO UPDATE SET data_json = excluded.data_json, fetched_on = excluded.fetched_on
            """,
            (kind, json.dumps(data), datetime.now(timezone.utc).isoformat()),
        )


def load_cache(kind: str):
    with db.get_db() as conn:
        row = conn.execute("SELECT data_json, fetched_on FROM fetch_cache WHERE kind = ?", (kind,)).fetchone()
        if not row:
            return [], None
        return json.loads(row["data_json"]), row["fetched_on"]


def _patch_cached_project(project_id: int, **fields):
    """Patch arbitrary fields on a single cached project (after a
    successful Redmine write) so the dashboard reflects the edit
    immediately, for everyone, without a full re-fetch. Preserves the
    existing 'fetched_on' timestamp — this isn't a fresh Redmine fetch."""
    projects, _fetched_on = load_cache("projects")
    found = False
    for p in projects:
        if p.get("id") == project_id:
            p.update(fields)
            found = True
            break
    if found:
        with db.get_db() as conn:
            conn.execute("UPDATE fetch_cache SET data_json = ? WHERE kind = 'projects'", (json.dumps(projects),))
    return found


def replace_cached_project(project_id: int, new_row: dict):
    """Wholesale-replaces one project's cached row (e.g. after a single-
    project re-fetch from Redmine — see redmine_client.fetch_single_project)
    — unlike _patch_cached_project, which only patches a few named fields.
    Preserves the cache's existing fetched_on, same reasoning as
    _patch_cached_project (this isn't a full Redmine refresh)."""
    projects, _fetched_on = load_cache("projects")
    found = False
    for i, p in enumerate(projects):
        if p.get("id") == project_id:
            projects[i] = new_row
            found = True
            break
    if not found:
        projects.append(new_row)
    with db.get_db() as conn:
        conn.execute("UPDATE fetch_cache SET data_json = ? WHERE kind = 'projects'", (json.dumps(projects),))


def replace_cached_timespent_for_project(project_id: int, new_entries: list):
    """Replaces every cached timespent row for one project with freshly
    fetched ones — paired with replace_cached_project() for a single-
    project refresh."""
    timespent, _fetched_on = load_cache("timespent")
    kept = [t for t in timespent if t.get("project_id") != project_id]
    kept.extend(new_entries)
    with db.get_db() as conn:
        conn.execute("UPDATE fetch_cache SET data_json = ? WHERE kind = 'timespent'", (json.dumps(kept),))


def update_cached_project_review(project_id: int, summary: str, comments: str, rev_date: str):
    return _patch_cached_project(project_id, summary=summary, comments=comments, revDate=rev_date)


def update_cached_project_fields(project_id: int, risk: str, order_amount: float):
    return _patch_cached_project(project_id, risk=risk, orderAmount=order_amount)


def try_start_running(message: str, started_by: str) -> bool:
    """Atomically flip the single shared status to 'running' only if it
    isn't already — a single UPDATE statement is atomic in SQLite,
    closing the race where two near-simultaneous /api/refresh calls
    (e.g. two different users clicking within the same instant) each
    check-then-set state separately and both proceed, firing two
    concurrent Redmine fetches. Returns True if this call started it,
    False if a fetch was already running (caller should not start
    another one)."""
    with db.get_db() as conn:
        conn.execute("INSERT OR IGNORE INTO fetch_status (id, state, message) VALUES (1, 'idle', '')")
        cur = conn.execute(
            "UPDATE fetch_status SET state = 'running', message = ?, started_by = ?, updated_on = datetime('now') "
            "WHERE id = 1 AND state != 'running'",
            (message, started_by),
        )
        return cur.rowcount > 0


def set_status(state: str, message: str = "", trigger: str = None):
    """trigger is 'manual' or 'auto' — only meaningful (and only stamps
    last_{trigger}_fetch_on) when state == 'done', so an error never
    overwrites the last time a refresh actually succeeded."""
    with db.get_db() as conn:
        conn.execute("INSERT OR IGNORE INTO fetch_status (id, state, message) VALUES (1, 'idle', '')")
        if state == "done" and trigger in ("manual", "auto"):
            now_iso = datetime.now(timezone.utc).isoformat()
            column = f"last_{trigger}_fetch_on"
            conn.execute(
                f"UPDATE fetch_status SET state = ?, message = ?, {column} = ?, updated_on = datetime('now') WHERE id = 1",
                (state, message, now_iso),
            )
        else:
            conn.execute(
                "UPDATE fetch_status SET state = ?, message = ?, updated_on = datetime('now') WHERE id = 1",
                (state, message),
            )


def list_countries():
    """Admin-configured Daily Rate per country, ordered alphabetically."""
    with db.get_db() as conn:
        rows = conn.execute("SELECT id, name, daily_rate, currency FROM countries ORDER BY name COLLATE NOCASE").fetchall()
        return [dict(r) for r in rows]


def create_country(name: str, daily_rate: float, currency: str):
    with db.get_db() as conn:
        conn.execute(
            "INSERT INTO countries (name, daily_rate, currency, updated_on) VALUES (?, ?, ?, datetime('now'))",
            (name.strip(), daily_rate, currency.strip()),
        )


def update_country(country_id: int, daily_rate: float, currency: str):
    with db.get_db() as conn:
        conn.execute(
            "UPDATE countries SET daily_rate = ?, currency = ?, updated_on = datetime('now') WHERE id = ?",
            (daily_rate, currency.strip(), country_id),
        )


def delete_country(country_id: int):
    with db.get_db() as conn:
        conn.execute("DELETE FROM countries WHERE id = ?", (country_id,))


def list_employees():
    """Every consultant with a saved hourly rate, ordered by name."""
    with db.get_db() as conn:
        rows = conn.execute(
            "SELECT redmine_user_id, redmine_user_name, hourly_rate FROM employees ORDER BY redmine_user_name COLLATE NOCASE"
        ).fetchall()
        return [dict(r) for r in rows]


def get_employee_rates() -> dict:
    """redmine_user_id -> hourly_rate, for every consultant with a saved
    rate. Not yet consumed by any report calculation — see the employees
    table's own comment in db.py."""
    with db.get_db() as conn:
        rows = conn.execute("SELECT redmine_user_id, hourly_rate FROM employees").fetchall()
        return {r["redmine_user_id"]: r["hourly_rate"] for r in rows}


def distinct_timesheet_users(timespent: list) -> list:
    """Every distinct Redmine user appearing in the cached time entries,
    as [{'id':, 'name':}], sorted by name — the roster the Employees admin
    screen offers rates against, since there's no separate Redmine 'users'
    endpoint cached here."""
    seen = {}
    for entry in timespent or []:
        user_id = entry.get("user_id")
        if user_id is None or user_id in seen:
            continue
        seen[user_id] = entry.get("user_name") or f"User #{user_id}"
    return sorted(({"id": uid, "name": name} for uid, name in seen.items()), key=lambda u: u["name"].lower())


def set_employee_rate(user_id: int, user_name: str, hourly_rate: float):
    with db.get_db() as conn:
        conn.execute(
            """
            INSERT INTO employees (redmine_user_id, redmine_user_name, hourly_rate, updated_on)
            VALUES (?, ?, ?, datetime('now'))
            ON CONFLICT(redmine_user_id) DO UPDATE SET
                redmine_user_name = excluded.redmine_user_name, hourly_rate = excluded.hourly_rate, updated_on = datetime('now')
            """,
            (user_id, user_name.strip(), hourly_rate),
        )


def delete_employee_rate(user_id: int):
    with db.get_db() as conn:
        conn.execute("DELETE FROM employees WHERE redmine_user_id = ?", (user_id,))


def get_status():
    with db.get_db() as conn:
        row = conn.execute(
            "SELECT state, message, started_by, updated_on, last_auto_fetch_on, last_manual_fetch_on FROM fetch_status WHERE id = 1"
        ).fetchone()
        if not row:
            return {
                "state": "idle", "message": "", "started_by": "", "updated_on": "",
                "last_auto_fetch_on": None, "last_manual_fetch_on": None,
            }
        return dict(row)
