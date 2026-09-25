"""Application log — data/app.log, rotated so it can't grow unbounded.

Separate from uvicorn's own access log; this captures app-level events
(logins, settings changes, Redmine fetches/errors, review edits) so issues
can be diagnosed without a terminal attached to the running server.
"""

import logging
import os
from logging.handlers import RotatingFileHandler

DEFAULT_LOG_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data")
DEFAULT_LOG_PATH = os.path.join(DEFAULT_LOG_DIR, "app.log")
# Override with ERM_LOG_PATH for local development/testing, mirroring
# ERM_DB_PATH — keeps test runs out of the real app.log.
LOG_PATH = os.environ.get("ERM_LOG_PATH", DEFAULT_LOG_PATH)
LOG_DIR = os.path.dirname(LOG_PATH)

logger = logging.getLogger("erm")


def setup_logging():
    if logger.handlers:
        return  # already configured (e.g. reloader re-import)

    os.makedirs(LOG_DIR, exist_ok=True)
    logger.setLevel(logging.INFO)

    file_handler = RotatingFileHandler(LOG_PATH, maxBytes=2_000_000, backupCount=5, encoding="utf-8")
    file_handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s", datefmt="%Y-%m-%d %H:%M:%S"))
    logger.addHandler(file_handler)

    console_handler = logging.StreamHandler()
    console_handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S"))
    logger.addHandler(console_handler)
