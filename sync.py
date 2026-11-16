"""Keeps the Telegram profile's music in step with what Spotify is playing.

Each tick asks Spotify what's playing and moves one step towards showing it:
post the Spotify link via @nowtrackbot, wait for the bot to attach the audio,
then put that audio on the profile and remove the previous song. Nothing
blocks for long, so a track change is noticed within one tick.
"""
from __future__ import annotations

import asyncio
import logging
import time

from pyrogram.errors import FloodWait, RPCError

from config import Config
from spotify import SpotifyBroken, SpotifyConnect, SpotifyUnavailable, Track
from state import ProfileTrack, StateStore
from telegram_music import AudioStatus, TelegramMusic

log = logging.getLogger("sync")

# After the bot fails on a track, don't ask about it again for this long.
RETRY_AFTER_NOT_FOUND = 60 * 60
RETRY_AFTER_TIMEOUT = 10 * 60
# Spotify pushes changes, so a tick runs as soon as one arrives. Otherwise
# tick this often for timers (clear delay, retries); it's cheap, no requests.
TICK_INTERVAL = 5.0
# Telegram has no push here: check on the bot's message this often.
BOT_CHECK_INTERVAL = 1.5
MAX_BACKOFF = 120.0
BROKEN_RETRY_INTERVAL = 300.0
MAX_REMOVAL_ATTEMPTS = 5
EXIT_CLEANUP_TIMEOUT = 15.0


def human_duration(seconds: float) -> str:
    seconds = int(seconds)
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m {seconds % 60:02d}s"
    return f"{seconds // 3600}h {seconds % 3600 // 60:02d}m"


def describe(exc: BaseException) -> str:
    if isinstance(exc, RPCError):
        return str(exc) or type(exc).__name__
    if isinstance(exc, (OSError, asyncio.TimeoutError, ConnectionError)):
        return f"network error ({type(exc).__name__})"
    return f"{type(exc).__name__}: {exc}"


class Trouble:
    """Logs an ongoing problem once instead of every retry, and logs recovery."""
    REMIND_EVERY = 600.0

    def __init__(self, name: str):
        self.name = name
        self.failures = 0
        self._reason: str | None = None
        self._since: float | None = None
        self._last_logged = 0.0

    @property
    def duration(self) -> float:
        return 0.0 if self._since is None else time.monotonic() - self._since

    def fail(self, reason: str, level: int = logging.WARNING, exc_info: BaseException | None = None) -> None:
        now = time.monotonic()
        if self._since is None:
            self._since = now
        self.failures += 1
        if reason != self._reason:
            log.log(level, "%s: %s", self.name, reason, exc_info=exc_info)
            self._reason, self._last_logged = reason, now
        elif now - self._last_logged >= self.REMIND_EVERY:
            log.log(level, "%s: still failing for %s (%s)", self.name, human_duration(self.duration), reason)
            self._last_logged = now

    def recovered(self) -> None:
        if self._since is not None:
            log.info("%s: OK again after %s", self.name, human_duration(self.duration))
        self.failures, self._reason, self._since = 0, None, None

    def backoff(self, base: float) -> float:
        return min(MAX_BACKOFF, base * 2 ** min(max(self.failures - 1, 0), 10))


class MusicSync:
    def __init__(self, spotify: SpotifyConnect, telegram: TelegramMusic, store: StateStore, config: Config):
        self.spotify = spotify
        self.telegram = telegram
        self.store = store
        self.config = config
        self.state = store.load()
        self._now_playing: str | None = None  # Last track ID announced in the log.
        self._stopped_since: float | None = None
        self._spotify_trouble = Trouble("Spotify")
        self._telegram_trouble = Trouble("Telegram")

    async def run_forever(self) -> None:
        if self.state.current:
            log.info("Profile currently shows %s (from the last run)", self.state.current.title)
        while True:
            delay = await self.tick()
            if self._spotify_trouble.failures or self._telegram_trouble.failures:
                await asyncio.sleep(delay)  # Backing off: don't let pushes cut it short.
            else:
                await self.spotify.wait_for_change(delay)

    async def tick(self) -> float:
        """Do one step; return how long to wait before the next one."""
        try:
            track = await self.spotify.currently_playing()
        except SpotifyUnavailable as exc:
            return await self._on_spotify_unavailable(exc)
        self._spotify_trouble.recovered()

        try:
            waiting_for_bot = await self.sync(track)
        except FloodWait as exc:
            self._telegram_trouble.fail(f"rate limited for {exc.value}s")
            return float(exc.value) + 1
        except (RPCError, OSError, asyncio.TimeoutError, ConnectionError, RuntimeError) as exc:
            self._telegram_trouble.fail(describe(exc))
            return self._telegram_trouble.backoff(TICK_INTERVAL)
        except Exception as exc:  # A bug: log it with a traceback, but keep running.
            self._telegram_trouble.fail(f"unexpected {describe(exc)}", logging.ERROR, exc_info=exc)
            return self._telegram_trouble.backoff(TICK_INTERVAL)
        self._telegram_trouble.recovered()
        return BOT_CHECK_INTERVAL if waiting_for_bot else TICK_INTERVAL

    async def sync(self, track: Track | None) -> bool:
        """Move the profile towards `track`. Returns True while waiting on the bot."""
        await self._remove_replaced()
        if track is None:
            await self._on_stopped()
            return False
        self._stopped_since = None
        if self._now_playing != track.id:
            self._now_playing = track.id
            log.info("Now playing %s", track.url)
        return await self._show(track)

    async def shutdown(self) -> None:
        if not self.config.clear_on_exit or not (self.state.current or self.state.to_remove):
            return
        try:
            await asyncio.wait_for(self._clear("script stopped"), EXIT_CLEANUP_TIMEOUT)
        except Exception as exc:
            log.warning("Couldn't clean up the profile on exit (%s); will do it on the next start",
                        describe(exc))

    # -- steps ------------------------------------------------------------

    async def _show(self, track: Track) -> bool:
        state = self.state
        if state.current and state.current.track_id == track.id:
            return False
        if state.is_unavailable(track.id):
            # Showing the previous song would be wrong too.
            await self._clear("no audio for the current track")
            return False

        message_id = state.messages.get(track.id)
        if message_id is None:
            message_id = await self.telegram.post_track(track.url)
            if message_id is None:
                await self._give_up(track, None, f"@{self.config.bot_username} found nothing",
                                    RETRY_AFTER_NOT_FOUND)
                return False
            log.debug("Asked the bot for %s (message %d)", track.url, message_id)
            state.remember_message(track.id, message_id)
            self._save()
            return True  # The bot needs a moment to attach the audio.

        audio = await self.telegram.inspect(message_id)
        if audio.status is AudioStatus.MISSING:
            log.debug("Message %d was deleted; asking the bot again", message_id)
            del state.messages[track.id]
            self._save()
            return True
        if audio.status is AudioStatus.LOADING:
            if audio.age < self.config.audio_timeout:
                return True
            await self._give_up(track, message_id, f"no audio after {human_duration(audio.age)}",
                                RETRY_AFTER_TIMEOUT)
            return False
        if audio.status is AudioStatus.FAILED:
            await self._give_up(track, message_id, f'bot says "{audio.note}"', RETRY_AFTER_NOT_FOUND)
            return False

        await self._put_on_profile(audio.to_profile_track(track.id))
        return False

    async def _put_on_profile(self, new: ProfileTrack) -> None:
        # Add first, then remove the old one, so the profile is never empty in between.
        await self.telegram.add_to_profile(new)
        previous, self.state.current = self.state.current, new
        if previous and previous.doc_id != new.doc_id:
            self.state.to_remove.append(previous)
        self._save()
        log.info("Profile: %s", new.title)
        await self._remove_replaced()

    async def _give_up(self, track: Track, message_id: int | None, reason: str, retry_after: float) -> None:
        log.warning("No audio for %s: %s. Won't retry for %s", track.url, reason, human_duration(retry_after))
        if message_id is not None:
            await self.telegram.delete(message_id)
            self.state.messages.pop(track.id, None)
        self.state.unavailable_until[track.id] = time.time() + retry_after
        self._save()
        await self._clear("no audio for the current track")

    async def _on_stopped(self) -> None:
        if self._now_playing is not None:
            log.info("Playback stopped")
            self._now_playing = None
        if self.state.current is None:
            return
        now = time.monotonic()
        if self._stopped_since is None:
            self._stopped_since = now
        if now - self._stopped_since >= self.config.clear_delay:
            await self._clear("playback stopped")

    async def _on_spotify_unavailable(self, exc: SpotifyUnavailable) -> float:
        broken = isinstance(exc, SpotifyBroken)
        self._spotify_trouble.fail(str(exc), logging.ERROR if broken else logging.WARNING)
        stale_after = self.config.stale_after
        if self.state.current and stale_after and self._spotify_trouble.duration >= stale_after:
            try:
                await self._clear(f"Spotify unreachable for {human_duration(self._spotify_trouble.duration)}")
            except Exception as clear_exc:
                self._telegram_trouble.fail(describe(clear_exc))
        return BROKEN_RETRY_INTERVAL if broken else self._spotify_trouble.backoff(TICK_INTERVAL)

    async def _clear(self, reason: str) -> None:
        current = self.state.current
        if current is not None:
            log.info("Removing %s from the profile: %s", current.title, reason)
            self.state.current = None
            self.state.to_remove.append(current)
            self._save()
        await self._remove_replaced()

    async def _remove_replaced(self) -> None:
        """Remove songs we replaced earlier; retries failures on later ticks."""
        keep = self.state.current.doc_id if self.state.current else None
        for item in list(self.state.to_remove):
            if item.doc_id != keep:  # Never remove the song that's showing now.
                try:
                    await self.telegram.remove_from_profile(item)
                except FloodWait:
                    raise
                except (RPCError, RuntimeError) as exc:
                    item.removal_failures += 1
                    if item.removal_failures < MAX_REMOVAL_ATTEMPTS:
                        log.warning("Couldn't remove %s from the profile (%s); will retry",
                                    item.title, describe(exc))
                        self._save()
                        continue
                    log.error("Giving up removing %s from the profile after %d attempts (%s). "
                              "Remove it manually.", item.title, item.removal_failures, describe(exc))
                else:
                    log.debug("Removed %s from the profile", item.title)
            self.state.to_remove.remove(item)
            self._save()

    def _save(self) -> None:
        self.store.save(self.state)
