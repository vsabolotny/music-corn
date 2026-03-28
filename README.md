# Music Corn

AI-powered personalized music discovery system. Aggregates music recommendations from podcasts and YouTube, analyzes your Spotify listening taste, and generates weekly personalized playlists and AI-narrated podcasts.

## Features

- **Source Ingestion** — RSS feeds, YouTube channels, Deutschlandfunk audio podcasts (with Whisper transcription)
- **LLM Extraction** — Claude Sonnet extracts artist names, tracks, albums, genres, and sentiment from transcripts
- **Spotify Resolution** — Matches extracted mentions to Spotify track IDs
- **Taste Profiling** — Analyzes your Spotify listening history (top tracks, artists, audio features, mood)
- **Recommendation Engine** — 5-factor weighted scoring with configurable discovery dial
- **Playlist Generation** — Creates Spotify playlists from recommendations
- **Podcast Generation** — AI-narrated weekly podcast with TTS and song previews
- **Automated Scheduling** — APScheduler runs the full pipeline on a configurable schedule

## Quick Start

```bash
# Prerequisites: Python 3.12+, Docker, ffmpeg

# Install dependencies
uv sync

# Start PostgreSQL
docker compose up -d db

# Run migrations
music-corn migrate

# Add a source
music-corn add-source --name "JazzFacts" \
  --url "https://www.deutschlandfunk.de/jazzfacts-100.html" \
  --plugin-type dlf

# Ingest content
music-corn ingest

# Extract music mentions (needs ANTHROPIC_API_KEY)
music-corn extract

# Resolve to Spotify (needs SPOTIFY_CLIENT_ID/SECRET)
music-corn resolve

# Connect your Spotify account
music-corn auth

# Analyze your taste
music-corn taste

# Generate recommendations
music-corn recommend

# Create Spotify playlist
music-corn create-playlist --name "My Discoveries"

# Or run everything automatically
music-corn scheduler
```

## Configuration

Copy `.env.example` to `.env` and fill in your API keys:

```bash
cp .env.example .env
```

| Variable | Required | Description |
|----------|----------|-------------|
| `ANTHROPIC_API_KEY` | Yes | For music extraction and podcast script writing |
| `SPOTIFY_CLIENT_ID` | Yes | From Spotify Developer Dashboard |
| `SPOTIFY_CLIENT_SECRET` | Yes | From Spotify Developer Dashboard |
| `SPOTIFY_CONSOLE_TOKEN` | Optional | Token from developer.spotify.com console (for playlist creation) |
| `OPENAI_API_KEY` | Optional | For TTS podcast narration |
| `DATABASE_URL` | Auto | PostgreSQL connection string |

## CLI Commands

| Command | Description |
|---------|-------------|
| `add-source` | Register an RSS feed, YouTube channel, or DLF podcast |
| `list-sources` | Show all registered sources |
| `ingest` | Fetch new content from sources |
| `extract` | Run LLM extraction on unanalyzed content |
| `resolve` | Match mentions to Spotify track IDs |
| `auth` | Connect your Spotify account via OAuth |
| `taste` | Compute your personal taste profile |
| `recommend` | Generate personalized recommendations |
| `create-playlist` | Create a Spotify playlist from recommendations |
| `generate-podcast` | Generate an AI-narrated podcast MP3 |
| `run-weekly` | Run the full weekly pipeline manually |
| `scheduler` | Start the automated scheduler |
| `migrate` | Apply database migrations |

## Tech Stack

Python 3.12 | PostgreSQL 16 | SQLAlchemy 2.0 | Claude API | Spotify Web API | faster-whisper | ffmpeg | APScheduler | Docker
