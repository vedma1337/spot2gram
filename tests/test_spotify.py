import unittest
from spotify import parse_playback, SpotifyUnavailable, totp, extract_totp_secret


class PlaybackTests(unittest.TestCase):
    def state(self, **changes):
        state = {'is_playing': True, 'is_paused': False,
                 'track': {'uri': 'spotify:track:2sY7k8ul4ELfW5sTJ4v7tM'}}
        state.update(changes)
        return {'active_device_id': 'phone', 'player_state': state}

    def test_active_device_yields_exact_link(self):
        self.assertEqual(parse_playback(self.state()), (
            '2sY7k8ul4ELfW5sTJ4v7tM',
            'https://open.spotify.com/track/2sY7k8ul4ELfW5sTJ4v7tM'))

    def test_pause_overrides_is_playing_flag(self):
        self.assertIsNone(parse_playback(self.state(is_paused=True)))

    def test_confirmed_stop(self):
        self.assertIsNone(parse_playback(self.state(is_playing=False)))

    def test_ads_episodes_and_local_tracks_are_not_sent_to_bot(self):
        for uri in ['spotify:ad:123', 'spotify:episode:123', 'spotify:local:artist:album:track', 'spotify:track:bad']:
            with self.subTest(uri=uri):
                self.assertIsNone(parse_playback(self.state(track={'uri': uri})))

    def test_unknown_state_is_not_a_confirmed_stop(self):
        for payload in [{}, {'active_device_id': 'phone'}, self.state(is_paused='false'), self.state(is_playing=None)]:
            with self.subTest(payload=payload):
                with self.assertRaises(SpotifyUnavailable):
                    parse_playback(payload)

    def test_no_active_device_does_not_publish_old_track(self):
        payload = self.state()
        payload['active_device_id'] = ''
        self.assertIsNone(parse_playback(payload))

    def test_totp_rfc6238_six_digits(self):
        self.assertEqual(totp(b'12345678901234567890', 59), '287082')

    def test_extract_highest_version_and_decode_javascript_escapes(self):
        self.assertEqual(extract_totp_secret('x={secret:"abc",version:58};y={secret:"a\\x62c",version:59}'), (59, 'abc'))


if __name__ == '__main__':
    unittest.main()
