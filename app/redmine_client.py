"""Redmine fetch logic — adapted from the original ERM redmine_status.py.

Difference from the original: base_url/api_key are passed in per-call
(the caller's own Redmine credentials), never read from environment
variables, so multiple users can fetch with their own API keys from the
same running server.
"""

import re
import time
from datetime import date

import requests

from .logging_config import logger

CUSTOM_FIELD_MAP = {
    "rapport_code": "Rapports code (PEP)",
    "risk": "Potential Risk",
    "last_rev_summary": "Last rev. summary",
    "last_rev_date": "Last rev. date",
    "last_rev_comments": "Last rev. comments",
    "sap_order": "SAP Order",
    "contract_type": "Contract type",
    "est_time": None,
    "spent_time": None,
}

# Fixed custom field ids on this EasyRedmine instance (redmine.seidor.es) —
# confirmed from the payload EasyRedmine's own "toggle custom fields on
# project" form posts (project[project_custom_field_ids][]=...). Custom
# field ids are global to the Redmine instance; only whether a given
# project has them *enabled* varies per project. If this app is ever
# pointed at a different Redmine instance, these will need updating
# (check Administration > Custom fields, or capture the same form's
# payload again).
REVIEW_FIELD_IDS = {
    "last_rev_date": 292,
    "last_rev_summary": 294,
    "last_rev_comments": 291,
}

# Risk (id=293) and SAP Order (id=273) are documented directly in the
# original redmine_status.py script's own comments next to CUSTOM_FIELD_MAP
# ("matches 'Potential Risk ' (id=293)", "custom field id=273, format=string")
# — confirmed again by both ids appearing in the same captured EasyRedmine
# form payload as the review fields above.
EDITABLE_FIELD_IDS = dict(REVIEW_FIELD_IDS, risk=293, sap_order=273)

# Country custom field id=108 — note its Redmine field *name* is the
# unrelated-looking "SAP IMPLE solutions, version" (a mislabeled/repurposed
# field on this instance), so it's looked up by id, never by name.
COUNTRY_FIELD_ID = 108

# Country derivation fallback when the field above is empty/not yet set:
# checked against the first 3 characters of the project's Rapport Code
# (e.g. "KWB1-..." -> Kuwait). Order matters only in the unlikely case a
# prefix could match more than one marker.
_COUNTRY_CODE_MARKERS = [("KW", "Kuwait"), ("AE", "UAE"), ("LB", "Lebanon")]


def derive_country_from_rapport_code(rapport_code: str) -> str:
    prefix = (rapport_code or "").strip().upper()[:3]
    for marker, country in _COUNTRY_CODE_MARKERS:
        if marker in prefix:
            return country
    return ""


def _raw_field_value_by_id(project_data, field_id):
    """Like custom_field_value(), but matches by custom field id instead
    of name — needed for fields whose Redmine *name* doesn't reflect what
    they're actually used for (see COUNTRY_FIELD_ID)."""
    for cf in project_data.get("custom_fields", []):
        if cf.get("id") == field_id:
            val = cf.get("value")
            if isinstance(val, list):
                return ", ".join(strip_html(str(v)) for v in val)
            if val is None:
                return ""
            return strip_html(str(val))
    return ""

TM_CONTRACT_TYPE_VALUE = "36"
ALL_STATUSES_QUERY_ID_DEFAULT = "52"
CLOSED_STATUS_ID = 5  # Redmine's built-in "Closed" project status

BROWSER_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

_HTML_TAG_RE = re.compile(r"<[^>]+>")


class RedmineError(Exception):
    pass


def api_get(base_url, api_key, path, params=None):
    params = dict(params or {})
    session_headers = {"Accept": "application/json", "User-Agent": BROWSER_USER_AGENT}

    headers = dict(session_headers, **{"X-Redmine-API-Key": api_key})
    resp = requests.get(f"{base_url}{path}", headers=headers, params=params, timeout=30)

    if resp.status_code in (401, 403) and "key" not in params:
        fallback_params = dict(params, key=api_key)
        resp = requests.get(f"{base_url}{path}", headers=session_headers, params=fallback_params, timeout=30)

    if resp.status_code == 401:
        raise RedmineError("Redmine rejected the API key (401 Unauthorized). Check the API key in Settings.")
    if resp.status_code == 403:
        raise RedmineError("Access forbidden (403) even after retrying. Check the Redmine URL and API key permissions.")
    if resp.status_code == 404:
        raise RedmineError(f"Not found (404) for {path}. Check the Redmine URL in Settings.")
    resp.raise_for_status()
    return resp.json()


def strip_html(value):
    if not isinstance(value, str):
        return value
    return _HTML_TAG_RE.sub("", value).strip()


def custom_field_value(project_data, field_name):
    if not field_name:
        return ""
    target = field_name.strip()
    for cf in project_data.get("custom_fields", []):
        if (cf.get("name") or "").strip() == target:
            val = cf.get("value")
            if isinstance(val, list):
                return ", ".join(strip_html(str(v)) for v in val)
            if val is None:
                return ""
            return strip_html(str(val))
    return ""


def fetch_all_projects(base_url, api_key, query_id):
    projects = []
    offset = 0
    limit = 100
    params_base = {"limit": limit, "include": "custom_fields"}
    if query_id:
        params_base["query_id"] = query_id
    while True:
        params = dict(params_base, offset=offset)
        data = api_get(base_url, api_key, "/projects.json", params=params)
        batch = data.get("projects", [])
        projects.extend(batch)
        total_count = data.get("total_count", len(projects))
        offset += limit
        if offset >= total_count or not batch:
            break
    return projects


def fetch_all_issues_grouped(base_url, api_key):
    """Paginates /issues.json once for the WHOLE instance (no project_id
    filter) and groups the results by project id — replaces looping
    fetch_all_issues(project_id) once per project, which meant one
    call-chain per project (~420 of them) for overhead-dominated round
    trips that didn't scale with actual data volume. Confirmed safe for
    this instance: its total issue count is small and entirely relevant
    (no unrelated projects diluting the fetch)."""
    by_project = {}
    offset = 0
    limit = 100
    while True:
        data = api_get(
            base_url, api_key, "/issues.json",
            params={"status_id": "*", "limit": limit, "offset": offset},
        )
        batch = data.get("issues", [])
        for issue in batch:
            project_id = (issue.get("project") or {}).get("id")
            by_project.setdefault(project_id, []).append(issue)
        total_count = data.get("total_count", offset + len(batch))
        offset += limit
        if offset >= total_count or not batch:
            break
    return by_project


def fetch_all_time_entries_grouped(base_url, api_key):
    """Instance-wide equivalent of fetch_all_issues_grouped() for
    /time_entries.json — see that function's docstring for why."""
    by_project = {}
    offset = 0
    limit = 100
    while True:
        data = api_get(
            base_url, api_key, "/time_entries.json",
            params={"user_id": "*", "limit": limit, "offset": offset},
        )
        batch = data.get("time_entries", [])
        for entry in batch:
            project_id = (entry.get("project") or {}).get("id")
            by_project.setdefault(project_id, []).append(entry)
        total_count = data.get("total_count", offset + len(batch))
        offset += limit
        if offset >= total_count or not batch:
            break
    return by_project


def compute_estimate_total(issues):
    return sum(issue.get("estimated_hours") or 0 for issue in issues)


def compute_spent_total(time_entries):
    return sum(te.get("hours") or 0 for te in time_entries)


def compute_display_order_amount(order_amount_raw, is_tm, spent):
    """The Dashboard's 'Order amount' column isn't always the raw SAP
    Order custom field value: for Time & Material projects, that field
    stores a *daily rate* instead, so the displayed figure is derived as
    rate * spent / 8 (8 working hours/day). Shared between the full
    Redmine fetch (build_report) and the editable-fields save flow, so
    editing Risk/Order amount from the Dashboard recomputes the same way
    a full refresh would, without needing to refetch spent from Redmine."""
    if is_tm:
        try:
            daily_rate = float(order_amount_raw) if order_amount_raw not in (None, "") else None
        except ValueError:
            daily_rate = None
        return round(daily_rate * spent / 8, 2) if daily_rate is not None else _to_float(order_amount_raw)
    return _to_float(order_amount_raw)


def build_report(base_url, api_key, query_id, excluded_ids=None):
    """Fetch everything and return (projects_json, timespent_json) already
    shaped for the dashboard template (same shape generate_daily_snapshot.py
    used to build from CSV)."""
    excluded_ids = excluded_ids or set()

    refresh_started = time.perf_counter()
    t0 = time.perf_counter()
    projects = fetch_all_projects(base_url, api_key, query_id)
    projects_fetch_seconds = time.perf_counter() - t0
    projects = [p for p in projects if p.get("id") not in excluded_ids]
    logger.info("Refresh timing — projects list: %.2fs (%d projects)", projects_fetch_seconds, len(projects))

    # Instance-wide, paginated once each — replaces one call-chain per
    # project (~420 of them) with a handful of large pages, grouped by
    # project id below. Confirmed for this instance: its total issue/time
    # entry counts are small and entirely relevant (no unrelated projects
    # diluting the fetch), so this is a straight win, not a trade-off.
    t0 = time.perf_counter()
    issues_by_project = fetch_all_issues_grouped(base_url, api_key)
    issues_fetch_seconds = time.perf_counter() - t0
    total_issues = sum(len(v) for v in issues_by_project.values())
    logger.info("Refresh timing — issues fetch (instance-wide): %.2fs (%d issues)", issues_fetch_seconds, total_issues)

    t0 = time.perf_counter()
    time_entries_by_project = fetch_all_time_entries_grouped(base_url, api_key)
    timespent_fetch_seconds = time.perf_counter() - t0
    total_time_entries = sum(len(v) for v in time_entries_by_project.values())
    logger.info("Refresh timing — spent time fetch (instance-wide): %.2fs (%d time entries)", timespent_fetch_seconds, total_time_entries)

    project_rows = []
    timespent_rows = []
    country_derive_seconds = 0.0

    for p in projects:
        issues = issues_by_project.get(p["id"], [])
        time_entries = time_entries_by_project.get(p["id"], [])
        row, row_timespent, derive_seconds = _build_project_row(p, issues, time_entries, base_url, api_key)
        project_rows.append(row)
        timespent_rows.extend(row_timespent)
        country_derive_seconds += derive_seconds

    # The project list comes from a saved query (which can omit internal /
    # activity / support projects), but time entries are fetched instance-wide —
    # so some projects have logged hours yet no row here, and their time would
    # land in Time Analysis with no country/manager. Pull those missing
    # projects' metadata too (one extra full-project fetch, used only as a
    # lookup) and build their rows, so every project that has hours is imported
    # with its country. Admin-excluded projects stay out.
    fetched_ids = {p["id"] for p in projects}
    orphan_ids = [pid for pid in time_entries_by_project
                  if pid and pid not in fetched_ids and pid not in excluded_ids]
    if orphan_ids:
        t0 = time.perf_counter()
        all_by_id = {p["id"]: p for p in fetch_all_projects(base_url, api_key, None)}
        found = 0
        for pid in orphan_ids:
            p = all_by_id.get(pid)
            if not p:
                continue
            found += 1
            issues = issues_by_project.get(pid, [])
            time_entries = time_entries_by_project.get(pid, [])
            row, row_timespent, derive_seconds = _build_project_row(p, issues, time_entries, base_url, api_key)
            project_rows.append(row)
            timespent_rows.extend(row_timespent)
            country_derive_seconds += derive_seconds
        logger.info("Refresh timing — imported %d project(s) that had time entries but weren't in the query: %.2fs",
                    found, time.perf_counter() - t0)

    logger.info("Refresh timing — derive country from rapport code (open projects only): %.2fs", country_derive_seconds)
    logger.info("Refresh timing — total: %.2fs", time.perf_counter() - refresh_started)

    return project_rows, timespent_rows


def _build_project_row(p, issues, time_entries, base_url, api_key, spent_override=None):
    """Builds one project's dashboard-shaped row (plus its timespent rows)
    from raw Redmine project/issues/time-entries data — the per-project body
    of build_report()'s loop, factored out so fetch_single_project() (a
    one-off re-fetch of just one project, e.g. the Dashboard's "Refresh this
    project" button) can reuse the exact same logic instead of duplicating
    it. Returns (project_row, timespent_rows, country_derive_seconds).

    spent_override: when given (fetch_single_project's fast path — see its
    docstring for why Spent isn't re-fetched there), Spent is taken from
    this value instead of summed from `time_entries`, and timespent_rows is
    returned as None to tell the caller not to touch the cached timespent
    rows for this project (they're left exactly as the last full refresh
    left them)."""
    est_computed = round(compute_estimate_total(issues), 2)

    if spent_override is not None:
        spent_computed = spent_override
        timespent_rows = None
    else:
        spent_computed = round(compute_spent_total(time_entries), 2)
        timespent_rows = []
        for te in time_entries:
            timespent_rows.append({
                "id": te.get("id"),
                "project_id": (te.get("project") or {}).get("id"),
                "project_name": (te.get("project") or {}).get("name") or p.get("name"),
                "issue_id": (te.get("issue") or {}).get("id"),
                "user_id": (te.get("user") or {}).get("id"),
                "user_name": (te.get("user") or {}).get("name"),
                "activity_name": (te.get("activity") or {}).get("name"),
                "hours": te.get("hours") or 0,
                "spent_on": te.get("spent_on") or "",
                "comments": te.get("comments") or "",
            })

    manager = p.get("manager") or {}
    contract_type = custom_field_value(p, CUSTOM_FIELD_MAP.get("contract_type"))
    is_tm = contract_type.strip() == TM_CONTRACT_TYPE_VALUE

    order_amount_raw = custom_field_value(p, CUSTOM_FIELD_MAP.get("sap_order"))
    est_val = est_computed
    spent_val = spent_computed
    if is_tm:
        est_val = spent_val
    order_amount = compute_display_order_amount(order_amount_raw, is_tm, spent_val)

    risk_raw = custom_field_value(p, CUSTOM_FIELD_MAP.get("risk"))
    risk_num = _to_float(risk_raw, default=None)
    remaining = round(est_val + (risk_num or 0) - spent_val, 1)

    rapport_code = custom_field_value(p, CUSTOM_FIELD_MAP.get("rapport_code"))
    country = _raw_field_value_by_id(p, COUNTRY_FIELD_ID).strip()
    country_derive_seconds = 0.0
    # Closed projects can't be edited in Redmine — any write attempt
    # gets rejected (and this same rejection would repeat on every
    # single future refresh, for every closed project, at ~1s/call
    # overhead each), so skip the country check/derivation/write-back
    # entirely for them rather than pay that cost for no possible
    # benefit.
    if not country and rapport_code and p.get("status") != CLOSED_STATUS_ID:
        # Never overwrites an existing value — only fills in when the
        # field is genuinely empty or not yet enabled for this project.
        # (An empty rapport code can never derive a country anyway —
        # derive_country_from_rapport_code() would just return "" — so
        # this is checked explicitly rather than relying on that.)
        derived_country = derive_country_from_rapport_code(rapport_code)
        if derived_country:
            country = derived_country
            t0 = time.perf_counter()
            try:
                _write_custom_fields(base_url, api_key, p["id"], {COUNTRY_FIELD_ID: derived_country})
            except RedmineError:
                # Keep the derived value for display even if writing
                # it back to Redmine failed (e.g. no 'Manage project'
                # rights to enable the field on this project) — a
                # future refresh will retry once permissions allow it.
                pass
            finally:
                country_derive_seconds += time.perf_counter() - t0

    row = {
        "id": p.get("id"),
        "name": p.get("name"),
        "country": country,
        "rapportCode": rapport_code,
        "orderAmount": order_amount,
        "est": est_val,
        "isTM": is_tm,
        "spent": spent_val,
        "pct": round((spent_val / est_val * 100), 1) if est_val else 0,
        "risk": risk_raw,
        "remaining": remaining,
        "status": "Active" if p.get("status") == 1 else ("Closed" if p.get("status") == CLOSED_STATUS_ID else str(p.get("status"))),
        "managerId": manager.get("id", 0) or 0,
        "managerName": manager.get("name", ""),
        "summary": custom_field_value(p, CUSTOM_FIELD_MAP.get("last_rev_summary")),
        "revDate": custom_field_value(p, CUSTOM_FIELD_MAP.get("last_rev_date")),
        "comments": custom_field_value(p, CUSTOM_FIELD_MAP.get("last_rev_comments")),
    }
    return row, timespent_rows, country_derive_seconds


def fetch_single_project(base_url, api_key, project_id, keep_spent):
    """Re-fetches ONE project's Estimated/Country/Manager/Status/Risk/SAP
    Order from Redmine — for the Dashboard's "Refresh this project" action,
    so a user chasing a single-project discrepancy (e.g. against an
    external report) doesn't have to wait for a full instance-wide refresh.
    Returns (project_row, timespent_rows) — timespent_rows is always None
    here (see below), so the caller knows not to touch the cached timespent
    rows for this project.

    Deliberately does NOT re-fetch Spent/time entries: this Redmine
    instance's /time_entries.json silently ignores every filter tried
    (project_id, issue_id, even a single issue_id) and returns zero results
    rather than erroring, so getting a correct Spent figure here would mean
    fetching the *entire* instance's time entries (fetch_all_time_entries_
    grouped(), the same thing a full refresh already does) — several
    minutes, defeating the point of a fast single-project refresh. Spent is
    instead carried over unchanged from whatever the last full refresh
    cached (`keep_spent`, passed by the caller from the current cache) —
    accurate as of that refresh, just not re-verified here. Run a full
    "Refresh from Redmine" to get a current Spent figure."""
    data = api_get(base_url, api_key, f"/projects/{project_id}.json", params={"include": "custom_fields"})
    p = data.get("project")
    if not p:
        raise RedmineError(f"Project #{project_id} not found in Redmine.")

    issues = []
    offset = 0
    limit = 100
    while True:
        resp = api_get(base_url, api_key, "/issues.json", params={"project_id": project_id, "status_id": "*", "limit": limit, "offset": offset})
        batch = resp.get("issues", [])
        issues.extend(batch)
        total_count = resp.get("total_count", offset + len(batch))
        offset += limit
        if offset >= total_count or not batch:
            break

    row, timespent_rows, _derive_seconds = _build_project_row(p, issues, [], base_url, api_key, spent_override=keep_spent or 0)
    return row, timespent_rows


def _to_float(val, default=0.0):
    if val is None:
        return default
    val = str(val).strip()
    if not val:
        return default
    try:
        return float(val)
    except ValueError:
        return default


def test_connection(base_url, api_key):
    """Lightweight check used by the Settings page's 'Test connection' button."""
    api_get(base_url, api_key, "/projects.json", params={"limit": 1})
    return True


def api_put(base_url, api_key, path, json_body):
    session_headers = {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "User-Agent": BROWSER_USER_AGENT,
    }

    headers = dict(session_headers, **{"X-Redmine-API-Key": api_key})
    resp = requests.put(f"{base_url}{path}", headers=headers, json=json_body, timeout=30)

    if resp.status_code in (401, 403):
        fallback_resp = requests.put(
            f"{base_url}{path}", headers=session_headers, params={"key": api_key}, json=json_body, timeout=30,
        )
        resp = fallback_resp

    if resp.status_code == 401:
        raise RedmineError("Redmine rejected the API key (401 Unauthorized) while saving. Check the API key in Settings.")
    if resp.status_code == 403:
        raise RedmineError(
            "Access forbidden (403) while saving — your Redmine API key may only have read access. "
            "Ask your Redmine admin to grant your account 'Edit project' permission to save reviews from here."
        )
    if resp.status_code == 404:
        raise RedmineError(f"Not found (404) for {path}.")
    if resp.status_code >= 400:
        raise RedmineError(f"Redmine rejected the update (HTTP {resp.status_code}): {(resp.text or '')[:300]}")
    return resp


def _fetch_project_custom_fields(base_url, api_key, project_id):
    project_data = api_get(base_url, api_key, f"/projects/{project_id}.json", params={"include": "custom_fields"})
    return project_data.get("project", {}).get("custom_fields", [])


def _enable_project_custom_fields(base_url, api_key, project_id, existing_ids, field_ids_to_add):
    """Turn on the given custom field ids for this project by writing
    EasyRedmine's `project_custom_field_ids` list — the same attribute its
    own 'toggle custom fields on project' form submits, gated by that
    project's 'Manage project' permission rather than site-wide Redmine
    Administrator. Sent through the standard PUT /projects/{id}.json
    endpoint (not EasyRedmine's own form route, which is CSRF/session
    protected and not usable with a plain API key).

    NOTE: unverified against a live EasyRedmine instance — if this
    instance's REST API doesn't expose project_custom_field_ids the same
    way the web form does, this PUT will silently have no effect and the
    caller's post-check will raise a clear error instead of failing
    silently."""
    desired_ids = sorted(set(existing_ids) | set(field_ids_to_add))
    api_put(base_url, api_key, f"/projects/{project_id}.json", {"project": {"project_custom_field_ids": desired_ids}})


def _write_custom_fields(base_url, api_key, project_id, updates_by_id: dict):
    """Write a set of {custom_field_id: value} to a project in one PUT.
    Redmine merges custom field updates by id rather than replacing the
    whole custom_fields list, so every field NOT in updates_by_id (Rapport
    code, other fields, ...) is left untouched.

    If a field isn't enabled for this project yet (EasyRedmine lets
    project custom fields be toggled per project), this tries to enable it
    via the project's own project_custom_field_ids list before giving up —
    that only requires 'Manage project' rights on this project, not full
    Redmine Admin (see _enable_project_custom_fields)."""
    existing_fields = _fetch_project_custom_fields(base_url, api_key, project_id)
    existing_ids = {cf.get("id") for cf in existing_fields}

    missing_ids = [fid for fid in updates_by_id if fid not in existing_ids]

    if missing_ids:
        _enable_project_custom_fields(base_url, api_key, project_id, existing_ids, missing_ids)
        existing_fields = _fetch_project_custom_fields(base_url, api_key, project_id)
        existing_ids = {cf.get("id") for cf in existing_fields}
        still_missing = [fid for fid in missing_ids if fid not in existing_ids]
        if still_missing:
            id_to_name = {v: k for k, v in EDITABLE_FIELD_IDS.items()}
            names = ", ".join(id_to_name.get(fid, str(fid)) for fid in still_missing)
            raise RedmineError(
                f"Could not enable custom field(s) {names} for project #{project_id}. "
                "Your Redmine account needs 'Manage project' rights on this project, or enable them "
                "manually in the project's own settings (the same screen used to toggle custom fields "
                "per project)."
            )

    custom_fields_payload = [{"id": fid, "value": value} for fid, value in updates_by_id.items()]
    api_put(base_url, api_key, f"/projects/{project_id}.json", {"project": {"custom_fields": custom_fields_payload}})


def get_project_editable_fields(base_url, api_key, project_id):
    """Fetch a project's CURRENT raw Risk and SAP Order values straight
    from Redmine (not from any cache) — used to populate the Dashboard's
    edit modal accurately. This matters especially for SAP Order: the
    Dashboard displays a T&M-transformed figure (see
    compute_display_order_amount), not the raw field, so editing must
    start from the real underlying value rather than the transformed one
    to avoid silently corrupting a T&M project's daily rate."""
    project_data = api_get(base_url, api_key, f"/projects/{project_id}.json", params={"include": "custom_fields"})
    project = project_data.get("project", {})
    contract_type = custom_field_value(project, CUSTOM_FIELD_MAP.get("contract_type"))
    return {
        "risk": custom_field_value(project, CUSTOM_FIELD_MAP.get("risk")),
        "sapOrder": custom_field_value(project, CUSTOM_FIELD_MAP.get("sap_order")),
        "isTM": contract_type.strip() == TM_CONTRACT_TYPE_VALUE,
        # Fetched fresh alongside Risk/SAP Order (not from cache) so the
        # save step can diff against Redmine's actual current values,
        # not whatever was last cached.
        "summary": custom_field_value(project, CUSTOM_FIELD_MAP.get("last_rev_summary")),
        "comments": custom_field_value(project, CUSTOM_FIELD_MAP.get("last_rev_comments")),
        "revDate": custom_field_value(project, CUSTOM_FIELD_MAP.get("last_rev_date")),
    }


def update_project_review(base_url, api_key, project_id, summary, comments, current, risk=None, sap_order=None):
    """Write only the fields that actually changed back to Redmine, diffed
    against `current` (a get_project_editable_fields() result fetched just
    before this call). 'Last rev. date' is only touched — set to today —
    when the summary or comments text actually changed; editing Risk or
    SAP Order alone leaves it untouched, since those aren't "the review".
    If nothing changed at all, no Redmine call is made."""
    updates_by_id = {}

    summary_changed = summary != (current.get("summary") or "")
    comments_changed = comments != (current.get("comments") or "")
    if summary_changed:
        updates_by_id[REVIEW_FIELD_IDS["last_rev_summary"]] = summary
    if comments_changed:
        updates_by_id[REVIEW_FIELD_IDS["last_rev_comments"]] = comments

    rev_date = current.get("revDate") or ""
    if summary_changed or comments_changed:
        rev_date = date.today().isoformat()
        updates_by_id[REVIEW_FIELD_IDS["last_rev_date"]] = rev_date

    if risk is not None and risk != (current.get("risk") or ""):
        updates_by_id[EDITABLE_FIELD_IDS["risk"]] = risk
    if sap_order is not None and sap_order != (current.get("sapOrder") or ""):
        updates_by_id[EDITABLE_FIELD_IDS["sap_order"]] = sap_order

    if updates_by_id:
        _write_custom_fields(base_url, api_key, project_id, updates_by_id)

    result = {"summary": summary, "comments": comments, "revDate": rev_date}
    if risk is not None:
        result["risk"] = risk
    if sap_order is not None:
        result["sapOrderRaw"] = sap_order
    return result
