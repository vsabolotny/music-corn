"""Recommendation engine — scores discovered music against user taste."""

import math
import uuid
from collections import defaultdict
from datetime import datetime, timezone

import structlog
from sqlalchemy import select, desc
from sqlalchemy.ext.asyncio import AsyncSession

from music_corn.db.models import (
    DigestTrack,
    MusicMention,
    TasteProfile,
    User,
    WeeklyDigest,
)
from music_corn.db.session import async_session_factory

logger = structlog.get_logger()

# Default scoring weights
DEFAULT_WEIGHTS = {
    "genre": 0.25,
    "artist": 0.20,
    "audio": 0.20,
    "sentiment": 0.15,
    "novelty": 0.20,
}


def _genre_overlap_score(mention_genres: list[str] | None, profile_genres: dict) -> float:
    """Score 0-1 based on overlap between mention genres and user genre preferences."""
    if not mention_genres or not profile_genres:
        return 0.0

    total = 0.0
    for genre in mention_genres:
        genre_lower = genre.lower()
        # Exact match
        if genre_lower in profile_genres:
            total += profile_genres[genre_lower]
            continue
        # Partial match (e.g. "indie" matches "indie rock")
        for pg, weight in profile_genres.items():
            if genre_lower in pg or pg in genre_lower:
                total += weight * 0.5
                break

    return min(total / max(len(mention_genres), 1), 1.0)


def _artist_familiarity_score(
    artist_name: str, profile_artists: list[dict]
) -> float:
    """Score 0-1 based on how familiar the artist is.

    Known artists get moderate scores. Unknown artists get a slight boost
    for discovery potential.
    """
    artist_lower = artist_name.lower()
    for artist in profile_artists:
        if artist["name"].lower() == artist_lower:
            # Known artist — return weight but cap it (we want discovery)
            return artist["weight"] * 0.6
    # Unknown artist — small baseline for discovery
    return 0.1


def _audio_similarity_score(
    mention_features: dict | None, profile_features: dict
) -> float:
    """Score 0-1 based on euclidean distance in audio feature space."""
    if not mention_features or not profile_features:
        return 0.5  # Neutral when we can't compare

    keys = ["danceability", "energy", "valence", "acousticness", "instrumentalness"]
    diffs = []
    for key in keys:
        a = mention_features.get(key)
        b = profile_features.get(key)
        if a is not None and b is not None:
            diffs.append((a - b) ** 2)

    if not diffs:
        return 0.5

    distance = math.sqrt(sum(diffs) / len(diffs))
    # Convert distance (0-1 range) to similarity
    return max(0.0, 1.0 - distance)


def _sentiment_score(sentiment: str, confidence: float) -> float:
    """Score based on source sentiment and extraction confidence."""
    sentiment_map = {"positive": 1.0, "neutral": 0.5, "negative": 0.1}
    base = sentiment_map.get(sentiment, 0.5)
    return base * confidence


def _novelty_score(
    mention: MusicMention,
    profile_artists: list[dict],
    already_recommended_ids: set[uuid.UUID],
) -> float:
    """Bonus for tracks that are genuinely new to the user."""
    if mention.id in already_recommended_ids:
        return 0.0

    artist_lower = mention.artist_name.lower()
    known = any(a["name"].lower() == artist_lower for a in profile_artists)

    if not known:
        return 1.0  # Maximum novelty — never heard of this artist
    elif mention.track_title:
        return 0.6  # Known artist, but potentially new track
    return 0.3


def score_mention(
    mention: MusicMention,
    profile: TasteProfile,
    audio_features_cache: dict[str, dict],
    already_recommended_ids: set[uuid.UUID],
    discovery_dial: float = 0.5,
) -> tuple[float, str]:
    """Score a single mention against the user's taste profile.

    Args:
        discovery_dial: 0.0 = safe/familiar, 1.0 = adventurous

    Returns:
        (score, reason) tuple
    """
    weights = DEFAULT_WEIGHTS.copy()
    # Shift weights based on discovery dial
    weights["artist"] *= (1.0 - discovery_dial * 0.5)
    weights["novelty"] *= (0.5 + discovery_dial * 0.5)

    # Normalize weights
    total_w = sum(weights.values())
    weights = {k: v / total_w for k, v in weights.items()}

    mention_audio = audio_features_cache.get(mention.spotify_track_id or "")

    genre_s = _genre_overlap_score(mention.genres, profile.top_genres)
    artist_s = _artist_familiarity_score(mention.artist_name, profile.top_artists)
    audio_s = _audio_similarity_score(mention_audio, profile.audio_features_avg)
    sentiment_s = _sentiment_score(mention.sentiment, mention.confidence)
    novelty_s = _novelty_score(mention, profile.top_artists, already_recommended_ids)

    score = (
        weights["genre"] * genre_s
        + weights["artist"] * artist_s
        + weights["audio"] * audio_s
        + weights["sentiment"] * sentiment_s
        + weights["novelty"] * novelty_s
    )

    # Build reason string
    reasons = []
    if genre_s > 0.5:
        matching = [g for g in (mention.genres or []) if g.lower() in profile.top_genres]
        if matching:
            reasons.append(f"genre match: {', '.join(matching[:3])}")
    if artist_s > 0.3:
        reasons.append("familiar artist")
    if novelty_s > 0.7:
        reasons.append("new discovery")
    if audio_s > 0.7:
        reasons.append("matches your audio taste")
    if sentiment_s > 0.7:
        reasons.append("highly recommended by source")

    reason = "; ".join(reasons) if reasons else "general match"

    return score, reason


def _enforce_diversity(
    scored: list[tuple[MusicMention, float, str]],
    max_per_artist: int = 2,
    target_count: int = 15,
) -> list[tuple[MusicMention, float, str]]:
    """Enforce diversity: limit tracks per artist, ensure genre spread."""
    artist_counts: defaultdict[str, int] = defaultdict(int)
    result = []

    for mention, score, reason in scored:
        artist_key = mention.artist_name.lower()
        if artist_counts[artist_key] >= max_per_artist:
            continue
        artist_counts[artist_key] += 1
        result.append((mention, score, reason))
        if len(result) >= target_count:
            break

    return result


async def generate_recommendations(
    email: str = "default",
    count: int = 15,
    discovery_dial: float = 0.5,
) -> WeeklyDigest | None:
    """Generate weekly recommendations for a user.

    Returns a WeeklyDigest with ranked tracks, or None on failure.
    """
    async with async_session_factory() as session:
        # Get user
        result = await session.execute(select(User).where(User.email == email))
        user = result.scalar_one_or_none()
        if not user:
            logger.error("User not found", email=email)
            return None

        # Get latest taste profile
        result = await session.execute(
            select(TasteProfile)
            .where(TasteProfile.user_id == user.id)
            .order_by(desc(TasteProfile.computed_at))
            .limit(1)
        )
        profile = result.scalar_one_or_none()
        if not profile:
            logger.error("No taste profile found. Run 'taste' first.", email=email)
            return None

        # Get already recommended mention IDs
        result = await session.execute(
            select(DigestTrack.music_mention_id)
            .join(WeeklyDigest)
            .where(WeeklyDigest.user_id == user.id)
        )
        already_recommended = {row[0] for row in result.all()}

        # Get all undelivered mentions with Spotify IDs
        result = await session.execute(
            select(MusicMention).where(
                MusicMention.spotify_track_id.isnot(None),
                MusicMention.sentiment != "negative",
            )
        )
        mentions = result.scalars().all()

        if not mentions:
            logger.info("No mentions available for recommendations")
            return None

        # Fetch audio features for scoring
        from music_corn.taste.spotify_client import get_authenticated_client, fetch_audio_features

        audio_cache: dict[str, dict] = {}
        try:
            sp = get_authenticated_client(user)
            track_ids = [m.spotify_track_id for m in mentions if m.spotify_track_id]
            features = fetch_audio_features(sp, track_ids[:500])
            audio_cache = {f["id"]: f for f in features if f and "id" in f}
        except Exception:
            logger.warning("Could not fetch audio features, scoring without them")

        # Score all mentions
        scored = []
        for mention in mentions:
            score, reason = score_mention(
                mention, profile, audio_cache, already_recommended, discovery_dial
            )
            scored.append((mention, score, reason))

        # Sort by score descending
        scored.sort(key=lambda x: -x[1])

        # Apply diversity filter
        final = _enforce_diversity(scored, max_per_artist=2, target_count=count)

        if not final:
            logger.info("No recommendations after filtering")
            return None

        # Create weekly digest
        now = datetime.now(timezone.utc)
        track_list = [
            {
                "artist": m.artist_name,
                "track": m.track_title,
                "spotify_id": m.spotify_track_id,
                "spotify_uri": m.spotify_uri,
                "score": round(s, 3),
                "reason": r,
            }
            for m, s, r in final
        ]

        digest = WeeklyDigest(
            id=uuid.uuid4(),
            user_id=user.id,
            week_start=now,
            track_list=track_list,
            created_at=now,
        )
        session.add(digest)
        await session.flush()

        # Create digest tracks
        for rank, (mention, score, reason) in enumerate(final, 1):
            dt = DigestTrack(
                id=uuid.uuid4(),
                digest_id=digest.id,
                music_mention_id=mention.id,
                rank=rank,
                match_reason=reason,
            )
            session.add(dt)

        await session.commit()

        logger.info(
            "Generated recommendations",
            digest_id=str(digest.id),
            track_count=len(final),
        )
        return digest
