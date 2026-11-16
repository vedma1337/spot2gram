"""Telegram side: get a track's audio from @nowtrackbot and manage profile music."""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from enum import Enum

from pyrogram import Client
from pyrogram.errors import (FileReferenceEmpty, FileReferenceExpired, FileReferenceInvalid,
                             RPCError)
from pyrogram.file_id import FileId
from pyrogram.raw import functions
from pyrogram.raw.types import InputDocument, InputUserSelf

from state import ProfileTrack

log = logging.getLogger("telegram")

FILE_REFERENCE_ERRORS = (FileReferenceEmpty, FileReferenceExpired, FileReferenceInvalid)
# @nowtrackbot's messages:
#   "⏳ Downloading: Title — Artist…"              while fetching
#   "Artist — Title\n\nAudio not found"            when it gives up
# plus the Russian versions ("⏳ Загружаю: …", "Аудио не найдено").
LOADING_PREFIX = "⏳"
NOT_FOUND = ("audio not found", "аудио не найдено")


class SetupError(RuntimeError):
    """Telegram is reachable but not set up the way the script needs."""


class AudioStatus(Enum):
    READY = "ready"      # The message has the audio.
    LOADING = "loading"  # The bot is still fetching it.
    FAILED = "failed"    # The bot gave up ("Audio not found").
    MISSING = "missing"  # The message was deleted.


@dataclass
class ChannelAudio:
    status: AudioStatus
    message_id: int
    age: float = 0.0  # Seconds since the message was posted.
    title: str = ""
    document: InputDocument | None = None
    note: str = ""    # What the bot wrote, for logs.

    def to_profile_track(self, track_id: str) -> ProfileTrack:
        doc = self.document
        return ProfileTrack(track_id=track_id, title=self.title, message_id=self.message_id,
                            doc_id=doc.id, access_hash=doc.access_hash,
                            file_reference=doc.file_reference)


def _title(audio) -> str:
    parts = [getattr(audio, "performer", None), getattr(audio, "title", None)]
    return " — ".join(p for p in parts if p) or getattr(audio, "file_name", None) or "unknown track"


def classify(message, message_id: int) -> ChannelAudio:
    """Work out whether @nowtrackbot's message has the audio yet."""
    if message is None or getattr(message, "empty", False):
        return ChannelAudio(AudioStatus.MISSING, message_id)
    date = getattr(message, "date", None)
    age = max(0.0, time.time() - date.timestamp()) if date else 0.0

    text = str(getattr(message, "text", None) or getattr(message, "caption", None) or "").strip()
    if text.startswith(LOADING_PREFIX):
        return ChannelAudio(AudioStatus.LOADING, message_id, age)

    audio = getattr(message, "audio", None)
    if audio:
        decoded = FileId.decode(audio.file_id)
        document = InputDocument(id=decoded.media_id, access_hash=decoded.access_hash,
                                 file_reference=decoded.file_reference)
        return ChannelAudio(AudioStatus.READY, message_id, age, _title(audio), document)

    # Only the last line: the first is "Artist — Title", which could say anything.
    last_line = text.splitlines()[-1].strip() if "\n" in text else ""
    if last_line.lower() in NOT_FOUND:
        return ChannelAudio(AudioStatus.FAILED, message_id, age, note=last_line)
    return ChannelAudio(AudioStatus.LOADING, message_id, age)


class TelegramMusic:
    def __init__(self, app: Client, channel_id: int, bot_username: str):
        self.app = app
        self.channel_id = channel_id
        self.bot_username = bot_username.lstrip("@")

    async def check_setup(self) -> None:
        """Fail early, with a clear message, if the channel or bot can't be used."""
        me = await self.app.get_me()
        channel = await self._find_channel()
        try:
            await self.app.resolve_peer(self.bot_username)
        except (RPCError, KeyError, ValueError) as exc:
            raise SetupError(f"Can't find @{self.bot_username}: {exc}") from None
        log.info("Signed in as %s; posting to %r", me.username and "@" + me.username or me.first_name,
                 channel.title)

    async def _find_channel(self):
        try:
            return await self.app.get_chat(self.channel_id)
        except (RPCError, KeyError, ValueError):
            pass
        # A fresh session doesn't know the channel yet; dialogs fill the cache.
        async for dialog in self.app.get_dialogs():
            if dialog.chat.id == self.channel_id:
                return dialog.chat
        raise SetupError(f"Channel {self.channel_id} not found. Check CHANNEL_ID and that this "
                         f"account is a member who can post there.")

    async def post_track(self, url: str) -> int | None:
        """Post the bot's inline result for `url`; None if the bot has no result."""
        results = await self.app.invoke(functions.messages.GetInlineBotResults(
            bot=await self.app.resolve_peer(self.bot_username),
            peer=await self.app.resolve_peer(self.channel_id),
            query=url, offset="",
        ))
        if not results.results:
            return None
        sent = await self.app.send_inline_bot_result(self.channel_id, results.query_id,
                                                     results.results[0].id)
        if sent is None:
            # Guessing from channel history could pick an unrelated message.
            raise RuntimeError("Telegram didn't return the posted message")
        return sent.id

    async def inspect(self, message_id: int) -> ChannelAudio:
        message = await self.app.get_messages(self.channel_id, message_id)
        return classify(message, message_id)

    async def delete(self, message_id: int) -> None:
        try:
            await self.app.delete_messages(self.channel_id, message_id)
        except RPCError as exc:
            log.debug("Couldn't delete message %d: %s", message_id, exc)

    async def add_to_profile(self, track: ProfileTrack) -> None:
        await self._save_music(self._document(track))

    async def remove_from_profile(self, track: ProfileTrack) -> None:
        try:
            await self._save_music(self._document(track), unsave=True)
            return
        except FILE_REFERENCE_ERRORS:
            log.debug("File reference for %s expired, refreshing", track.title)
        document = await self._refreshed_document(track)
        if document is None:
            log.debug("%s is no longer on the profile", track.title)
            return
        await self._save_music(document, unsave=True)

    async def _refreshed_document(self, track: ProfileTrack) -> InputDocument | None:
        audio = await self.inspect(track.message_id)
        if audio.status is AudioStatus.READY and audio.document.id == track.doc_id:
            return audio.document
        # The channel message is gone; ask the profile itself.
        result = await self.app.invoke(functions.users.GetSavedMusicByID(
            id=InputUserSelf(),
            documents=[InputDocument(id=track.doc_id, access_hash=track.access_hash, file_reference=b"")],
        ))
        for doc in getattr(result, "documents", None) or []:
            if doc.id == track.doc_id:
                return InputDocument(id=doc.id, access_hash=doc.access_hash,
                                     file_reference=doc.file_reference)
        return None

    async def _save_music(self, document: InputDocument, unsave: bool = False) -> None:
        ok = await self.app.invoke(functions.account.SaveMusic(id=document, unsave=unsave or None))
        if not ok:
            raise RuntimeError("Telegram refused to " + ("remove" if unsave else "save") + " profile music")

    @staticmethod
    def _document(track: ProfileTrack) -> InputDocument:
        return InputDocument(id=track.doc_id, access_hash=track.access_hash,
                             file_reference=track.file_reference)
