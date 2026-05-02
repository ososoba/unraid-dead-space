"""APScheduler integration: cron-driven background sync.

Reads `SYNC_CRON` (default `0 4 * * *`) and `TZ` (default UTC) and adds
a single recurring job that calls `start_background_sync`. Lifecycle
managed by the FastAPI app's lifespan.

Single Uvicorn worker is required so this scheduler runs once per
process; PLAN.md decision #10 + the serve CLI's --workers default both
enforce that.
"""

from __future__ import annotations

import logging
import os
from os import PathLike

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from dms.config import load_config
from dms.sync.background import start_background_sync

logger = logging.getLogger(__name__)

DEFAULT_CRON = "0 4 * * *"
JOB_ID = "dms_scheduled_sync"


def _trigger() -> CronTrigger:
    cron = (os.environ.get("SYNC_CRON") or DEFAULT_CRON).strip()
    tz = (os.environ.get("TZ") or "UTC").strip()
    return CronTrigger.from_crontab(cron, timezone=tz)


def build_scheduler(db_path: str | PathLike[str]) -> AsyncIOScheduler:
    """Create (but do not start) a scheduler with the sync cron job."""
    scheduler = AsyncIOScheduler()

    def _fire() -> None:
        # The scheduler thread does not own the asyncio loop FastAPI runs in;
        # AsyncIOScheduler ensures the coroutine is dispatched onto the loop.
        config = load_config()
        started = start_background_sync(
            db_path,
            config,
            kind="full",
            requested_by="scheduler",
        )
        if not started:
            logger.info("scheduled sync skipped — previous task still running")

    scheduler.add_job(
        _fire,
        trigger=_trigger(),
        id=JOB_ID,
        replace_existing=True,
        coalesce=True,
        max_instances=1,
    )
    return scheduler
