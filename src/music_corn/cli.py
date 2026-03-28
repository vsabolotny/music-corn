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
def migrate():
    """Run database migrations."""
    import subprocess
    subprocess.run(["alembic", "upgrade", "head"], check=True)
    typer.echo("Migrations applied.")


if __name__ == "__main__":
    app()
