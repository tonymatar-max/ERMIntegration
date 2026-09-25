"""Auto-refresh scheduler — periodically triggers the same shared Redmine
refresh as clicking "Refresh from Redmine" manually, on an admin-configured
schedule (see App Settings' auto-refresh section). Runs as a background
asyncio task for the life of the server process (started once at app
startup — see main.py); nothing here is per-request state.

All configured times are the SERVER's local time zone — the schedule has
to run consistently regardless of which admin's browser configured it,
same as the original Windows Task Scheduler setup this replaces.
"""

import asyncio
import calendar
from datetime import datetime, timedelta, timezone

from . import settings_store
from .logging_config import logger
from .refresh import run_refresh

CHECK_INTERVAL_SECONDS = 30


def _parse_hhmm(time_str, default=(0, 0)):
    if not time_str:
        return default
    try:
        hh, mm = time_str.split(":")
        return int(hh), int(mm)
    except (ValueError, AttributeError):
        return default


def _monthly_candidate(year, month, day, hour, minute):
    # Clamp to the actual last day of the month — e.g. "31st" in a
    # 30-day month runs on the 30th instead of erroring or skipping.
    last_day = calendar.monthrange(year, month)[1]
    return datetime(year, month, min(day, last_day), hour, minute)


def compute_next_run(schedule: dict, now: datetime, last_auto_fetch_on: datetime = None):
    """Next run time (naive, server-local) for the given schedule config,
    or None if auto-refresh is off. `last_auto_fetch_on` (also naive,
    server-local) only matters for 'interval' mode — the other modes are
    fixed wall-clock times, independent of when they last ran."""
    mode = schedule.get("mode") or "off"
    if mode == "off":
        return None

    if mode == "interval":
        minutes = schedule.get("interval_minutes") or 60
        if last_auto_fetch_on is None:
            return now  # never run yet — fire as soon as the scheduler notices
        next_run = last_auto_fetch_on + timedelta(minutes=minutes)
        # If downtime caused us to miss several intervals, fire once now
        # rather than trying to "catch up" on every missed occurrence.
        return next_run if next_run > now else now

    hour, minute = _parse_hhmm(schedule.get("time"))

    if mode == "daily":
        candidate = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if candidate <= now:
            candidate += timedelta(days=1)
        return candidate

    if mode == "weekly":
        target_weekday = schedule.get("weekday")
        target_weekday = 0 if target_weekday is None else target_weekday
        candidate = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        days_ahead = (target_weekday - candidate.weekday()) % 7
        candidate += timedelta(days=days_ahead)
        if candidate <= now:
            candidate += timedelta(days=7)
        return candidate

    if mode == "monthly":
        day = schedule.get("day_of_month") or 1
        candidate = _monthly_candidate(now.year, now.month, day, hour, minute)
        if candidate <= now:
            if now.month == 12:
                candidate = _monthly_candidate(now.year + 1, 1, day, hour, minute)
            else:
                candidate = _monthly_candidate(now.year, now.month + 1, day, hour, minute)
        return candidate

    return None


def _parse_iso_to_local(value):
    """fetch_status stores timestamps as UTC-aware ISO strings
    (datetime.now(timezone.utc).isoformat()) — converts to a naive
    datetime in the server's local time zone, the same frame
    datetime.now() (and therefore `now` in compute_next_run) uses."""
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value)
    except ValueError:
        return None
    if dt.tzinfo is not None:
        dt = dt.astimezone().replace(tzinfo=None)
    return dt


def compute_next_run_iso(schedule: dict, last_auto_fetch_on_iso: str = None):
    """UTC ISO string of the next scheduled run (or None if off/unset) —
    for display; callers convert to the viewer's local time client-side,
    same as every other timestamp shown in the app."""
    last_local = _parse_iso_to_local(last_auto_fetch_on_iso)
    next_run = compute_next_run(schedule, datetime.now(), last_local)
    if next_run is None:
        return None
    return next_run.astimezone(timezone.utc).isoformat()


async def _tick():
    schedule = settings_store.get_auto_refresh_settings()
    if schedule["mode"] == "off":
        return

    settings = settings_store.get_app_settings()
    if not (settings["redmine_url"] and settings["api_key"]):
        return  # nothing to fetch yet — App Settings not configured

    status = settings_store.get_status()
    if status["state"] == "running":
        return  # a refresh (manual or auto) is already in flight

    now = datetime.now()
    last_auto_fetch_on = _parse_iso_to_local(status.get("last_auto_fetch_on"))
    next_run = compute_next_run(schedule, now, last_auto_fetch_on)
    if next_run is None or now < next_run:
        return

    # Same atomic compare-and-set the manual refresh button uses — closes
    # the race against a manual refresh starting at almost the same instant.
    started = settings_store.try_start_running("Fetching from Redmine (scheduled)...", "(scheduled)")
    if not started:
        return

    logger.info("Auto-refresh due (scheduled for %s, now %s) — starting", next_run, now)
    # Fire-and-forget in a worker thread — run_refresh() is a long,
    # blocking series of HTTP calls (minutes, not seconds); awaiting it
    # directly here would freeze this scheduler loop (and, since it
    # shares the app's event loop, every other request) for that whole
    # duration.
    loop = asyncio.get_running_loop()
    loop.run_in_executor(None, run_refresh, "(scheduled)", "auto")


async def run_forever():
    """Started once at app startup (see main.py) and runs for the life of
    the process, checking every CHECK_INTERVAL_SECONDS whether the next
    scheduled run is due."""
    logger.info("Auto-refresh scheduler started (checks every %ss)", CHECK_INTERVAL_SECONDS)
    while True:
        try:
            await _tick()
        except Exception:
            logger.exception("Auto-refresh scheduler tick crashed")
        await asyncio.sleep(CHECK_INTERVAL_SECONDS)
