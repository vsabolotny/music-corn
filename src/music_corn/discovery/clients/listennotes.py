"""ListenNotes API client."""

from datetime import datetime, timezone

import httpx
import structlog

from music_corn.config import settings
from music_corn.discovery.models import (
    LISTENNOTES_MUSIC_GENRE_IDS,
    PodcastResult,
    SearchFilters,
    normalize_genres,
)

logger = structlog.get_logger()

BASE_URL = "https://listen-api.listennotes.com/api/v2"

# ListenNotes genre ID → name mapping (music-related)
GENRE_ID_TO_NAME = {
    134: "music",
    100: "music history",
    67: "pop",
    68: "rock",
    69: "jazz",
    70: "classical",
    71: "hip-hop",
    72: "electronic",
    73: "folk",
    74: "metal",
    75: "world",
    76: "latin",
    77: "r&b",
    78: "indie",
}

# Reverse: name → genre ID (for filtering)
GENRE_NAME_TO_ID = {
    "jazz": 69,
    "rock": 68,
    "electronic": 72,
    "classical": 70,
    "hip-hop": 71,
    "pop": 67,
    "world": 75,
    "metal": 74,
    "folk": 73,
    "indie": 78,
    "r-and-b": 77,
    "latin": 76,
    "music-commentary": 134,
    "music-history": 100,
}


def _parse_podcast(data: dict) -> PodcastResult:
    """Parse a ListenNotes podcast result into a PodcastResult."""
    # Parse genres
    raw_genres = []
    for gid in data.get("genre_ids", []):
        name = GENRE_ID_TO_NAME.get(gid)
        if name:
            raw_genres.append(name)

    # Parse latest episode date
    latest_at = None
    latest_ts = data.get("latest_episode_pub_date_ms") or data.get("latest_pub_date_ms")
    if latest_ts:
        latest_at = datetime.fromtimestamp(latest_ts / 1000, tz=timezone.utc)

    return PodcastResult(
        title=data.get("title_original") or data.get("title", ""),
        author=data.get("publisher_original") or data.get("publisher"),
        description=data.get("description_original") or data.get("description", ""),
        feed_url=data.get("rss", ""),
        website_url=data.get("website"),
        image_url=data.get("image") or data.get("thumbnail"),
        language=data.get("language"),
        country=data.get("country"),
        genres=normalize_genres(raw_genres),
        episode_count=data.get("total_episodes"),
        latest_episode_at=latest_at,
        listen_score=data.get("listen_score"),
        api_source="listennotes",
        raw_data=data,
    )


async def search(filters: SearchFilters) -> list[PodcastResult]:
    """Search ListenNotes for podcasts matching filters."""
    if not settings.listennotes_api_key:
        logger.warning("ListenNotes API key not configured, skipping")
        return []

    params: dict = {
        "q": filters.query,
        "type": "podcast",
        "sort_by_date": 0,  # Sort by relevance
        "offset": 0,
        "len_min": 0,
        "only_in": "title,description",
    }

    # Genre filter
    if filters.genre:
        genre_id = GENRE_NAME_TO_ID.get(filters.genre.lower())
        if genre_id:
            params["genre_ids"] = str(genre_id)
        else:
            # Default to music category
            params["genre_ids"] = "134"
    else:
        # Search within all music genres
        params["genre_ids"] = ",".join(str(gid) for gid in LISTENNOTES_MUSIC_GENRE_IDS)

    # Country filter
    if filters.country:
        params["region"] = filters.country.lower()

    # Language filter
    if filters.language:
        params["language"] = filters.language

    # Year filter — use publish date range
    if filters.year:
        from_ts = int(datetime(filters.year, 1, 1, tzinfo=timezone.utc).timestamp() * 1000)
        to_ts = int(datetime(filters.year, 12, 31, 23, 59, 59, tzinfo=timezone.utc).timestamp() * 1000)
        params["published_after"] = from_ts
        params["published_before"] = to_ts

    logger.info("Searching ListenNotes", query=filters.query, genre=filters.genre)

    async with httpx.AsyncClient(timeout=30) as client:
        response = await client.get(
            f"{BASE_URL}/search",
            params=params,
            headers={"X-ListenAPI-Key": settings.listennotes_api_key},
        )
        response.raise_for_status()
        data = response.json()

    results_data = data.get("results", [])
    results = [_parse_podcast(r) for r in results_data if r.get("rss")]

    logger.info("ListenNotes results", count=len(results))
    return results[:filters.limit]


async def best_podcasts(genre_id: int = 134, region: str | None = None) -> list[PodcastResult]:
    """Get best podcasts by genre from ListenNotes."""
    if not settings.listennotes_api_key:
        return []

    params: dict = {"genre_id": genre_id}
    if region:
        params["region"] = region.lower()

    async with httpx.AsyncClient(timeout=30) as client:
        response = await client.get(
            f"{BASE_URL}/best_podcasts",
            params=params,
            headers={"X-ListenAPI-Key": settings.listennotes_api_key},
        )
        response.raise_for_status()
        data = response.json()

    podcasts = data.get("podcasts", [])
    return [_parse_podcast(p) for p in podcasts if p.get("rss")]
