"""Source ingestion pipeline — fetches new content from all active sources."""

import asyncio

import structlog
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql import func

from music_corn.db.models import ContentItem, Source
from music_corn.db.session import async_session_factory
from music_corn.sources.registry import get_plugin

logger = structlog.get_logger()


async def ingest_source(session: AsyncSession, source: Source) -> int:
    """Ingest new items from a single source. Returns count of new items."""
    plugin = get_plugin(source.plugin_type)
    items = await plugin.fetch_new_items(source, source.last_fetched_at)

    if not items:
        return 0

    new_count = 0
    for item in items:
        stmt = (
            insert(ContentItem)
            .values(
                id=item.id,
                source_id=item.source_id,
                external_id=item.external_id,
                title=item.title,
                url=item.url,
                published_at=item.published_at,
                raw_text=item.raw_text,
                analyzed=False,
            )
            .on_conflict_do_nothing(constraint="uq_source_external")
        )
        result = await session.execute(stmt)
        if result.rowcount > 0:
            new_count += 1

    source.last_fetched_at = func.now()
    await session.commit()

    logger.info("Ingested source", source=source.name, new_items=new_count)
    return new_count


async def ingest_all_sources() -> int:
    """Ingest from all active sources. Returns total new items."""
    async with async_session_factory() as session:
        result = await session.execute(
            select(Source).where(Source.is_active.is_(True))
        )
        sources = result.scalars().all()

        if not sources:
            logger.info("No active sources to ingest")
            return 0

        total = 0
        for source in sources:
            try:
                count = await ingest_source(session, source)
                total += count
            except Exception:
                logger.exception("Failed to ingest source", source=source.name)

        logger.info("Ingestion complete", total_new_items=total)
        return total


def run_ingest() -> int:
    """Synchronous entry point for the ingestion pipeline."""
    return asyncio.run(ingest_all_sources())
