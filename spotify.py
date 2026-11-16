"""Follow what a Spotify account is playing, using the web player's session cookie.

We register a hidden Connect device that can't play anything. Spotify answers
the registration with the account's full Connect state (the data the web player
uses to show "Listening on <device>"), then pushes every change over the dealer
websocket. It never takes over or changes playback.

This is an unofficial protocol, and there's deliberately no polling fallback:
if Spotify changes it, `SpotifyBroken` is raised so the logs say so right away.
Anything else we can't be sure about raises `SpotifyUnavailable` ("we don't
know") instead of being read as "stopped". Error messages never include URLs
or response bodies: they can carry the access token.
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import re
import struct
import time
import uuid
from dataclasses import dataclass
from urllib.parse import urljoin, urlparse

import requests
from websockets.asyncio.client import ClientConnection, connect
from websockets.exceptions import ConnectionClosed, WebSocketException

log = logging.getLogger("spotify")

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/144.0.0.0 Safari/537.36"
)
TRACK_URI = re.compile(r"spotify:track:([A-Za-z0-9]{22})")
# Keys that only appear in a genuine Connect cluster.
CLUSTER_KEYS = {"active_device_id", "player_state", "devices", "timestamp", "server_timestamp_ms"}
CLUSTER_PUSH_URI = "hm://connect-state/v1/cluster"
# The dealer expects an application-level ping about this often.
APP_PING_INTERVAL = 30.0


class SpotifyUnavailable(RuntimeError):
    """The playback state is unknown right now (network, server), not "stopped"."""


class SpotifyBroken(SpotifyUnavailable):
    """Won't fix itself: the cookie expired or Spotify changed its protocol."""


@dataclass(frozen=True)
class Track:
    id: str

    @property
    def url(self) -> str:
        return f"https://open.spotify.com/track/{self.id}"


def parse_playback(cluster: object) -> Track | None:
    """Return the track playing on the account, or None if nothing is playing.

    Spotify serialises protobuf to JSON, which drops empty and false fields:
    with no active device `active_device_id` is missing entirely, and a
    missing `is_paused` means False.
    """
    if not isinstance(cluster, dict) or not CLUSTER_KEYS & cluster.keys():
        raise SpotifyBroken("Connect state has an unknown format")
    if not cluster.get("active_device_id"):
        return None  # No device is active on the account.

    state = cluster.get("player_state")
    if not isinstance(state, dict):
        raise SpotifyBroken("Connect state has an active device but no player state")
    playing = state.get("is_playing", False)
    paused = state.get("is_paused", False)
    if not isinstance(playing, bool) or not isinstance(paused, bool):
        raise SpotifyBroken("Connect player state has an unknown format")
    if not playing or paused:
        return None

    track = state.get("track")
    uri = track.get("uri") if isinstance(track, dict) else None
    if not isinstance(uri, str):
        raise SpotifyBroken("Connect is playing something but didn't say what")
    match = TRACK_URI.fullmatch(uri)
    # Ads, podcast episodes and local files aren't tracks @nowtrackbot can find.
    return Track(match[1]) if match else None


def cluster_from_push(message: object) -> object | None:
    """The Connect state in a dealer message, or None if it's some other message.

    A state update looks like
    {"type": "message", "uri": "hm://connect-state/v1/cluster",
     "payloads": [{"cluster": {...}, "update_reason": "...", ...}]}
    """
    if not isinstance(message, dict) or message.get("uri") != CLUSTER_PUSH_URI:
        return None
    payloads = message.get("payloads")
    if not (isinstance(payloads, list) and len(payloads) == 1
            and isinstance(payloads[0], dict) and "cluster" in payloads[0]):
        raise SpotifyBroken("Connect sent a state update in an unknown format")
    return payloads[0]["cluster"]


def totp(key: bytes, seconds: float) -> str:
    """RFC 6238 six-digit code, which the web player sends when requesting a token."""
    digest = hmac.new(key, struct.pack(">Q", int(seconds) // 30), hashlib.sha1).digest()
    offset = digest[-1] & 15
    value = int.from_bytes(digest[offset:offset + 4], "big") & 0x7FFFFFFF
    return f"{value % 1_000_000:06d}"


def extract_totp_secret(script: str) -> tuple[int, str] | None:
    """Find the newest `{secret: "...", version: N}` pair in a web player script."""
    found = []
    for raw, version in re.findall(r'secret:\s*"((?:[^"\\]|\\.)*)"\s*,\s*version:\s*(\d+)', script):
        # JavaScript allows \xNN escapes; JSON doesn't.
        raw = re.sub(r"\\x([0-9a-fA-F]{2})", r"\\u00\1", raw)
        found.append((int(version), json.loads(f'"{raw}"')))
    return max(found, default=None)


def totp_key(secret: str) -> bytes:
    if secret.isdigit():
        return secret.encode()
    return "".join(str(ord(c) ^ ((i % 33) + 9)) for i, c in enumerate(secret)).encode()


def describe_error(exc: BaseException) -> str:
    """A short reason that's safe to log (no URLs, tokens or bodies)."""
    if isinstance(exc, (asyncio.TimeoutError, requests.Timeout)):
        return "request timed out"
    if isinstance(exc, (ValueError, KeyError)):  # Includes requests' JSON errors.
        return "unexpected response from Spotify"
    if isinstance(exc, (requests.ConnectionError, OSError)):
        return "network error"
    if isinstance(exc, WebSocketException):
        return f"dealer connection failed ({type(exc).__name__})"
    return f"unexpected error ({type(exc).__name__})"


class SpotifyConnect:
    def __init__(self, sp_dc: str):
        if not sp_dc.strip():
            raise ValueError("sp_dc cookie is empty")
        self._cookie = sp_dc.strip()
        self._http = requests.Session()
        self._http.headers["User-Agent"] = USER_AGENT
        self._device_id = "hobs_" + uuid.uuid4().hex
        self._totp: tuple[int, str] | None = None
        self._token: str | None = None
        self._token_refresh_at = 0.0  # time.monotonic()
        self._ws: ClientConnection | None = None
        self._connection_id: str | None = None
        self._listener: asyncio.Task | None = None
        self._listener_error: SpotifyUnavailable | None = None
        self._track: Track | None = None
        self._changed = asyncio.Event()
        self._lock = asyncio.Lock()

    async def currently_playing(self) -> Track | None:
        """The playing track, None when nothing plays; raises SpotifyUnavailable if unknown.

        Answers from the last pushed state; connects first if needed.
        """
        async with self._lock:
            self._changed.clear()
            if self._listener is not None and self._listener.done():
                error, self._listener_error = self._listener_error, None
                await self._disconnect()
                if error is not None:
                    raise error
                log.info("Spotify closed the connection; reconnecting")
            if self._ws is not None and time.monotonic() >= self._token_refresh_at:
                log.debug("access token is about to expire; reconnecting")
                await self._disconnect()
            if self._ws is None:
                try:
                    await self._connect()
                except asyncio.CancelledError:
                    await self._disconnect()
                    raise
                except SpotifyUnavailable:
                    await self._disconnect()
                    raise
                except Exception as exc:
                    await self._disconnect()
                    raise SpotifyUnavailable(describe_error(exc)) from None
            return self._track

    async def wait_for_change(self, timeout: float) -> None:
        """Return when Spotify pushes a change (or the connection drops), or after `timeout`."""
        try:
            await asyncio.wait_for(self._changed.wait(), timeout)
        except asyncio.TimeoutError:
            pass

    async def close(self) -> None:
        async with self._lock:
            await self._disconnect()
        self._http.close()

    # -- connection -------------------------------------------------------

    async def _connect(self) -> None:
        if self._token is None or time.monotonic() >= self._token_refresh_at:
            await asyncio.to_thread(self._refresh_token)
        self._ws = await connect(
            "wss://dealer.spotify.com/?access_token=" + self._token,
            open_timeout=20, close_timeout=3, ping_interval=20, ping_timeout=10,
            max_size=8 * 1024 * 1024, proxy=None,
        )
        hello = json.loads(await asyncio.wait_for(self._ws.recv(), 15))
        self._connection_id = (hello.get("headers") or {}).get("Spotify-Connection-Id")
        if not self._connection_id:
            raise SpotifyBroken("dealer didn't return a connection ID")
        # Registering returns the current state; changes are pushed from now on.
        self._track = parse_playback(await asyncio.to_thread(self._register_device))
        self._listener = asyncio.create_task(self._listen(self._ws))
        log.debug("connected, listening for playback changes")

    async def _listen(self, ws: ClientConnection) -> None:
        next_ping = time.monotonic() + APP_PING_INTERVAL
        try:
            while True:
                try:
                    raw = await asyncio.wait_for(ws.recv(), max(0.0, next_ping - time.monotonic()))
                except asyncio.TimeoutError:
                    await ws.send('{"type":"ping"}')
                    next_ping = time.monotonic() + APP_PING_INTERVAL
                    continue
                cluster = cluster_from_push(json.loads(raw))
                if cluster is None:
                    continue
                track = parse_playback(cluster)
                if track != self._track:
                    self._track = track
                    self._changed.set()
        except ConnectionClosed as exc:
            log.debug("dealer closed the connection (code %s)", exc.rcvd.code if exc.rcvd else "none")
        except SpotifyUnavailable as exc:
            self._listener_error = exc
        except Exception as exc:
            self._listener_error = SpotifyUnavailable(describe_error(exc))
        finally:
            self._changed.set()  # Wake the sync loop so it notices right away.

    async def _disconnect(self) -> None:
        listener, self._listener = self._listener, None
        ws, self._ws, self._connection_id = self._ws, None, None
        if listener is not None and not listener.done():
            listener.cancel()
        if ws is not None:
            try:
                await ws.close()
            except Exception:
                pass

    # -- blocking HTTP, run in a worker thread ----------------------------

    def _register_device(self) -> object:
        response = self._http.put(
            f"https://spclient.wg.spotify.com/connect-state/v1/devices/{self._device_id}",
            headers={"Authorization": f"Bearer {self._token}",
                     "X-Spotify-Connection-Id": self._connection_id},
            json={"member_type": "CONNECT_STATE", "device": {"device_info": {"capabilities": {
                "can_be_player": False, "hidden": True, "needs_full_player_state": True}}}},
            timeout=20, allow_redirects=False,
        )
        if response.status_code == 401:
            self._token = None  # Expired early; log in again next time.
        self._require_ok(response, "Connect registration")
        try:
            return response.json()
        except ValueError:
            raise SpotifyBroken("Connect registration didn't return JSON") from None

    def _refresh_token(self) -> None:
        if self._totp is None:
            self._totp = self._find_totp_secret()
        version, secret = self._totp
        key = totp_key(secret)

        response = self._require_ok(
            self._http.get("https://open.spotify.com/api/server-time", timeout=15), "Server time")
        server_time = response.json()["serverTime"]

        # Only open.spotify.com ever gets the cookie, never the CDN hosts.
        response = self._http.get(
            "https://open.spotify.com/api/token",
            params={"reason": "init", "productType": "web-player",
                    "totp": totp(key, time.time()), "totpServer": totp(key, server_time),
                    "totpVer": version},
            cookies={"sp_dc": self._cookie}, timeout=20, allow_redirects=False,
        )
        if response.status_code in (400, 401, 403):
            self._totp = None  # The web player may have rotated its secret.
        data = self._require_ok(response, "Token").json()
        if data.get("isAnonymous") is not False or not data.get("accessToken"):
            raise SpotifyBroken("sp_dc cookie is expired or signed out; put a fresh one in .env")

        ttl = float(data.get("accessTokenExpirationTimestampMs", 0)) / 1000 - time.time()
        if ttl <= 5:
            # Spotify sometimes hands out a cached token that's about to expire;
            # a new one is issued as soon as it does.
            raise SpotifyUnavailable("Spotify returned an expired token, waiting for a new one")
        self._token = data["accessToken"]
        # Use most of the lifetime, but leave a margin for long-lived tokens.
        self._token_refresh_at = time.monotonic() + ttl - (60 if ttl > 120 else 5)
        log.debug("got access token, valid for %ds", ttl)

    def _find_totp_secret(self) -> tuple[int, str]:
        page = self._require_ok(self._http.get("https://open.spotify.com/", timeout=20), "Web player")
        urls = set(re.findall(
            r'(https://[^/"\s]+/cdn/build/web-player/[^"\s>]+\.js|/cdn/build/web-player/[^"\s>]+\.js)',
            page.text))
        candidates = []
        for url in sorted(urls)[:10]:
            url = urljoin("https://open.spotifycdn.com", url)
            if urlparse(url).hostname not in ("open.spotifycdn.com", "open.spotify.com"):
                continue
            script = self._require_ok(self._http.get(url, timeout=20), "Web player script")
            if found := extract_totp_secret(script.text):
                candidates.append(found)
        if not candidates:
            raise SpotifyBroken("couldn't find the web player's TOTP secret; Spotify changed its login")
        log.debug("using TOTP secret version %d", max(candidates)[0])
        return max(candidates)

    @staticmethod
    def _require_ok(response: requests.Response, what: str) -> requests.Response:
        if response.status_code != 200:
            raise SpotifyUnavailable(f"{what} request failed: HTTP {response.status_code}")
        return response
