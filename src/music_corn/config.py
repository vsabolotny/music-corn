"""Application configuration loaded from environment variables."""

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8")

    # Database
    database_url: str = "postgresql+asyncpg://music_corn:music_corn@localhost:5433/music_corn"

    # Anthropic
    anthropic_api_key: str = ""

    # OpenAI (for Whisper + TTS)
    openai_api_key: str = ""

    # Spotify
    spotify_client_id: str = ""
    spotify_client_secret: str = ""
    spotify_redirect_uri: str = "http://127.0.0.1:8888/callback"
    spotify_console_token: str = ""  # Token from developer.spotify.com console (full API access)

    # Podcast Discovery
    podcastindex_api_key: str = ""
    podcastindex_api_secret: str = ""
    listennotes_api_key: str = ""

    # Podcast generation
    tts_voice: str = "nova"
    podcast_output_dir: str = "./output/podcasts"

    # Scheduling
    ingest_interval_hours: int = 6
    weekly_pipeline_day: str = "mon"
    weekly_pipeline_hour: int = 6

    # Logging
    log_level: str = "INFO"


settings = Settings()
