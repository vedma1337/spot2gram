import unittest
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

from pyrogram.file_id import FileId, FileType

from telegram_music import AudioStatus, TelegramMusic, classify

FILE_ID = FileId(file_type=FileType.AUDIO, dc_id=2, media_id=11, access_hash=22,
                 file_reference=b"ref").encode()


def message(text=None, audio=None, age=0):
    return SimpleNamespace(empty=False, text=text, caption=None, audio=audio,
                           date=datetime.now() - timedelta(seconds=age))


def audio():
    return SimpleNamespace(file_id=FILE_ID, performer="k1n1ch, itzice, 2late", title="мориарти")


class ClassifyTests(unittest.TestCase):
    def test_ready_audio(self):
        result = classify(message(audio=audio()), 5)
        self.assertIs(result.status, AudioStatus.READY)
        self.assertEqual((result.document.id, result.document.access_hash), (11, 22))
        self.assertEqual(result.title, "k1n1ch, itzice, 2late — мориарти")

    def test_downloading(self):
        for text in ["⏳ Загружаю: мориарти — k1n1ch, itzice, 2late…",
                     "⏳ Downloading: мориарти — k1n1ch, itzice, 2late…",
                     "⏳ Downloading: Audio Not Found — Band…"]:
            with self.subTest(text=text):
                self.assertIs(classify(message(text), 5).status, AudioStatus.LOADING)

    def test_audio_not_found(self):
        for note in ["Audio not found", "Аудио не найдено"]:
            with self.subTest(note=note):
                result = classify(message(f"k1n1ch, itzice, 2late — мориарти\n\n{note}"), 5)
                self.assertIs(result.status, AudioStatus.FAILED)
                self.assertEqual(result.note, note)

    def test_title_alone_is_not_a_failure_even_if_it_says_so(self):
        result = classify(message("Band — Audio not found"), 5)
        self.assertIs(result.status, AudioStatus.LOADING)

    def test_deleted_message(self):
        self.assertIs(classify(None, 5).status, AudioStatus.MISSING)
        self.assertIs(classify(SimpleNamespace(empty=True), 5).status, AudioStatus.MISSING)

    def test_age(self):
        self.assertGreaterEqual(classify(message("x", age=120), 5).age, 119)


class PostTrackTests(unittest.IsolatedAsyncioTestCase):
    def app(self, results, sent):
        return SimpleNamespace(
            resolve_peer=AsyncMock(return_value=None),
            invoke=AsyncMock(return_value=SimpleNamespace(results=results, query_id=1)),
            send_inline_bot_result=AsyncMock(return_value=sent))

    async def test_no_inline_results(self):
        app = self.app([], None)
        self.assertIsNone(await TelegramMusic(app, -100, "nowtrackbot").post_track("url"))
        app.send_inline_bot_result.assert_not_called()

    async def test_missing_sent_message_is_an_error_not_a_guess(self):
        app = self.app([SimpleNamespace(id="one")], None)
        with self.assertRaises(RuntimeError):
            await TelegramMusic(app, -100, "nowtrackbot").post_track("url")

    async def test_returns_message_id(self):
        app = self.app([SimpleNamespace(id="one")], SimpleNamespace(id=42))
        self.assertEqual(await TelegramMusic(app, -100, "nowtrackbot").post_track("url"), 42)


if __name__ == "__main__":
    unittest.main()
