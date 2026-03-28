"""PodcastIndex.org API client."""

import hashlib
import time
from datetime import datetime, timezone

import httpx
import structlog

from music_corn.config import settings
from music_corn.discovery.models import PodcastResult, SearchFilters, normalize_genres

logger = structlog.get_logger()

BASE_URL = "https://api.podcastindex.org/api/1.0"


def _auth_headers() -> dict:
    """Generate HMAC auth headers for PodcastIndex API."""
    api_key = settings.podcastindex_api_key
    api_secret = settings.podcastindex_api_secret
    epoch = str(int(time.time()))
    data = api_key + api_secret + epoch
    sha1 = hashlib.sha1(data.encode("utf-8")).hexdigest()
    return {
        "X-Auth-Key": api_key,
        "X-Auth-Date": epoch,
        "Authorization": sha1,
        "User-Agent": "music-corn/1.0",
    }


def _parse_podcast(data: dict) -> PodcastResult:
    """Parse a PodcastIndex podcast result into a PodcastResult."""
    # Parse categories
    raw_genres = []
    categories = data.get("categories", {})
    if isinstance(categories, dict):
        raw_genres = list(categories.values())
    elif isinstance(categories, list):
        raw_genres = categories

    # Parse latest episode date
    latest_at = None
    newest_ts = data.get("newestItemPubdate") or data.get("lastUpdateTime")
    if newest_ts and isinstance(newest_ts, (int, float)) and newest_ts > 0:
        latest_at = datetime.fromtimestamp(newest_ts, tz=timezone.utc)

    trending = data.get("trendScore")

    return PodcastResult(
        title=data.get("title", ""),
        author=data.get("author"),
        description=data.get("description", ""),
        feed_url=data.get("url") or data.get("originalUrl", ""),
        website_url=data.get("link"),
        image_url=data.get("image") or data.get("artwork"),
        language=data.get("language"),
        country=None,  # PodcastIndex doesn't provide country
        genres=normalize_genres(raw_genres),
        episode_count=data.get("episodeCount"),
        latest_episode_at=latest_at,
        podcastindex_trending=float(trending) if trending else None,
        api_source="podcastindex",
        raw_data=data,
    )


async def search(filters: SearchFilters) -> list[PodcastResult]:
    """Search PodcastIndex for podcasts matching filters."""
    if not settings.podcastindex_api_key:
        logger.warning("PodcastIndex API key not configured, skipping")
        return []

    # Build query — combine user query with genre if provided
    query = filters.query
    if filters.genre:
        query = f"{filters.genre} {query}"

    params = {"q": query, "max": min(filters.limit, 100)}

    if filters.language:
        params["language"] = filters.language

    logger.info("Searching PodcastIndex", query=query)

    async with httpx.AsyncClient(timeout=30) as client:
        response = await client.get(
            f"{BASE_URL}/search/byterm",
            params=params,
            headers=_auth_headers(),
        )
        response.raise_for_status()
        data = response.json()

    feeds = data.get("feeds", [])
    results = [_parse_podcast(f) for f in feeds if f.get("url")]

    # Filter by genre if specified
    if filters.genre:
        genre_lower = filters.genre.lower()
        results = [
            r for r in results
            if genre_lower in [g.lower() for g in r.genres]
            or genre_lower in (r.description or "").lower()
            or genre_lower in r.title.lower()
        ]

    logger.info("PodcastIndex results", count=len(results))
    return results


async def trending(limit: int = 20, language: str | None = None) -> list[PodcastResult]:
    """Get trending podcasts from PodcastIndex."""
    if not settings.podcastindex_api_key:
        return []

    params = {"max": min(limit, 100), "cat": "Music"}
    if language:
        params["lang"] = language

    async with httpx.AsyncClient(timeout=30) as client:
        response = await client.get(
            f"{BASE_URL}/podcasts/trending",
            params=params,
            headers=_auth_headers(),
        )
        response.raise_for_status()
        data = response.json()

    feeds = data.get("feeds", [])
    return [_parse_podcast(f) for f in feeds if f.get("url")]
