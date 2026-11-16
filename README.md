# spot2gram

Show the track you're listening to on Spotify in your Telegram profile's music section. Works with Spotify Free — no Premium subscription or Spotify API keys required.

**English** · [Русский](README-RU.md)

> [!WARNING]
> This project uses an unofficial Spotify interface. Use it at your own risk.

## How it works

Play music on Spotify on your phone, computer, or another device. The script reads the current track, sends its Spotify link to [@nowtrackbot](https://t.me/nowtrackbot), and posts the resulting audio in your Telegram channel. It then adds that audio to your profile music.

When the track changes, your profile music updates. When playback stops (after a short grace period) or the script exits, the script removes it. Audio messages stay in the channel and are reused when you play the same song again. If @nowtrackbot can't find a track ("Audio not found"), the script deletes that message, removes the previous song from your profile, and doesn't ask about the track again for an hour.

Keep the script running while you want syncing enabled. Spotify doesn't need to be open on the same computer: the script follows your account's active device. Spotify pushes every change to the script, so the profile updates within a second or two.

## Requirements

- Python 3.10 or newer.
- Spotify and Telegram accounts.
- A dedicated Telegram channel where your account can post. A private channel is recommended.
- Your Spotify web player's `sp_dc` cookie — see the instructions below.

## Installation

Download the project and open a terminal in its folder.

**Windows — PowerShell:**

```powershell
python -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt
```

**Linux / macOS:**

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
```

## Configuration

Copy `.env-example` to a new file named `.env`. If you already have `.env`, edit it instead.

### 1. Get your Spotify cookie

1. Open [open.spotify.com](https://open.spotify.com) and sign in.
2. Open browser developer tools: **F12** or **Ctrl+Shift+I**. On macOS, use **⌘⌥I**.
3. In Chrome / Edge, select **Application → Storage → Cookies**. In Firefox, select **Storage → Cookies**.
4. Select the Spotify domain and find **`sp_dc`**.
5. Copy its **Value** into `SPOTIFY_SP_DC` in `.env`.

This cookie grants access to your Spotify session. Don't publish or share your `.env` file.

### 2. Set your channel

Set `CHANNEL_ID` to the channel's full **numeric ID**, such as `-1001234567890`. Use the ID, not the channel's name, link, or `@username`.

Your `.env` should look like this:

```ini
SPOTIFY_SP_DC=your_cookie_value
CHANNEL_ID=-1001234567890
```

Optional settings, all in seconds unless noted:

| Setting | Default | Meaning |
| --- | --- | --- |
| `CLEAR_DELAY_SECONDS` | `15` | How long after playback stops to remove the track, so short pauses don't make it blink. |
| `STALE_AFTER_SECONDS` | `600` | Remove the track if Spotify can't be reached for this long. `0` keeps it. |
| `AUDIO_TIMEOUT_SECONDS` | `90` | How long @nowtrackbot gets to attach the audio. |
| `CLEAR_ON_EXIT` | `true` | Remove the track from your profile when the script stops. |
| `LOG_LEVEL` | `INFO` | `DEBUG` shows every step. |

## Run

From the project folder:

**Windows:**

```powershell
.venv\Scripts\python.exe main.py
```

**Linux / macOS:**

```bash
.venv/bin/python main.py
```

On the first run, follow the terminal prompts to sign in to Telegram. Your login is saved in `music_sync.session` and reused on later runs.

Start a track in Spotify. It should appear in your channel and profile shortly.

## Troubleshooting

Repeated errors are logged once, with a reminder every 10 minutes and an "OK again" line when they clear.

- `sp_dc cookie is expired or signed out` — get a fresh `sp_dc`, replace it in `.env`, and restart. You can check the cookie's expiry in the browser's **Expires / Max-Age** column; the session may be revoked earlier.
- `unknown format` / `TOTP secret` — Spotify changed its unofficial protocol. The script logs this as an error and retries every 5 minutes, but it needs a code fix.
- `Channel ... not found` — check `CHANNEL_ID` and that your account is a member who can post there.
- `No audio for ...` — @nowtrackbot couldn't get that track. The script tries again after an hour.
- `Spotify: network error` and similar — the script retries with a growing delay. A network failure isn't treated as a pause; the track stays on your profile for up to `STALE_AFTER_SECONDS`.

The script records the music it added in `state.json`, so after a restart or crash it still removes its old track instead of leaving it behind.

## Project files

| File | Purpose |
| --- | --- |
| `main.py` | Entry point: logging, Telegram login, startup and shutdown |
| `sync.py` | Decides what the profile should show and retries failures |
| `spotify.py` | Authenticates with Spotify and reads the current track |
| `telegram_music.py` | Asks @nowtrackbot for audio and updates profile music |
| `state.py` | Remembers what the script put on the profile (`state.json`) |
| `config.py` | Reads `.env` |
| `.env-example` | Example configuration |
| `requirements.txt` | Python dependencies |
| `tests/` | Tests for synchronization and error handling |

Run tests without sending messages or changing your profile:

```bash
python -m unittest discover -s tests -v
```

Use the Python interpreter from `.venv`, as shown in the run commands above.
