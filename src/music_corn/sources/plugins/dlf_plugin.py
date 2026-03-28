"""Deutschlandfunk audio podcast plugin.

Fetches episodes from DLF RSS feeds, downloads HLS audio streams,
and transcribes them using OpenAI Whisper API.
"""

import html
import json
import os
import re
import subprocess
import tempfile
import uuid
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

import feedparser
import httpx
import structlog

from music_corn.config import settings
from music_corn.db.models import ContentItem, Source
from music_corn.sources.base import SourcePlugin
from music_corn.sources.registry import register_plugin

logger = structlog.get_logger()

# Pattern to extract audio JSON from episode page
AUDIO_JSON_PATTERN = re.compile(
    r'"__typename":\s*"Audio".*?"audioUrl":\s*"([^"]+)".*?"duration":\s*"(\d+)"',
    re.DOTALL,
)


def _fetch_episode_audio_url(episode_url: str) -> tuple[str | None, int]:
    """Scrape an episode page to find the HLS audio URL and duration.

    Returns (audio_url, duration_seconds).
    """
    try:
        response = httpx.get(episode_url, timeout=30, follow_redirects=True)
        response.raise_for_status()
        page_html = response.text

        # Unescape HTML entities in script tags
        unescaped = html.unescape(page_html)
        match = AUDIO_JSON_PATTERN.search(unescaped)
        if match:
            audio_url = match.group(1)
            duration = int(match.group(2))
            return audio_url, duration

        logger.warning("No audio URL found on page", url=episode_url)
        return None, 0

    except Exception:
        logger.exception("Failed to scrape episode page", url=episode_url)
        return None, 0


def _download_hls_to_mp3(hls_url: str, output_path: str, max_duration: int = 0) -> bool:
    """Download an HLS stream and convert to MP3 using ffmpeg.

    Args:
        hls_url: The m3u8 URL
        output_path: Where to save the MP3
        max_duration: Max seconds to download (0 = full)

    Returns True on success.
    """
    cmd = ["ffmpeg", "-y", "-i", hls_url]
    if max_duration > 0:
        cmd.extend(["-t", str(max_duration)])
    cmd.extend([
        "-vn",          # No video
        "-acodec", "libmp3lame",
        "-ab", "64k",   # Lower bitrate to keep file small for transcription
        "-ar", "16000",  # 16kHz for Whisper
        "-ac", "1",      # Mono
        output_path,
    ])

    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=600
        )
        if result.returncode != 0:
            logger.error("ffmpeg failed", stderr=result.stderr[:500])
            return False
        return True
    except subprocess.TimeoutExpired:
        logger.error("ffmpeg timed out")
        return False


def _transcribe_audio(audio_path: str) -> str:
    """Transcribe an audio file using local faster-whisper.

    Free, runs locally, no API key needed. Uses the 'base' model by default
    for speed; can be upgraded to 'medium' or 'large-v3' for better accuracy.
    """
    from faster_whisper import WhisperModel

    file_size = os.path.getsize(audio_path)
    logger.info("Transcribing audio locally", size_mb=round(file_size / 1024 / 1024, 1))

    model = WhisperModel("medium", compute_type="int8")
    segments, info = model.transcribe(audio_path, language="de", beam_size=5)

    logger.info("Detected language", language=info.language, probability=round(info.language_probability, 2))

    transcript_parts = []
    for segment in segments:
        transcript_parts.append(segment.text.strip())

    transcript = " ".join(transcript_parts)
    logger.info("Transcription complete", length=len(transcript))
    return transcript

    return " ".join(transcripts)


@register_plugin("dlf")
class DLFPlugin(SourcePlugin):
    """Deutschlandfunk audio podcast plugin.

    Uses the RSS feed to discover episodes, scrapes episode pages for
    HLS audio URLs, downloads and transcribes with Whisper.
    """

    async def fetch_new_items(
        self, source: Source, since: datetime | None
    ) -> list[ContentItem]:
        rss_url = source.config_json.get("rss_url") or source.url.replace(".html", ".rss")
        logger.info("Fetching DLF feed", source=source.name, url=rss_url)

        feed = feedparser.parse(rss_url)
        if feed.bozo and not feed.entries:
            logger.error("Failed to parse DLF feed", error=str(feed.bozo_exception))
            return []

        items = []
        for entry in feed.entries:
            published = self._parse_date(entry)
            if since and published and published <= since:
                continue

            external_id = entry.get("id") or entry.get("link", "")
            episode_url = entry.get("link", "")

            item = ContentItem(
                id=uuid.uuid4(),
                source_id=source.id,
                external_id=external_id,
                title=entry.get("title", "Untitled"),
                url=episode_url,
                published_at=published,
                raw_text=None,  # Will be filled by extract_text
                analyzed=False,
            )
            items.append(item)

        logger.info("Fetched DLF episodes", source=source.name, count=len(items))
        return items

    async def extract_text(self, item: ContentItem) -> str:
        """Download audio from episode page and transcribe with Whisper."""
        if item.raw_text:
            return item.raw_text

        logger.info("Processing DLF episode", title=item.title, url=item.url)

        # Step 1: Get audio URL from episode page
        audio_url, duration = _fetch_episode_audio_url(item.url)
        if not audio_url:
            logger.warning("No audio URL found", title=item.title)
            return ""

        logger.info("Found audio", url=audio_url[:80], duration_s=duration)

        # Step 2: Download HLS stream to MP3
        with tempfile.TemporaryDirectory() as tmpdir:
            mp3_path = os.path.join(tmpdir, "episode.mp3")

            logger.info("Downloading audio", title=item.title)
            success = _download_hls_to_mp3(audio_url, mp3_path, max_duration=duration)
            if not success:
                logger.error("Failed to download audio", title=item.title)
                return ""

            file_size = os.path.getsize(mp3_path)
            logger.info("Downloaded", size_mb=round(file_size / 1024 / 1024, 1))

            # Step 3: Transcribe
            logger.info("Transcribing with Whisper", title=item.title)
            transcript = _transcribe_audio(mp3_path)

        logger.info("Transcription complete", title=item.title, length=len(transcript))
        return transcript

    @staticmethod
    def _parse_date(entry) -> datetime | None:
        published_str = entry.get("published") or entry.get("updated")
        if not published_str:
            return None
        try:
            return parsedate_to_datetime(published_str).astimezone(timezone.utc)
        except (ValueError, TypeError):
            return None
