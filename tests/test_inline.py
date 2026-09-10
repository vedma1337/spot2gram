import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
import main


class InlineTests(unittest.IsolatedAsyncioTestCase):
    async def test_missing_send_response_never_uses_unrelated_channel_message(self):
        app = SimpleNamespace(
            resolve_peer=AsyncMock(return_value=None),
            invoke=AsyncMock(return_value=SimpleNamespace(
                results=[SimpleNamespace(id='one')], query_id=123)),
            send_inline_bot_result=AsyncMock(return_value=None))
        async def history(*args, **kwargs):
            yield SimpleNamespace(id=999, audio='unrelated audio')
        app.get_chat_history = history
        self.assertIsNone(await main.send_inline_top_result_and_get_message(
            app, 'https://open.spotify.com/track/4cOdK2wGLETKBW3PvgPWqT'))


if __name__ == '__main__':
    unittest.main()
