import asyncio
import json
import os
import re
import sqlite3

import jinja2
from urllib.parse import quote

from fastapi import BackgroundTasks, FastAPI, File, Form, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse, Response
from pydantic import BaseModel

from . import analysis, auth, data_quality, db, digest, mailer, openrouter_client, pp_report, resource_planning, scheduler, settings_store
from .logging_config import LOG_PATH, logger, setup_logging
from .redmine_client import (
    EDITABLE_FIELD_IDS,
    RedmineError,
    compute_display_order_amount,
    fetch_single_project,
    get_project_editable_fields,
    test_connection,
    update_project_review,
    _write_custom_fields,
)
from .refresh import run_refresh

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
TEMPLATE_PATH = os.path.join(BASE_DIR, "templates", "dashboard_template.html")

app = FastAPI(title="ERM Project Ledger")

# A plain jinja2.Environment, rather than Starlette's Jinja2Templates
# wrapper — the wrapper's template cache hits a weakref/LRUCache
# incompatibility under newer Python (observed on 3.14).
_jinja_env = jinja2.Environment(
    loader=jinja2.FileSystemLoader(os.path.join(BASE_DIR, "templates")),
    autoescape=True,
)


# Which left-rail nav item to highlight, keyed by the template being
# rendered. Injected centrally (below) so every page lights up the right
# icon without each route having to pass it.
NAV_BY_TEMPLATE = {
    "dashboard_shell.html": "dashboard",
    "analysis.html": "analysis",
    "pp_report.html": "pp-report",
    "resource_planning.html": "resource",
    "utilization_report.html": "utilization",
    "settings.html": "settings",
    "app_settings.html": "app-settings",
    "countries.html": "countries",
    "employees.html": "employees",
    "pp_report_months.html": "pp-data",
    "data_quality.html": "data-quality",
    "users.html": "users",
}


# OpenRouter's model list is public but a touch slow to fetch — cache it
# in-process for an hour so re-rendering App Settings doesn't re-hit the network.
_OR_MODELS_CACHE = {"ts": 0.0, "models": []}


def _openrouter_models():
    import time
    if not _OR_MODELS_CACHE["models"] or (time.time() - _OR_MODELS_CACHE["ts"]) > 3600:
        _OR_MODELS_CACHE["models"] = openrouter_client.fetch_models()
        _OR_MODELS_CACHE["ts"] = time.time()
    return _OR_MODELS_CACHE["models"]


class templates:
    @staticmethod
    def TemplateResponse(name: str, context: dict, status_code: int = 200):
        context = dict(context)
        context.pop("request", None)
        context.setdefault("nav_active", NAV_BY_TEMPLATE.get(name))
        if name == "app_settings.html":
            ors = settings_store.get_openrouter_settings()
            context.setdefault("openrouter", {"model": ors["model"], "api_key": "•" * 12 if ors["api_key"] else ""})
            smtp = settings_store.get_smtp_settings()
            context.setdefault("smtp", {**smtp, "password": "•" * 12 if smtp["password"] else ""})
            context.setdefault("digest", settings_store.get_digest_settings())
            context.setdefault("digest_recipient_count", len(digest.recipients()))
        if name == "pp_report_months.html":
            # The KSA upload panel lives on this page, which is rendered from
            # several routes — inject its data here instead of in each one.
            context.setdefault("ksa_months", pp_report.list_ksa_months())
            context.setdefault("ksa_timesheet_months", pp_report.list_ksa_timesheet_months())
            context.setdefault("default_ksa_month", pp_report.next_month_to_lock())
            context.setdefault("ksa_excluded_codes", settings_store.get_ksa_excluded_codes_raw())
            context.setdefault("consultant_name_map", settings_store.get_consultant_name_map_raw())
        html = _jinja_env.get_template(name).render(**context)
        return HTMLResponse(html, status_code=status_code)


@app.on_event("startup")
async def startup():
    setup_logging()
    db.init_db()
    logger.info("Application startup (db=%s)", db.DB_PATH)

    # A refresh that was genuinely in progress when the app crashed or
    # restarted leaves fetch_status stuck at 'running' forever otherwise —
    # nothing else ever clears it, and try_start_running()'s atomic check
    # then permanently refuses every future refresh attempt (manual or
    # scheduled), since "running" never becomes untrue on its own. Any
    # 'running' state found at this fresh process's own startup is
    # necessarily stale — this same process hasn't started a refresh yet —
    # so it's safe to self-heal here unconditionally.
    stale = settings_store.get_status()
    if stale["state"] == "running":
        settings_store.set_status(
            "error",
            "Previous refresh was interrupted by an application restart — click Refresh to try again.",
        )
        logger.warning(
            "Refresh was stuck 'running' at startup (started by %r) — reset to 'error' so future refreshes aren't blocked.",
            stale.get("started_by") or "unknown",
        )

    asyncio.create_task(scheduler.run_forever())

    generated_password = auth.seed_admin_if_missing()
    if generated_password:
        banner = (
            "\n"
            + "=" * 70 + "\n"
            "  ADMIN ACCOUNT CREATED\n"
            f"    username: admin\n"
            f"    password: {generated_password}\n"
            "  Save this now and change it after logging in — it will not be shown again.\n"
            + "=" * 70 + "\n"
        )
        print(banner, flush=True)
        logger.info("Admin account 'admin' created on first startup (password shown once on console, not logged).")


def _assigned_manager_id(user: dict) -> str:
    """The Redmine Project Manager id an admin has restricted this user to
    (user_prefs.manager_id — see /admin/users), or '' if unrestricted.
    Admins are never restricted, regardless of what's stored."""
    if user["is_admin"]:
        return ""
    return settings_store.get_user_prefs(user["id"])["manager_id"]


def _visible_projects(projects: list, user: dict) -> list:
    """Hard-restricts a regular user to only the projects of their
    admin-assigned Project Manager (see _assigned_manager_id) — other PMs'
    projects never reach this user's browser. Admins, and any user nobody
    has restricted yet, see every project (unrestricted is the default so
    rolling this out doesn't silently lock out existing users)."""
    manager_id = _assigned_manager_id(user)
    if not manager_id:
        return projects
    return [p for p in projects if str(p.get("managerId") or "") == manager_id]


def _visible_timespent(timespent: list, visible_project_ids: set) -> list:
    return [t for t in timespent if t.get("project_id") in visible_project_ids]


def _drop_excluded_managers(projects: list) -> list:
    """Admin-configured 'Excluded Project Managers' (see /admin/pp-report-months)
    — applied on the Dashboard too, not just the PP Report (pp_report.py's
    build_report applies the same list independently), so an excluded PM's
    projects disappear everywhere consistently rather than just from the
    monthly report."""
    excluded_manager_ids = set(settings_store.get_pp_report_excluded_manager_ids())
    if not excluded_manager_ids:
        return projects
    return [p for p in projects if (p.get("managerId") or 0) not in excluded_manager_ids]


def _drop_excluded_projects(projects: list) -> list:
    """Admin-configured 'Excluded projects' (see /admin/pp-report-months) —
    same fix as _drop_excluded_managers above: applied on the Dashboard too,
    not just the PP Report, so an excluded project disappears everywhere
    consistently."""
    excluded_ids = set(settings_store.get_pp_report_excluded_project_ids())
    if not excluded_ids:
        return projects
    return [p for p in projects if p["id"] not in excluded_ids]


def _project_is_visible(project_id: int, user: dict) -> bool:
    """Guards the project-edit API endpoints (fields/review) against a
    restricted user reaching a project outside their assigned PM by calling
    the API directly, bypassing the Dashboard's already-filtered list."""
    manager_id = _assigned_manager_id(user)
    if not manager_id:
        return True
    projects, _ = settings_store.load_cache("projects")
    project = next((p for p in projects if p.get("id") == project_id), None)
    return bool(project) and str(project.get("managerId") or "") == manager_id


def _visible_report_rows(rows: list, user: dict) -> list:
    """Same restriction as _visible_projects, applied to already-built PP
    Report rows — needed on top of filtering the input project list, since
    a LOCKED month's rows come from a frozen snapshot (build_report ignores
    the project list entirely once a month is locked)."""
    manager_id = _assigned_manager_id(user)
    if not manager_id:
        return rows
    return [r for r in rows if str(r.get("managerId") or "") == manager_id]


def _refresh_timing_context() -> dict:
    """Shared by App Settings, Dashboard, and PP Report — last auto-fetch/
    manual-fetch timestamps plus the next scheduled run, all as UTC ISO
    strings (templates convert to the viewer's local time client-side)."""
    fetch_status = settings_store.get_status()
    auto_refresh = settings_store.get_auto_refresh_settings()
    return {
        "fetch_status": fetch_status,
        "auto_refresh": auto_refresh,
        "next_fetch_iso": scheduler.compute_next_run_iso(auto_refresh, fetch_status.get("last_auto_fetch_on")),
    }


def current_user(request: Request):
    cookie = request.cookies.get(auth.COOKIE_NAME)
    uid = auth.read_session_cookie(cookie)
    if not uid:
        return None
    return auth.get_user(uid)


def require_login(request: Request):
    user = current_user(request)
    if not user:
        return None
    return user


# ---------------------------------------------------------------------------
# Auth routes
# ---------------------------------------------------------------------------

@app.get("/", response_class=HTMLResponse)
def index(request: Request):
    user = current_user(request)
    if user:
        return RedirectResponse("/dashboard")
    return RedirectResponse("/login")


@app.get("/login", response_class=HTMLResponse)
def login_form(request: Request):
    return templates.TemplateResponse("login.html", {"request": request, "user": None, "error": None})


@app.post("/login", response_class=HTMLResponse)
def login_submit(request: Request, username: str = Form(...), password: str = Form(...)):
    user = auth.authenticate(username.strip(), password)
    if not user:
        logger.warning("Failed login attempt for %r", username.strip())
        return templates.TemplateResponse("login.html", {"request": request, "user": None, "error": "Invalid username or password."})
    logger.info("Login: %r (id=%s)", user["username"], user["id"])
    resp = RedirectResponse("/dashboard", status_code=302)
    resp.set_cookie(auth.COOKIE_NAME, auth.make_session_cookie(user["id"]), httponly=True, samesite="lax", max_age=auth.MAX_AGE_SECONDS)
    return resp


@app.get("/logout")
def logout():
    resp = RedirectResponse("/login", status_code=302)
    resp.delete_cookie(auth.COOKIE_NAME)
    return resp


# ---------------------------------------------------------------------------
# Settings — per-user preferences (My Team, Project Manager id)
# ---------------------------------------------------------------------------

@app.get("/settings", response_class=HTMLResponse)
def settings_form(request: Request):
    user = require_login(request)
    if not user:
        return RedirectResponse("/login")
    prefs = settings_store.get_user_prefs(user["id"])
    return templates.TemplateResponse("settings.html", {"request": request, "user": user, "prefs": prefs, "error": None, "ok": None})


@app.post("/settings", response_class=HTMLResponse)
def settings_submit(request: Request, my_team_ids: str = Form(""), email: str = Form("")):
    user = require_login(request)
    if not user:
        return RedirectResponse("/login")

    my_team_ids = my_team_ids.strip()
    email = email.strip()

    def render(error=None, ok=None):
        prefs = settings_store.get_user_prefs(user["id"])
        prefs["my_team_ids"] = my_team_ids if error else prefs["my_team_ids"]
        return templates.TemplateResponse("settings.html", {"request": request, "user": auth.get_user(user["id"]), "prefs": prefs, "error": error, "ok": ok})

    if my_team_ids and not all(part.strip().isdigit() for part in my_team_ids.split(",") if part.strip()):
        return render(error="My Team must be comma-separated numeric Redmine user ids (e.g. 3252,1048,2077).")
    if email and "@" not in email:
        return render(error="That doesn't look like a valid email address.")

    settings_store.save_my_team(user["id"], my_team_ids)
    auth.set_email(user["id"], email)
    logger.info("Preferences saved for user %r (my_team_ids=%r, email set=%s)", user["username"], my_team_ids, bool(email))
    return render(ok="Settings saved.")


@app.post("/settings/password", response_class=HTMLResponse)
def settings_change_password(request: Request, current_password: str = Form(""), new_password: str = Form(""), confirm_password: str = Form("")):
    user = require_login(request)
    if not user:
        return RedirectResponse("/login")

    def render(error=None, ok=None):
        prefs = settings_store.get_user_prefs(user["id"])
        return templates.TemplateResponse("settings.html", {"request": request, "user": auth.get_user(user["id"]), "prefs": prefs, "error": error, "ok": ok})

    if new_password != confirm_password:
        return render(error="New password and confirmation don't match.")
    try:
        auth.change_password(user["id"], current_password, new_password)
    except ValueError as e:
        return render(error=str(e))
    logger.info("Password changed by user %r", user["username"])
    return render(ok="Password changed.")


# ---------------------------------------------------------------------------
# App settings — shared Redmine connection used by every user's fetches
# ---------------------------------------------------------------------------

@app.get("/app-settings", response_class=HTMLResponse)
def app_settings_form(request: Request):
    user = require_login(request)
    if not user:
        return RedirectResponse("/login")
    if not user["is_admin"]:
        return RedirectResponse("/dashboard")
    settings = settings_store.get_app_settings()
    settings["api_key"] = "•" * 12 if settings["api_key"] else ""
    return templates.TemplateResponse("app_settings.html", {
        "request": request, "user": user, "settings": settings, "error": None, "ok": None,
        **_refresh_timing_context(),
    })


@app.post("/app-settings/digest", response_class=HTMLResponse)
def app_settings_digest_submit(request: Request, digest_enabled: str = Form(""), digest_weekday: str = Form("0"),
                               digest_time: str = Form("08:00"), action: str = Form("")):
    """Save the weekly-digest schedule, or send it now."""
    user = require_login(request)
    if not user:
        return RedirectResponse("/login")
    if not user["is_admin"]:
        return RedirectResponse("/dashboard")

    settings = settings_store.get_app_settings()
    settings["api_key"] = "•" * 12 if settings["api_key"] else ""
    base = {"request": request, "user": user, "settings": settings, **_refresh_timing_context()}

    if action == "send":
        try:
            result = digest.send_digest()
            return templates.TemplateResponse("app_settings.html", {**base, "error": None, "ok": f"Digest sent to {result['sent']} recipient(s)."})
        except Exception as e:
            return templates.TemplateResponse("app_settings.html", {**base, "error": f"Could not send digest: {e}", "ok": None})

    try:
        weekday = int(digest_weekday)
    except ValueError:
        weekday = 0
    settings_store.save_digest_settings(bool(digest_enabled), weekday, digest_time)
    logger.info("Digest schedule saved by %r (enabled=%s, weekday=%s, time=%r)", user["username"], bool(digest_enabled), weekday, digest_time)
    return templates.TemplateResponse("app_settings.html", {**base, "error": None, "ok": "Digest schedule saved."})


@app.post("/app-settings/smtp", response_class=HTMLResponse)
def app_settings_smtp_submit(request: Request, smtp_host: str = Form(""), smtp_port: str = Form("587"),
                             smtp_username: str = Form(""), smtp_password: str = Form(""),
                             smtp_from: str = Form(""), smtp_use_tls: str = Form(""),
                             test_to: str = Form(""), action: str = Form("")):
    """Save (or test) the outgoing-email (SMTP) settings for alerts/digests."""
    user = require_login(request)
    if not user:
        return RedirectResponse("/login")
    if not user["is_admin"]:
        return RedirectResponse("/dashboard")

    settings = settings_store.get_app_settings()
    settings["api_key"] = "•" * 12 if settings["api_key"] else ""
    base = {"request": request, "user": user, "settings": settings, **_refresh_timing_context()}
    try:
        port = int(smtp_port or 587)
    except ValueError:
        port = 587

    if action == "test":
        # Save first (so the test uses what's on screen), then send.
        settings_store.save_smtp_settings(smtp_host, port, smtp_username, smtp_password, smtp_from, bool(smtp_use_tls))
        ok_send, msg = mailer.test_send((test_to or user.get("email") or "").strip())
        return templates.TemplateResponse("app_settings.html", {**base, "error": None if ok_send else msg, "ok": msg if ok_send else None})

    settings_store.save_smtp_settings(smtp_host, port, smtp_username, smtp_password, smtp_from, bool(smtp_use_tls))
    logger.info("SMTP settings saved by %r (host=%r)", user["username"], smtp_host.strip())
    return templates.TemplateResponse("app_settings.html", {**base, "error": None, "ok": "Email (SMTP) settings saved."})


@app.post("/app-settings/openrouter", response_class=HTMLResponse)
def app_settings_openrouter_submit(request: Request, openrouter_api_key: str = Form(""), openrouter_model: str = Form(""), action: str = Form("")):
    """Save (or test) the OpenRouter API key + model used for AI features. The
    key is stored encrypted; leaving it blank keeps the saved one. 'Test' only
    validates the key (no tokens spent)."""
    user = require_login(request)
    if not user:
        return RedirectResponse("/login")
    if not user["is_admin"]:
        return RedirectResponse("/dashboard")

    settings = settings_store.get_app_settings()
    settings["api_key"] = "•" * 12 if settings["api_key"] else ""
    base = {"request": request, "user": user, "settings": settings, **_refresh_timing_context()}

    if action == "test":
        existing = settings_store.get_openrouter_settings()
        key_to_test = openrouter_api_key.strip() or existing["api_key"]
        ok_conn, msg = openrouter_client.test_connection(key_to_test)
        return templates.TemplateResponse("app_settings.html", {**base, "error": None if ok_conn else msg, "ok": msg if ok_conn else None})

    settings_store.save_openrouter_settings(openrouter_api_key.strip(), openrouter_model)
    logger.info("OpenRouter settings saved by %r (model=%r, key %s)", user["username"], openrouter_model.strip(), "updated" if openrouter_api_key.strip() else "unchanged")
    return templates.TemplateResponse("app_settings.html", {**base, "error": None, "ok": "OpenRouter settings saved."})


@app.post("/app-settings/reset-stuck-refresh", response_class=HTMLResponse)
def app_settings_reset_stuck_refresh(request: Request):
    """Manual admin escape hatch for a refresh stuck at 'running' while the
    app is still alive (e.g. a hung Redmine connection) — the startup
    self-heal (see the 'startup' handler) only catches a crash/restart, not
    this case, since the process never actually stops."""
    user = require_login(request)
    if not user:
        return RedirectResponse("/login")
    if not user["is_admin"]:
        return RedirectResponse("/dashboard")

    current = settings_store.get_status()
    ok, error = None, None
    if current["state"] != "running":
        error = "No refresh is currently running."
    else:
        settings_store.set_status("error", "Refresh manually stopped by an admin — click Refresh to try again.")
        logger.warning("Stuck refresh (started by %r) manually reset by admin %r", current.get("started_by") or "unknown", user["username"])
        ok = "Stuck refresh cleared — Refresh can be tried again now."

    settings = settings_store.get_app_settings()
    settings["api_key"] = "•" * 12 if settings["api_key"] else ""
    return templates.TemplateResponse("app_settings.html", {
        "request": request, "user": user, "settings": settings, "error": error, "ok": ok,
        **_refresh_timing_context(),
    })


@app.post("/app-settings/auto-refresh", response_class=HTMLResponse)
def app_settings_auto_refresh_submit(
    request: Request,
    auto_refresh_mode: str = Form("off"),
    auto_refresh_interval_minutes: str = Form(""),
    auto_refresh_time: str = Form(""),
    auto_refresh_weekday: str = Form(""),
    auto_refresh_day_of_month: str = Form(""),
):
    user = require_login(request)
    if not user:
        return RedirectResponse("/login")
    if not user["is_admin"]:
        return RedirectResponse("/dashboard")

    error = None
    if auto_refresh_mode not in ("off", "interval", "daily", "weekly", "monthly"):
        error = "Unrecognized auto-refresh mode."
    interval_minutes = None
    weekday = None
    day_of_month = None
    try:
        if auto_refresh_mode == "interval":
            interval_minutes = int(auto_refresh_interval_minutes)
            if interval_minutes < 1:
                raise ValueError
        if auto_refresh_mode == "weekly":
            weekday = int(auto_refresh_weekday)
            if not (0 <= weekday <= 6):
                raise ValueError
        if auto_refresh_mode == "monthly":
            day_of_month = int(auto_refresh_day_of_month)
            if not (1 <= day_of_month <= 31):
                raise ValueError
    except (TypeError, ValueError):
        error = "Please fill in a valid value for the selected frequency."

    if not error and auto_refresh_mode in ("daily", "weekly", "monthly") and not auto_refresh_time:
        error = "Please choose a time of day."

    if not error:
        settings_store.save_auto_refresh_settings(
            auto_refresh_mode, interval_minutes,
            auto_refresh_time or None, weekday, day_of_month,
        )
        logger.info("Auto-refresh schedule saved by %r (%s)", user["username"], auto_refresh_mode)

    settings = settings_store.get_app_settings()
    settings["api_key"] = "•" * 12 if settings["api_key"] else ""
    return templates.TemplateResponse("app_settings.html", {
        "request": request, "user": user, "settings": settings,
        "error": error, "ok": None if error else "Auto-refresh schedule saved.",
        **_refresh_timing_context(),
    })


@app.post("/app-settings", response_class=HTMLResponse)
def app_settings_submit(
    request: Request,
    redmine_url: str = Form(...),
    api_key: str = Form(""),
    all_statuses_query_id: str = Form(""),
    action: str = Form(""),
):
    user = require_login(request)
    if not user:
        return RedirectResponse("/login")
    if not user["is_admin"]:
        return RedirectResponse("/dashboard")

    redmine_url = redmine_url.strip().rstrip("/")
    all_statuses_query_id = all_statuses_query_id.strip()

    if action == "test":
        existing = settings_store.get_app_settings()
        key_to_test = api_key.strip() or existing["api_key"]
        try:
            test_connection(redmine_url, key_to_test)
            ok, error = "Connection successful.", None
            logger.info("App settings test OK by %r (%s)", user["username"], redmine_url)
        except RedmineError as e:
            ok, error = None, str(e)
            logger.warning("App settings test failed by %r (%s): %s", user["username"], redmine_url, e)
        except Exception as e:
            ok, error = None, f"Connection failed: {e}"
            logger.exception("App settings test error by %r (%s)", user["username"], redmine_url)
        settings = {"redmine_url": redmine_url, "api_key": "•" * 12 if key_to_test else "", "all_statuses_query_id": all_statuses_query_id}
        return templates.TemplateResponse("app_settings.html", {
            "request": request, "user": user, "settings": settings, "error": error, "ok": ok,
            **_refresh_timing_context(),
        })

    settings_store.save_app_settings(redmine_url, api_key.strip(), all_statuses_query_id)
    logger.info("App settings saved by %r (%s)", user["username"], redmine_url)
    settings = settings_store.get_app_settings()
    settings["api_key"] = "•" * 12 if settings["api_key"] else ""
    return templates.TemplateResponse("app_settings.html", {
        "request": request, "user": user, "settings": settings, "error": None, "ok": "App settings saved.",
        **_refresh_timing_context(),
    })


# ---------------------------------------------------------------------------
# Admin — user management (only way to create accounts; public registration
# is disabled)
# ---------------------------------------------------------------------------

@app.get("/admin/users", response_class=HTMLResponse)
def users_list(request: Request):
    user = require_login(request)
    if not user:
        return RedirectResponse("/login")
    if not user["is_admin"]:
        return RedirectResponse("/dashboard")
    return templates.TemplateResponse(
        "users.html",
        {"request": request, "user": user, "users": auth.list_users(), "error": None, "ok": None},
    )


@app.post("/admin/users", response_class=HTMLResponse)
def users_create(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
    password2: str = Form(...),
    is_admin: str = Form(""),
    manager_id: str = Form(""),
    email: str = Form(""),
):
    user = require_login(request)
    if not user:
        return RedirectResponse("/login")
    if not user["is_admin"]:
        return RedirectResponse("/dashboard")

    username = username.strip()
    manager_id = manager_id.strip()
    email = email.strip()
    error = None
    if len(username) < 3:
        error = "Username must be at least 3 characters."
    elif password != password2:
        error = "Passwords do not match."
    elif len(password) < 8:
        error = "Password must be at least 8 characters."
    elif manager_id and not manager_id.isdigit():
        error = "Project Manager id must be a numeric Redmine user id."
    elif email and "@" not in email:
        error = "That doesn't look like a valid email address."

    if not error:
        try:
            new_id = auth.create_user(username, password, is_admin=bool(is_admin), manager_id=manager_id, email=email)
            logger.info("User %r (id=%s, admin=%s, manager_id=%r, email set=%s) created by %r", username, new_id, bool(is_admin), manager_id, bool(email), user["username"])
        except ValueError as e:
            error = str(e)

    return templates.TemplateResponse(
        "users.html",
        {"request": request, "user": user, "users": auth.list_users(), "error": error, "ok": None if error else f"User '{username}' created."},
    )


@app.post("/admin/users/{target_id}/email", response_class=HTMLResponse)
def users_set_email(request: Request, target_id: int, email: str = Form("")):
    """Admin sets/updates a user's email (for alerts/digest)."""
    user = require_login(request)
    if not user:
        return RedirectResponse("/login")
    if not user["is_admin"]:
        return RedirectResponse("/dashboard")
    email = email.strip()
    error, ok = None, None
    if email and "@" not in email:
        error = "That doesn't look like a valid email address."
    elif not auth.get_user(target_id):
        error = "User not found."
    else:
        auth.set_email(target_id, email)
        ok = "Email updated."
    return templates.TemplateResponse(
        "users.html",
        {"request": request, "user": user, "users": auth.list_users(), "error": error, "ok": ok},
    )


@app.post("/admin/users/{target_id}/manager", response_class=HTMLResponse)
def users_set_manager(request: Request, target_id: int, manager_id: str = Form("")):
    """Assigns (or clears) the Redmine Project Manager id a regular user is
    hard-restricted to — see _visible_projects in this module. Blank clears
    the restriction (back to seeing every project); admins are never
    restricted regardless of what's stored here."""
    user = require_login(request)
    if not user:
        return RedirectResponse("/login")
    if not user["is_admin"]:
        return RedirectResponse("/dashboard")

    manager_id = manager_id.strip()
    error, ok = None, None
    if manager_id and not manager_id.isdigit():
        error = "Project Manager id must be a numeric Redmine user id."
    else:
        target = auth.get_user(target_id)
        if not target:
            error = "User not found."
        else:
            settings_store.save_user_manager(target_id, manager_id)
            logger.info("Project Manager restriction for %r set to %r by %r", target["username"], manager_id or "(none)", user["username"])
            ok = f"Project Manager updated for '{target['username']}'."

    return templates.TemplateResponse(
        "users.html",
        {"request": request, "user": user, "users": auth.list_users(), "error": error, "ok": ok},
    )


@app.post("/admin/users/{target_id}/reset-password", response_class=HTMLResponse)
def users_reset_password(request: Request, target_id: int, new_password: str = Form(...)):
    user = require_login(request)
    if not user:
        return RedirectResponse("/login")
    if not user["is_admin"]:
        return RedirectResponse("/dashboard")

    error, ok = None, None
    if len(new_password) < 8:
        error = "Password must be at least 8 characters."
    else:
        target = auth.get_user(target_id)
        if not target:
            error = "User not found."
        else:
            auth.set_password(target_id, new_password)
            logger.info("Password reset for %r by %r", target["username"], user["username"])
            ok = f"Password reset for '{target['username']}'."

    return templates.TemplateResponse(
        "users.html",
        {"request": request, "user": user, "users": auth.list_users(), "error": error, "ok": ok},
    )


@app.post("/admin/users/{target_id}/delete", response_class=HTMLResponse)
def users_delete(request: Request, target_id: int):
    user = require_login(request)
    if not user:
        return RedirectResponse("/login")
    if not user["is_admin"]:
        return RedirectResponse("/dashboard")

    error, ok = None, None
    try:
        target = auth.get_user(target_id)
        if not target:
            raise ValueError("User not found.")
        auth.delete_user(target_id)
        logger.info("User %r deleted by %r", target["username"], user["username"])
        ok = f"User '{target['username']}' deleted."
    except ValueError as e:
        error = str(e)

    return templates.TemplateResponse(
        "users.html",
        {"request": request, "user": user, "users": auth.list_users(), "error": error, "ok": ok},
    )


# ---------------------------------------------------------------------------
# Admin — Daily Rate per country
# ---------------------------------------------------------------------------

@app.get("/admin/countries", response_class=HTMLResponse)
def countries_list(request: Request):
    user = require_login(request)
    if not user:
        return RedirectResponse("/login")
    if not user["is_admin"]:
        return RedirectResponse("/dashboard")
    return templates.TemplateResponse(
        "countries.html",
        {"request": request, "user": user, "countries": settings_store.list_countries(), "error": None, "ok": None},
    )


@app.post("/admin/countries", response_class=HTMLResponse)
def countries_create(request: Request, name: str = Form(...), daily_rate: str = Form(...), currency: str = Form("")):
    user = require_login(request)
    if not user:
        return RedirectResponse("/login")
    if not user["is_admin"]:
        return RedirectResponse("/dashboard")

    name = name.strip()
    error, ok = None, None
    try:
        rate = float(daily_rate)
        if not name:
            raise ValueError("Country name is required.")
        if rate < 0:
            raise ValueError("Daily rate cannot be negative.")
    except ValueError as e:
        error = str(e) if str(e) else "Enter a valid daily rate."
    else:
        try:
            settings_store.create_country(name, rate, currency)
            logger.info("Country %r added (daily_rate=%s, currency=%r) by %r", name, rate, currency, user["username"])
            ok = f"'{name}' added."
        except sqlite3.IntegrityError:
            error = f"'{name}' already has a daily rate configured."

    return templates.TemplateResponse(
        "countries.html",
        {"request": request, "user": user, "countries": settings_store.list_countries(), "error": error, "ok": ok},
    )


@app.post("/admin/countries/{country_id}", response_class=HTMLResponse)
def countries_update(request: Request, country_id: int, daily_rate: str = Form(...), currency: str = Form("")):
    user = require_login(request)
    if not user:
        return RedirectResponse("/login")
    if not user["is_admin"]:
        return RedirectResponse("/dashboard")

    error, ok = None, None
    try:
        rate = float(daily_rate)
        if rate < 0:
            raise ValueError
    except ValueError:
        error = "Enter a valid daily rate."
    else:
        settings_store.update_country(country_id, rate, currency)
        logger.info("Country id=%s daily rate updated (daily_rate=%s, currency=%r) by %r", country_id, rate, currency, user["username"])
        ok = "Daily rate saved."

    return templates.TemplateResponse(
        "countries.html",
        {"request": request, "user": user, "countries": settings_store.list_countries(), "error": error, "ok": ok},
    )


@app.post("/admin/countries/{country_id}/delete", response_class=HTMLResponse)
def countries_delete(request: Request, country_id: int):
    user = require_login(request)
    if not user:
        return RedirectResponse("/login")
    if not user["is_admin"]:
        return RedirectResponse("/dashboard")

    settings_store.delete_country(country_id)
    logger.info("Country id=%s deleted by %r", country_id, user["username"])

    return templates.TemplateResponse(
        "countries.html",
        {"request": request, "user": user, "countries": settings_store.list_countries(), "error": None, "ok": "Country removed."},
    )


# ---------------------------------------------------------------------------
# Admin — PP Report month data cleanup (delete a stale/unwanted month's
# locked snapshot and/or draft outright, so its next visit starts fresh)
# ---------------------------------------------------------------------------

@app.get("/admin/pp-report-months", response_class=HTMLResponse)
def pp_report_months_list(request: Request):
    user = require_login(request)
    if not user:
        return RedirectResponse("/login")
    if not user["is_admin"]:
        return RedirectResponse("/dashboard")
    return templates.TemplateResponse(
        "pp_report_months.html",
        {
            "request": request, "user": user, "months": pp_report.list_report_months(),
            "excluded_project_ids": settings_store.get_pp_report_excluded_project_ids_raw(),
            "excluded_manager_ids": settings_store.get_pp_report_excluded_manager_ids_raw(),
            "error": None, "ok": None,
        },
    )


@app.post("/admin/pp-report-months/{month}/delete", response_class=HTMLResponse)
def pp_report_months_delete(request: Request, month: str):
    user = require_login(request)
    if not user:
        return RedirectResponse("/login")
    if not user["is_admin"]:
        return RedirectResponse("/dashboard")

    error, ok = None, None
    order_error = pp_report.check_delete_order(month)
    if order_error:
        logger.warning("PP report month-data delete blocked (out of order) for %s by %r: %s", month, user["username"], order_error)
        error = order_error
    else:
        pp_report.delete_month_data(month)
        logger.info("PP report month data deleted for %s by %r", month, user["username"])
        ok = f"All data for {pp_report.format_month_label(month)} deleted — it will start fresh next time it's opened."

    return templates.TemplateResponse(
        "pp_report_months.html",
        {
            "request": request, "user": user, "months": pp_report.list_report_months(),
            "excluded_project_ids": settings_store.get_pp_report_excluded_project_ids_raw(),
            "excluded_manager_ids": settings_store.get_pp_report_excluded_manager_ids_raw(),
            "error": error, "ok": ok,
        },
    )


@app.post("/admin/pp-report-months/ksa-upload", response_class=HTMLResponse)
async def pp_report_ksa_upload(request: Request, month: str = Form(...), file: UploadFile = File(...)):
    """Loads the KSA projects for `month` from the monthly PP Report workbook
    (KSA isn't tracked in Redmine — see pp_report.parse_ksa_workbook). A new
    upload replaces that month's previous KSA rows. Refused for a locked
    month, whose figures are already frozen."""
    user = require_login(request)
    if not user:
        return RedirectResponse("/login")
    if not user["is_admin"]:
        return RedirectResponse("/dashboard")

    error, ok = None, None
    month = month.strip()
    if not re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", month):
        error = "Choose a valid month."
    else:
        try:
            parsed = pp_report.parse_ksa_workbook(await file.read(), month)
            pp_report.save_ksa_rows(month, parsed)
            total = sum(r["amount_to_take"] for r in parsed)
            logger.info("KSA data uploaded by %r for %s: %d project(s), Amount to Take %.2f", user["username"], month, len(parsed), total)
            ok = f"Loaded {len(parsed)} KSA project(s) for {pp_report.format_month_label(month)} (Amount to Take {total:,.0f})."
            if pp_report.refresh_ksa_in_locked_snapshot(month) is not None:
                ok += " The month is locked: only its KSA lines were updated, every other country's figures are untouched."
        except ValueError as e:
            error = str(e)
        except Exception as e:
            logger.exception("KSA upload failed for %s", month)
            error = f"Could not read that file: {e}"

    return templates.TemplateResponse(
        "pp_report_months.html",
        {
            "request": request, "user": user, "months": pp_report.list_report_months(),
            "excluded_project_ids": settings_store.get_pp_report_excluded_project_ids_raw(),
            "excluded_manager_ids": settings_store.get_pp_report_excluded_manager_ids_raw(),
            "error": error, "ok": ok,
        },
    )


@app.post("/admin/pp-report-months/ksa-timesheet-upload", response_class=HTMLResponse)
async def pp_report_ksa_timesheet_upload(request: Request, month: str = Form(...), file: UploadFile = File(...)):
    """Loads KSA consultants' time entries for `month` from an Intra
    timesheet export (see pp_report.parse_ksa_timesheet). They feed the
    Utilization Report / Resource Planning and the per-consultant cost on
    the KSA PP Report rows. A new upload replaces that month's entries.
    A locked PP Report month keeps its frozen figures (reopen it to pick
    the new hours up); Utilization is unaffected by locking."""
    user = require_login(request)
    if not user:
        return RedirectResponse("/login")
    if not user["is_admin"]:
        return RedirectResponse("/dashboard")

    error, ok = None, None
    month = month.strip()
    if not re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", month):
        error = "Choose a valid month."
    else:
        try:
            parsed = pp_report.parse_ksa_timesheet(await file.read(), month)
            pp_report.save_ksa_timesheet(month, parsed)
            hours = sum(r["hours"] for r in parsed)
            people = len({r["user_name"] for r in parsed})
            skipped = pp_report.ksa_timesheet_skipped(month)
            skipped_hours = sum(h for _, h in skipped)
            logger.info("KSA timesheet uploaded by %r for %s: %d entries, %.1f h, %d people", user["username"], month, len(parsed), hours, people)
            ok = f"Loaded {len(parsed)} KSA time entries ({hours:,.1f} h, {people} people) for {pp_report.format_month_label(month)}."
            if skipped:
                ok += f" {skipped_hours:,.1f} h are not KSA project time and are left out: " + ", ".join(f"{c or '(no code)'} {h:,.1f}h" for c, h in skipped[:8]) + ("…" if len(skipped) > 8 else "") + "."
            if not pp_report.get_ksa_rows(month):
                ok += " Upload the KSA workbook for this month too — hours only attach to KSA projects it lists."
            if pp_report.refresh_ksa_in_locked_snapshot(month) is not None:
                ok += " The PP Report month is locked: only its KSA lines were refreshed (cost from the new hours); other countries are untouched."
        except ValueError as e:
            error = str(e)
        except Exception as e:
            logger.exception("KSA timesheet upload failed for %s", month)
            error = f"Could not read that file: {e}"

    return templates.TemplateResponse(
        "pp_report_months.html",
        {
            "request": request, "user": user, "months": pp_report.list_report_months(),
            "excluded_project_ids": settings_store.get_pp_report_excluded_project_ids_raw(),
            "excluded_manager_ids": settings_store.get_pp_report_excluded_manager_ids_raw(),
            "error": error, "ok": ok,
        },
    )


@app.post("/admin/pp-report-months/ksa-timesheet-delete", response_class=HTMLResponse)
def pp_report_ksa_timesheet_delete(request: Request, month: str = Form(...)):
    user = require_login(request)
    if not user:
        return RedirectResponse("/login")
    if not user["is_admin"]:
        return RedirectResponse("/dashboard")

    pp_report.delete_ksa_timesheet(month)
    pp_report.refresh_ksa_in_locked_snapshot(month)
    logger.info("KSA timesheet for %s removed by %r", month, user["username"])
    return templates.TemplateResponse(
        "pp_report_months.html",
        {
            "request": request, "user": user, "months": pp_report.list_report_months(),
            "excluded_project_ids": settings_store.get_pp_report_excluded_project_ids_raw(),
            "excluded_manager_ids": settings_store.get_pp_report_excluded_manager_ids_raw(),
            "error": None, "ok": f"KSA timesheet for {pp_report.format_month_label(month)} removed.",
        },
    )


@app.post("/admin/pp-report-months/ksa-delete", response_class=HTMLResponse)
def pp_report_ksa_delete(request: Request, month: str = Form(...)):
    user = require_login(request)
    if not user:
        return RedirectResponse("/login")
    if not user["is_admin"]:
        return RedirectResponse("/dashboard")

    error, ok = None, None
    pp_report.delete_ksa_rows(month)
    pp_report.refresh_ksa_in_locked_snapshot(month)
    logger.info("KSA data for %s removed by %r", month, user["username"])
    ok = f"KSA data for {pp_report.format_month_label(month)} removed."

    return templates.TemplateResponse(
        "pp_report_months.html",
        {
            "request": request, "user": user, "months": pp_report.list_report_months(),
            "excluded_project_ids": settings_store.get_pp_report_excluded_project_ids_raw(),
            "excluded_manager_ids": settings_store.get_pp_report_excluded_manager_ids_raw(),
            "error": error, "ok": ok,
        },
    )


@app.post("/admin/pp-report-months/excluded-projects", response_class=HTMLResponse)
def pp_report_excluded_projects_save(request: Request, project_ids: str = Form("")):
    """Admin-configured comma-separated Redmine project ids to leave out of
    the PP Report entirely (build_report filters them out — see
    pp_report.py). Non-numeric entries are silently dropped rather than
    rejecting the whole save, since a stray typo/extra comma shouldn't
    block the valid ids from taking effect."""
    user = require_login(request)
    if not user:
        return RedirectResponse("/login")
    if not user["is_admin"]:
        return RedirectResponse("/dashboard")

    valid_ids, invalid_parts = [], []
    for part in project_ids.split(","):
        part = part.strip()
        if not part:
            continue
        try:
            valid_ids.append(int(part))
        except ValueError:
            invalid_parts.append(part)

    normalized = ", ".join(str(i) for i in valid_ids)
    settings_store.save_pp_report_excluded_project_ids(normalized)
    logger.info("PP report excluded project ids saved by %r: %s", user["username"], normalized or "(none)")

    ok = f"Saved — {len(valid_ids)} project id(s) excluded from the PP Report." if valid_ids else "Saved — no projects excluded."
    error = f"Ignored non-numeric entries: {', '.join(invalid_parts)}" if invalid_parts else None

    return templates.TemplateResponse(
        "pp_report_months.html",
        {
            "request": request, "user": user, "months": pp_report.list_report_months(),
            "excluded_project_ids": normalized,
            "excluded_manager_ids": settings_store.get_pp_report_excluded_manager_ids_raw(),
            "error": error, "ok": ok,
        },
    )


@app.post("/admin/pp-report-months/excluded-managers", response_class=HTMLResponse)
def pp_report_excluded_managers_save(request: Request, manager_ids: str = Form("")):
    """Admin-configured comma-separated Redmine Project Manager ids whose
    projects are left out of the PP Report entirely — same idea as
    excluded-projects above, by manager instead of by individual project."""
    user = require_login(request)
    if not user:
        return RedirectResponse("/login")
    if not user["is_admin"]:
        return RedirectResponse("/dashboard")

    valid_ids, invalid_parts = [], []
    for part in manager_ids.split(","):
        part = part.strip()
        if not part:
            continue
        try:
            valid_ids.append(int(part))
        except ValueError:
            invalid_parts.append(part)

    normalized = ", ".join(str(i) for i in valid_ids)
    settings_store.save_pp_report_excluded_manager_ids(normalized)
    logger.info("PP report excluded manager ids saved by %r: %s", user["username"], normalized or "(none)")

    ok = f"Saved — {len(valid_ids)} Project Manager(s) excluded from the PP Report." if valid_ids else "Saved — no Project Managers excluded."
    error = f"Ignored non-numeric entries: {', '.join(invalid_parts)}" if invalid_parts else None

    return templates.TemplateResponse(
        "pp_report_months.html",
        {
            "request": request, "user": user, "months": pp_report.list_report_months(),
            "excluded_project_ids": settings_store.get_pp_report_excluded_project_ids_raw(),
            "excluded_manager_ids": normalized,
            "error": error, "ok": ok,
        },
    )


@app.post("/admin/pp-report-months/ksa-excluded-codes", response_class=HTMLResponse)
def ksa_excluded_codes_save(request: Request, codes: str = Form("")):
    """Admin-configured comma-separated KSA timesheet project codes to treat as
    non-project time (leave, support, sales, …). Their hours are excluded from
    the Time Analysis tab. Codes are free-form text (e.g. 123, PSASUPPORT), so
    unlike the id-based lists above every entry is accepted as-is."""
    user = require_login(request)
    if not user:
        return RedirectResponse("/login")
    if not user["is_admin"]:
        return RedirectResponse("/dashboard")

    parts = [p.strip() for p in codes.split(",") if p.strip()]
    normalized = ", ".join(parts)
    settings_store.save_ksa_excluded_codes(normalized)
    logger.info("KSA excluded timesheet codes saved by %r: %s", user["username"], normalized or "(none)")

    ok = f"Saved — {len(parts)} KSA code(s) excluded from Time Analysis." if parts else "Saved — no KSA codes excluded."
    return templates.TemplateResponse(
        "pp_report_months.html",
        {
            "request": request, "user": user, "months": pp_report.list_report_months(),
            "excluded_project_ids": settings_store.get_pp_report_excluded_project_ids_raw(),
            "excluded_manager_ids": settings_store.get_pp_report_excluded_manager_ids_raw(),
            "error": None, "ok": ok,
        },
    )


@app.post("/admin/pp-report-months/consultant-name-map", response_class=HTMLResponse)
def consultant_name_map_save(request: Request, name_map: str = Form("")):
    """Admin-editable consultant name map — one 'variant => canonical' per
    line. Merges different spellings/orderings of one consultant (e.g. the KSA
    timesheet's reversed name order) so utilization, resource planning and Time
    Analysis treat them as a single person."""
    user = require_login(request)
    if not user:
        return RedirectResponse("/login")
    if not user["is_admin"]:
        return RedirectResponse("/dashboard")

    settings_store.save_consultant_name_map(name_map)
    n = len(settings_store.get_consultant_name_map())
    logger.info("Consultant name map saved by %r: %d mapping(s)", user["username"], n)
    ok = f"Saved — {n} consultant name mapping(s)." if n else "Saved — no name mappings."
    return templates.TemplateResponse(
        "pp_report_months.html",
        {
            "request": request, "user": user, "months": pp_report.list_report_months(),
            "excluded_project_ids": settings_store.get_pp_report_excluded_project_ids_raw(),
            "excluded_manager_ids": settings_store.get_pp_report_excluded_manager_ids_raw(),
            "error": None, "ok": ok,
        },
    )


# ---------------------------------------------------------------------------
# Dashboard
# ---------------------------------------------------------------------------

def _render_dashboard_fragment(projects, timespent, report_date, team_ids, default_manager_id, redmine_base_url, ksa_projects=None, excluded_projects=None, excluded_project_ids=None, ta_project_meta=None):
    with open(TEMPLATE_PATH, "r", encoding="utf-8") as f:
        html = f.read()
    html = html.replace("__PROJECT_DATA__", json.dumps(projects))
    html = html.replace("__REPORT_DATE__", report_date)
    html = html.replace("__TIMESPENT_DATA__", json.dumps(timespent))
    html = html.replace("__TEAM_USER_IDS__", json.dumps(team_ids))
    html = html.replace("__DEFAULT_MANAGER_ID__", json.dumps(default_manager_id))
    html = html.replace("__KSA_PROJECTS__", json.dumps(ksa_projects or []))
    # Excluded projects (admin "Excluded projects" setting): kept out of the
    # Projects tab, but their time entries are still sent so the Time Analysis
    # tab can optionally show them via its "Show excluded projects" toggle.
    html = html.replace("__EXCLUDED_PROJECTS__", json.dumps(excluded_projects or []))
    html = html.replace("__EXCLUDED_PROJECT_IDS__", json.dumps([str(i) for i in (excluded_project_ids or [])]))
    # Extra project meta for Time Analysis only (other-team projects a restricted
    # PM's consultants logged on) — used for grouping/lookups, not the Projects tab.
    html = html.replace("__TA_PROJECT_META__", json.dumps(ta_project_meta or []))
    html = html.replace("__REDMINE_BASE_URL__", redmine_base_url)
    return html


@app.get("/dashboard", response_class=HTMLResponse)
def dashboard(request: Request):
    user = require_login(request)
    if not user:
        return RedirectResponse("/login")

    settings = settings_store.get_app_settings()
    has_settings = bool(settings["redmine_url"] and settings["api_key"])

    dashboard_html = ""
    fetched_on = None
    if has_settings:
        all_projects, fetched_on = settings_store.load_cache("projects")
        all_timespent, _ = settings_store.load_cache("timespent")
        # Excluded *projects* and excluded *managers* are both removed from
        # the Projects tab, but neither is hard-dropped here (unlike the PP
        # Report) — their time entries stay available, and both tabs' "Show
        # excluded projects" toggle can reveal the rows on demand.
        assigned_manager_id = _assigned_manager_id(user)
        visible_full = _visible_projects(all_projects, user)
        excluded_project_ids = set(settings_store.get_pp_report_excluded_project_ids())
        excluded_manager_ids = set(settings_store.get_pp_report_excluded_manager_ids())
        excluded_ids = {
            p["id"] for p in visible_full
            if p["id"] in excluded_project_ids or (p.get("managerId") or 0) in excluded_manager_ids
        }
        projects = [p for p in visible_full if p["id"] not in excluded_ids]
        excluded_projects = [p for p in visible_full if p["id"] in excluded_ids]

        # Time Analysis visibility differs from the Projects tab for a restricted
        # Project Manager: their consultants frequently log time on OTHER teams'
        # projects, and the PM needs to see that. So Time Analysis shows every
        # entry by anyone who works on this PM's projects, across ALL projects —
        # while the Projects tab (above) stays limited to the PM's own projects.
        ta_project_meta = []
        if assigned_manager_id:
            pm_project_ids = {p["id"] for p in visible_full}
            team_user_ids = {t.get("user_id") for t in all_timespent
                             if t.get("project_id") in pm_project_ids and t.get("user_id")}
            timespent = [t for t in all_timespent if t.get("user_id") in team_user_ids]
            # Meta for the other-team projects those entries land on, so Time
            # Analysis grouping (by manager/country) and Rapport code resolve.
            appearing = {t.get("project_id") for t in timespent}
            known = {p["id"] for p in visible_full}
            ta_project_meta = [p for p in all_projects if p["id"] in appearing and p["id"] not in known]
        else:
            timespent = _visible_timespent(all_timespent, {p["id"] for p in visible_full})
        ksa_ta_projects = []
        if not assigned_manager_id:
            # KSA consultants' uploaded timesheets (KSA isn't in Redmine) show
            # up in the Time Analysis tab alongside the Redmine time entries;
            # their projects are listed (with the KSA manager) so that tab's
            # Project Manager filter can select them.
            # Time Analysis shows ALL logged KSA hours (leave/support included).
            # Codes on the admin "Excluded KSA codes" list are hidden by default
            # but revealed by the same "Show excluded projects" toggle — so their
            # synthetic project ids are added to the excluded set (with meta),
            # exactly like Redmine excluded projects.
            ksa_entries = pp_report.ksa_timesheet_entries_all(
                settings_store.distinct_timesheet_users(timespent))
            timespent = timespent + ksa_entries
            ksa_excluded_codes = settings_store.get_ksa_excluded_codes()
            if ksa_excluded_codes:
                seen_ex = {p["id"] for p in excluded_projects}
                for e in ksa_entries:
                    if (e.get("project_code") or "").upper() in ksa_excluded_codes:
                        excluded_ids.add(e["project_id"])
                        if e["project_id"] not in seen_ex:
                            seen_ex.add(e["project_id"])
                            excluded_projects.append({
                                "id": e["project_id"], "name": e["project_name"],
                                "country": pp_report.KSA_COUNTRY, "rapportCode": e["project_code"],
                                "managerId": pp_report.KSA_MANAGER_KEY, "managerName": pp_report.KSA_MANAGER_NAME,
                                "est": 0, "spent": 0,
                            })
            ksa_ta_projects = [
                {"name": name, "managerId": pp_report.KSA_MANAGER_KEY, "managerName": pp_report.KSA_MANAGER_NAME}
                for name in sorted({e["project_name"] for e in ksa_entries})
            ]
        prefs = settings_store.get_user_prefs(user["id"])
        team_ids = [t.strip() for t in prefs["my_team_ids"].split(",") if t.strip()]
        report_date = (fetched_on or "")[:10]
        dashboard_html = _render_dashboard_fragment(projects, timespent, report_date, team_ids, assigned_manager_id, settings["redmine_url"], ksa_ta_projects, excluded_projects, sorted(excluded_ids), ta_project_meta)

    return templates.TemplateResponse(
        "dashboard_shell.html",
        {
            "request": request,
            "user": user,
            "has_settings": has_settings,
            "dashboard_html": dashboard_html,
            "fetched_on": fetched_on,
            **_refresh_timing_context(),
        },
    )


def _build_ai_summary_prompt(stats: dict, scope: str) -> str:
    """Turn the dashboard's current figures into a compact, numeric prompt."""
    import json as _json
    scope = scope or "the current Project Ledger view"
    return (
        f"Write a short management summary of {scope} for a SAP Business One "
        "consulting firm. Lead with the 2-3 things that matter most (budget "
        "overruns, utilization/capacity risk, revenue concentration), name "
        "specific projects/countries/PMs from the data, and end with 2-3 concrete "
        "actions. Keep it under 180 words. Use the figures below; do not invent "
        "numbers.\n\nFIGURES (JSON):\n" + _json.dumps(stats, ensure_ascii=False)
    )


@app.get("/analysis", response_class=HTMLResponse)
def analysis_page(request: Request):
    """Live insights (utilization, margin leakage, country efficiency, KSA,
    cross-team) computed from the current cache — refreshes with every Redmine
    refresh. Same visibility rules as the dashboard."""
    user = require_login(request)
    if not user:
        return RedirectResponse("/login")

    settings = settings_store.get_app_settings()
    has_settings = bool(settings["redmine_url"] and settings["api_key"])
    data = None
    fetched_on = None
    if has_settings:
        projects, fetched_on = settings_store.load_cache("projects")
        timespent, _ = settings_store.load_cache("timespent")
        # Respect the admin-excluded managers AND excluded projects (same as
        # the PP Report / Dashboard), plus the viewer's PM visibility, so the
        # Analysis figures match what those pages count.
        projects = _drop_excluded_managers(projects)
        projects = _drop_excluded_projects(projects)
        assigned = _assigned_manager_id(user)
        visible_full = _visible_projects(projects, user)
        if not assigned:
            ts = _visible_timespent(timespent, {p["id"] for p in visible_full})
            ts = ts + pp_report.ksa_timesheet_entries_all(settings_store.distinct_timesheet_users(ts))
        else:
            pm_ids = {p["id"] for p in visible_full}
            team = {t.get("user_id") for t in timespent if t.get("project_id") in pm_ids and t.get("user_id")}
            ts = [t for t in timespent if t.get("user_id") in team]
        report_month = (fetched_on or "")[:7] or resource_planning.current_month_str()
        data = analysis.build_analysis(visible_full, ts, report_month, settings_store.get_ksa_excluded_codes())
        trends = analysis.build_trends(visible_full, ts, report_month, 12)

    ors = settings_store.get_openrouter_settings()
    return templates.TemplateResponse("analysis.html", {
        "request": request, "user": user, "has_settings": has_settings,
        "data": data, "trends": trends if has_settings else None, "fetched_on": fetched_on,
        "openrouter_ready": bool(ors["api_key"] and ors["models"]),
    })


@app.post("/api/ai-summary")
async def api_ai_summary(request: Request):
    """Send the dashboard's current figures to the configured OpenRouter model
    and return a short narrative. The key never leaves the server."""
    user = require_login(request)
    if not user:
        return JSONResponse(status_code=401, content={"detail": "Not logged in."})
    ors = settings_store.get_openrouter_settings()
    if not ors["api_key"] or not ors["models"]:
        return JSONResponse(status_code=400, content={"detail": "OpenRouter isn't configured yet — add an API key and at least one model under App Settings → OpenRouter."})
    try:
        body = await request.json()
    except Exception:
        body = {}
    stats = body.get("stats") or {}
    scope = (body.get("scope") or "").strip()
    if not stats:
        return JSONResponse(status_code=400, content={"detail": "No figures to summarize."})
    try:
        result = openrouter_client.chat(
            ors["api_key"], ors["models"],
            [
                {"role": "system", "content": "You are a delivery-operations analyst for a SAP Business One consulting firm. Be concise, specific and numeric. No preamble, no restating the question."},
                {"role": "user", "content": _build_ai_summary_prompt(stats, scope)},
            ],
        )
    except openrouter_client.OpenRouterError as e:
        return JSONResponse(status_code=502, content={"detail": str(e)})
    except Exception as e:
        # Never let an unexpected error fall through as a non-JSON 500 — the
        # client parses JSON. Surface a readable message instead.
        logger.exception("AI summary failed")
        return JSONResponse(status_code=500, content={"detail": f"AI summary failed: {e}"})
    # chat() returns a dict; tolerate an old deployment that returned a string.
    if isinstance(result, str):
        result = {"text": result, "model": ors["models"][0]}
    logger.info("AI summary generated for %r via %s", user["username"], result.get("model"))
    return JSONResponse(content={"summary": result.get("text", ""), "model": result.get("model", "")})


def _data_quality_inputs(user: dict):
    """Projects + time entries for the Data Quality page / AI query, assembled
    with the same visibility rules and KSA merge as the Analysis page, so the
    checks see exactly what the dashboards see."""
    projects, fetched_on = settings_store.load_cache("projects")
    timespent, _ = settings_store.load_cache("timespent")
    # Same filtering as the Analysis page / PP Report: drop admin-excluded
    # managers and admin-excluded projects, then apply the viewer's PM
    # visibility — so Data Quality only flags projects that actually count.
    projects = _drop_excluded_managers(projects)
    projects = _drop_excluded_projects(projects)
    visible = _visible_projects(projects, user)
    if not _assigned_manager_id(user):
        ts = _visible_timespent(timespent, {p["id"] for p in visible})
        ts = ts + pp_report.ksa_timesheet_entries_all(settings_store.distinct_timesheet_users(ts))
    else:
        pm_ids = {p["id"] for p in visible}
        team = {t.get("user_id") for t in timespent if t.get("project_id") in pm_ids and t.get("user_id")}
        ts = [t for t in timespent if t.get("user_id") in team]
    return visible, ts, fetched_on


@app.get("/data-quality", response_class=HTMLResponse)
def data_quality_page(request: Request):
    """Surfaces the recurring hygiene problems — projects with no country,
    likely consultant duplicates, stale/missing reviews, missing estimates —
    as one actionable list, plus a natural-language query over the ledger.
    Recomputed from the cache on every load, so it reflects the latest refresh."""
    user = require_login(request)
    if not user:
        return RedirectResponse("/login")

    settings = settings_store.get_app_settings()
    has_settings = bool(settings["redmine_url"] and settings["api_key"])
    report = None
    fetched_on = None
    if has_settings:
        projects, timespent, fetched_on = _data_quality_inputs(user)
        report = data_quality.build_report(projects, timespent, settings_store.get_consultant_name_map())

    ors = settings_store.get_openrouter_settings()
    return templates.TemplateResponse("data_quality.html", {
        "request": request, "user": user, "has_settings": has_settings,
        "report": report, "fetched_on": fetched_on,
        "openrouter_ready": bool(ors["api_key"] and ors["models"]),
        **_refresh_timing_context(),
    })


@app.post("/api/data-quality/query")
async def api_data_quality_query(request: Request):
    """Answer a natural-language question about the portfolio via OpenRouter.
    The model is given a compact snapshot of the (visible) projects and asked
    to answer only from it. The key never leaves the server."""
    user = require_login(request)
    if not user:
        return JSONResponse(status_code=401, content={"detail": "Not logged in."})
    ors = settings_store.get_openrouter_settings()
    if not ors["api_key"] or not ors["models"]:
        return JSONResponse(status_code=400, content={"detail": "OpenRouter isn't configured yet — add an API key and at least one model under App Settings → OpenRouter."})
    try:
        body = await request.json()
    except Exception:
        body = {}
    question = (body.get("question") or "").strip()
    if not question:
        return JSONResponse(status_code=400, content={"detail": "Type a question first."})

    projects, _, _ = _data_quality_inputs(user)
    if not projects:
        return JSONResponse(status_code=400, content={"detail": "No project data cached yet — run a refresh first."})
    context = data_quality.build_query_context(projects)
    try:
        result = openrouter_client.chat(
            ors["api_key"], ors["models"],
            [
                {"role": "system", "content": "You are a delivery-operations analyst for a SAP Business One consulting firm. Answer ONLY from the portfolio snapshot provided. If the data can't answer the question, say so plainly. Be concise and numeric; cite project ids/names. Never invent figures."},
                {"role": "user", "content": f"{context}\n\nQUESTION: {question}"},
            ],
        )
    except openrouter_client.OpenRouterError as e:
        return JSONResponse(status_code=502, content={"detail": str(e)})
    except Exception as e:
        logger.exception("Data-quality AI query failed")
        return JSONResponse(status_code=500, content={"detail": f"Query failed: {e}"})
    if isinstance(result, str):
        result = {"text": result, "model": ors["models"][0]}
    logger.info("Data-quality query answered for %r via %s", user["username"], result.get("model"))
    return JSONResponse(content={"answer": result.get("text", ""), "model": result.get("model", "")})


@app.post("/api/refresh")
def api_refresh(request: Request, background_tasks: BackgroundTasks):
    user = require_login(request)
    if not user:
        return RedirectResponse("/login")

    settings = settings_store.get_app_settings()
    if not (settings["redmine_url"] and settings["api_key"]):
        return JSONResponse(status_code=400, content={"detail": "Add the Redmine URL and API key in App Settings first."})

    # Atomic compare-and-set: only one caller can win this even if two
    # requests land at nearly the same instant (e.g. two different users
    # clicking within the same instant) — closes a race that could
    # otherwise fire two concurrent Redmine fetches and double-count the
    # cached time entries. The shared status is visible to every user.
    started = settings_store.try_start_running("Fetching from Redmine...", user["username"])
    if not started:
        current = settings_store.get_status()
        return {"status": "already_running", "message": current["message"]}

    # Runs in a background task so the HTTP response returns immediately —
    # a page reload no longer loses the fetch in progress. Progress is
    # tracked in the shared fetch_status and polled via /api/status.
    background_tasks.add_task(run_refresh, user["username"], "manual")
    return {"status": "started"}


@app.get("/api/status")
def api_status(request: Request):
    user = require_login(request)
    if not user:
        return RedirectResponse("/login")
    return settings_store.get_status()


# ---------------------------------------------------------------------------
# Project edit modal (review fields + Risk + Order amount — writes back to Redmine)
# ---------------------------------------------------------------------------

@app.get("/api/projects/{project_id}/fields")
def api_get_project_fields(project_id: int, request: Request):
    """Fetches CURRENT raw Risk/SAP Order values straight from Redmine (not
    cache) to populate the edit modal — important for SAP Order, since the
    Dashboard shows a T&M-transformed figure, not the raw field; editing
    must start from the real value to avoid corrupting a T&M project's
    daily rate."""
    user = require_login(request)
    if not user:
        return JSONResponse(status_code=401, content={"detail": "Not logged in."})
    if not _project_is_visible(project_id, user):
        return JSONResponse(status_code=403, content={"detail": "You don't have access to this project."})

    settings = settings_store.get_app_settings()
    if not (settings["redmine_url"] and settings["api_key"]):
        return JSONResponse(status_code=400, content={"detail": "Add the Redmine URL and API key in App Settings first."})

    try:
        fields = get_project_editable_fields(settings["redmine_url"], settings["api_key"], project_id)
    except RedmineError as e:
        return JSONResponse(status_code=502, content={"detail": str(e)})
    except Exception as e:
        logger.exception("Fetching editable fields failed for user %r, project #%s", user["username"], project_id)
        return JSONResponse(status_code=502, content={"detail": f"Unexpected error: {e}"})
    return fields


class ReviewEdit(BaseModel):
    summary: str = ""
    comments: str = ""
    risk: str = ""
    sap_order: str = ""


@app.post("/api/projects/{project_id}/review")
def api_update_review(project_id: int, body: ReviewEdit, request: Request):
    user = require_login(request)
    if not user:
        return JSONResponse(status_code=401, content={"detail": "Not logged in."})
    if not _project_is_visible(project_id, user):
        return JSONResponse(status_code=403, content={"detail": "You don't have access to this project."})

    settings = settings_store.get_app_settings()
    if not (settings["redmine_url"] and settings["api_key"]):
        return JSONResponse(status_code=400, content={"detail": "Add the Redmine URL and API key in App Settings first."})

    try:
        # Fetched fresh (not from cache) — needed both for isTM (to
        # correctly translate the raw SAP Order value back into the
        # Dashboard's T&M-transformed figure) and to diff against, so only
        # actually-changed fields get written and 'Last rev. date' only
        # moves when the summary/comments text itself changed.
        fields_before = get_project_editable_fields(settings["redmine_url"], settings["api_key"], project_id)
        result = update_project_review(
            settings["redmine_url"], settings["api_key"], project_id,
            body.summary.strip(), body.comments.strip(), fields_before,
            risk=body.risk.strip(), sap_order=body.sap_order.strip(),
        )
    except RedmineError as e:
        logger.warning("Project edit save failed for user %r, project #%s: %s", user["username"], project_id, e)
        return JSONResponse(status_code=502, content={"detail": str(e)})
    except Exception as e:
        logger.exception("Project edit save crashed for user %r, project #%s", user["username"], project_id)
        return JSONResponse(status_code=502, content={"detail": f"Unexpected error: {e}"})

    logger.info("Project fields saved by %r for project #%s", user["username"], project_id)

    projects, _ = settings_store.load_cache("projects")
    cached_project = next((p for p in projects if p["id"] == project_id), None)
    cached_spent = cached_project.get("spent", 0) if cached_project else 0
    display_order_amount = compute_display_order_amount(result["sapOrderRaw"], fields_before["isTM"], cached_spent)

    settings_store.update_cached_project_review(project_id, result["summary"], result["comments"], result["revDate"])
    settings_store.update_cached_project_fields(project_id, result["risk"], display_order_amount)

    return {**result, "orderAmount": display_order_amount}


@app.post("/api/projects/{project_id}/refresh")
def api_refresh_one_project(project_id: int, request: Request):
    """Re-fetches ONE project's full data (Estimated/Spent/Country/Manager/
    Risk/SAP Order/status — everything the Dashboard/PP Report use) from
    Redmine and replaces just that project's cached row, without running a
    full instance-wide refresh. For chasing a single-project discrepancy
    (e.g. against an external report) without waiting minutes for
    "Refresh from Redmine" to cover every project."""
    user = require_login(request)
    if not user:
        return JSONResponse(status_code=401, content={"detail": "Not logged in."})
    if not _project_is_visible(project_id, user):
        return JSONResponse(status_code=403, content={"detail": "You don't have access to this project."})

    settings = settings_store.get_app_settings()
    if not (settings["redmine_url"] and settings["api_key"]):
        return JSONResponse(status_code=400, content={"detail": "Add the Redmine URL and API key in App Settings first."})

    projects, _ = settings_store.load_cache("projects")
    existing = next((p for p in projects if p.get("id") == project_id), None)
    keep_spent = existing.get("spent", 0) if existing else 0

    try:
        row, timespent_rows = fetch_single_project(settings["redmine_url"], settings["api_key"], project_id, keep_spent)
    except RedmineError as e:
        logger.warning("Single-project refresh failed for user %r, project #%s: %s", user["username"], project_id, e)
        return JSONResponse(status_code=502, content={"detail": str(e)})
    except Exception as e:
        logger.exception("Single-project refresh crashed for user %r, project #%s", user["username"], project_id)
        return JSONResponse(status_code=502, content={"detail": f"Unexpected error: {e}"})

    settings_store.replace_cached_project(project_id, row)
    if timespent_rows is not None:
        settings_store.replace_cached_timespent_for_project(project_id, timespent_rows)
    logger.info("Project #%s refreshed individually by %r (Spent left as-is from last full refresh)", project_id, user["username"])

    return {"project": row}


# ---------------------------------------------------------------------------
# Monthly PP (Earned Revenue) report
# ---------------------------------------------------------------------------

@app.get("/pp-report", response_class=HTMLResponse)
def pp_report_page(request: Request, month: str = ""):
    user = require_login(request)
    if not user:
        return RedirectResponse("/login")

    settings = settings_store.get_app_settings()
    if not (settings["redmine_url"] and settings["api_key"]):
        return templates.TemplateResponse("pp_report.html", {
            "request": request, "user": user, "has_settings": False,
            "month": month or pp_report.next_month_to_lock(), "report": None,
            **_refresh_timing_context(),
        })

    # Default to the earliest month not yet locked — the next thing
    # actually needing attention — rather than today's calendar month,
    # which may already be closed out or may be well ahead of where
    # locking has actually gotten to.
    month = month or pp_report.next_month_to_lock()
    projects, fetched_on = settings_store.load_cache("projects")
    timespent, _ = settings_store.load_cache("timespent")
    projects = _visible_projects(projects, user)
    timespent = _visible_timespent(timespent, {p["id"] for p in projects})
    report = pp_report.build_report(month, projects, timespent)
    report["rows"] = _visible_report_rows(report["rows"], user)

    country_summary = pp_report.country_summary_for(report["rows"])
    country_project_breakdown = pp_report.country_project_breakdown_for(report["rows"])
    prev_month = pp_report.previous_month_str(month)
    prev_month_meta = pp_report.get_month_meta(prev_month)
    profitability_rows = pp_report.profitability_rows_for(report["rows"])
    return templates.TemplateResponse("pp_report.html", {
        "request": request, "user": user, "has_settings": True,
        "month": month, "report": report, "fetched_on": fetched_on,
        "totals": pp_report.totals_for(report["rows"]),
        "prev_month": prev_month,
        "prev_month_label": pp_report.format_month_label(prev_month),
        "prev_month_locked": prev_month_meta["locked"],
        "prev_month_reopened_by": prev_month_meta["reopened_by"],
        "prev_month_reopened_on": prev_month_meta["reopened_on"],
        "lock_blocked_reason": pp_report.check_lock_order(month) if not report["locked"] else None,
        "unlock_blocked_reason": pp_report.check_unlock_order(month) if report["locked"] else None,
        "country_summary": country_summary,
        "country_project_breakdown": country_project_breakdown,
        "pie_slices": pp_report.pie_slices_for(country_summary),
        "is_opening_balance_month": month == pp_report.OPENING_BALANCE_MONTH,
        "performance_rows": pp_report.performance_rows_for(report["rows"]),
        "profitability_rows": profitability_rows,
        "profitability_country_summary": pp_report.profitability_country_summary_for(profitability_rows),
        **_refresh_timing_context(),
    })


@app.get("/api/pp-report/{month}/lock-status")
def api_pp_lock_status(month: str, request: Request):
    """Polled periodically by the page (see pp_report.html) so a banner
    about the previous month having been reopened — and the Lock/Reopen
    buttons' enabled state — can update live without a manual reload,
    since another user's lock/unlock action elsewhere isn't otherwise
    visible until the next full page load."""
    user = require_login(request)
    if not user:
        return JSONResponse(status_code=401, content={"detail": "Not logged in."})

    meta = pp_report.get_month_meta(month)
    prev_month = pp_report.previous_month_str(month)
    prev_meta = pp_report.get_month_meta(prev_month)
    return {
        "locked": meta["locked"],
        "prev_month_label": pp_report.format_month_label(prev_month),
        "prev_month_locked": prev_meta["locked"],
        "prev_month_reopened_by": prev_meta["reopened_by"],
        "prev_month_reopened_on": prev_meta["reopened_on"],
        "lock_blocked_reason": pp_report.check_lock_order(month) if not meta["locked"] else None,
        "unlock_blocked_reason": pp_report.check_unlock_order(month) if meta["locked"] else None,
    }


class RateUpdate(BaseModel):
    daily_rate: float


@app.post("/api/pp-report/{month}/rate")
def api_pp_set_rate(month: str, body: RateUpdate, request: Request):
    user = require_login(request)
    if not user:
        return JSONResponse(status_code=401, content={"detail": "Not logged in."})
    meta = pp_report.get_month_meta(month)
    if meta["locked"]:
        return JSONResponse(status_code=400, content={"detail": "This month is locked — reopen it first."})
    pp_report.set_daily_rate(month, body.daily_rate)
    return {"status": "ok"}


class DraftRow(BaseModel):
    project_id: int
    total_revenue: str = ""  # plain number or "a+b+c" — see pp_report.parse_revenue_input
    risk: str = ""
    notes: str = ""
    completion: str = ""  # percentage (e.g. "62.5"); blank means "use the formula-derived value"


class DraftSave(BaseModel):
    rows: list[DraftRow]


class LockRequest(BaseModel):
    # The currently-visible draft, auto-saved before locking — so Lock can
    # never silently discard in-progress edits that were never explicitly
    # "Save draft"-ed (see api_pp_lock). Empty by default: locking with no
    # unsaved edits (e.g. right after a Save draft) doesn't need to send any.
    rows: list[DraftRow] = []


def _sync_risk_to_redmine(project_id: int, risk_value: float, projects_by_id: dict, settings: dict):
    """Pushes a PP Report Risk edit back to Redmine's own 'Potential Risk'
    custom field too — only when it actually differs from what's already
    cached (Save draft/Lock resubmit the WHOLE visible table every time,
    not just changed rows, so this guard is what keeps a table-wide save
    from hammering Redmine with ~400 no-op writes). Only ever reached for
    an unlocked month — locked months already refuse the save before this
    point — so this only affects the currently open month, as intended.
    Failures (e.g. a Closed project Redmine won't allow editing) are
    logged and swallowed rather than failing the whole draft save over one
    project's Redmine write."""
    cached = projects_by_id.get(project_id)
    cached_risk = pp_report.to_float(cached.get("risk")) if cached else None
    if cached_risk is not None and abs(cached_risk - risk_value) <= 0.005:
        return
    try:
        _write_custom_fields(settings["redmine_url"], settings["api_key"], project_id, {EDITABLE_FIELD_IDS["risk"]: str(risk_value)})
        settings_store._patch_cached_project(project_id, risk=str(risk_value))
        if cached is not None:
            cached["risk"] = str(risk_value)
    except RedmineError as e:
        logger.warning("PP report Risk->Redmine sync failed for project #%s: %s", project_id, e)


def _save_draft_rows(month: str, rows: list) -> str | None:
    """Validates and persists one batch of draft rows (see api_pp_save_draft).
    Returns an error message if any row is invalid, else None. Shared with
    api_pp_lock's auto-save so both endpoints apply identical validation.

    Also pushes any real Risk value to Redmine's own 'Potential Risk' field
    (see _sync_risk_to_redmine) — loads the projects cache/App Settings
    once up front rather than per-row, since a save can cover ~400 rows."""
    settings = settings_store.get_app_settings()
    can_sync_to_redmine = bool(settings["redmine_url"] and settings["api_key"])
    projects, _ = settings_store.load_cache("projects")
    projects_by_id = {p["id"]: p for p in projects}

    for row in rows:
        if row.project_id <= 0:
            continue  # KSA rows are read-only lines from the uploaded workbook (negative ids) — never a draft
        try:
            total_revenue = pp_report.parse_revenue_input(row.total_revenue)
        except ValueError:
            return f"Project #{row.project_id}: Total Project Revenue must be a number (or a '+' separated sum, e.g. 19500+4900)."

        risk_text = row.risk.strip()
        risk_value = None
        if risk_text:
            try:
                risk_value = float(risk_text)
            except ValueError:
                return f"Project #{row.project_id}: Risk must be a number."

        completion_text = row.completion.strip()
        completion_value = None
        if completion_text:
            try:
                completion_value = float(completion_text) / 100.0
            except ValueError:
                return f"Project #{row.project_id}: Project Completion must be a number (percentage)."

        pp_report.save_override(month, row.project_id, total_revenue, risk_value, row.notes.strip(), completion_value)
        if risk_value is not None and can_sync_to_redmine:
            _sync_risk_to_redmine(row.project_id, risk_value, projects_by_id, settings)
    return None


@app.post("/api/pp-report/{month}/save-draft")
def api_pp_save_draft(month: str, body: DraftSave, request: Request):
    """'Save as draft' — persists Total Revenue/Risk/Notes for every row
    submitted in one batch (the whole visible table, not just changed
    rows) into pp_report_overrides. This is explicitly NOT auto-save —
    edits only take effect once this button is clicked. Locking a month
    leaves this draft in place (dormant, since the locked view never reads
    it) rather than clearing it — see pp_report.lock_month — so if the
    month is ever reopened later, Total Revenue/Risk/Notes/Project
    Completion come back without retyping anything."""
    user = require_login(request)
    if not user:
        return JSONResponse(status_code=401, content={"detail": "Not logged in."})

    meta = pp_report.get_month_meta(month)
    if meta["locked"]:
        return JSONResponse(status_code=400, content={"detail": "This month is locked — reopen it first."})

    error = _save_draft_rows(month, body.rows)
    if error:
        return JSONResponse(status_code=400, content={"detail": error})

    logger.info("PP report draft saved by %r for %s (%d row(s))", user["username"], month, len(body.rows))
    return {"status": "ok", "saved": len(body.rows)}


@app.post("/api/pp-report/{month}/lock")
def api_pp_lock(month: str, request: Request, body: LockRequest = LockRequest()):
    """Locking previously read straight from the database — so any edits
    still sitting unsaved in the browser (never "Save draft"-ed) were
    silently discarded instead of being frozen into the snapshot. Now the
    currently-visible draft is auto-saved first (see body.rows) so Lock can
    never lose in-progress edits, whether or not the user remembered to
    click Save draft."""
    user = require_login(request)
    if not user:
        return JSONResponse(status_code=401, content={"detail": "Not logged in."})

    meta = pp_report.get_month_meta(month)
    if meta["locked"]:
        return JSONResponse(status_code=400, content={"detail": "Already locked."})

    order_error = pp_report.check_lock_order(month)
    if order_error:
        logger.warning("PP report lock blocked (out of order) for %s by %r: %s", month, user["username"], order_error)
        return JSONResponse(status_code=400, content={"detail": order_error})

    if body.rows:
        error = _save_draft_rows(month, body.rows)
        if error:
            return JSONResponse(status_code=400, content={"detail": error})

    projects, _ = settings_store.load_cache("projects")
    timespent, _ = settings_store.load_cache("timespent")
    report = pp_report.build_report(month, projects, timespent)

    pp_report.lock_month(month, report["rows"])
    logger.info("PP report locked by %r for %s (auto-saved %d row(s) first)", user["username"], month, len(body.rows))
    return {"status": "ok"}


@app.post("/api/pp-report/{month}/unlock")
def api_pp_unlock(month: str, request: Request):
    user = require_login(request)
    if not user:
        return JSONResponse(status_code=401, content={"detail": "Not logged in."})

    order_error = pp_report.check_unlock_order(month)
    if order_error:
        logger.warning("PP report reopen blocked (out of order) for %s by %r: %s", month, user["username"], order_error)
        return JSONResponse(status_code=400, content={"detail": order_error})

    pp_report.unlock_month(month, user["username"])
    logger.info("PP report reopened by %r for %s", user["username"], month)
    return {"status": "ok"}


@app.get("/pp-report/{month}/export")
def pp_report_export(month: str, request: Request):
    user = require_login(request)
    if not user:
        return RedirectResponse("/login")

    projects, _ = settings_store.load_cache("projects")
    timespent, _ = settings_store.load_cache("timespent")
    projects = _visible_projects(projects, user)
    timespent = _visible_timespent(timespent, {p["id"] for p in projects})
    report = pp_report.build_report(month, projects, timespent)
    report["rows"] = _visible_report_rows(report["rows"], user)

    xlsx_bytes = pp_report.build_workbook(month, report["daily_rate"], report["rows"])
    logger.info("PP report exported by %r for %s", user["username"], month)

    filename = f"PP Report {month}.xlsx"
    return Response(
        content=xlsx_bytes,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.get("/pp-report/{month}/performance-export")
def pp_report_performance_export(month: str, request: Request):
    user = require_login(request)
    if not user:
        return RedirectResponse("/login")

    projects, _ = settings_store.load_cache("projects")
    timespent, _ = settings_store.load_cache("timespent")
    projects = _visible_projects(projects, user)
    timespent = _visible_timespent(timespent, {p["id"] for p in projects})
    report = pp_report.build_report(month, projects, timespent)
    report["rows"] = _visible_report_rows(report["rows"], user)

    performance_rows = pp_report.performance_rows_for(report["rows"])
    xlsx_bytes = pp_report.build_performance_workbook(month, performance_rows)
    logger.info("Projects Performance report exported by %r for %s", user["username"], month)

    filename = f"Projects Performance {month}.xlsx"
    return Response(
        content=xlsx_bytes,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.get("/pp-report/{month}/profitability-export")
def pp_report_profitability_export(month: str, request: Request):
    user = require_login(request)
    if not user:
        return RedirectResponse("/login")

    projects, _ = settings_store.load_cache("projects")
    timespent, _ = settings_store.load_cache("timespent")
    projects = _visible_projects(projects, user)
    timespent = _visible_timespent(timespent, {p["id"] for p in projects})
    report = pp_report.build_report(month, projects, timespent)
    report["rows"] = _visible_report_rows(report["rows"], user)

    profitability_rows = pp_report.profitability_rows_for(report["rows"])
    country_summary = pp_report.profitability_country_summary_for(profitability_rows)
    xlsx_bytes = pp_report.build_profitability_workbook(month, profitability_rows, country_summary, settings_store.list_countries())
    logger.info("Project Profitability report exported by %r for %s", user["username"], month)

    filename = f"Project Profitability {month}.xlsx"
    return Response(
        content=xlsx_bytes,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.get("/pp-report/{month}/country-project-export")
def pp_report_country_project_export(month: str, request: Request):
    user = require_login(request)
    if not user:
        return RedirectResponse("/login")

    projects, _ = settings_store.load_cache("projects")
    timespent, _ = settings_store.load_cache("timespent")
    projects = _visible_projects(projects, user)
    timespent = _visible_timespent(timespent, {p["id"] for p in projects})
    report = pp_report.build_report(month, projects, timespent)
    report["rows"] = _visible_report_rows(report["rows"], user)

    breakdown = pp_report.country_project_breakdown_for(report["rows"])
    grand_total = pp_report.totals_for(report["rows"])["amountToBeTaken"]
    xlsx_bytes = pp_report.build_country_project_workbook(month, breakdown, grand_total)
    logger.info("PP report country x project breakdown exported by %r for %s", user["username"], month)

    filename = f"PP Report Country x Project {month}.xlsx"
    return Response(
        content=xlsx_bytes,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


# ---------------------------------------------------------------------------
# Resource Planning — rolling 6-month planned hours per consultant per
# project, and capacity/utilization roll-up
# ---------------------------------------------------------------------------

@app.get("/resource-planning", response_class=HTMLResponse)
def resource_planning_page(request: Request, import_ok: str = "", import_error: str = ""):
    user = require_login(request)
    if not user:
        return RedirectResponse("/login")

    projects, fetched_on = settings_store.load_cache("projects")
    timespent, _ = settings_store.load_cache("timespent")
    projects = _visible_projects(projects, user)
    timespent = _visible_timespent(timespent, {p["id"] for p in projects})
    if not _assigned_manager_id(user):
        # KSA (from the uploaded workbook/timesheets) is added for anyone not
        # restricted to a single PM — KSA projects have no Redmine manager.
        projects, timespent = pp_report.with_ksa(projects, timespent)

    months = resource_planning.plan_months()
    rows = resource_planning.build_plan(projects, timespent, months)
    summary = resource_planning.consultant_summary(rows, months)
    roster = settings_store.distinct_timesheet_users(timespent)
    my_team_ids = [t.strip() for t in settings_store.get_user_prefs(user["id"])["my_team_ids"].split(",") if t.strip()]

    return templates.TemplateResponse("resource_planning.html", {
        "request": request, "user": user, "fetched_on": fetched_on,
        "months": months, "month_labels": [resource_planning.format_month_label(m) for m in months],
        "rows": rows, "summary": summary, "roster": roster,
        "my_team_ids": my_team_ids,
        "import_ok": import_ok, "import_error": import_error,
        **_refresh_timing_context(),
    })


class ResourcePlanReassign(BaseModel):
    project_id: int
    user_id: int
    user_name: str


@app.post("/api/resource-planning/reassign")
def api_resource_planning_reassign(body: ResourcePlanReassign, request: Request):
    user = require_login(request)
    if not user:
        return JSONResponse(status_code=401, content={"detail": "Not logged in."})

    resource_planning.save_assignee_override(body.project_id, body.user_id, body.user_name)
    logger.info(
        "Resource plan project %s reassigned to %r (id=%s) by %r",
        body.project_id, body.user_name, body.user_id, user["username"],
    )
    return {"status": "ok"}


class ResourcePlanSplit(BaseModel):
    project_id: int
    user_id: int
    user_name: str
    monthly: dict[str, float]


@app.post("/api/resource-planning/split")
def api_resource_planning_split(body: ResourcePlanSplit, request: Request):
    """Adds an extra, manually-editable planning row for `user_id` on
    project_id — the Split button's "duplicate the line" action. `monthly`
    is normally a straight copy of whatever the project's row was
    showing at the moment the button was clicked, so both rows start
    identical and the two can then be manually rebalanced."""
    user = require_login(request)
    if not user:
        return JSONResponse(status_code=401, content={"detail": "Not logged in."})

    resource_planning.add_extra_assignee(body.project_id, body.user_id, body.user_name, body.monthly)
    logger.info(
        "Resource plan project %s split — added row for %r (id=%s) by %r",
        body.project_id, body.user_name, body.user_id, user["username"],
    )
    return {"status": "ok"}


class ResourcePlanSplitRemove(BaseModel):
    project_id: int
    user_id: int


@app.post("/api/resource-planning/split/remove")
def api_resource_planning_split_remove(body: ResourcePlanSplitRemove, request: Request):
    user = require_login(request)
    if not user:
        return JSONResponse(status_code=401, content={"detail": "Not logged in."})

    resource_planning.remove_extra_assignee(body.project_id, body.user_id)
    logger.info("Resource plan project %s split row for user %s removed by %r", body.project_id, body.user_id, user["username"])
    return {"status": "ok"}


def _utilization_report_data(user: dict, from_month: str, to_month: str):
    projects, fetched_on = settings_store.load_cache("projects")
    timespent, _ = settings_store.load_cache("timespent")
    projects = _visible_projects(projects, user)
    timespent = _visible_timespent(timespent, {p["id"] for p in projects})
    if not _assigned_manager_id(user):
        # KSA (from the uploaded workbook/timesheets) is added for anyone not
        # restricted to a single PM — KSA projects have no Redmine manager.
        projects, timespent = pp_report.with_ksa(projects, timespent)

    months = resource_planning.month_range(from_month, to_month)
    rows = resource_planning.actual_hours_for_months(projects, timespent, months)
    summary = resource_planning.actual_consultant_summary(rows, months)
    return fetched_on, months, rows, summary


@app.get("/utilization-report", response_class=HTMLResponse)
def utilization_report_page(request: Request, from_month: str = "", to_month: str = ""):
    user = require_login(request)
    if not user:
        return RedirectResponse("/login")

    last_month = resource_planning.previous_month_str(resource_planning.current_month_str())
    to_month = to_month or last_month
    from_month = from_month or resource_planning.previous_month_str(resource_planning.previous_month_str(to_month))
    fetched_on, months, rows, summary = _utilization_report_data(user, from_month, to_month)

    return templates.TemplateResponse("utilization_report.html", {
        "request": request, "user": user, "fetched_on": fetched_on,
        "from_month": months[0], "to_month": months[-1],
        "months": months, "month_labels": [resource_planning.format_month_label(m) for m in months],
        "rows": rows, "summary": summary,
        **_refresh_timing_context(),
    })


@app.get("/utilization-report/export")
def utilization_report_export(request: Request, from_month: str = "", to_month: str = ""):
    user = require_login(request)
    if not user:
        return RedirectResponse("/login")

    last_month = resource_planning.previous_month_str(resource_planning.current_month_str())
    to_month = to_month or last_month
    from_month = from_month or resource_planning.previous_month_str(resource_planning.previous_month_str(to_month))
    _, months, rows, summary = _utilization_report_data(user, from_month, to_month)

    xlsx_bytes = resource_planning.build_utilization_workbook(months, rows, summary)
    logger.info("Utilization report exported by %r for %s..%s", user["username"], months[0], months[-1])

    filename = f"Utilization Report {months[0]} to {months[-1]}.xlsx"
    return Response(
        content=xlsx_bytes,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.get("/resource-planning/export")
def resource_planning_export(request: Request):
    user = require_login(request)
    if not user:
        return RedirectResponse("/login")

    projects, _ = settings_store.load_cache("projects")
    timespent, _ = settings_store.load_cache("timespent")
    projects = _visible_projects(projects, user)
    timespent = _visible_timespent(timespent, {p["id"] for p in projects})
    if not _assigned_manager_id(user):
        # KSA (from the uploaded workbook/timesheets) is added for anyone not
        # restricted to a single PM — KSA projects have no Redmine manager.
        projects, timespent = pp_report.with_ksa(projects, timespent)

    months = resource_planning.plan_months()
    rows = resource_planning.build_plan(projects, timespent, months)
    summary = resource_planning.consultant_summary(rows, months)

    xlsx_bytes = resource_planning.build_resource_plan_workbook(months, rows, summary)
    logger.info("Resource Planning exported by %r for %s..%s", user["username"], months[0], months[-1])

    filename = f"Resource Planning {months[0]} to {months[-1]}.xlsx"
    return Response(
        content=xlsx_bytes,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.post("/resource-planning/import")
async def resource_planning_import(request: Request, file: UploadFile = File(...)):
    """Import planned hours from a previously-exported plan workbook (its
    'Detail' sheet, keyed on the Project ID / User ID columns). Only the month
    columns matching the current window are applied; closed/removed projects in
    the file that aren't plannable now are still written (harmless — they just
    won't surface), so round-tripping an export is lossless for the user."""
    user = require_login(request)
    if not user:
        return RedirectResponse("/login")

    months = resource_planning.plan_months()
    try:
        data = await file.read()
        cells, stats = resource_planning.parse_resource_plan_workbook(data, months)
    except resource_planning.ImportError_ as e:
        return RedirectResponse(f"/resource-planning?import_error={quote(str(e))}", status_code=302)
    except Exception as e:
        logger.exception("Resource Planning import failed for %r", user["username"])
        return RedirectResponse(f"/resource-planning?import_error={quote('Unexpected error reading the file: ' + str(e))}", status_code=302)

    if not cells:
        return RedirectResponse(f"/resource-planning?import_error={quote('No rows to import were found in the file.')}", status_code=302)

    resource_planning.save_planned_hours_batch(cells)
    logger.info("Resource Planning imported by %r: %d cells across %d row(s), %d skipped",
                user["username"], stats["cells"], stats["rows"], stats["skipped"])
    msg = f"Imported {stats['rows']} project row(s) across {stats['months_matched']} month(s)."
    if stats["skipped"]:
        msg += f" {stats['skipped']} row(s) without a Project/User ID were skipped."
    return RedirectResponse(f"/resource-planning?import_ok={quote(msg)}", status_code=302)


class ResourcePlanCellUpdate(BaseModel):
    project_id: int
    user_id: int
    month: str
    hours: float


@app.post("/api/resource-planning/cell")
def api_resource_planning_save_cell(body: ResourcePlanCellUpdate, request: Request):
    user = require_login(request)
    if not user:
        return JSONResponse(status_code=401, content={"detail": "Not logged in."})
    if body.hours < 0:
        return JSONResponse(status_code=400, content={"detail": "Planned hours cannot be negative."})

    resource_planning.save_planned_hours(body.project_id, body.user_id, body.month, body.hours)
    logger.info(
        "Resource plan cell updated by %r: project=%s user=%s month=%s hours=%s",
        user["username"], body.project_id, body.user_id, body.month, body.hours,
    )
    return {"status": "ok"}


class ResourcePlanSaveAll(BaseModel):
    cells: list[ResourcePlanCellUpdate]


@app.post("/api/resource-planning/save-all")
def api_resource_planning_save_all(body: ResourcePlanSaveAll, request: Request):
    """The Save button — persists every hour cell currently on screen in
    one batch, regardless of whether each one's already been saved by
    its own on-blur save. The over-estimate warning itself is computed
    client-side from the same page state, since every row already
    carries its project's remaining-hours figure."""
    user = require_login(request)
    if not user:
        return JSONResponse(status_code=401, content={"detail": "Not logged in."})
    if any(c.hours < 0 for c in body.cells):
        return JSONResponse(status_code=400, content={"detail": "Planned hours cannot be negative."})

    resource_planning.save_planned_hours_batch([c.dict() for c in body.cells])
    logger.info("Resource plan saved by %r: %d cells", user["username"], len(body.cells))
    return {"status": "ok"}


# ---------------------------------------------------------------------------
# Admin — per-consultant hourly rate (scaffolding for a future switch away
# from the per-country Daily Rate; not yet consumed by any report)
# ---------------------------------------------------------------------------

@app.get("/admin/employees", response_class=HTMLResponse)
def employees_list(request: Request):
    user = require_login(request)
    if not user:
        return RedirectResponse("/login")
    if not user["is_admin"]:
        return RedirectResponse("/dashboard")

    timespent, _ = settings_store.load_cache("timespent")
    roster = pp_report.roster_with_ksa(timespent or [])
    rates = settings_store.get_employee_rates()
    saved_ids = {e["redmine_user_id"] for e in settings_store.list_employees()}
    # Union of the current timesheet roster and anyone with a previously
    # saved rate (e.g. no longer logging time this refresh) — nobody with
    # a saved rate silently disappears just because the cache moved on.
    saved_only = [e for e in settings_store.list_employees() if e["redmine_user_id"] not in {u["id"] for u in roster}]
    employees = sorted(
        [{"id": u["id"], "name": u["name"], "hourly_rate": rates.get(u["id"], 0.0)} for u in roster]
        + [{"id": e["redmine_user_id"], "name": e["redmine_user_name"], "hourly_rate": e["hourly_rate"]} for e in saved_only],
        key=lambda e: e["name"].lower(),
    )
    return templates.TemplateResponse(
        "employees.html",
        {"request": request, "user": user, "employees": employees, "saved_ids": saved_ids, "error": None, "ok": None},
    )


@app.post("/admin/employees/{user_id}", response_class=HTMLResponse)
def employees_set_rate(request: Request, user_id: int, name: str = Form(""), hourly_rate: str = Form(...)):
    user = require_login(request)
    if not user:
        return RedirectResponse("/login")
    if not user["is_admin"]:
        return RedirectResponse("/dashboard")

    error, ok = None, None
    try:
        rate = float(hourly_rate)
        if rate < 0:
            raise ValueError
    except ValueError:
        error = "Enter a valid hourly rate."
    else:
        settings_store.set_employee_rate(user_id, name, rate)
        logger.info("Employee id=%s (%r) hourly rate set to %s by %r", user_id, name, rate, user["username"])
        ok = "Hourly rate saved."

    timespent, _ = settings_store.load_cache("timespent")
    roster = pp_report.roster_with_ksa(timespent or [])
    rates = settings_store.get_employee_rates()
    saved_only = [e for e in settings_store.list_employees() if e["redmine_user_id"] not in {u["id"] for u in roster}]
    employees = sorted(
        [{"id": u["id"], "name": u["name"], "hourly_rate": rates.get(u["id"], 0.0)} for u in roster]
        + [{"id": e["redmine_user_id"], "name": e["redmine_user_name"], "hourly_rate": e["hourly_rate"]} for e in saved_only],
        key=lambda e: e["name"].lower(),
    )
    saved_ids = {e["redmine_user_id"] for e in settings_store.list_employees()}
    return templates.TemplateResponse(
        "employees.html",
        {"request": request, "user": user, "employees": employees, "saved_ids": saved_ids, "error": error, "ok": ok},
    )


# ---------------------------------------------------------------------------
# Application log viewer (so issues can be diagnosed without shell access)
# ---------------------------------------------------------------------------

@app.get("/admin/log", response_class=PlainTextResponse)
def view_log(request: Request, lines: int = 300):
    user = require_login(request)
    if not user:
        return RedirectResponse("/login")
    if not user["is_admin"]:
        return PlainTextResponse("Forbidden — admin only.", status_code=403)
    if not os.path.exists(LOG_PATH):
        return PlainTextResponse("No log file yet.")
    with open(LOG_PATH, "r", encoding="utf-8", errors="replace") as f:
        all_lines = f.readlines()
    tail = all_lines[-max(1, min(lines, 5000)):]
    return PlainTextResponse("".join(tail) or "(log file is empty)")
