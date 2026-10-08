"""Database backups — safe, consistent copies of data/erm.db.

Uses SQLite's online backup API (conn.backup) rather than a plain file copy,
so a backup taken while the service is running is always internally consistent
(no half-written pages, respects WAL). Backups land in data/backups/ as
erm-YYYYMMDD-HHMMSS.db; old ones are pruned to a rolling limit.
"""
import os
import sqlite3
import time
from datetime import datetime, timezone

from . import db, settings_store
from .logging_config import logger

DEFAULT_KEEP = 30  # fallback retention when settings are unavailable


def default_backups_dir() -> str:
    return os.path.join(os.path.dirname(db.DB_PATH), "backups")


def backups_dir() -> str:
    """The configured backup directory (App Settings), or the default
    data/backups folder when unset. Created if missing; falls back to the
    default if the configured path can't be created (e.g. a dead network path)."""
    try:
        configured = settings_store.get_backup_settings()["dir"]
    except Exception:
        configured = ""
    d = configured or default_backups_dir()
    try:
        os.makedirs(d, exist_ok=True)
        return d
    except OSError:
        logger.warning("Backup dir %r unavailable; falling back to default", d)
        fallback = default_backups_dir()
        os.makedirs(fallback, exist_ok=True)
        return fallback


def _is_backup_name(name: str) -> bool:
    return name.startswith("erm-") and name.endswith(".db")


def create_backup(label: str = "manual") -> dict:
    """Write a consistent snapshot of the live DB into data/backups/ and prune
    old ones. `label` is recorded only in the log. Returns {name, path, size}."""
    dest_dir = backups_dir()
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    name = f"erm-{stamp}.db"
    path = os.path.join(dest_dir, name)
    # Guard against two backups in the same second overwriting each other.
    n = 1
    while os.path.exists(path):
        name = f"erm-{stamp}-{n}.db"
        path = os.path.join(dest_dir, name)
        n += 1

    src = sqlite3.connect(db.DB_PATH)
    try:
        dst = sqlite3.connect(path)
        try:
            with dst:
                src.backup(dst)
        finally:
            dst.close()
    finally:
        src.close()

    size = os.path.getsize(path)
    logger.info("DB backup created (%s): %s -> %s (%d bytes)", label, name, dest_dir, size)
    _prune(dest_dir)
    return {"name": name, "path": path, "size": size, "dir": dest_dir}


def _prune(dest_dir: str):
    try:
        keep = settings_store.get_backup_settings()["keep"]
    except Exception:
        keep = DEFAULT_KEEP
    files = [f for f in os.listdir(dest_dir) if _is_backup_name(f)]
    files.sort(reverse=True)  # names are timestamped, so newest first
    for old in files[keep:]:
        try:
            os.remove(os.path.join(dest_dir, old))
            logger.info("Pruned old DB backup: %s", old)
        except OSError:
            pass


def list_backups() -> list:
    """Newest-first list of backups with size and mtime (UTC ISO)."""
    dest_dir = backups_dir()
    out = []
    for f in os.listdir(dest_dir):
        if not _is_backup_name(f):
            continue
        p = os.path.join(dest_dir, f)
        try:
            st = os.stat(p)
        except OSError:
            continue
        out.append({
            "name": f, "size": st.st_size,
            "modified": datetime.fromtimestamp(st.st_mtime, timezone.utc).isoformat(),
        })
    out.sort(key=lambda b: b["name"], reverse=True)
    return out


def backup_path(name: str) -> str | None:
    """Resolve a backup file name to its path, guarding against path traversal.
    Returns None if the name isn't a valid backup in the backups dir."""
    if not _is_backup_name(name) or "/" in name or "\\" in name or ".." in name:
        return None
    p = os.path.join(backups_dir(), name)
    return p if os.path.isfile(p) else None


def latest_backup() -> dict | None:
    b = list_backups()
    return b[0] if b else None


# --- daily auto-backup bookkeeping (driven by the scheduler) ---

def last_auto_backup_date() -> str:
    """YYYY-MM-DD of the most recent auto-backup (stored in app_settings)."""
    try:
        return settings_store.get_backup_settings()["last_auto"]
    except Exception:
        return ""


def mark_auto_backup(date_str: str):
    settings_store.mark_backup_done(date_str)
