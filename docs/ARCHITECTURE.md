# Music Corn — Technical Architecture

## Overview

Music Corn is a Python-based system that discovers music from podcasts and YouTube, builds a personal taste profile from Spotify listening data, and generates weekly personalized playlists and AI-narrated podcasts.

## System Architecture

```
┌─────────────────────────────────────────────────────────────────────────┐
│                        SCHEDULER (APScheduler)                          │
│  Ingest (every 6h) → Extract (6h+30m) → Resolve (6h+60m)              │
│  Weekly pipeline: Monday 6:00 AM UTC                                    │
└──────┬──────────────────────────────────────┬───────────────────────────┘
       │                                      │
       ▼                                      ▼
┌──────────────────────┐           ┌──────────────────────────────┐
│  SOURCE INGESTION    │           │  WEEKLY PIPELINE             │
│                      │           │                              │
│  ┌────────────────┐  │           │  1. Taste Profile Refresh    │
│  │ Plugin Registry │  │           │     (Spotify API)            │
│  └───────┬────────┘  │           │  2. Recommendation Scoring   │
│          │           │           │     (5-factor algorithm)     │
│  ┌───────▼────────┐  │           │  3. Playlist Creation        │
│  │ RSS Plugin     │  │           │     (Spotify API)            │
│  │ YouTube Plugin │  │           │  4. Script Generation        │
│  │ DLF Plugin     │  │           │     (Claude Opus)            │
│  │ ... extensible │  │           │  5. TTS Narration            │
│  └───────┬────────┘  │           │     (OpenAI TTS)             │
│          │           │           │  6. Audio Assembly            │
│  ┌───────▼────────┐  │           │     (pydub + ffmpeg)         │
│  │ LLM Extractor  │  │           └──────────────┬───────────────┘
│  │ (Claude Sonnet)│  │                          │
│  └───────┬────────┘  │                          │
│          │           │                          │
│  ┌───────▼────────┐  │                          │
│  │Spotify Resolver│  │                          │
│  └───────┬────────┘  │                          │
└──────────┼───────────┘                          │
           │                                      │
           ▼                                      ▼
┌─────────────────────────────────────────────────────────────────────────┐
│                          PostgreSQL DATABASE                             │
│  sources │ content_items │ music_mentions │ users │ taste_profiles       │
│  weekly_digests │ digest_tracks                                         │
└─────────────────────────────────────────────────────────────────────────┘
```

## Data Flow

```
Source (RSS/YouTube/DLF)
  │
  ▼
Content Item (title, URL, raw_text)
  │
  ├─ [DLF] Download HLS audio → ffmpeg → MP3 → faster-whisper → transcript
  ├─ [YouTube] yt-dlp → subtitles/captions
  └─ [RSS] feedparser → entry text/description
  │
  ▼
Claude Sonnet (structured extraction via tool_use)
  │
  ▼
Music Mention (artist, track, album, genres, sentiment, confidence)
  │
  ▼
Spotify Search API → spotify_track_id, spotify_uri, enriched genres
  │
  ▼
Recommendation Engine (scored against Taste Profile)
  │
  ▼
Weekly Digest → Spotify Playlist + AI Podcast
```

## Components

### 1. Source Plugin System

**Location**: `src/music_corn/sources/`

Extensible plugin architecture using Python ABC + decorator-based registry.

```python
# Adding a new source = one file, zero changes elsewhere
@register_plugin("my_source")
class MyPlugin(SourcePlugin):
    async def fetch_new_items(self, source, since) -> list[ContentItem]: ...
    async def extract_text(self, item) -> str: ...
```

**Built-in plugins**:

| Plugin | Module | Description |
|--------|--------|-------------|
| `rss` | `plugins/rss_plugin.py` | RSS/Atom feeds via feedparser |
| `youtube` | `plugins/youtube_plugin.py` | YouTube channels via yt-dlp (subtitle extraction) |
| `dlf` | `plugins/dlf_plugin.py` | Deutschlandfunk audio podcasts (HLS download + Whisper transcription) |

**DLF Plugin pipeline**:
```
RSS feed (episode list)
  → Scrape episode page (extract HLS m3u8 URL from embedded JSON)
  → ffmpeg (download HLS → MP3, 64kbps mono 16kHz)
  → faster-whisper medium model (local, German language)
  → Full transcript stored as raw_text
```

### 2. LLM Extraction

**Location**: `src/music_corn/extraction/extractor.py`

Uses Claude Sonnet with `tool_use` for structured JSON output:

- **Chunking**: Long transcripts split into ~12,000 char windows with 500 char overlap
- **Extraction prompt**: Instructs Claude to return artist, track, album, genres, sentiment, confidence, context snippet
- **Deduplication**: Merges mentions of the same artist+track across chunks (keeps highest confidence)
- **Output**: `MusicMention` records in the database

**Extraction tool schema**:
```json
{
  "artist_name": "string (required)",
  "track_title": "string (optional)",
  "album_title": "string (optional)",
  "genres": ["string"],
  "sentiment": "positive | neutral | negative",
  "context_snippet": "string",
  "confidence": 0.0-1.0
}
```

### 3. Spotify Resolver

**Location**: `src/music_corn/extraction/spotify_resolver.py`

Resolves extracted mentions to Spotify track IDs:

- Searches by `artist:{name} track:{title}` when track is known
- Falls back to artist-only search
- Enriches mention with genres from Spotify artist profile
- Stores `spotify_track_id` and `spotify_uri` for downstream use

### 4. Taste Profiling

**Location**: `src/music_corn/taste/`

**OAuth flow** (`spotify_client.py`):
- Authorization Code flow with local FastAPI callback server on `127.0.0.1:8888`
- Token persistence in `users` table with automatic refresh
- Scopes: `user-top-read`, `user-library-read`, `playlist-read-private`, `playlist-modify-public`, `playlist-modify-private`, `user-read-recently-played`

**Data fetched from Spotify**:
- Top tracks (short/medium/long term)
- Top artists (short/medium/long term)
- Saved/liked tracks (up to 200)
- Recently played (up to 50)
- Audio features for all tracks

**Profile computation** (`profiler.py`):

| Component | Method |
|-----------|--------|
| Genre weights | Weighted by time range (recent 3x, medium 2x, long 1x) × artist popularity, normalized to 0-1 |
| Artist affinities | Cross-referenced from top artists + top tracks, top 50 artists scored |
| Audio features avg | Mean of danceability, energy, valence, acousticness, instrumentalness, liveness, tempo |
| Mood tags | Derived from audio features (e.g., energy > 0.7 → "energetic", valence < 0.35 → "melancholic") |
| Era bias | Decade distribution from track release dates |

### 5. Recommendation Engine

**Location**: `src/music_corn/recommendation/engine.py`

**5-factor weighted scoring**:

```
score = w1 × genre_overlap
      + w2 × artist_familiarity
      + w3 × audio_feature_similarity
      + w4 × source_sentiment
      + w5 × novelty_bonus
```

| Factor | Weight | Description |
|--------|--------|-------------|
| Genre overlap | 0.25 | Cosine similarity between mention genres and user genre preferences (with fuzzy matching) |
| Artist familiarity | 0.20 | Known artists get moderate scores, unknown artists get discovery bonus |
| Audio similarity | 0.20 | Euclidean distance in audio feature space (danceability, energy, valence, acousticness, instrumentalness) |
| Source sentiment | 0.15 | Higher confidence positive mentions score higher |
| Novelty bonus | 0.20 | Tracks user hasn't heard get a bonus; unknown artists get maximum novelty |

**Discovery dial** (0.0–1.0): Shifts weights between safe/familiar picks and adventurous discoveries.

**Diversity enforcement**: Max 2 tracks per artist, sorted by score. Output: top 10-15 tracks.

### 6. Podcast Generation

**Location**: `src/music_corn/podcast/`

Three-stage pipeline:

**Script Writer** (`script_writer.py`):
- Claude Opus generates radio-show-style narration
- Host persona "Corn" — warm, knowledgeable, conversational
- Personalized based on taste profile and recommendation reasons
- Inserts `[TRACK_BREAK: spotify:uri]` markers

**TTS** (`tts.py`):
- Splits script at `TRACK_BREAK` markers
- OpenAI TTS API (`tts-1-hd`) synthesizes narration segments
- Configurable voice (default: nova)

**Audio Assembler** (`audio_assembler.py`):
- Loads narration MP3 segments
- Fetches Spotify 30-second preview clips for each track break
- Crossfades narration ↔ previews (500ms)
- Normalizes loudness to -16 dBFS
- Exports final MP3 with ID3 tags

### 7. Pipeline Orchestration

**Location**: `src/music_corn/pipeline/`

**Scheduler** (`scheduler.py`):
- APScheduler with BlockingScheduler
- Graceful shutdown on SIGTERM/SIGINT
- Runs initial ingest on startup

| Job | Schedule | Description |
|-----|----------|-------------|
| `ingest` | Every 6 hours | Fetch new content from all active sources |
| `extract` | Every 6h + 30min | Run LLM extraction on unanalyzed items |
| `resolve` | Every 6h + 60min | Resolve mentions to Spotify IDs |
| `weekly` | Monday 6:00 AM | Full pipeline: taste → recommend → podcast |

**Weekly pipeline** (`weekly.py`): Orchestrates taste refresh → recommendations → script → TTS → audio assembly in sequence.

## Database Schema

```
sources (1) ──── (N) content_items (1) ──── (N) music_mentions
                                                      │
users (1) ──── (1) taste_profiles                     │
  │                                                   │
  └──── (N) weekly_digests (1) ──── (N) digest_tracks ┘
```

| Table | Key Columns |
|-------|-------------|
| `sources` | plugin_type, name, url, config_json, last_fetched_at |
| `content_items` | source_id, external_id (unique per source), title, raw_text, analyzed |
| `music_mentions` | artist_name, track_title, album_title, genres[], sentiment, confidence, spotify_track_id |
| `users` | email, spotify_access_token, spotify_refresh_token |
| `taste_profiles` | top_genres (JSONB), top_artists (JSONB), audio_features_avg (JSONB), mood_tags[] |
| `weekly_digests` | user_id, week_start, track_list (JSONB), podcast_script, audio_file_path |
| `digest_tracks` | digest_id, music_mention_id, rank, match_reason |

## Project Structure

```
music-corn/
├── pyproject.toml                    # Dependencies (uv)
├── Dockerfile                        # Multi-stage build with ffmpeg
├── docker-compose.yml                # PostgreSQL + app
├── alembic/                          # Database migrations
│   ├── alembic.ini
│   └── versions/
├── src/music_corn/
│   ├── config.py                     # pydantic-settings from .env
│   ├── cli.py                        # Typer CLI (12 commands)
│   ├── db/
│   │   ├── models.py                 # 7 SQLAlchemy models
│   │   └── session.py                # Async engine + session factory
│   ├── sources/
│   │   ├── base.py                   # SourcePlugin ABC
│   │   ├── registry.py               # Plugin registry (decorator-based)
│   │   └── plugins/
│   │       ├── rss_plugin.py         # RSS/Atom feeds
│   │       ├── youtube_plugin.py     # YouTube (yt-dlp)
│   │       └── dlf_plugin.py         # Deutschlandfunk (HLS + Whisper)
│   ├── extraction/
│   │   ├── extractor.py              # Claude Sonnet extraction
│   │   └── spotify_resolver.py       # Mention → Spotify ID
│   ├── taste/
│   │   ├── spotify_client.py         # OAuth + Spotify data fetching
│   │   └── profiler.py               # Taste profile computation
│   ├── recommendation/
│   │   └── engine.py                 # 5-factor scoring + diversity
│   ├── podcast/
│   │   ├── script_writer.py          # Claude Opus script generation
│   │   ├── tts.py                    # OpenAI TTS
│   │   └── audio_assembler.py        # pydub + ffmpeg assembly
│   └── pipeline/
│       ├── scheduler.py              # APScheduler jobs
│       ├── ingest.py                 # Source ingestion orchestration
│       └── weekly.py                 # Weekly pipeline orchestration
├── tests/
├── assets/jingles/
└── .env.example
```

## AI Models Used

| Task | Model | Why |
|------|-------|-----|
| Audio transcription | faster-whisper medium (local) | Free, runs on-device, good German accuracy |
| Music extraction | Claude Sonnet | Structured extraction via tool_use, cost-effective |
| Podcast scripting | Claude Opus | Creative quality for natural radio-show narration |
| Text-to-speech | OpenAI TTS (tts-1-hd) | High quality voice synthesis |

## Infrastructure

**Local development**:
```bash
docker compose up -d db    # PostgreSQL on port 5433
uv sync                    # Install dependencies
music-corn migrate         # Apply migrations
```

**Production** (Docker):
```bash
docker compose up -d       # PostgreSQL + app (scheduler)
```

The `Dockerfile` uses a multi-stage build: builder stage with `uv` for dependency installation, runtime stage with `ffmpeg` as the only system dependency.

**Deployment targets**: Railway, Fly.io, or any Docker-compatible platform. PostgreSQL provisioned as a managed add-on.
