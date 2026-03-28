"""CLI entry point for music-corn."""

import asyncio
import uuid

import typer
import structlog

structlog.configure(
    processors=[
        structlog.dev.ConsoleRenderer(),
    ],
    wrapper_class=structlog.make_filtering_bound_logger(0),
)

# Import plugins so they self-register
import music_corn.sources.plugins.rss_plugin  # noqa: F401
import music_corn.sources.plugins.youtube_plugin  # noqa: F401
import music_corn.sources.plugins.dlf_plugin  # noqa: F401

app = typer.Typer(name="music-corn", help="AI-powered personalized music discovery system")


@app.command()
def ingest(
    source: str | None = typer.Option(None, help="Ingest a specific source by name"),
):
    """Fetch new content from sources."""
    from music_corn.pipeline.ingest import ingest_all_sources, ingest_source
    from music_corn.db.session import async_session_factory
    from music_corn.db.models import Source
    from sqlalchemy import select

    async def _run():
        if source:
            async with async_session_factory() as session:
                result = await session.execute(
                    select(Source).where(Source.name == source)
                )
                src = result.scalar_one_or_none()
                if not src:
                    typer.echo(f"Source '{source}' not found.")
                    raise typer.Exit(1)
                count = await ingest_source(session, src)
                typer.echo(f"Ingested {count} new items from '{source}'.")
        else:
            total = await ingest_all_sources()
            typer.echo(f"Ingested {total} new items from all sources.")

    asyncio.run(_run())


@app.command()
def add_source(
    name: str = typer.Option(..., help="Human-readable name for the source"),
    url: str = typer.Option(..., help="Feed URL or channel URL"),
    plugin_type: str = typer.Option("rss", help="Plugin type (rss, youtube)"),
):
    """Register a new content source."""
    from music_corn.db.models import Source
    from music_corn.db.session import async_session_factory

    async def _run():
        async with async_session_factory() as session:
            src = Source(
                id=uuid.uuid4(),
                name=name,
                url=url,
                plugin_type=plugin_type,
            )
            session.add(src)
            await session.commit()
            typer.echo(f"Added source '{name}' ({plugin_type}) -> {url}")

    asyncio.run(_run())


@app.command()
def list_sources():
    """List all registered sources."""
    from music_corn.db.models import Source
    from music_corn.db.session import async_session_factory
    from sqlalchemy import select

    async def _run():
        async with async_session_factory() as session:
            result = await session.execute(select(Source))
            sources = result.scalars().all()
            if not sources:
                typer.echo("No sources registered. Use 'add-source' to add one.")
                return
            for src in sources:
                status = "active" if src.is_active else "inactive"
                typer.echo(f"  [{status}] {src.name} ({src.plugin_type}) -> {src.url}")

    asyncio.run(_run())


@app.command()
def extract():
    """Run LLM extraction on unanalyzed content items."""
    from music_corn.extraction.extractor import extract_all_unanalyzed

    total = asyncio.run(extract_all_unanalyzed())
    typer.echo(f"Extracted {total} music mentions.")


@app.command()
def resolve():
    """Resolve unresolved music mentions to Spotify track IDs."""
    from music_corn.extraction.spotify_resolver import resolve_all_unresolved

    resolved = asyncio.run(resolve_all_unresolved())
    typer.echo(f"Resolved {resolved} mentions to Spotify tracks.")


@app.command()
def auth(
    email: str = typer.Option("default", help="User email/identifier"),
):
    """Connect your Spotify account via OAuth."""
    from music_corn.taste.spotify_client import run_auth_flow, save_user_tokens

    token_info = run_auth_flow()
    user = asyncio.run(save_user_tokens(token_info, email))
    typer.echo(f"Authenticated as user '{email}' (id: {user.id})")


@app.command()
def taste(
    email: str = typer.Option("default", help="User email/identifier"),
):
    """Analyze your Spotify listening and compute a taste profile."""
    from music_corn.taste.profiler import compute_taste_profile

    profile = asyncio.run(compute_taste_profile(email))
    if not profile:
        typer.echo("Failed to compute taste profile. Run 'auth' first.")
        raise typer.Exit(1)

    typer.echo(f"\nTaste Profile (id: {profile.id})")
    typer.echo(f"  Top genres: {', '.join(list(profile.top_genres.keys())[:10])}")
    typer.echo(f"  Top artists: {', '.join(a['name'] for a in profile.top_artists[:10])}")
    typer.echo(f"  Mood tags: {', '.join(profile.mood_tags or [])}")
    typer.echo(f"  Era bias: {profile.listening_era_bias}")

    audio = profile.audio_features_avg
    if audio:
        typer.echo(f"  Audio profile: energy={audio.get('energy', '?')}, "
                    f"valence={audio.get('valence', '?')}, "
                    f"danceability={audio.get('danceability', '?')}")


@app.command()
def recommend(
    email: str = typer.Option("default", help="User email/identifier"),
    count: int = typer.Option(15, help="Number of tracks to recommend"),
    discovery: float = typer.Option(0.5, help="Discovery dial: 0.0=safe, 1.0=adventurous"),
):
    """Generate personalized music recommendations."""
    from music_corn.recommendation.engine import generate_recommendations

    digest = asyncio.run(generate_recommendations(email, count, discovery))
    if not digest:
        typer.echo("Could not generate recommendations. Run 'extract', 'resolve', and 'taste' first.")
        raise typer.Exit(1)

    typer.echo(f"\nWeekly Recommendations (digest: {digest.id})")
    typer.echo(f"{'#':>3}  {'Artist':<25} {'Track':<35} {'Score':>5}  Reason")
    typer.echo("-" * 100)
    for i, track in enumerate(digest.track_list, 1):
        artist = track.get("artist", "?")[:24]
        title = (track.get("track") or "?")[:34]
        score = track.get("score", 0)
        reason = track.get("reason", "")[:30]
        typer.echo(f"{i:>3}  {artist:<25} {title:<35} {score:>5.3f}  {reason}")


@app.command()
def create_playlist(
    email: str = typer.Option("default", help="User email/identifier"),
    name: str = typer.Option("Music Corn Discoveries", help="Playlist name"),
):
    """Create a Spotify playlist from the latest weekly digest."""
    import httpx as _httpx
    from sqlalchemy import select, desc
    from music_corn.db.models import User, WeeklyDigest
    from music_corn.db.session import async_session_factory
    from music_corn.taste.spotify_client import get_user, get_authenticated_client

    async def _run():
        from music_corn.config import settings

        # Use console token if available (full API access), otherwise OAuth token
        token = settings.spotify_console_token
        if not token:
            user = await get_user(email)
            if not user or not user.spotify_access_token:
                typer.echo("Not authenticated. Run 'auth' first or set SPOTIFY_CONSOLE_TOKEN.")
                raise typer.Exit(1)
            token = user.spotify_access_token

        headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}

        # Get track URIs from latest digest or all resolved mentions
        async with async_session_factory() as session:
            # Try digest first
            user_obj = await get_user(email)
            track_uris = []
            if user_obj:
                result = await session.execute(
                    select(WeeklyDigest)
                    .where(WeeklyDigest.user_id == user_obj.id)
                    .order_by(desc(WeeklyDigest.created_at))
                    .limit(1)
                )
                digest = result.scalar_one_or_none()
                if digest and digest.track_list:
                    track_uris = [t["spotify_uri"] for t in digest.track_list if t.get("spotify_uri")]

            # Fallback: all resolved mentions
            if not track_uris:
                from music_corn.db.models import MusicMention
                result = await session.execute(
                    select(MusicMention).where(MusicMention.spotify_uri.isnot(None))
                )
                mentions = result.scalars().all()
                track_uris = [m.spotify_uri for m in mentions]

        if not track_uris:
            typer.echo("No Spotify tracks found.")
            raise typer.Exit(1)

        # Get user ID
        r = _httpx.get("https://api.spotify.com/v1/me", headers=headers)
        if r.status_code != 200:
            typer.echo(f"Token invalid: {r.status_code}")
            raise typer.Exit(1)
        user_id = r.json()["id"]

        # Create playlist
        r = _httpx.post(
            f"https://api.spotify.com/v1/users/{user_id}/playlists",
            headers=headers,
            json={"name": name, "public": False, "description": "Generated by music-corn"},
        )
        if r.status_code != 201:
            # Fallback to /me/playlists
            r = _httpx.post(
                "https://api.spotify.com/v1/me/playlists",
                headers=headers,
                json={"name": name, "public": False, "description": "Generated by music-corn"},
            )
        if r.status_code != 201:
            typer.echo(f"Failed to create playlist: {r.status_code} {r.text}")
            raise typer.Exit(1)

        playlist = r.json()

        # Add tracks via query params (most compatible)
        uris_param = ",".join(track_uris)
        _httpx.post(
            f"https://api.spotify.com/v1/playlists/{playlist['id']}/tracks?uris={uris_param}",
            headers={"Authorization": f"Bearer {token}"},
        )

        typer.echo(f"Created playlist: {playlist['name']}")
        typer.echo(f"Tracks: {len(track_uris)}")
        typer.echo(f"Open: {playlist['external_urls']['spotify']}")

    asyncio.run(_run())


@app.command()
def generate_podcast(
    email: str = typer.Option("default", help="User email/identifier"),
):
    """Generate an AI-narrated podcast from the latest weekly digest."""
    import tempfile
    from pathlib import Path
    from datetime import datetime, timezone

    from sqlalchemy import select, desc
    from music_corn.db.models import User, WeeklyDigest, TasteProfile
    from music_corn.db.session import async_session_factory
    from music_corn.podcast.script_writer import generate_script
    from music_corn.podcast.tts import split_script, synthesize_segments
    from music_corn.podcast.audio_assembler import assemble_podcast
    from music_corn.taste.spotify_client import get_authenticated_client

    async def _run():
        async with async_session_factory() as session:
            # Get user
            result = await session.execute(select(User).where(User.email == email))
            user = result.scalar_one_or_none()
            if not user:
                typer.echo("User not found. Run 'auth' first.")
                raise typer.Exit(1)

            # Get latest digest
            result = await session.execute(
                select(WeeklyDigest)
                .where(WeeklyDigest.user_id == user.id)
                .order_by(desc(WeeklyDigest.created_at))
                .limit(1)
            )
            digest = result.scalar_one_or_none()
            if not digest:
                typer.echo("No digest found. Run 'recommend' first.")
                raise typer.Exit(1)

            # Get latest taste profile
            result = await session.execute(
                select(TasteProfile)
                .where(TasteProfile.user_id == user.id)
                .order_by(desc(TasteProfile.computed_at))
                .limit(1)
            )
            profile = result.scalar_one_or_none()
            if not profile:
                typer.echo("No taste profile found. Run 'taste' first.")
                raise typer.Exit(1)

            # Step 1: Generate script
            typer.echo("Generating podcast script...")
            script = generate_script(digest, profile)
            digest.podcast_script = script
            typer.echo(f"Script generated ({len(script)} chars)")

            # Step 2: TTS
            typer.echo("Synthesizing speech...")
            segments = split_script(script)
            with tempfile.TemporaryDirectory() as tmpdir:
                work_dir = Path(tmpdir)
                synthesized = synthesize_segments(segments, work_dir)

                # Step 3: Assemble
                typer.echo("Assembling podcast...")
                sp = None
                try:
                    sp = get_authenticated_client(user)
                except Exception:
                    typer.echo("Warning: Could not connect to Spotify for previews.")

                now = datetime.now(timezone.utc)
                output_dir = Path(settings.podcast_output_dir)
                output_path = output_dir / f"music-corn-{now.strftime('%Y-%m-%d')}.mp3"

                assemble_podcast(synthesized, output_path, sp=sp)

                digest.audio_file_path = str(output_path)
                await session.commit()

            typer.echo(f"\nPodcast ready: {output_path}")

    asyncio.run(_run())


@app.command()
def run_weekly(
    email: str = typer.Option("default", help="User email/identifier"),
):
    """Run the full weekly pipeline manually (taste + recommend + podcast)."""
    from music_corn.pipeline.weekly import run_weekly_sync

    typer.echo("Running full weekly pipeline...")
    success = run_weekly_sync(email)
    if success:
        typer.echo("Weekly pipeline completed successfully.")
    else:
        typer.echo("Weekly pipeline failed. Check logs for details.")
        raise typer.Exit(1)


@app.command()
def scheduler():
    """Start the automated scheduler (long-running process)."""
    from music_corn.pipeline.scheduler import start_scheduler

    typer.echo("Starting music-corn scheduler...")
    start_scheduler()


@app.command()
def migrate():
    """Run database migrations."""
    import subprocess
    subprocess.run(["alembic", "upgrade", "head"], check=True)
    typer.echo("Migrations applied.")


if __name__ == "__main__":
    app()
