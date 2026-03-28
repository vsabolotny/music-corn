"""Generate a radio-show-style podcast script using Claude."""

import anthropic
import structlog

from music_corn.config import settings
from music_corn.db.models import TasteProfile, WeeklyDigest

logger = structlog.get_logger()

SYSTEM_PROMPT = """\
You are a warm, knowledgeable music radio host named Corn. You're creating a personal \
weekly music discovery show for one specific listener. Your tone is conversational, \
enthusiastic but not over-the-top, and you genuinely love sharing music.

Rules:
- Keep the total script under 2000 words.
- Start with a brief, personal greeting (1-2 sentences).
- For each track, write a natural transition and explain why the listener might enjoy it. \
Reference the source where it was discovered when available.
- Place [TRACK_BREAK: <spotify_uri>] on its own line where a song preview should play.
- End with a brief sign-off summarizing the vibe of this week's picks.
- Do NOT use generic filler. Every sentence should add value.
- Write for spoken word — short sentences, natural rhythm, no markdown formatting."""


def generate_script(digest: WeeklyDigest, profile: TasteProfile) -> str:
    """Generate a podcast script from a weekly digest and taste profile."""
    # Build taste summary
    top_genres = list(profile.top_genres.keys())[:5]
    top_artists = [a["name"] for a in profile.top_artists[:5]]
    moods = profile.mood_tags or []

    taste_summary = (
        f"Listener's taste: genres — {', '.join(top_genres)}. "
        f"Favorite artists — {', '.join(top_artists)}. "
        f"Mood preferences — {', '.join(moods) if moods else 'varied'}."
    )

    # Build track list for prompt
    tracks_text = ""
    for i, track in enumerate(digest.track_list, 1):
        artist = track.get("artist", "Unknown")
        title = track.get("track") or "Unknown track"
        uri = track.get("spotify_uri", "")
        reason = track.get("reason", "")
        tracks_text += (
            f"\n{i}. \"{title}\" by {artist}"
            f"\n   Spotify URI: {uri}"
            f"\n   Why recommended: {reason}\n"
        )

    user_message = (
        f"{taste_summary}\n\n"
        f"This week's {len(digest.track_list)} discoveries:\n"
        f"{tracks_text}\n"
        f"Write the podcast script now."
    )

    client = anthropic.Anthropic(api_key=settings.anthropic_api_key)

    logger.info("Generating podcast script", track_count=len(digest.track_list))

    response = client.messages.create(
        model="claude-opus-4-20250514",
        max_tokens=4096,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": user_message}],
    )

    script = response.content[0].text
    logger.info("Script generated", length=len(script))
    return script
