"""What the script has put on the profile, saved to disk so a restart can clean it up."""
from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

log = logging.getLogger("state")

# Channel messages remembered per track, so replaying a song reuses its audio
# instead of asking the bot again.
MAX_REMEMBERED_MESSAGES = 1000


@dataclass
class ProfileTrack:
    """A song this script saved to the profile, plus how to find its audio again."""
    track_id: str
    title: str
    message_id: int
    doc_id: int
    access_hash: int
    file_reference: bytes
    removal_failures: int = 0

    def to_json(self) -> dict:
        data = asdict(self)
        data["file_reference"] = self.file_reference.hex()
        return data

    @classmethod
    def from_json(cls, data: dict) -> ProfileTrack:
        return cls(**{**data, "file_reference": bytes.fromhex(data["file_reference"])})


@dataclass
class SyncState:
    # The song currently on the profile, if we put it there.
    current: ProfileTrack | None = None
    # Songs we replaced but haven't managed to remove yet.
    to_remove: list[ProfileTrack] = field(default_factory=list)
    # Spotify track ID -> channel message with its audio (oldest first).
    messages: dict[str, int] = field(default_factory=dict)
    # Spotify track ID -> unix time after which we may ask the bot again.
    unavailable_until: dict[str, float] = field(default_factory=dict)

    def remember_message(self, track_id: str, message_id: int) -> None:
        self.messages.pop(track_id, None)
        self.messages[track_id] = message_id
        while len(self.messages) > MAX_REMEMBERED_MESSAGES:
            del self.messages[next(iter(self.messages))]

    def is_unavailable(self, track_id: str, now: float | None = None) -> bool:
        until = self.unavailable_until.get(track_id)
        if until is None:
            return False
        if until > (now or time.time()):
            return True
        del self.unavailable_until[track_id]
        return False


class StateStore:
    def __init__(self, path: Path):
        self.path = Path(path)

    def load(self) -> SyncState:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            now = time.time()
            return SyncState(
                current=ProfileTrack.from_json(data["current"]) if data.get("current") else None,
                to_remove=[ProfileTrack.from_json(item) for item in data.get("to_remove", [])],
                messages={str(k): int(v) for k, v in data.get("messages", {}).items()},
                unavailable_until={str(k): float(v) for k, v in data.get("unavailable_until", {}).items()
                                   if float(v) > now},
            )
        except FileNotFoundError:
            return SyncState()
        except (OSError, ValueError, KeyError, TypeError) as exc:
            log.warning("Ignoring unreadable %s (%s); starting fresh", self.path.name, exc)
            return SyncState()

    def save(self, state: SyncState) -> None:
        data = {
            "current": state.current.to_json() if state.current else None,
            "to_remove": [item.to_json() for item in state.to_remove],
            "messages": state.messages,
            "unavailable_until": state.unavailable_until,
        }
        tmp = self.path.with_suffix(".tmp")
        try:
            tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
            os.replace(tmp, self.path)  # Atomic: a crash never leaves half a file.
        except OSError as exc:
            log.warning("Couldn't save %s: %s", self.path.name, exc)
