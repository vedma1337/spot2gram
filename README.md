# spot2gram

Show the track you're listening to on Spotify in your Telegram profile's music section. Works with Spotify Free — no Premium subscription or Spotify API keys required.

**English** · [Русский](README-RU.md)

> [!WARNING]
> This project uses an unofficial Spotify interface. Use it at your own risk.

![Music in a Telegram profile](.github/images/demo.png)

## How it works

Play music on Spotify on your phone, computer, or another device. The script reads the current track, sends its Spotify link to [@nowtrackbot](https://t.me/nowtrackbot), and posts the resulting audio in your Telegram channel. It then adds that audio to your profile music.

When the track changes, your profile music updates. When playback pauses, the script removes it. Audio messages stay in the channel.

Keep the script running while you want syncing enabled. Spotify doesn't need to be open on the same computer: the script follows your account's active device.

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
POLL_INTERVAL_SECONDS=5
```

`POLL_INTERVAL_SECONDS` controls how often the script checks Spotify, in seconds. The default is every 5 seconds.

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

- `Spotify cookie expired or is not authenticated` — get a fresh `sp_dc`, replace it in `.env`, and restart. You can check the cookie's expiry in the browser's **Expires / Max-Age** column; the session may be revoked earlier.
- `[tg] inline send error` / `[tg] no inline results for` — check `CHANNEL_ID`, your account's posting permissions, and whether @nowtrackbot returns an inline result for the track URL.
- `Connect unavailable` — the script retries. Network failures aren't treated as a pause and don't remove profile music.

The script remembers the music it added only until it restarts. Stopping the script doesn't remove the last track from your profile. After a restart, remove previously added profile music manually if needed.

## Project files

| File | Purpose |
| --- | --- |
| `main.py` | Posts tracks to Telegram and updates profile music |
| `spotify.py` | Authenticates with Spotify and reads the current track |
| `.env-example` | Example configuration |
| `requirements.txt` | Python dependencies |
| `tests/` | Tests for synchronization and error handling |

Run tests without sending messages or changing your profile:

```bash
python -m unittest discover -s tests -v
```

Use the Python interpreter from `.venv`, as shown in the run commands above.
