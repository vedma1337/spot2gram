"""Settings, read from environment variables and the project's `.env` file."""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent


class ConfigError(ValueError):
    """The configuration is missing or invalid; the message says how to fix it."""


def _number(name: str, default: float, minimum: float) -> float:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError:
        raise ConfigError(f"{name} must be a number, got {raw!r}") from None
    if value < minimum:
        raise ConfigError(f"{name} must be at least {minimum:g}, got {value:g}")
    return value


def _flag(name: str, default: bool) -> bool:
    raw = os.getenv(name, "").strip().lower()
    if not raw:
        return default
    if raw in ("1", "true", "yes", "on"):
        return True
    if raw in ("0", "false", "no", "off"):
        return False
    raise ConfigError(f"{name} must be true or false, got {raw!r}")


@dataclass(frozen=True)
class Config:
    spotify_sp_dc: str
    channel_id: int
    # Keep the track on the profile this long after playback stops, so short
    # pauses and track transitions don't make it blink.
    clear_delay: float = 15.0
    # If Spotify can't be reached for this long, the profile track is probably
    # stale, so remove it. 0 keeps it until Spotify answers again.
    stale_after: float = 600.0
    # How long @nowtrackbot gets to attach the audio to its message.
    audio_timeout: float = 90.0
    # Remove the track from the profile when the script stops.
    clear_on_exit: bool = True
    log_level: str = "INFO"
    bot_username: str = "nowtrackbot"
    session_name: str = "music_sync"
    workdir: Path = BASE_DIR
    state_file: Path = BASE_DIR / "state.json"

    @classmethod
    def from_env(cls) -> Config:
        load_dotenv(BASE_DIR / ".env")

        sp_dc = os.getenv("SPOTIFY_SP_DC", "").strip()
        if not sp_dc:
            raise ConfigError("Set SPOTIFY_SP_DC in .env to your Spotify web player's sp_dc cookie")

        raw_channel = os.getenv("CHANNEL_ID", "").strip()
        if not raw_channel:
            raise ConfigError("Set CHANNEL_ID in .env to your channel's numeric ID, e.g. -1001234567890")
        try:
            channel_id = int(raw_channel)
        except ValueError:
            raise ConfigError(
                f"CHANNEL_ID must be the channel's numeric ID like -1001234567890, got {raw_channel!r}"
            ) from None
        if channel_id == 0:
            raise ConfigError("CHANNEL_ID can't be 0")

        log_level = os.getenv("LOG_LEVEL", "INFO").strip().upper() or "INFO"
        if log_level not in ("DEBUG", "INFO", "WARNING", "ERROR"):
            raise ConfigError(f"LOG_LEVEL must be DEBUG, INFO, WARNING or ERROR, got {log_level!r}")

        return cls(
            spotify_sp_dc=sp_dc,
            channel_id=channel_id,
            clear_delay=_number("CLEAR_DELAY_SECONDS", cls.clear_delay, 0),
            stale_after=_number("STALE_AFTER_SECONDS", cls.stale_after, 0),
            audio_timeout=_number("AUDIO_TIMEOUT_SECONDS", cls.audio_timeout, 10),
            clear_on_exit=_flag("CLEAR_ON_EXIT", cls.clear_on_exit),
            log_level=log_level,
        )
