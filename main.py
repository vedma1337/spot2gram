from pyrogram import Client
from pyrogram.raw.types import InputDocument
from pyrogram.raw import functions
from pyrogram.file_id import FileId
import asyncio
import os
from dataclasses import dataclass
from typing import Optional
from spotify import SpotifyConnect, SpotifyUnavailable
from dotenv import load_dotenv
load_dotenv()


TARGET_CHANNEL_ID = int(os.getenv("CHANNEL_ID", "0"))
POLL_INTERVAL_SECONDS = max(1.0, float(os.getenv("POLL_INTERVAL_SECONDS", "5")))


async def send_inline_top_result_and_get_message(app: Client, query_text: str):
    try:
        results = await app.invoke(functions.messages.GetInlineBotResults(
            bot=await app.resolve_peer("nowtrackbot"),
            peer=await app.resolve_peer(TARGET_CHANNEL_ID),
            query=query_text,
            offset="",
        ))
        if not results or not results.results:
            print(f"[tg] no inline results for: {query_text}")
            return None
        top = results.results[0]
        sent = await app.send_inline_bot_result(TARGET_CHANNEL_ID, results.query_id, top.id)
        if not sent:
            print("[tg] Telegram returned no sent message; cannot safely identify the audio")
        return sent
    except Exception as exc:
        print(f"[tg] inline send error: {exc}")
        return None


def build_input_document_from_message_audio(message) -> Optional[InputDocument]:
    try:
        if not message or not getattr(message, "audio", None):
            return None
        fileid = message.audio.file_id
        decoded = FileId.decode(fileid)
        return InputDocument(id=decoded.media_id, access_hash=decoded.access_hash, file_reference=decoded.file_reference)
    except Exception as exc:
        print(f"[tg] fileid decode error: {exc}")
        return None


async def save_music(app: Client, input_document: InputDocument) -> bool:
    try:
        r = await app.invoke(functions.account.SaveMusic(id=input_document))
        return bool(r)
    except Exception as exc:
        print(f"[tg] save music error: {exc}")
        return False


async def unsave_music(app: Client, input_document: InputDocument) -> bool:
    try:
        r = await app.invoke(functions.account.SaveMusic(id=input_document, unsave=True))
        return bool(r)
    except Exception as exc:
        print(f"[tg] unsave music error: {exc}")
        return False


def has_downloading_button(message) -> bool:
    try:
        markup = getattr(message, "reply_markup", None)
        keyboard = getattr(markup, "inline_keyboard", None)
        if not keyboard:
            return False
        for row in keyboard:
            for btn in row:
                text = getattr(btn, "text", "") or ""
                if "downloading" in text.lower():
                    return True
        return False
    except Exception:
        return False


async def wait_for_audio_ready(app: Client, chat_id: int, message_id: int, poll_seconds: float = 1.0):
    try:
        attempts = 20
        for _ in range(max(attempts, 1)):
            msg = await app.get_messages(chat_id, message_id)
            if getattr(msg, "audio", None) and not has_downloading_button(msg):
                return msg
            await asyncio.sleep(poll_seconds)
        return await app.get_messages(chat_id, message_id)
    except Exception as exc:
        print(f"[tg] wait_for_audio_ready error: {exc}")
        return None

@dataclass
class SyncState:
    track_id: Optional[str] = None
    document: Optional[InputDocument] = None
    pending_track_id: Optional[str] = None
    pending_message: object = None


async def sync_once(app, spotify, state: SyncState):
    now_playing = await spotify.currently_playing()
    if not now_playing:
        state.pending_track_id = None
        state.pending_message = None
        if state.document is not None:
            if not await unsave_music(app, state.document):
                return
            print("[state] music stopped, cleaned up")
        state.track_id = None
        state.document = None
        return

    track_id, track_url = now_playing
    if track_id == state.track_id:
        return
    if state.pending_track_id != track_id:
        state.pending_track_id = track_id
        state.pending_message = None
    if state.pending_message is None:
        state.pending_message = await send_inline_top_result_and_get_message(app, track_url)
    if state.pending_message is None:
        return
    message = await wait_for_audio_ready(app, TARGET_CHANNEL_ID, state.pending_message.id)
    document = build_input_document_from_message_audio(message)
    if document is None:
        print("[tg] audio not ready; will retry the same message")
        return
    # The bot may take seconds to download. Don't save a song that has stopped
    # or changed in the meantime. An unavailable source raises without cleanup.
    if await spotify.currently_playing() != now_playing:
        return
    if state.document is not None:
        if not await unsave_music(app, state.document):
            return
        state.document = None
        state.track_id = None
    if await save_music(app, document):
        state.track_id = track_id
        state.document = document
        state.pending_message = None
        state.pending_track_id = None
        print(f"[state] saved to profile: {track_url}")


async def main():
    if not TARGET_CHANNEL_ID:
        raise ValueError("Set CHANNEL_ID in .env")
    spotify = SpotifyConnect(os.getenv("SPOTIFY_SP_DC", ""))
    state = SyncState()
    try:
        async with Client("music_sync", fetch_topics=False, skip_updates=True, no_updates=True, api_id=2040, api_hash="b18441a1ff607e10a989891a5462e627", app_version="6.1.3 x64", device_model="Z690-c/ac", system_version="Windows 10 x64", lang_code="ru", system_lang_code="ru") as app:
            print("starting music sync (Spotify Connect -> @nowtrackbot)")
            while True:
                try:
                    await sync_once(app, spotify, state)
                except SpotifyUnavailable as exc:
                    print(f"[spotify] {exc}; preserving current profile music")
                    await asyncio.sleep(max(30, POLL_INTERVAL_SECONDS))
                    continue
                except Exception as exc:
                    print(f"[runner] sync error: {type(exc).__name__}")
                    await asyncio.sleep(10)
                    continue
                await asyncio.sleep(POLL_INTERVAL_SECONDS)
    finally:
        await spotify.close()


if __name__ == "__main__":
    asyncio.run(main())
