import asyncio
import json
import unittest

from spotify import (SpotifyBroken, SpotifyConnect, SpotifyUnavailable, Track, cluster_from_push,
                     extract_totp_secret, parse_playback, totp)

TRACK_ID = "2sY7k8ul4ELfW5sTJ4v7tM"


def cluster(**player_state):
    state = {"is_playing": True, "is_paused": False, "track": {"uri": f"spotify:track:{TRACK_ID}"}}
    state.update(player_state)
    return {"timestamp": "1", "active_device_id": "phone", "player_state": state}


class ParsePlaybackTests(unittest.TestCase):
    def test_playing_track(self):
        track = parse_playback(cluster())
        self.assertEqual(track, Track(TRACK_ID))
        self.assertEqual(track.url, f"https://open.spotify.com/track/{TRACK_ID}")

    def test_paused_wins_over_is_playing(self):
        self.assertIsNone(parse_playback(cluster(is_paused=True)))

    def test_not_playing(self):
        self.assertIsNone(parse_playback(cluster(is_playing=False)))

    def test_omitted_false_flags_use_protobuf_defaults(self):
        payload = cluster()
        del payload["player_state"]["is_paused"]
        self.assertEqual(parse_playback(payload), Track(TRACK_ID))
        del payload["player_state"]["is_playing"]
        self.assertIsNone(parse_playback(payload))

    def test_ads_episodes_and_local_files_are_skipped(self):
        for uri in ["spotify:ad:123", "spotify:episode:123", "spotify:local:a:b:c:1", "spotify:track:bad"]:
            with self.subTest(uri=uri):
                self.assertIsNone(parse_playback(cluster(track={"uri": uri})))

    def test_no_active_device_means_stopped(self):
        payload = cluster()
        payload["active_device_id"] = ""
        self.assertIsNone(parse_playback(payload))

    def test_omitted_active_device_means_stopped(self):
        # What Spotify sends once nothing has played for a while: this used to
        # be logged as "incomplete state" forever and kept a stale track.
        self.assertIsNone(parse_playback({"timestamp": "1", "devices": {}, "server_timestamp_ms": "2"}))
        payload = cluster()
        del payload["active_device_id"]
        self.assertIsNone(parse_playback(payload))

    def test_unrecognisable_responses_are_unknown_not_stopped(self):
        bad = [None, [], {}, {"error": "x"}, {"active_device_id": "phone"},
               cluster(is_paused="false"), cluster(is_playing=None), cluster(track=None)]
        for payload in bad:
            with self.subTest(payload=payload):
                with self.assertRaises(SpotifyUnavailable):
                    parse_playback(payload)


def push(cluster_state, uri="hm://connect-state/v1/cluster"):
    return {"type": "message", "uri": uri, "headers": {"content-type": "application/json"},
            "payloads": [{"cluster": cluster_state, "update_reason": "DEVICE_STATE_CHANGED"}]}


class PushTests(unittest.TestCase):
    def test_state_update(self):
        self.assertEqual(parse_playback(cluster_from_push(push(cluster()))), Track(TRACK_ID))

    def test_other_messages_are_ignored(self):
        other = {"type": "message", "uri": "hm://herodotus/uri/whatever", "payloads": ["AAAA"]}
        self.assertIsNone(cluster_from_push(other))
        self.assertIsNone(cluster_from_push({"type": "pong"}))

    def test_changed_format_is_broken(self):
        for payloads in [None, [], ["base64"], [{"no": "cluster"}], [{"cluster": {}}, {"cluster": {}}]]:
            with self.subTest(payloads=payloads):
                with self.assertRaises(SpotifyBroken):
                    cluster_from_push({"uri": "hm://connect-state/v1/cluster", "payloads": payloads})


class FakeWebSocket:
    def __init__(self, *messages):
        self.messages = [json.dumps(m) for m in messages]

    async def recv(self):
        if self.messages:
            return self.messages.pop(0)
        await asyncio.Event().wait()  # Nothing more: block like a quiet connection.

    async def send(self, data):
        pass

    async def close(self):
        pass


class ListenerTests(unittest.IsolatedAsyncioTestCase):
    async def listen(self, *messages):
        spotify = SpotifyConnect("cookie")
        self.addCleanup(spotify._http.close)
        spotify._ws = FakeWebSocket(*messages)
        spotify._listener = asyncio.create_task(spotify._listen(spotify._ws))
        spotify._token_refresh_at = float("inf")
        await asyncio.sleep(0.01)
        return spotify

    async def test_push_updates_state_and_wakes_waiter(self):
        spotify = await self.listen({"uri": "hm://other"}, push(cluster()))
        await asyncio.wait_for(spotify.wait_for_change(5), 1)
        self.assertEqual(await spotify.currently_playing(), Track(TRACK_ID))
        await spotify.close()

    async def test_pause_push(self):
        spotify = await self.listen(push(cluster()), push(cluster(is_paused=True)))
        self.assertIsNone(await spotify.currently_playing())
        await spotify.close()

    async def test_unknown_push_format_fails_loudly(self):
        spotify = await self.listen({"uri": "hm://connect-state/v1/cluster", "payloads": ["gzip?"]})
        with self.assertRaises(SpotifyBroken):
            await spotify.currently_playing()
        await spotify.close()


class TotpTests(unittest.TestCase):
    def test_rfc6238_six_digits(self):
        self.assertEqual(totp(b"12345678901234567890", 59), "287082")

    def test_extract_highest_version_and_decode_javascript_escapes(self):
        script = 'x={secret:"abc",version:58};y={secret:"a\\x62c",version:59}'
        self.assertEqual(extract_totp_secret(script), (59, "abc"))

    def test_no_secret(self):
        self.assertIsNone(extract_totp_secret("nothing here"))


if __name__ == "__main__":
    unittest.main()
