import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
import main
from spotify import SpotifyUnavailable

TRACK = ('2sY7k8ul4ELfW5sTJ4v7tM', 'https://open.spotify.com/track/2sY7k8ul4ELfW5sTJ4v7tM')


class SyncTests(unittest.IsolatedAsyncioTestCase):
    async def test_network_error_preserves_saved_music(self):
        state = main.SyncState(track_id='old', document='old-document')
        provider = SimpleNamespace(currently_playing=AsyncMock(side_effect=SpotifyUnavailable('offline')))
        with self.assertRaises(SpotifyUnavailable):
            await main.sync_once(None, provider, state)
        self.assertEqual(state.document, 'old-document')
        self.assertEqual(state.track_id, 'old')

    async def test_failed_unsave_keeps_state_for_retry(self):
        state = main.SyncState(track_id='old', document='old-document')
        provider = SimpleNamespace(currently_playing=AsyncMock(return_value=None))
        with patch.object(main, 'unsave_music', AsyncMock(return_value=False)):
            await main.sync_once(None, provider, state)
        self.assertEqual(state.document, 'old-document')

    async def test_track_changed_while_downloading_does_not_replace_profile(self):
        state = main.SyncState(track_id='old', document='old-document')
        provider = SimpleNamespace(currently_playing=AsyncMock(side_effect=[TRACK, None]))
        with patch.object(main, 'send_inline_top_result_and_get_message', AsyncMock(return_value=SimpleNamespace(id=1))), \
             patch.object(main, 'wait_for_audio_ready', AsyncMock(return_value=SimpleNamespace(audio=True))), \
             patch.object(main, 'build_input_document_from_message_audio', return_value='new-document'):
            await main.sync_once(None, provider, state)
        self.assertEqual(state.document, 'old-document')

    async def test_save_retry_reuses_message_instead_of_posting_again(self):
        state = main.SyncState()
        provider = SimpleNamespace(currently_playing=AsyncMock(return_value=TRACK))
        sends = []
        async def send(*args):
            sends.append(args)
            return SimpleNamespace(id=1)
        with patch.object(main, 'send_inline_top_result_and_get_message', send), \
             patch.object(main, 'wait_for_audio_ready', AsyncMock(return_value=SimpleNamespace(audio=True))), \
             patch.object(main, 'build_input_document_from_message_audio', return_value='new-document'), \
             patch.object(main, 'save_music', AsyncMock(side_effect=[False, True])):
            await main.sync_once(None, provider, state)
            self.assertIsNone(state.track_id)
            await main.sync_once(None, provider, state)
        self.assertEqual(len(sends), 1)
        self.assertEqual(state.document, 'new-document')
        self.assertEqual(state.track_id, TRACK[0])
