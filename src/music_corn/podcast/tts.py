"""Text-to-speech using OpenAI TTS API."""

import re
import tempfile
from pathlib import Path

import httpx
import structlog

from music_corn.config import settings

logger = structlog.get_logger()

TRACK_BREAK_PATTERN = re.compile(r"\[TRACK_BREAK:\s*(spotify:[^\]]+)\]")


def split_script(script: str) -> list[dict]:
    """Split script at TRACK_BREAK markers into narration segments and track URIs.

    Returns list of dicts:
        {"type": "narration", "text": "..."} or
        {"type": "track_break", "spotify_uri": "spotify:track:..."}
    """
    segments = []
    last_end = 0

    for match in TRACK_BREAK_PATTERN.finditer(script):
        text = script[last_end : match.start()].strip()
        if text:
            segments.append({"type": "narration", "text": text})
        segments.append({"type": "track_break", "spotify_uri": match.group(1)})
        last_end = match.end()

    # Remaining text after last marker
    remaining = script[last_end:].strip()
    if remaining:
        segments.append({"type": "narration", "text": remaining})

    return segments


def synthesize_speech(text: str, output_path: Path, voice: str | None = None) -> Path:
    """Convert text to speech using OpenAI TTS API. Returns path to MP3 file."""
    voice = voice or settings.tts_voice

    logger.info("Synthesizing speech", length=len(text), voice=voice)

    with httpx.Client(timeout=120) as client:
        response = client.post(
            "https://api.openai.com/v1/audio/speech",
            headers={"Authorization": f"Bearer {settings.openai_api_key}"},
            json={
                "model": "tts-1-hd",
                "input": text,
                "voice": voice,
                "response_format": "mp3",
            },
        )
        response.raise_for_status()

    output_path.write_bytes(response.content)
    logger.info("Speech synthesized", output=str(output_path), size_kb=len(response.content) // 1024)
    return output_path


def synthesize_segments(
    segments: list[dict], work_dir: Path
) -> list[dict]:
    """Synthesize all narration segments. Returns segments with file paths added.

    Narration segments get a "file" key with the path to the MP3.
    Track breaks are passed through unchanged.
    """
    result = []
    narration_idx = 0

    for seg in segments:
        if seg["type"] == "narration":
            output_path = work_dir / f"narration_{narration_idx:03d}.mp3"
            synthesize_speech(seg["text"], output_path)
            result.append({**seg, "file": output_path})
            narration_idx += 1
        else:
            result.append(seg)

    logger.info("All segments synthesized", count=narration_idx)
    return result
