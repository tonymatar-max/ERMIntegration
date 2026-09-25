"""Shared "run a Redmine refresh" logic — used by both the manual
/api/refresh endpoint (main.py) and the auto-refresh scheduler
(scheduler.py), so there's exactly one place that fetches, caches, and
records the result regardless of what triggered it."""

from . import settings_store
from .logging_config import logger
from .redmine_client import RedmineError, build_report


def run_refresh(started_by: str, trigger: str):
    """trigger is 'manual' or 'auto' — determines which of
    last_manual_fetch_on/last_auto_fetch_on gets stamped on success (see
    settings_store.set_status)."""
    logger.info("Refresh started by %r (%s)", started_by, trigger)
    try:
        settings = settings_store.get_app_settings()
        query_id = settings["all_statuses_query_id"] or ""
        projects, timespent = build_report(settings["redmine_url"], settings["api_key"], query_id)
        settings_store.save_cache("projects", projects)
        settings_store.save_cache("timespent", timespent)
        settings_store.set_status("done", f"Fetched {len(projects)} project(s).", trigger=trigger)
        logger.info("Refresh done (started by %r, %s): %d project(s)", started_by, trigger, len(projects))
    except RedmineError as e:
        settings_store.set_status("error", str(e))
        logger.warning("Refresh failed (started by %r, %s): %s", started_by, trigger, e)
    except Exception as e:
        settings_store.set_status("error", f"Unexpected error: {e}")
        logger.exception("Refresh crashed (started by %r, %s)", started_by, trigger)
