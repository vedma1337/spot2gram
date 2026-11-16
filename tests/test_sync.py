import logging
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from pyrogram.errors import BadRequest
from pyrogram.raw.types import InputDocument

import sync as sync_module
from config import Config
from spotify import SpotifyBroken, SpotifyUnavailable, Track
from state import StateStore
from sync import BOT_CHECK_INTERVAL, MusicSync
from telegram_music import AudioStatus, ChannelAudio

logging.getLogger().addHandler(logging.NullHandler())  # Keep test output clean.

A = Track("A" * 22)
B = Track("B" * 22)


class FakeSpotify:
    def __init__(self):
        self.playing = None  # A Track, None, or an exception to raise.

    async def currently_playing(self):
        if isinstance(self.playing, Exception):
            raise self.playing
        return self.playing


class FakeTelegram:
    """Channel messages and profile music, with a log of calls in order."""

    def __init__(self):
        self.messages: dict[int, ChannelAudio] = {}
        self.profile: list[int] = []
        self.calls: list[tuple] = []
        self.remove_errors: list[Exception] = []
        self._next_id = 100
        self.no_results = False

    def finish(self, message_id, doc_id, title="Song"):
        doc = InputDocument(id=doc_id, access_hash=1, file_reference=b"r")
        self.messages[message_id] = ChannelAudio(AudioStatus.READY, message_id, 1, title, doc)

    def fail(self, message_id, note="Audio not found"):
        self.messages[message_id] = ChannelAudio(AudioStatus.FAILED, message_id, 1, note=note)

    async def post_track(self, url):
        self.calls.append(("post", url))
        if self.no_results:
            return None
        self._next_id += 1
        self.messages[self._next_id] = ChannelAudio(AudioStatus.LOADING, self._next_id, 0)
        return self._next_id

    async def inspect(self, message_id):
        return self.messages.get(message_id, ChannelAudio(AudioStatus.MISSING, message_id))

    async def delete(self, message_id):
        self.calls.append(("delete", message_id))
        self.messages.pop(message_id, None)

    async def add_to_profile(self, track):
        self.calls.append(("add", track.doc_id))
        if track.doc_id not in self.profile:
            self.profile.insert(0, track.doc_id)

    async def remove_from_profile(self, track):
        if self.remove_errors:
            raise self.remove_errors.pop(0)
        self.calls.append(("remove", track.doc_id))
        if track.doc_id in self.profile:
            self.profile.remove(track.doc_id)


class SyncTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.spotify = FakeSpotify()
        self.telegram = FakeTelegram()
        self.sync = self.make_sync()

    def make_sync(self, **config):
        config = Config(**{"spotify_sp_dc": "x", "channel_id": -100, "clear_delay": 0,
                           "state_file": Path(self.dir.name) / "state.json", **config})
        return MusicSync(self.spotify, self.telegram, StateStore(config.state_file), config)

    async def show(self, track, doc_id):
        """Play `track` until it's on the profile, with the bot answering with `doc_id`."""
        self.spotify.playing = track
        await self.sync.tick()
        message_id = self.sync.state.messages[track.id]
        if self.telegram.messages[message_id].status is AudioStatus.LOADING:
            self.telegram.finish(message_id, doc_id)
        await self.sync.tick()
        self.assertEqual(self.sync.state.current.doc_id, doc_id)
        return message_id

    async def test_posts_link_then_saves_audio_once_ready(self):
        self.spotify.playing = A
        self.assertEqual(await self.sync.tick(), BOT_CHECK_INTERVAL)
        self.assertEqual(self.telegram.calls, [("post", A.url)])
        self.assertEqual(self.telegram.profile, [])

        await self.sync.tick()  # Still loading: no second post.
        self.assertEqual(len([c for c in self.telegram.calls if c[0] == "post"]), 1)

        self.telegram.finish(101, doc_id=1)
        self.assertEqual(await self.sync.tick(), 5)
        self.assertEqual(self.telegram.profile, [1])
        self.assertEqual(self.sync.state.current.track_id, A.id)

    async def test_track_change_adds_new_before_removing_old(self):
        await self.show(A, 1)
        await self.show(B, 2)
        self.assertEqual(self.telegram.calls[-2:], [("add", 2), ("remove", 1)])
        self.assertEqual(self.telegram.profile, [2])

    async def test_audio_not_found_cleans_up_and_is_not_retried(self):
        await self.show(A, 1)
        self.spotify.playing = B
        await self.sync.tick()
        self.telegram.fail(102)
        await self.sync.tick()
        self.assertIn(("delete", 102), self.telegram.calls)
        self.assertEqual(self.telegram.profile, [])  # A no longer shown either.
        self.assertIsNone(self.sync.state.current)

        posts = len(self.telegram.calls)
        for _ in range(3):
            self.assertEqual(await self.sync.tick(), 5)
        self.assertEqual(len(self.telegram.calls), posts)

    async def test_bot_without_results_is_not_retried(self):
        self.telegram.no_results = True
        self.spotify.playing = A
        await self.sync.tick()
        await self.sync.tick()
        self.assertEqual(self.telegram.calls, [("post", A.url)])

    async def test_loading_too_long_gives_up(self):
        self.spotify.playing = A
        await self.sync.tick()
        self.telegram.messages[101].age = 1000
        await self.sync.tick()
        self.assertIn(("delete", 101), self.telegram.calls)
        self.assertTrue(self.sync.state.is_unavailable(A.id))

    async def test_deleted_message_is_posted_again(self):
        self.spotify.playing = A
        await self.sync.tick()
        del self.telegram.messages[101]
        await self.sync.tick()
        await self.sync.tick()
        self.assertEqual(self.telegram.calls, [("post", A.url), ("post", A.url)])

    async def test_replay_reuses_audio_already_in_channel(self):
        await self.show(A, 1)
        self.spotify.playing = None
        await self.sync.tick()
        self.assertEqual(self.telegram.profile, [])
        self.spotify.playing = A
        await self.sync.tick()
        self.assertEqual(self.telegram.profile, [1])
        self.assertEqual(len([c for c in self.telegram.calls if c[0] == "post"]), 1)

    async def test_short_pause_keeps_track(self):
        self.sync = self.make_sync(clear_delay=30)
        await self.show(A, 1)
        self.spotify.playing = None
        await self.sync.tick()
        self.assertEqual(self.telegram.profile, [1])
        with mock.patch.object(sync_module.time, "monotonic", return_value=sync_module.time.monotonic() + 31):
            await self.sync.tick()
        self.assertEqual(self.telegram.profile, [])

    async def test_spotify_outage_keeps_track_until_stale(self):
        await self.show(A, 1)
        self.spotify.playing = SpotifyUnavailable("network error")
        await self.sync.tick()
        self.assertEqual(self.telegram.profile, [1])
        with mock.patch.object(sync_module.time, "monotonic", return_value=sync_module.time.monotonic() + 601):
            await self.sync.tick()
        self.assertEqual(self.telegram.profile, [])

    async def test_outage_logged_once_then_recovery(self):
        self.spotify.playing = SpotifyUnavailable("network error")
        with self.assertLogs("sync", "INFO") as logs:
            delays = [await self.sync.tick() for _ in range(5)]
            self.spotify.playing = None
            await self.sync.tick()
        self.assertEqual(len([line for line in logs.output if "network error" in line]), 1)
        self.assertIn("OK again", logs.output[-1])
        self.assertEqual(delays, sorted(delays))  # Backs off.

    async def test_broken_spotify_is_an_error_and_retries_slowly(self):
        self.spotify.playing = SpotifyBroken("cookie expired")
        with self.assertLogs("sync", "ERROR"):
            self.assertEqual(await self.sync.tick(), 300)

    async def test_failed_removal_is_retried_then_abandoned(self):
        await self.show(A, 1)
        self.telegram.remove_errors = [BadRequest("x")] * 10
        await self.show(B, 2)
        self.assertEqual(len(self.sync.state.to_remove), 1)
        with self.assertLogs("sync", "WARNING"):
            for _ in range(5):
                await self.sync.tick()
        self.assertEqual(self.sync.state.to_remove, [])

    async def test_removal_retry_succeeds_later(self):
        await self.show(A, 1)
        self.telegram.remove_errors = [BadRequest("x")]
        with self.assertLogs("sync", "WARNING"):
            await self.show(B, 2)
        await self.sync.tick()
        self.assertEqual(self.telegram.profile, [2])
        self.assertEqual(self.sync.state.to_remove, [])

    async def test_restart_remembers_profile_track(self):
        await self.show(A, 1)
        restarted = self.make_sync()
        self.assertEqual(restarted.state.current.doc_id, 1)
        self.sync = restarted
        await self.show(B, 2)
        self.assertEqual(self.telegram.profile, [2])

    async def test_shutdown_removes_track(self):
        await self.show(A, 1)
        await self.sync.shutdown()
        self.assertEqual(self.telegram.profile, [])
        self.assertIsNone(self.make_sync().state.current)

    async def test_shutdown_can_keep_track(self):
        self.sync = self.make_sync(clear_on_exit=False)
        await self.show(A, 1)
        await self.sync.shutdown()
        self.assertEqual(self.telegram.profile, [1])

    async def test_telegram_errors_back_off_and_keep_state(self):
        await self.show(A, 1)
        self.spotify.playing = B
        with mock.patch.object(self.telegram, "post_track", side_effect=OSError("down")), \
                self.assertLogs("sync", "WARNING"):
            first = await self.sync.tick()
            second = await self.sync.tick()
        self.assertLess(first, second)
        self.assertEqual(self.telegram.profile, [1])


if __name__ == "__main__":
    unittest.main()
