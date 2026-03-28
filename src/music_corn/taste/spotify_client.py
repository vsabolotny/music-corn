"""Spotify OAuth and data fetching."""

import asyncio
import threading
import uuid
import webbrowser
from datetime import datetime, timezone

import spotipy
import structlog
import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from spotipy.oauth2 import SpotifyOAuth
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from music_corn.config import settings
from music_corn.db.models import User
from music_corn.db.session import async_session_factory

logger = structlog.get_logger()

SCOPES = [
    "user-top-read",
    "user-library-read",
    "playlist-read-private",
    "playlist-read-collaborative",
    "user-read-recently-played",
]


def _get_oauth_manager(cache_path: str = ".spotify_cache") -> SpotifyOAuth:
    return SpotifyOAuth(
        client_id=settings.spotify_client_id,
        client_secret=settings.spotify_client_secret,
        redirect_uri=settings.spotify_redirect_uri,
        scope=" ".join(SCOPES),
        cache_path=cache_path,
        open_browser=False,
    )


def run_auth_flow() -> dict:
    """Run the Spotify OAuth flow with a local callback server.

    Opens browser for login, captures the callback, returns token info.
    """
    oauth = _get_oauth_manager()
    auth_url = oauth.get_authorize_url()

    token_result = {"token_info": None}
    shutdown_event = threading.Event()

    callback_app = FastAPI()

    @callback_app.get("/callback")
    async def callback(request: Request):
        code = request.query_params.get("code")
        error = request.query_params.get("error")

        if error:
            token_result["error"] = error
            shutdown_event.set()
            return HTMLResponse(
                "<h2>Authorization failed</h2><p>You can close this tab.</p>"
            )

        token_info = oauth.get_access_token(code, as_dict=True)
        token_result["token_info"] = token_info
        shutdown_event.set()
        return HTMLResponse(
            "<h2>Success! &#127925;</h2><p>music-corn is now connected to your Spotify. "
            "You can close this tab.</p>"
        )

    config = uvicorn.Config(
        callback_app, host="127.0.0.1", port=8888, log_level="error"
    )
    server = uvicorn.Server(config)

    server_thread = threading.Thread(target=server.run, daemon=True)
    server_thread.start()

    print(f"\nOpening Spotify login in your browser...\n")
    print(f"If it doesn't open, visit:\n{auth_url}\n")
    webbrowser.open(auth_url)

    shutdown_event.wait(timeout=120)
    server.should_exit = True
    server_thread.join(timeout=5)

    if "error" in token_result:
        raise RuntimeError(f"Spotify auth failed: {token_result['error']}")

    if not token_result["token_info"]:
        raise RuntimeError("Spotify auth timed out (120s)")

    return token_result["token_info"]


async def save_user_tokens(token_info: dict, email: str = "default") -> User:
    """Save or update Spotify tokens for a user."""
    async with async_session_factory() as session:
        result = await session.execute(select(User).where(User.email == email))
        user = result.scalar_one_or_none()

        expires_at = datetime.fromtimestamp(
            token_info["expires_at"], tz=timezone.utc
        )

        if user:
            user.spotify_access_token = token_info["access_token"]
            user.spotify_refresh_token = token_info.get("refresh_token")
            user.spotify_token_expires_at = expires_at
        else:
            user = User(
                id=uuid.uuid4(),
                email=email,
                spotify_access_token=token_info["access_token"],
                spotify_refresh_token=token_info.get("refresh_token"),
                spotify_token_expires_at=expires_at,
            )
            session.add(user)

        await session.commit()
        await session.refresh(user)
        logger.info("Saved Spotify tokens", user_id=str(user.id), email=email)
        return user


def get_authenticated_client(user: User) -> spotipy.Spotify:
    """Get an authenticated Spotify client for a user, refreshing token if needed."""
    oauth = _get_oauth_manager()

    token_info = {
        "access_token": user.spotify_access_token,
        "refresh_token": user.spotify_refresh_token,
        "expires_at": int(user.spotify_token_expires_at.timestamp())
        if user.spotify_token_expires_at
        else 0,
    }

    if oauth.is_token_expired(token_info):
        logger.info("Refreshing Spotify token", user_id=str(user.id))
        token_info = oauth.refresh_access_token(token_info["refresh_token"])
        # Update tokens in DB (fire and forget in sync context)
        asyncio.get_event_loop().run_until_complete(
            _update_tokens(user.id, token_info)
        )

    return spotipy.Spotify(auth=token_info["access_token"])


async def _update_tokens(user_id: uuid.UUID, token_info: dict):
    """Update stored tokens after a refresh."""
    async with async_session_factory() as session:
        user = await session.get(User, user_id)
        if user:
            user.spotify_access_token = token_info["access_token"]
            user.spotify_refresh_token = token_info.get(
                "refresh_token", user.spotify_refresh_token
            )
            user.spotify_token_expires_at = datetime.fromtimestamp(
                token_info["expires_at"], tz=timezone.utc
            )
            await session.commit()


async def get_user(email: str = "default") -> User | None:
    """Get a user by email."""
    async with async_session_factory() as session:
        result = await session.execute(select(User).where(User.email == email))
        return result.scalar_one_or_none()


def fetch_user_top_tracks(sp: spotipy.Spotify, limit: int = 50) -> list[dict]:
    """Fetch user's top tracks across time ranges."""
    all_tracks = []
    for time_range in ["short_term", "medium_term", "long_term"]:
        results = sp.current_user_top_tracks(limit=limit, time_range=time_range)
        for item in results.get("items", []):
            item["_time_range"] = time_range
            all_tracks.append(item)
    return all_tracks


def fetch_user_top_artists(sp: spotipy.Spotify, limit: int = 50) -> list[dict]:
    """Fetch user's top artists across time ranges."""
    all_artists = []
    for time_range in ["short_term", "medium_term", "long_term"]:
        results = sp.current_user_top_artists(limit=limit, time_range=time_range)
        for item in results.get("items", []):
            item["_time_range"] = time_range
            all_artists.append(item)
    return all_artists


def fetch_saved_tracks(sp: spotipy.Spotify, limit: int = 200) -> list[dict]:
    """Fetch user's saved/liked tracks."""
    tracks = []
    offset = 0
    while offset < limit:
        batch_size = min(50, limit - offset)
        results = sp.current_user_saved_tracks(limit=batch_size, offset=offset)
        items = results.get("items", [])
        if not items:
            break
        tracks.extend(items)
        offset += len(items)
    return tracks


def fetch_recently_played(sp: spotipy.Spotify, limit: int = 50) -> list[dict]:
    """Fetch user's recently played tracks."""
    results = sp.current_user_recently_played(limit=limit)
    return results.get("items", [])


def fetch_audio_features(sp: spotipy.Spotify, track_ids: list[str]) -> list[dict]:
    """Fetch audio features for a batch of tracks."""
    features = []
    # Spotify API allows max 100 per request
    for i in range(0, len(track_ids), 100):
        batch = track_ids[i : i + 100]
        result = sp.audio_features(batch)
        features.extend([f for f in result if f is not None])
    return features
