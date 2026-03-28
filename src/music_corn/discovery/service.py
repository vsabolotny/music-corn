"""Discovery service — orchestrates search, merge, rank, persist, and promotion."""

import asyncio
import uuid
from datetime import datetime, timezone

import structlog
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from music_corn.db.models import DiscoveredPodcast, Source
from music_corn.db.session import async_session_factory
from music_corn.discovery.clients import listennotes, podcastindex
from music_corn.discovery.models import PodcastResult, SearchFilters
from music_corn.discovery.ranking import compute_quality_rank

logger = structlog.get_logger()


def _merge_results(
    pi_results: list[PodcastResult], ln_results: list[PodcastResult]
) -> list[PodcastResult]:
    """Merge results from PodcastIndex and ListenNotes, deduplicating by feed URL."""
    by_feed: dict[str, PodcastResult] = {}

    # PodcastIndex first (catalog data)
    for r in pi_results:
        if r.feed_url:
            by_feed[r.feed_url.rstrip("/")] = r

    # ListenNotes enriches or adds
    for r in ln_results:
        key = r.feed_url.rstrip("/")
        if key in by_feed:
            existing = by_feed[key]
            # Merge quality signals from ListenNotes into existing
            existing.listen_score = r.listen_score
            if not existing.country:
                existing.country = r.country
            if not existing.genres:
                existing.genres = r.genres
            elif r.genres:
                # Combine genres
                combined = list(set(existing.genres + r.genres))
                existing.genres = combined
            if r.episode_count and (not existing.episode_count or r.episode_count > existing.episode_count):
                existing.episode_count = r.episode_count
            if r.latest_episode_at and (not existing.latest_episode_at or r.latest_episode_at > existing.latest_episode_at):
                existing.latest_episode_at = r.latest_episode_at
            # Merge raw API data
            existing.raw_data = {"podcastindex": existing.raw_data, "listennotes": r.raw_data}
        else:
            r.raw_data = {"listennotes": r.raw_data}
            by_feed[key] = r

    return list(by_feed.values())


async def search_podcasts(filters: SearchFilters) -> list[PodcastResult]:
    """Search both APIs in parallel, merge, and rank results."""
    # Fan out to both APIs
    pi_task = podcastindex.search(filters)
    ln_task = listennotes.search(filters)

    pi_results, ln_results = await asyncio.gather(
        pi_task, ln_task, return_exceptions=True
    )

    # Handle errors gracefully
    if isinstance(pi_results, Exception):
        logger.error("PodcastIndex search failed", error=str(pi_results))
        pi_results = []
    if isinstance(ln_results, Exception):
        logger.error("ListenNotes search failed", error=str(ln_results))
        ln_results = []

    # Merge
    merged = _merge_results(pi_results, ln_results)

    # Compute quality ranks
    for podcast in merged:
        podcast.raw_data["quality_rank"] = compute_quality_rank(podcast)

    # Sort by quality rank descending
    merged.sort(key=lambda p: p.raw_data.get("quality_rank", 0), reverse=True)

    # Apply country filter if API didn't handle it
    if filters.country:
        country_upper = filters.country.upper()
        merged = [
            p for p in merged
            if not p.country or p.country.upper() == country_upper
        ]

    return merged[:filters.limit]


async def persist_results(
    results: list[PodcastResult], search_query: str
) -> int:
    """Save search results to discovered_podcasts table. Returns count of new/updated."""
    now = datetime.now(timezone.utc)
    count = 0

    async with async_session_factory() as session:
        for podcast in results:
            rank = podcast.raw_data.get("quality_rank", compute_quality_rank(podcast))

            stmt = (
                insert(DiscoveredPodcast)
                .values(
                    id=uuid.uuid4(),
                    title=podcast.title,
                    author=podcast.author,
                    description=(podcast.description or "")[:2000],
                    feed_url=podcast.feed_url,
                    website_url=podcast.website_url,
                    image_url=podcast.image_url,
                    language=podcast.language,
                    country=podcast.country,
                    genres=podcast.genres or None,
                    episode_count=podcast.episode_count,
                    latest_episode_at=podcast.latest_episode_at,
                    listen_score=podcast.listen_score,
                    itunes_rating=podcast.itunes_rating,
                    itunes_review_count=podcast.itunes_review_count,
                    podcastindex_trending=podcast.podcastindex_trending,
                    quality_rank=rank,
                    discovered_at=now,
                    last_seen_at=now,
                    search_query=search_query,
                    api_sources=podcast.raw_data,
                )
                .on_conflict_do_update(
                    index_elements=["feed_url"],
                    set_={
                        "title": podcast.title,
                        "author": podcast.author,
                        "episode_count": podcast.episode_count,
                        "latest_episode_at": podcast.latest_episode_at,
                        "listen_score": podcast.listen_score,
                        "podcastindex_trending": podcast.podcastindex_trending,
                        "quality_rank": rank,
                        "last_seen_at": now,
                        "api_sources": podcast.raw_data,
                    },
                )
            )
            await session.execute(stmt)
            count += 1

        await session.commit()

    logger.info("Persisted discovery results", count=count)
    return count


async def list_discovered(
    min_rank: float = 0.0,
    genre: str | None = None,
    limit: int = 50,
) -> list[DiscoveredPodcast]:
    """List discovered podcasts from DB with optional filters."""
    async with async_session_factory() as session:
        query = (
            select(DiscoveredPodcast)
            .where(DiscoveredPodcast.quality_rank >= min_rank)
            .order_by(DiscoveredPodcast.quality_rank.desc())
            .limit(limit)
        )

        if genre:
            query = query.where(DiscoveredPodcast.genres.any(genre.lower()))

        result = await session.execute(query)
        return list(result.scalars().all())


async def promote_to_source(podcast_id: uuid.UUID | None = None, title: str | None = None) -> Source | None:
    """Promote a discovered podcast to an active source for ingestion."""
    async with async_session_factory() as session:
        if podcast_id:
            podcast = await session.get(DiscoveredPodcast, podcast_id)
        elif title:
            result = await session.execute(
                select(DiscoveredPodcast).where(
                    DiscoveredPodcast.title.ilike(f"%{title}%")
                )
            )
            podcast = result.scalar_one_or_none()
        else:
            return None

        if not podcast:
            logger.error("Discovered podcast not found", id=str(podcast_id), title=title)
            return None

        if podcast.promoted_source_id:
            logger.info("Already promoted", source_id=str(podcast.promoted_source_id))
            source = await session.get(Source, podcast.promoted_source_id)
            return source

        # Create source first and flush to get it in the DB
        source = Source(
            id=uuid.uuid4(),
            name=podcast.title,
            url=podcast.feed_url,
            plugin_type="rss",
            config_json={
                "discovered_podcast_id": str(podcast.id),
                "quality_rank": podcast.quality_rank,
                "genres": podcast.genres or [],
            },
            is_active=True,
        )
        session.add(source)
        await session.flush()  # Persist source before setting FK

        # Link back
        podcast.promoted_source_id = source.id
        await session.commit()

        logger.info(
            "Promoted podcast to source",
            title=podcast.title,
            source_id=str(source.id),
        )
        return source
