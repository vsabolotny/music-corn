"""Compute a taste profile from Spotify user data."""

import uuid
from collections import Counter, defaultdict
from datetime import datetime, timezone

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from music_corn.db.models import TasteProfile, User
from music_corn.db.session import async_session_factory
from music_corn.taste.spotify_client import (
    fetch_audio_features,
    fetch_recently_played,
    fetch_saved_tracks,
    fetch_user_top_artists,
    fetch_user_top_tracks,
    get_authenticated_client,
    get_user,
)

logger = structlog.get_logger()

# Weight multipliers for different time ranges (recent matters more for taste)
TIME_RANGE_WEIGHTS = {
    "short_term": 3.0,
    "medium_term": 2.0,
    "long_term": 1.0,
}

AUDIO_FEATURE_KEYS = [
    "danceability",
    "energy",
    "speechiness",
    "acousticness",
    "instrumentalness",
    "liveness",
    "valence",
    "tempo",
]

MOOD_THRESHOLDS = {
    "energetic": ("energy", 0.7, True),
    "chill": ("energy", 0.4, False),
    "happy": ("valence", 0.65, True),
    "melancholic": ("valence", 0.35, False),
    "danceable": ("danceability", 0.7, True),
    "acoustic": ("acousticness", 0.6, True),
    "instrumental": ("instrumentalness", 0.5, True),
}


def _compute_genre_weights(top_artists: list[dict]) -> dict[str, float]:
    """Compute weighted genre distribution from top artists."""
    genre_scores: defaultdict[str, float] = defaultdict(float)

    for artist in top_artists:
        weight = TIME_RANGE_WEIGHTS.get(artist.get("_time_range", "long_term"), 1.0)
        popularity_factor = artist.get("popularity", 50) / 100.0

        for genre in artist.get("genres", []):
            genre_scores[genre] += weight * popularity_factor

    if not genre_scores:
        return {}

    # Normalize to 0-1 range
    max_score = max(genre_scores.values())
    return {g: round(s / max_score, 3) for g, s in sorted(
        genre_scores.items(), key=lambda x: -x[1]
    )[:30]}


def _compute_artist_affinities(
    top_artists: list[dict], top_tracks: list[dict]
) -> list[dict]:
    """Compute artist affinity scores."""
    artist_scores: defaultdict[str, float] = defaultdict(float)
    artist_ids: dict[str, str] = {}

    for artist in top_artists:
        name = artist.get("name", "")
        weight = TIME_RANGE_WEIGHTS.get(artist.get("_time_range", "long_term"), 1.0)
        artist_scores[name] += weight
        artist_ids[name] = artist.get("id", "")

    # Boost artists that appear in top tracks
    for track in top_tracks:
        weight = TIME_RANGE_WEIGHTS.get(track.get("_time_range", "long_term"), 1.0)
        for artist in track.get("artists", []):
            name = artist.get("name", "")
            artist_scores[name] += weight * 0.5
            if name not in artist_ids:
                artist_ids[name] = artist.get("id", "")

    if not artist_scores:
        return []

    max_score = max(artist_scores.values())
    return [
        {
            "name": name,
            "spotify_id": artist_ids.get(name, ""),
            "weight": round(score / max_score, 3),
        }
        for name, score in sorted(artist_scores.items(), key=lambda x: -x[1])[:50]
    ]


def _compute_audio_features_avg(features: list[dict]) -> dict[str, float]:
    """Compute average audio features across tracks."""
    if not features:
        return {}

    sums: defaultdict[str, float] = defaultdict(float)
    count = 0

    for f in features:
        has_data = False
        for key in AUDIO_FEATURE_KEYS:
            if key in f and f[key] is not None:
                sums[key] += f[key]
                has_data = True
        if has_data:
            count += 1

    if count == 0:
        return {}

    return {key: round(sums[key] / count, 4) for key in AUDIO_FEATURE_KEYS if key in sums}


def _derive_mood_tags(audio_avg: dict[str, float]) -> list[str]:
    """Derive mood tags from average audio features."""
    tags = []
    for tag, (feature, threshold, above) in MOOD_THRESHOLDS.items():
        value = audio_avg.get(feature)
        if value is None:
            continue
        if above and value >= threshold:
            tags.append(tag)
        elif not above and value <= threshold:
            tags.append(tag)
    return tags


def _compute_era_bias(tracks: list[dict]) -> dict[str, float]:
    """Compute decade preferences from track release dates."""
    decade_counts: Counter[str] = Counter()

    for track in tracks:
        album = track.get("album") or {}
        release_date = album.get("release_date", "")
        if len(release_date) >= 4:
            try:
                year = int(release_date[:4])
                decade = f"{(year // 10) * 10}s"
                decade_counts[decade] += 1
            except ValueError:
                continue

    total = sum(decade_counts.values())
    if total == 0:
        return {}

    return {decade: round(count / total, 3) for decade, count in decade_counts.most_common()}


async def compute_taste_profile(email: str = "default") -> TasteProfile | None:
    """Fetch Spotify data and compute a full taste profile for a user."""
    user = await get_user(email)
    if not user:
        logger.error("User not found", email=email)
        return None

    if not user.spotify_access_token:
        logger.error("No Spotify tokens for user", email=email)
        return None

    sp = get_authenticated_client(user)
    logger.info("Fetching Spotify data", email=email)

    # Fetch all data
    top_tracks = fetch_user_top_tracks(sp)
    logger.info("Fetched top tracks", count=len(top_tracks))

    top_artists = fetch_user_top_artists(sp)
    logger.info("Fetched top artists", count=len(top_artists))

    saved = fetch_saved_tracks(sp, limit=200)
    logger.info("Fetched saved tracks", count=len(saved))

    recent = fetch_recently_played(sp)
    logger.info("Fetched recently played", count=len(recent))

    # Collect unique track IDs for audio features
    track_ids = set()
    all_tracks_for_era = []

    for t in top_tracks:
        track_ids.add(t["id"])
        all_tracks_for_era.append(t)

    for item in saved:
        track = item.get("track", {})
        if track and track.get("id"):
            track_ids.add(track["id"])
            all_tracks_for_era.append(track)

    for item in recent:
        track = item.get("track", {})
        if track and track.get("id"):
            track_ids.add(track["id"])

    # Fetch audio features
    audio_features = fetch_audio_features(sp, list(track_ids)[:500])
    logger.info("Fetched audio features", count=len(audio_features))

    # Compute profile components
    genre_weights = _compute_genre_weights(top_artists)
    artist_affinities = _compute_artist_affinities(top_artists, top_tracks)
    audio_avg = _compute_audio_features_avg(audio_features)
    mood_tags = _derive_mood_tags(audio_avg)
    era_bias = _compute_era_bias(all_tracks_for_era)

    logger.info(
        "Computed taste profile",
        genres=len(genre_weights),
        artists=len(artist_affinities),
        moods=mood_tags,
        eras=era_bias,
    )

    # Save to DB
    async with async_session_factory() as session:
        profile = TasteProfile(
            id=uuid.uuid4(),
            user_id=user.id,
            computed_at=datetime.now(timezone.utc),
            top_genres=genre_weights,
            top_artists=artist_affinities,
            audio_features_avg=audio_avg,
            mood_tags=mood_tags,
            listening_era_bias=era_bias,
        )
        session.add(profile)
        await session.commit()
        await session.refresh(profile)

        logger.info("Saved taste profile", profile_id=str(profile.id))
        return profile
