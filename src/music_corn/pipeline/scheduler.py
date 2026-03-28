"""APScheduler setup for automated pipeline execution."""

import signal
import sys

import structlog
from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from music_corn.config import settings

logger = structlog.get_logger()


def _job_ingest():
    """Scheduled job: ingest from all sources."""
    logger.info("Scheduled ingest starting")
    try:
        from music_corn.pipeline.ingest import run_ingest
        count = run_ingest()
        logger.info("Scheduled ingest complete", new_items=count)
    except Exception:
        logger.exception("Scheduled ingest failed")


def _job_extract():
    """Scheduled job: run LLM extraction on unanalyzed items."""
    logger.info("Scheduled extraction starting")
    try:
        import asyncio
        from music_corn.extraction.extractor import extract_all_unanalyzed
        count = asyncio.run(extract_all_unanalyzed())
        logger.info("Scheduled extraction complete", mentions=count)
    except Exception:
        logger.exception("Scheduled extraction failed")


def _job_resolve():
    """Scheduled job: resolve mentions to Spotify IDs."""
    logger.info("Scheduled resolution starting")
    try:
        import asyncio
        from music_corn.extraction.spotify_resolver import resolve_all_unresolved
        count = asyncio.run(resolve_all_unresolved())
        logger.info("Scheduled resolution complete", resolved=count)
    except Exception:
        logger.exception("Scheduled resolution failed")


def _job_weekly():
    """Scheduled job: full weekly pipeline."""
    logger.info("Scheduled weekly pipeline starting")
    try:
        from music_corn.pipeline.weekly import run_weekly_sync
        success = run_weekly_sync()
        logger.info("Scheduled weekly pipeline complete", success=success)
    except Exception:
        logger.exception("Scheduled weekly pipeline failed")


def create_scheduler() -> BlockingScheduler:
    """Create and configure the scheduler with all jobs."""
    scheduler = BlockingScheduler()

    # Ingest from sources every N hours
    scheduler.add_job(
        _job_ingest,
        trigger=IntervalTrigger(hours=settings.ingest_interval_hours),
        id="ingest",
        name="Source ingestion",
        replace_existing=True,
    )

    # Extract mentions 30 min after each ingest cycle
    scheduler.add_job(
        _job_extract,
        trigger=IntervalTrigger(hours=settings.ingest_interval_hours, minutes=30),
        id="extract",
        name="LLM extraction",
        replace_existing=True,
    )

    # Resolve to Spotify 1 hour after each ingest cycle
    scheduler.add_job(
        _job_resolve,
        trigger=IntervalTrigger(hours=settings.ingest_interval_hours, minutes=60),
        id="resolve",
        name="Spotify resolution",
        replace_existing=True,
    )

    # Weekly pipeline on configured day/hour
    day_map = {"mon": "0", "tue": "1", "wed": "2", "thu": "3", "fri": "4", "sat": "5", "sun": "6"}
    day_of_week = day_map.get(settings.weekly_pipeline_day.lower(), "0")

    scheduler.add_job(
        _job_weekly,
        trigger=CronTrigger(
            day_of_week=day_of_week,
            hour=settings.weekly_pipeline_hour,
            minute=0,
        ),
        id="weekly",
        name="Weekly podcast pipeline",
        replace_existing=True,
    )

    return scheduler


def start_scheduler():
    """Start the scheduler (blocking)."""
    scheduler = create_scheduler()

    def _shutdown(signum, frame):
        logger.info("Shutting down scheduler")
        scheduler.shutdown(wait=False)
        sys.exit(0)

    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT, _shutdown)

    logger.info(
        "Scheduler starting",
        ingest_interval_h=settings.ingest_interval_hours,
        weekly_day=settings.weekly_pipeline_day,
        weekly_hour=settings.weekly_pipeline_hour,
    )

    # Run initial ingest on startup
    logger.info("Running initial ingest on startup")
    _job_ingest()

    scheduler.start()
