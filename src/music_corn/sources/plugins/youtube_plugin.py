"""YouTube channel source plugin using yt-dlp."""

import asyncio
import json
import uuid
from datetime import datetime, timezone
from functools import partial

import structlog

from music_corn.db.models import ContentItem, Source
from music_corn.sources.base import SourcePlugin
from music_corn.sources.registry import register_plugin

logger = structlog.get_logger()


def _run_yt_dlp(url: str, yt_dlp_opts: dict) -> list[dict]:
    """Run yt-dlp in a thread-safe way (yt-dlp is not async)."""
    import yt_dlp

    results = []

    class Collector(yt_dlp.postprocessor.PostProcessor):
        def run(self, info):
            results.append(info)
            return [], info

    with yt_dlp.YoutubeDL(yt_dlp_opts) as ydl:
        ydl.add_post_processor(Collector())
        ydl.download([url])

    return results


def _fetch_channel_metadata(url: str, max_videos: int = 20) -> list[dict]:
    """Fetch video metadata + subtitles from a YouTube channel/playlist."""
    import yt_dlp

    entries = []

    opts = {
        "quiet": True,
        "no_warnings": True,
        "extract_flat": True,
        "playlistend": max_videos,
    }

    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=False)
        if not info:
            return []
        for entry in info.get("entries", []):
            if entry:
                entries.append(entry)

    return entries


def _get_subtitles(video_url: str) -> str:
    """Download auto-generated or manual subtitles for a video."""
    import yt_dlp
    import tempfile
    import os

    with tempfile.TemporaryDirectory() as tmpdir:
        opts = {
            "quiet": True,
            "no_warnings": True,
            "skip_download": True,
            "writesubtitles": True,
            "writeautomaticsub": True,
            "subtitleslangs": ["en"],
            "subtitlesformat": "json3",
            "outtmpl": os.path.join(tmpdir, "%(id)s"),
        }

        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(video_url, download=True)
            if not info:
                return ""

            video_id = info.get("id", "")
            # Look for subtitle files
            for suffix in [f"{video_id}.en.json3", f"{video_id}.en-orig.json3"]:
                sub_path = os.path.join(tmpdir, suffix)
                if os.path.exists(sub_path):
                    return _parse_json3_subs(sub_path)

    return ""


def _parse_json3_subs(path: str) -> str:
    """Parse json3 subtitle format into plain text."""
    with open(path) as f:
        data = json.load(f)

    segments = []
    for event in data.get("events", []):
        segs = event.get("segs", [])
        text = "".join(s.get("utf8", "") for s in segs).strip()
        if text and text != "\n":
            segments.append(text)

    return " ".join(segments)


@register_plugin("youtube")
class YouTubePlugin(SourcePlugin):
    """Fetches content from YouTube channels using yt-dlp."""

    async def fetch_new_items(
        self, source: Source, since: datetime | None
    ) -> list[ContentItem]:
        logger.info("Fetching YouTube channel", source=source.name, url=source.url)

        max_videos = source.config_json.get("max_videos", 15)

        loop = asyncio.get_event_loop()
        entries = await loop.run_in_executor(
            None, partial(_fetch_channel_metadata, source.url, max_videos)
        )

        items = []
        for entry in entries:
            video_id = entry.get("id", "")
            if not video_id:
                continue

            # Filter by upload date if available
            upload_date_str = entry.get("upload_date")
            published = None
            if upload_date_str:
                try:
                    published = datetime.strptime(upload_date_str, "%Y%m%d").replace(
                        tzinfo=timezone.utc
                    )
                    if since and published <= since:
                        continue
                except ValueError:
                    pass

            video_url = entry.get("url") or f"https://www.youtube.com/watch?v={video_id}"

            item = ContentItem(
                id=uuid.uuid4(),
                source_id=source.id,
                external_id=video_id,
                title=entry.get("title", "Untitled"),
                url=video_url,
                published_at=published,
                raw_text=None,  # Text extracted separately
                analyzed=False,
            )
            items.append(item)

        logger.info("Fetched YouTube items", source=source.name, count=len(items))
        return items

    async def extract_text(self, item: ContentItem) -> str:
        """Extract subtitles/captions from a YouTube video."""
        if item.raw_text:
            return item.raw_text

        logger.info("Extracting subtitles", video_id=item.external_id)
        loop = asyncio.get_event_loop()
        text = await loop.run_in_executor(None, partial(_get_subtitles, item.url))

        if not text:
            logger.warning("No subtitles found", video_id=item.external_id)

        return text
