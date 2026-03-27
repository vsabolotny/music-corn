"""LLM-based music mention extraction using Claude API."""

import uuid
from datetime import datetime, timezone

import anthropic
import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from music_corn.config import settings
from music_corn.db.models import ContentItem, MusicMention, Source
from music_corn.db.session import async_session_factory
from music_corn.sources.registry import get_plugin

logger = structlog.get_logger()

EXTRACTION_TOOL = {
    "name": "extract_music_mentions",
    "description": "Extract music recommendations and mentions from text content.",
    "input_schema": {
        "type": "object",
        "properties": {
            "mentions": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "artist_name": {
                            "type": "string",
                            "description": "The artist or band name",
                        },
                        "track_title": {
                            "type": "string",
                            "description": "The song/track title, if mentioned",
                        },
                        "album_title": {
                            "type": "string",
                            "description": "The album title, if mentioned",
                        },
                        "genres": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "Genre tags for this music",
                        },
                        "sentiment": {
                            "type": "string",
                            "enum": ["positive", "neutral", "negative"],
                            "description": "How the source feels about this music",
                        },
                        "context_snippet": {
                            "type": "string",
                            "description": "Brief quote or paraphrase of what was said about the music",
                        },
                        "confidence": {
                            "type": "number",
                            "description": "Confidence that this is a genuine music recommendation (0.0-1.0)",
                        },
                    },
                    "required": ["artist_name", "sentiment", "confidence"],
                },
            }
        },
        "required": ["mentions"],
    },
}

SYSTEM_PROMPT = """You are a music discovery assistant. Your task is to extract music \
recommendations and mentions from podcast transcripts, video descriptions, and blog posts.

Rules:
- Only extract music that is being recommended, reviewed, or discussed as worth listening to.
- Ignore passing references, ads, or background music credits.
- For each mention, assess the sentiment (positive/neutral/negative) and your confidence (0.0-1.0).
- Include genre tags when you can infer them from context.
- Keep context_snippet brief (1-2 sentences max).
- If no music is mentioned, return an empty mentions array."""

MAX_CHUNK_CHARS = 12000
CHUNK_OVERLAP_CHARS = 500


def _chunk_text(text: str) -> list[str]:
    """Split text into overlapping chunks for processing."""
    if len(text) <= MAX_CHUNK_CHARS:
        return [text]

    chunks = []
    start = 0
    while start < len(text):
        end = start + MAX_CHUNK_CHARS
        chunk = text[start:end]
        chunks.append(chunk)
        start = end - CHUNK_OVERLAP_CHARS

    return chunks


async def extract_mentions_from_text(text: str, source_title: str) -> list[dict]:
    """Extract music mentions from a text using Claude API."""
    if not text.strip():
        return []

    client = anthropic.Anthropic(api_key=settings.anthropic_api_key)
    chunks = _chunk_text(text)
    all_mentions = []

    for i, chunk in enumerate(chunks):
        user_msg = f"Source: {source_title}\n\nContent:\n{chunk}"

        try:
            response = client.messages.create(
                model="claude-sonnet-4-20250514",
                max_tokens=4096,
                system=SYSTEM_PROMPT,
                tools=[EXTRACTION_TOOL],
                tool_choice={"type": "tool", "name": "extract_music_mentions"},
                messages=[{"role": "user", "content": user_msg}],
            )

            for block in response.content:
                if block.type == "tool_use" and block.name == "extract_music_mentions":
                    mentions = block.input.get("mentions", [])
                    all_mentions.extend(mentions)
                    logger.info(
                        "Extracted mentions from chunk",
                        chunk=f"{i + 1}/{len(chunks)}",
                        count=len(mentions),
                    )

        except anthropic.APIError as e:
            logger.error("Claude API error during extraction", error=str(e), chunk=i + 1)
            continue

    return _deduplicate_mentions(all_mentions)


def _deduplicate_mentions(mentions: list[dict]) -> list[dict]:
    """Deduplicate mentions by artist+track, keeping the highest confidence one."""
    seen: dict[str, dict] = {}
    for m in mentions:
        key = f"{m['artist_name'].lower()}|{(m.get('track_title') or '').lower()}"
        if key not in seen or m.get("confidence", 0) > seen[key].get("confidence", 0):
            seen[key] = m
    return list(seen.values())


async def extract_for_item(session: AsyncSession, item: ContentItem) -> int:
    """Run extraction for a single content item. Returns count of new mentions."""
    # Get text — may need to fetch from source plugin
    text = item.raw_text
    if not text:
        source = await session.get(Source, item.source_id)
        if source:
            plugin = get_plugin(source.plugin_type)
            text = await plugin.extract_text(item)
            if text:
                item.raw_text = text

    if not text:
        logger.warning("No text content for item", item_id=str(item.id), title=item.title)
        item.analyzed = True
        return 0

    mentions_data = await extract_mentions_from_text(text, item.title)

    count = 0
    for m in mentions_data:
        mention = MusicMention(
            id=uuid.uuid4(),
            content_item_id=item.id,
            artist_name=m["artist_name"],
            track_title=m.get("track_title"),
            album_title=m.get("album_title"),
            genres=m.get("genres"),
            sentiment=m.get("sentiment", "positive"),
            context_snippet=m.get("context_snippet"),
            confidence=m.get("confidence", 0.5),
            discovered_at=datetime.now(timezone.utc),
        )
        session.add(mention)
        count += 1

    item.analyzed = True
    await session.flush()

    logger.info("Extracted mentions for item", title=item.title, count=count)
    return count


async def extract_all_unanalyzed() -> int:
    """Run extraction on all unanalyzed content items. Returns total mentions extracted."""
    async with async_session_factory() as session:
        result = await session.execute(
            select(ContentItem).where(ContentItem.analyzed.is_(False))
        )
        items = result.scalars().all()

        if not items:
            logger.info("No unanalyzed content items")
            return 0

        total = 0
        for item in items:
            try:
                count = await extract_for_item(session, item)
                total += count
            except Exception:
                logger.exception("Failed to extract from item", item_id=str(item.id))

        await session.commit()
        logger.info("Extraction complete", total_mentions=total, items_processed=len(items))
        return total
