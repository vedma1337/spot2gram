"""Observe account-wide Spotify Connect state using the user's web session.

The hidden observer cannot play audio and never transfers playback. Spotify's
unofficial web protocol may change; failures are never reported as a pause.
"""
import asyncio
import hashlib
import hmac
import json
import re
import struct
import time
import uuid
from urllib.parse import urljoin, urlparse

import requests
from websockets.asyncio.client import connect


class SpotifyUnavailable(RuntimeError):
    """The playback state is unknown, rather than stopped."""


def totp(key: bytes, seconds: float) -> str:
    digest = hmac.new(key, struct.pack('>Q', int(seconds) // 30), hashlib.sha1).digest()
    offset = digest[-1] & 15
    value = int.from_bytes(digest[offset:offset + 4], 'big') & 0x7fffffff
    return f'{value % 1000000:06d}'


def extract_totp_secret(script: str):
    matches = []
    for raw, version in re.findall(r'secret:\s*"((?:[^"\\]|\\.)*)"\s*,\s*version:\s*(\d+)', script):
        # JavaScript also permits \xNN escapes, which JSON doesn't accept.
        raw = re.sub(r'\\x([0-9a-fA-F]{2})', r'\\u00\1', raw)
        matches.append((int(version), json.loads('"' + raw + '"')))
    return max(matches, default=None)


def parse_playback(payload):
    if not isinstance(payload, dict) or 'active_device_id' not in payload:
        raise SpotifyUnavailable('Connect returned an incomplete state')
    if not payload['active_device_id']:
        return None
    state = payload.get('player_state')
    if not isinstance(state, dict) or any(type(state.get(k)) is not bool for k in ('is_playing', 'is_paused')):
        raise SpotifyUnavailable('Connect returned an incomplete player state')
    if not state['is_playing'] or state['is_paused']:
        return None
    track = state.get('track')
    if not isinstance(track, dict) or not isinstance(track.get('uri'), str):
        raise SpotifyUnavailable('Connect did not include the playing track')
    match = re.fullmatch(r'spotify:track:([A-Za-z0-9]{22})', track['uri'])
    if not match:  # Advertisement, podcast, or local file.
        return None
    track_id = match[1]
    return track_id, f'https://open.spotify.com/track/{track_id}'


class SpotifyConnect:
    def __init__(self, sp_dc: str):
        if not sp_dc.strip():
            raise ValueError('Set SPOTIFY_SP_DC in .env using your Spotify web cookie')
        self._cookie = sp_dc.strip()
        self._token = None
        self._expires = 0
        self._secret = None
        self._ws = None
        self._connection_id = None
        self._device_id = 'hobs_' + uuid.uuid4().hex
        self._lock = asyncio.Lock()

    @staticmethod
    def _checked(response, action):
        if response.status_code != 200:
            # Never include response bodies or URLs: they may contain credentials.
            raise SpotifyUnavailable(f'{action}: HTTP {response.status_code}')
        return response

    def _login(self):
        with requests.Session() as session:
            session.headers['User-Agent'] = ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
                'AppleWebKit/537.36 (KHTML, like Gecko) Chrome/144.0.0.0 Safari/537.36')
            if self._secret is None:
                page = self._checked(session.get('https://open.spotify.com/', timeout=20), 'Web player')
                urls = set(re.findall(r'(https://[^/]+/cdn/build/web-player/[^"\s>]+\.js|/cdn/build/web-player/[^"\s>]+\.js)', page.text))
                candidates = []
                for url in sorted(urls)[:10]:
                    url = urljoin('https://open.spotifycdn.com', url)
                    if urlparse(url).hostname not in ('open.spotifycdn.com', 'open.spotify.com'):
                        continue
                    script = self._checked(session.get(url, timeout=20), 'Web player script')
                    candidate = extract_totp_secret(script.text)
                    if candidate:
                        candidates.append(candidate)
                if not candidates:
                    raise SpotifyUnavailable('Spotify web authentication changed: no TOTP configuration found')
                self._secret = max(candidates)
            version, secret = self._secret
            key = (secret if secret.isdigit() else ''.join(
                str(ord(c) ^ ((i % 33) + 9)) for i, c in enumerate(secret))).encode()
            now = time.time()
            response = self._checked(session.get('https://open.spotify.com/api/server-time', timeout=15), 'Server time')
            server_time = response.json()['serverTime']
            # Scope the cookie to Spotify only; never send it to script hosts.
            session.cookies.set('sp_dc', self._cookie, domain='open.spotify.com', path='/')
            response = session.get('https://open.spotify.com/api/token', params={
                'reason': 'init', 'productType': 'web-player', 'totp': totp(key, now),
                'totpServer': totp(key, server_time), 'totpVer': version,
            }, timeout=20, allow_redirects=False)
            if response.status_code != 200:
                self._secret = None  # Refresh the public configuration on next retry.
            data = self._checked(response, 'Spotify web login').json()
            if data.get('isAnonymous') is not False or not data.get('accessToken'):
                raise SpotifyUnavailable('Spotify cookie expired or is not authenticated; update SPOTIFY_SP_DC')
            ttl = float(data['accessTokenExpirationTimestampMs']) / 1000 - time.time()
            if ttl <= 60:
                raise SpotifyUnavailable('Spotify returned an expired token')
            self._token = data['accessToken']
            self._expires = time.monotonic() + ttl - 60

    async def _disconnect(self):
        ws, self._ws = self._ws, None
        self._connection_id = None
        if ws is not None:
            await ws.close()

    async def close(self):
        async with self._lock:
            await self._disconnect()

    async def _ensure_connected(self):
        if self._token is None or time.monotonic() >= self._expires:
            await self._disconnect()
            await asyncio.to_thread(self._login)
        if self._ws is None:
            self._ws = await connect(
                'wss://dealer.spotify.com/?access_token=' + self._token,
                open_timeout=20, close_timeout=3, ping_interval=20,
                ping_timeout=10, max_size=8 * 1024 * 1024, proxy=None,
            )
            hello = json.loads(await asyncio.wait_for(self._ws.recv(), 15))
            self._connection_id = hello.get('headers', {}).get('Spotify-Connection-Id')
            if not self._connection_id:
                raise SpotifyUnavailable('Dealer did not return a connection ID')
        # Spotify's application heartbeat is separate from WebSocket ping frames.
        await self._ws.send('{"type":"ping"}')
        async def wait_pong():
            while True:
                message = json.loads(await self._ws.recv())
                if message.get('type') == 'pong':
                    return
        await asyncio.wait_for(wait_pong(), 10)

    def _snapshot(self):
        response = requests.put(
            'https://spclient.wg.spotify.com/connect-state/v1/devices/' + self._device_id,
            headers={'Authorization': 'Bearer ' + self._token,
                     'X-Spotify-Connection-Id': self._connection_id},
            json={'member_type': 'CONNECT_STATE', 'device': {'device_info': {
                'capabilities': {'can_be_player': False, 'hidden': True,
                                 'needs_full_player_state': True}}}},
            timeout=20, allow_redirects=False,
        )
        if response.status_code == 401:
            self._expires = 0
        return parse_playback(self._checked(response, 'Connect state').json())

    async def currently_playing(self):
        async with self._lock:
            try:
                await self._ensure_connected()
                return await asyncio.to_thread(self._snapshot)
            except asyncio.CancelledError:
                await self._disconnect()
                raise
            except Exception as exc:
                await self._disconnect()
                if isinstance(exc, SpotifyUnavailable):
                    raise
                # Exceptions from HTTP/WebSocket clients may include bearer URLs.
                raise SpotifyUnavailable(f'Connect unavailable ({type(exc).__name__})') from None
