"""spot2gram: show the track playing on Spotify in your Telegram profile's music."""
from __future__ import annotations

import asyncio
import logging
import signal
import sys

import requests
from pyrogram import Client

from config import Config, ConfigError
from spotify import SpotifyConnect
from state import StateStore
from sync import MusicSync
from telegram_music import SetupError, TelegramMusic

log = logging.getLogger("spot2gram")

# Connect the way official Telegram Desktop on Windows does: its public API
# credentials, a motherboard as the device model, and its language pack.
TELEGRAM_CLIENT = dict(
    api_id=2040, api_hash="b18441a1ff607e10a989891a5462e627",
    device_model="B650 AORUS ELITE AX", system_version="Windows 10 x64",
    lang_pack="tdesktop", lang_code="en", system_lang_code="en",
    init_connection_params={"tz_offset": 0},
)
TDESKTOP_RELEASES = "https://api.github.com/repos/telegramdesktop/tdesktop/releases/latest"
TDESKTOP_FALLBACK_VERSION = "7.4.4"


def tdesktop_version() -> str:
    """The latest Telegram Desktop release, formatted like its app_version ("7.4.4 x64")."""
    try:
        response = requests.get(TDESKTOP_RELEASES, timeout=10)
        response.raise_for_status()
        version = response.json()["tag_name"].removeprefix("v")
    except (requests.RequestException, ValueError, KeyError) as exc:
        log.warning("Couldn't get the latest Telegram Desktop version (%s); using %s",
                    type(exc).__name__, TDESKTOP_FALLBACK_VERSION)
        version = TDESKTOP_FALLBACK_VERSION
    return f"{version} x64"


def setup_logging(level: str) -> None:
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)-7s %(name)-8s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        stream=sys.stdout,
    )
    # Pyrogram and websockets are chatty; only show their real problems.
    for noisy in ("pyrogram", "websockets", "urllib3"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


async def run(config: Config) -> None:
    # Ctrl+C already cancels us; make `kill` (SIGTERM) do the same on Linux/macOS.
    task = asyncio.current_task()
    try:
        asyncio.get_running_loop().add_signal_handler(signal.SIGTERM, task.cancel)
    except (NotImplementedError, AttributeError):
        pass  # Windows

    app_version = await asyncio.to_thread(tdesktop_version)
    spotify = SpotifyConnect(config.spotify_sp_dc)
    try:
        async with Client(config.session_name, workdir=str(config.workdir), fetch_topics=False,
                          skip_updates=True, no_updates=True, app_version=app_version,
                          **TELEGRAM_CLIENT) as app:
            telegram = TelegramMusic(app, config.channel_id, config.bot_username)
            await telegram.check_setup()
            sync = MusicSync(spotify, telegram, StateStore(config.state_file), config)
            log.info("Syncing Spotify -> Telegram profile. Press Ctrl+C to stop.")
            try:
                await sync.run_forever()
            finally:
                await sync.shutdown()
    finally:
        await spotify.close()


def main() -> int:
    try:
        config = Config.from_env()
    except ConfigError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 2
    setup_logging(config.log_level)
    try:
        asyncio.run(run(config))
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass
    except SetupError as exc:
        log.error("%s", exc)
        return 1
    log.info("Stopped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
