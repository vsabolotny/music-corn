"""YouTube channel source plugin (placeholder for Sprint 2)."""

from datetime import datetime

import structlog

from music_corn.db.models import ContentItem, Source
from music_corn.sources.base import SourcePlugin
from music_corn.sources.registry import register_plugin

logger = structlog.get_logger()


@register_plugin("youtube")
class YouTubePlugin(SourcePlugin):
    """Fetches content from YouTube channels using yt-dlp. Full implementation in Sprint 2."""

    async def fetch_new_items(
        self, source: Source, since: datetime | None
    ) -> list[ContentItem]:
        logger.warning("YouTube plugin not yet implemented", source=source.name)
        return []

    async def extract_text(self, item: ContentItem) -> str:
        return item.raw_text or ""
