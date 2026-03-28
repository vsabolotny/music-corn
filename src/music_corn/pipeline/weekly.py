"""Weekly pipeline orchestration — taste refresh, recommend, generate podcast."""

import asyncio
import tempfile
from datetime import datetime, timezone
from pathlib import Path

import structlog
from sqlalchemy import desc, select

from music_corn.config import settings
from music_corn.db.models import TasteProfile, User, WeeklyDigest
from music_corn.db.session import async_session_factory

logger = structlog.get_logger()


async def run_weekly_pipeline(email: str = "default") -> bool:
    """Run the full weekly pipeline for a user.

    Steps:
        1. Refresh taste profile from Spotify
        2. Generate recommendations
        3. Generate podcast

    Returns True on success, False on failure.
    """
    logger.info("Starting weekly pipeline", email=email)

    # Step 1: Refresh taste profile
    try:
        from music_corn.taste.profiler import compute_taste_profile

        profile = await compute_taste_profile(email)
        if not profile:
            logger.error("Failed to compute taste profile", email=email)
            return False
        logger.info("Taste profile refreshed", profile_id=str(profile.id))
    except Exception:
        logger.exception("Taste profiling failed", email=email)
        return False

    # Step 2: Generate recommendations
    try:
        from music_corn.recommendation.engine import generate_recommendations

        digest = await generate_recommendations(email, count=15, discovery_dial=0.5)
        if not digest:
            logger.error("No recommendations generated", email=email)
            return False
        logger.info("Recommendations generated", digest_id=str(digest.id))
    except Exception:
        logger.exception("Recommendation generation failed", email=email)
        return False

    # Step 3: Generate podcast
    try:
        from music_corn.podcast.script_writer import generate_script
        from music_corn.podcast.tts import split_script, synthesize_segments
        from music_corn.podcast.audio_assembler import assemble_podcast
        from music_corn.taste.spotify_client import get_authenticated_client, get_user

        user = await get_user(email)
        if not user:
            logger.error("User not found for podcast generation", email=email)
            return False

        # Re-fetch digest and profile within a session
        async with async_session_factory() as session:
            result = await session.execute(
                select(WeeklyDigest)
                .where(WeeklyDigest.user_id == user.id)
                .order_by(desc(WeeklyDigest.created_at))
                .limit(1)
            )
            digest = result.scalar_one_or_none()

            result = await session.execute(
                select(TasteProfile)
                .where(TasteProfile.user_id == user.id)
                .order_by(desc(TasteProfile.computed_at))
                .limit(1)
            )
            profile = result.scalar_one_or_none()

            if not digest or not profile:
                logger.error("Missing digest or profile for podcast")
                return False

            # Generate script
            script = generate_script(digest, profile)
            digest.podcast_script = script

            # TTS + Assembly
            segments = split_script(script)
            with tempfile.TemporaryDirectory() as tmpdir:
                work_dir = Path(tmpdir)
                synthesized = synthesize_segments(segments, work_dir)

                sp = None
                try:
                    sp = get_authenticated_client(user)
                except Exception:
                    logger.warning("Could not get Spotify client for previews")

                now = datetime.now(timezone.utc)
                output_dir = Path(settings.podcast_output_dir)
                output_path = output_dir / f"music-corn-{now.strftime('%Y-%m-%d')}.mp3"

                assemble_podcast(synthesized, output_path, sp=sp)
                digest.audio_file_path = str(output_path)

            await session.commit()

        logger.info("Podcast generated", output=str(output_path))

    except Exception:
        logger.exception("Podcast generation failed", email=email)
        return False

    logger.info("Weekly pipeline complete", email=email)
    return True


def run_weekly_sync(email: str = "default") -> bool:
    """Synchronous entry point for the weekly pipeline."""
    return asyncio.run(run_weekly_pipeline(email))
