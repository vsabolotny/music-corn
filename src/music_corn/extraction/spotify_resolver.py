"""Resolve music mentions to Spotify track IDs."""

import structlog
import spotipy
from spotipy.oauth2 import SpotifyClientCredentials
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from music_corn.config import settings
from music_corn.db.models import MusicMention
from music_corn.db.session import async_session_factory

logger = structlog.get_logger()


def _get_spotify_client() -> spotipy.Spotify:
    """Create an authenticated Spotify client using client credentials."""
    auth_manager = SpotifyClientCredentials(
        client_id=settings.spotify_client_id,
        client_secret=settings.spotify_client_secret,
    )
    return spotipy.Spotify(auth_manager=auth_manager)


def _search_track(sp: spotipy.Spotify, mention: MusicMention) -> dict | None:
    """Search Spotify for a track matching the mention. Returns track dict or None."""
    if mention.track_title:
        query = f"artist:{mention.artist_name} track:{mention.track_title}"
    else:
        query = f"artist:{mention.artist_name}"

    try:
        results = sp.search(q=query, type="track", limit=3)
        tracks = results.get("tracks", {}).get("items", [])
        if not tracks:
            return None

        # If we have a track title, try to find an exact match
        if mention.track_title:
            title_lower = mention.track_title.lower()
            for track in tracks:
                if track["name"].lower() == title_lower:
                    return track

        # Fall back to first result
        return tracks[0]

    except spotipy.SpotifyException as e:
        logger.error("Spotify search error", error=str(e), artist=mention.artist_name)
        return None


async def resolve_mention(session: AsyncSession, mention: MusicMention, sp: spotipy.Spotify) -> bool:
    """Resolve a single mention to a Spotify track. Returns True if resolved."""
    if mention.spotify_track_id:
        return True

    track = _search_track(sp, mention)
    if not track:
        logger.debug("No Spotify match", artist=mention.artist_name, track=mention.track_title)
        return False

    mention.spotify_track_id = track["id"]
    mention.spotify_uri = track["uri"]

    # Enrich genres from artist if we don't have them
    if not mention.genres:
        artists = track.get("artists", [])
        if artists:
            try:
                artist_info = sp.artist(artists[0]["id"])
                mention.genres = artist_info.get("genres", [])[:5]
            except spotipy.SpotifyException:
                pass

    logger.info(
        "Resolved to Spotify",
        artist=mention.artist_name,
        track=track["name"],
        spotify_id=track["id"],
    )
    return True


async def resolve_all_unresolved() -> int:
    """Resolve all mentions that don't have Spotify IDs yet. Returns count resolved."""
    sp = _get_spotify_client()

    async with async_session_factory() as session:
        result = await session.execute(
            select(MusicMention).where(MusicMention.spotify_track_id.is_(None))
        )
        mentions = result.scalars().all()

        if not mentions:
            logger.info("No unresolved mentions")
            return 0

        resolved = 0
        for mention in mentions:
            try:
                if await resolve_mention(session, mention, sp):
                    resolved += 1
            except Exception:
                logger.exception("Failed to resolve mention", mention_id=str(mention.id))

        await session.commit()
        logger.info(
            "Resolution complete",
            resolved=resolved,
            total=len(mentions),
        )
        return resolved
