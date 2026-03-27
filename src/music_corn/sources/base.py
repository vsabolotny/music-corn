"""Abstract base class for source plugins."""

from abc import ABC, abstractmethod
from datetime import datetime

from music_corn.db.models import ContentItem, Source


class SourcePlugin(ABC):
    """Base class all source plugins must implement."""

    plugin_type: str

    @abstractmethod
    async def fetch_new_items(
        self, source: Source, since: datetime | None
    ) -> list[ContentItem]:
        """Fetch new content items from the source since the given timestamp."""
        ...

    @abstractmethod
    async def extract_text(self, item: ContentItem) -> str:
        """Extract full text content from a content item (transcript, description, etc.)."""
        ...
