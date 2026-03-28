"""Pydantic models for podcast discovery."""

from datetime import datetime
from pydantic import BaseModel

# Normalized genre taxonomy across APIs
GENRE_MAPPING = {
    # PodcastIndex categories → normalized
    "music": "music-commentary",
    "music commentary": "music-commentary",
    "music history": "music-history",
    "music interviews": "music-commentary",
    "jazz": "jazz",
    "rock": "rock",
    "electronic": "electronic",
    "classical": "classical",
    "hip-hop": "hip-hop",
    "hip hop": "hip-hop",
    "pop": "pop",
    "world": "world",
    "metal": "metal",
    "folk": "folk",
    "indie": "indie",
    "alternative": "indie",
    "r&b": "r-and-b",
    "rnb": "r-and-b",
    "r-and-b": "r-and-b",
    "soul": "r-and-b",
    "latin": "latin",
    "reggae": "world",
    "blues": "jazz",
    "country": "folk",
    "punk": "rock",
    "ambient": "electronic",
    "techno": "electronic",
    "house": "electronic",
    "radio": "radio",
    # ListenNotes genre IDs → normalized
    "134": "music-commentary",  # Music
    "100": "music-history",     # Music History
    "67": "pop",
    "68": "rock",
    "69": "jazz",
    "70": "classical",
    "71": "hip-hop",
    "72": "electronic",
    "73": "folk",
    "74": "metal",
    "75": "world",
    "76": "latin",
    "77": "r-and-b",
    "78": "indie",
}

# ListenNotes genre IDs for music-related categories
LISTENNOTES_MUSIC_GENRE_IDS = [134, 100, 67, 68, 69, 70, 71, 72, 73, 74, 75, 76, 77, 78]


def normalize_genre(raw_genre: str) -> str | None:
    """Normalize a genre string to our taxonomy. Returns None if not mappable."""
    key = raw_genre.lower().strip()
    return GENRE_MAPPING.get(key)


def normalize_genres(raw_genres: list[str]) -> list[str]:
    """Normalize a list of genres, removing duplicates and unmapped values."""
    seen = set()
    result = []
    for g in raw_genres:
        normalized = normalize_genre(g)
        if normalized and normalized not in seen:
            seen.add(normalized)
            result.append(normalized)
    return result


class SearchFilters(BaseModel):
    """Filters for podcast search."""
    query: str = "music"
    genre: str | None = None
    country: str | None = None  # ISO 3166-1 alpha-2 (e.g. "DE", "US")
    language: str | None = None  # ISO 639-1 (e.g. "de", "en")
    year: int | None = None
    limit: int = 20


class PodcastResult(BaseModel):
    """Unified podcast result from any API."""
    title: str
    author: str | None = None
    description: str | None = None
    feed_url: str
    website_url: str | None = None
    image_url: str | None = None
    language: str | None = None
    country: str | None = None
    genres: list[str] = []
    episode_count: int | None = None
    latest_episode_at: datetime | None = None

    # Quality signals
    listen_score: float | None = None
    itunes_rating: float | None = None
    itunes_review_count: int | None = None
    podcastindex_trending: float | None = None

    # Provenance
    api_source: str = ""  # "podcastindex" or "listennotes"
    raw_data: dict = {}
