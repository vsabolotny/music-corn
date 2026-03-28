"""Assemble podcast audio from narration segments and track previews."""

import tempfile
from pathlib import Path

import httpx
import structlog
from pydub import AudioSegment

from music_corn.config import settings

logger = structlog.get_logger()

CROSSFADE_MS = 500
PREVIEW_VOLUME_DB = -3  # Slightly lower than narration
NARRATION_VOLUME_DB = 0


def _fetch_spotify_preview(spotify_uri: str, sp) -> AudioSegment | None:
    """Fetch the 30-second preview MP3 from Spotify."""
    try:
        track_id = spotify_uri.replace("spotify:track:", "")
        track_info = sp.track(track_id)
        preview_url = track_info.get("preview_url")

        if not preview_url:
            logger.warning("No preview URL available", track_id=track_id)
            return None

        response = httpx.get(preview_url, timeout=30, follow_redirects=True)
        response.raise_for_status()

        # Write to temp file and load
        with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as f:
            f.write(response.content)
            f.flush()
            audio = AudioSegment.from_mp3(f.name)

        logger.info("Fetched preview", track_id=track_id, duration_ms=len(audio))
        return audio

    except Exception:
        logger.exception("Failed to fetch preview", spotify_uri=spotify_uri)
        return None


def _create_silence(duration_ms: int = 1000) -> AudioSegment:
    """Create a silent audio segment."""
    return AudioSegment.silent(duration=duration_ms)


def assemble_podcast(
    segments: list[dict],
    output_path: Path,
    sp=None,
) -> Path:
    """Assemble the final podcast MP3 from narration segments and track previews.

    Args:
        segments: List from tts.synthesize_segments (narration with files + track breaks)
        output_path: Where to save the final MP3
        sp: Authenticated spotipy.Spotify client (for fetching previews)

    Returns:
        Path to the final MP3 file
    """
    podcast = AudioSegment.empty()

    for seg in segments:
        if seg["type"] == "narration" and "file" in seg:
            narration = AudioSegment.from_mp3(str(seg["file"]))
            narration = narration + NARRATION_VOLUME_DB

            if len(podcast) > 0:
                podcast = podcast.append(narration, crossfade=min(CROSSFADE_MS, len(podcast) // 2))
            else:
                podcast = narration

        elif seg["type"] == "track_break" and sp:
            preview = _fetch_spotify_preview(seg["spotify_uri"], sp)
            if preview:
                preview = preview + PREVIEW_VOLUME_DB
                podcast = podcast.append(
                    _create_silence(300), crossfade=0
                )
                podcast = podcast.append(preview, crossfade=CROSSFADE_MS)
                podcast = podcast.append(
                    _create_silence(300), crossfade=0
                )
            else:
                # No preview available — just add a brief pause
                podcast = podcast.append(_create_silence(1500), crossfade=0)

    # Normalize loudness
    target_dbfs = -16.0
    change_in_dbfs = target_dbfs - podcast.dBFS
    podcast = podcast.apply_gain(change_in_dbfs)

    # Export
    output_path.parent.mkdir(parents=True, exist_ok=True)
    podcast.export(str(output_path), format="mp3", bitrate="192k", tags={
        "title": "Music Corn - Weekly Discovery",
        "artist": "Music Corn",
        "genre": "Podcast",
    })

    duration_min = len(podcast) / 1000 / 60
    logger.info(
        "Podcast assembled",
        output=str(output_path),
        duration_min=round(duration_min, 1),
        size_mb=round(output_path.stat().st_size / 1024 / 1024, 1),
    )
    return output_path
