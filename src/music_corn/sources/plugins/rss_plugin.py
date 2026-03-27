"""RSS/Atom feed source plugin."""

import uuid
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

import feedparser
import structlog

from music_corn.db.models import ContentItem, Source
from music_corn.sources.base import SourcePlugin
from music_corn.sources.registry import register_plugin

logger = structlog.get_logger()


@register_plugin("rss")
class RSSPlugin(SourcePlugin):
    """Fetches content from RSS/Atom podcast and music blog feeds."""

    async def fetch_new_items(
        self, source: Source, since: datetime | None
    ) -> list[ContentItem]:
        logger.info("Fetching RSS feed", source=source.name, url=source.url)
        feed = feedparser.parse(source.url)

        if feed.bozo and not feed.entries:
            logger.error("Failed to parse feed", source=source.name, error=str(feed.bozo_exception))
            return []

        items = []
        for entry in feed.entries:
            published = self._parse_date(entry)
            if since and published and published <= since:
                continue

            external_id = entry.get("id") or entry.get("link", "")
            raw_text = self._extract_entry_text(entry)

            item = ContentItem(
                id=uuid.uuid4(),
                source_id=source.id,
                external_id=external_id,
                title=entry.get("title", "Untitled"),
                url=entry.get("link", ""),
                published_at=published,
                raw_text=raw_text,
                analyzed=False,
            )
            items.append(item)

        logger.info("Fetched RSS items", source=source.name, count=len(items))
        return items

    async def extract_text(self, item: ContentItem) -> str:
        return item.raw_text or ""

    @staticmethod
    def _parse_date(entry) -> datetime | None:
        published_str = entry.get("published") or entry.get("updated")
        if not published_str:
            return None
        try:
            return parsedate_to_datetime(published_str).astimezone(timezone.utc)
        except (ValueError, TypeError):
            return None

    @staticmethod
    def _extract_entry_text(entry) -> str:
        """Extract text from RSS entry, preferring full content over summary."""
        content_list = entry.get("content", [])
        if content_list:
            return content_list[0].get("value", "")
        return entry.get("summary", entry.get("description", ""))
