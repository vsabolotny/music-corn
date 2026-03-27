"""SQLAlchemy ORM models for music-corn."""

import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, Float, ForeignKey, Integer, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship
from sqlalchemy.sql import func


class Base(DeclarativeBase):
    pass


class Source(Base):
    __tablename__ = "sources"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    plugin_type: Mapped[str] = mapped_column(Text, nullable=False)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    url: Mapped[str] = mapped_column(Text, nullable=False)
    config_json: Mapped[dict] = mapped_column(JSONB, default=dict)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    last_fetched_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    content_items: Mapped[list["ContentItem"]] = relationship(back_populates="source")


class ContentItem(Base):
    __tablename__ = "content_items"
    __table_args__ = (UniqueConstraint("source_id", "external_id", name="uq_source_external"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    source_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("sources.id"), nullable=False
    )
    external_id: Mapped[str] = mapped_column(Text, nullable=False)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    url: Mapped[str] = mapped_column(Text, nullable=False)
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    raw_text: Mapped[str | None] = mapped_column(Text)
    analyzed: Mapped[bool] = mapped_column(Boolean, default=False)
    fetched_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    source: Mapped["Source"] = relationship(back_populates="content_items")
    music_mentions: Mapped[list["MusicMention"]] = relationship(back_populates="content_item")


class MusicMention(Base):
    __tablename__ = "music_mentions"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    content_item_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("content_items.id"), nullable=False
    )
    artist_name: Mapped[str] = mapped_column(Text, nullable=False)
    track_title: Mapped[str | None] = mapped_column(Text)
    album_title: Mapped[str | None] = mapped_column(Text)
    genres: Mapped[list[str] | None] = mapped_column(ARRAY(Text))
    sentiment: Mapped[str] = mapped_column(Text, default="positive")
    context_snippet: Mapped[str | None] = mapped_column(Text)
    confidence: Mapped[float] = mapped_column(Float, default=0.5)
    spotify_track_id: Mapped[str | None] = mapped_column(Text)
    spotify_uri: Mapped[str | None] = mapped_column(Text)
    discovered_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    content_item: Mapped["ContentItem"] = relationship(back_populates="music_mentions")


class User(Base):
    __tablename__ = "users"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    email: Mapped[str] = mapped_column(Text, unique=True, nullable=False)
    spotify_access_token: Mapped[str | None] = mapped_column(Text)
    spotify_refresh_token: Mapped[str | None] = mapped_column(Text)
    spotify_token_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    taste_profiles: Mapped[list["TasteProfile"]] = relationship(back_populates="user")
    weekly_digests: Mapped[list["WeeklyDigest"]] = relationship(back_populates="user")


class TasteProfile(Base):
    __tablename__ = "taste_profiles"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id"), nullable=False
    )
    computed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    top_genres: Mapped[dict] = mapped_column(JSONB, default=dict)
    top_artists: Mapped[list] = mapped_column(JSONB, default=list)
    audio_features_avg: Mapped[dict] = mapped_column(JSONB, default=dict)
    mood_tags: Mapped[list[str] | None] = mapped_column(ARRAY(Text))
    listening_era_bias: Mapped[dict] = mapped_column(JSONB, default=dict)

    user: Mapped["User"] = relationship(back_populates="taste_profiles")


class WeeklyDigest(Base):
    __tablename__ = "weekly_digests"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id"), nullable=False
    )
    week_start: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    track_list: Mapped[list] = mapped_column(JSONB, default=list)
    podcast_script: Mapped[str | None] = mapped_column(Text)
    audio_file_path: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    user: Mapped["User"] = relationship(back_populates="weekly_digests")
    digest_tracks: Mapped[list["DigestTrack"]] = relationship(back_populates="digest")


class DigestTrack(Base):
    __tablename__ = "digest_tracks"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    digest_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("weekly_digests.id"), nullable=False
    )
    music_mention_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("music_mentions.id"), nullable=False
    )
    rank: Mapped[int] = mapped_column(Integer, nullable=False)
    match_reason: Mapped[str | None] = mapped_column(Text)

    digest: Mapped["WeeklyDigest"] = relationship(back_populates="digest_tracks")
    music_mention: Mapped["MusicMention"] = relationship()
