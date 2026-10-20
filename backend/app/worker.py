"""Background worker: polls the job table and processes recordings.

Run with `python -m app.worker` (or the `worker` service in docker-compose).
Inline processing (`PROCESS_INLINE=1`) is the single-process demo alternative.
"""

from __future__ import annotations

import logging
import signal
import time

from . import db
from .config import get_settings
from .pipeline import jobs

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
)
logger = logging.getLogger("worker")

_running = True


def _stop(_signum, _frame):
    global _running
    _running = False
    logger.info("shutdown requested")


def main() -> None:
    settings = get_settings()
    db.init_db()
    signal.signal(signal.SIGINT, _stop)
    signal.signal(signal.SIGTERM, _stop)
    logger.info("worker started (poll=%ss, transcriber=%s)",
                settings.worker_poll_seconds, settings.transcriber)

    while _running:
        try:
            processed = jobs.run_pending_jobs(limit=2)
            if processed == 0:
                time.sleep(settings.worker_poll_seconds)
        except Exception:  # pragma: no cover - keep the worker alive
            logger.exception("worker loop error")
            time.sleep(max(settings.worker_poll_seconds, 2.0))

    logger.info("worker stopped")


if __name__ == "__main__":
    main()
