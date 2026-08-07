<img width="1280" height="720" alt="vibed_header" src="https://github.com/user-attachments/assets/d005bede-e90a-4335-8436-f8b2400f93ad" />

# Vibed.win Discord Radio Bot
 
A 24/7 Discord music bot that plays audio files from a local folder as a continuous radio station. Supports MP3, FLAC, M4A, and Ogg Vorbis. Extracts metadata and album art automatically. **When nobody is listening, the bot leaves the voice channel** — no more random disconnects. Rejoins instantly when someone enters. Runs on Windows, Linux, and macOS.

---

## What This Bot Does

| Feature | Details |
|---------|---------|
| **Continuous music playback** | Shuffles your music folder and plays forever in a loop. When the queue runs out, it reshuffles all files and keeps going. |
| **Now Playing embeds** | Sends a rich embed to the voice channel's text chat showing the current track, artist, album, duration, and album art thumbnail. Shows the next track as "Up Next". |
| **Voice channel status** | Updates the voice channel's sub-text status to show the current song title (e.g. "🎧 Bohemian Rhapsody"). |
| **Album art extraction** | Reads embedded artwork from FLAC and MP3 tags. Falls back to `cover.jpg`, `cover.png`, `folder.jpg`, or `albumart.jpg` in the music folder. |
| **AFK leave & auto-rejoin** | Monitors the voice channel every 5 seconds. If the channel is empty for a configurable timeout (default 60s), sets the channel status to "🎧 DJ waiting.." and **disconnects from voice** to avoid Discord's idle-connection timeout. Rejoins instantly when someone enters. |
| **Auto-reconnect** | Detects unexpected voice disconnects and retries with exponential backoff (3s → 6s → 12s). Notifies the voice channel's text chat if all retries fail. Intentional AFK leaves (empty channel) do not trigger reconnection — the bot waits for someone to join instead. |
| **Old message cleanup** | On startup, deletes the bot's previous messages from the voice channel's text chat so you don't accumulate stale embeds. |
| **Optional header banner** | Configure a 1280×720 JPG image and it appears full-width at the top of every Now Playing embed — great for branding. |
| **Display toggles** | Turn individual Now Playing embed elements on/off via `.env`: artist, album, duration, up next, album art, or the entire embed. Mix and match to keep your chat clean. |
| **Embed button controls** | The Now Playing embed includes optional [NEXT] [PREV] and [PAUSE/PLAY] buttons. Click to skip, go back, or pause — no commands needed. The PREV/NEXT buttons can be hidden entirely via `.env` for admin-only skipping. |
| **Democratic voting** | When 2+ people are listening and PREV/NEXT buttons are enabled, those buttons trigger a 10-second vote (≥75% YES required). Solo listeners skip/pause freely with no vote. The PAUSE/PLAY button is hidden when multiple people are in the channel. |
| **Track history** | The bot remembers the last 50 tracks played. The PREV button pops the most recent track back into the queue. |
| **Auto-delete chat replies** | All bot confirmation, error, and vote-result messages automatically delete after 20 seconds to keep the channel tidy. |
| **Admin permission system** | State-changing commands (!skip, !volume, etc.) are restricted to users with a specific role, the Administrator permission, or everyone (if ADMIN_ROLE_ID=0). |
| **Graceful shutdown** | Handles Ctrl+C and SIGTERM cleanly — disconnects from voice, cancels background tasks, and exits without leaving stale state. |

### Commands

| Command | Who Can Use | What It Does |
|---------|------------|--------------|
| `!hint` | Everyone | Shows the help message listing all commands |
| `!now` | Everyone | Shows the currently playing song and volume |
| `!volume <0-100>` | Admin | Sets playback volume (applies to next song) |
| `!skip` | Admin | Skips to the next track |
| `!refresh` | Admin | Re-sends the Now Playing embed |
| `!queue` | Admin | Shows the next 10 upcoming tracks |
| `!resume` | Admin | Rejoins the voice channel and resumes playback. Useful for starting the music before anyone else joins. |
| `!folders` | Admin\* | Lists all configured music folders with the currently active one highlighted. |
| `!switch <name>` | Admin\* | Switches playback to the named music folder. Supports partial name matching. |

Commands are typed in the voice channel's text chat. Each bot only sees commands in its own voice channel, so running multiple bots on the same server is safe.

> \* When `FOLDER_SELECTION_ENABLED=true`, permission is controlled by `FOLDER_SELECTION_PERMISSION` — set to `all` to let anyone switch folders, or `admin` (default) to restrict to admins. These commands only appear in `!hint` when the feature is enabled.

### Supported Audio Formats

- `.mp3` / `.MP3`
- `.flac` / `.FLAC`
- `.m4a` / `.M4A`
- `.ogg` / `.OGG`

---

## Quick Start

### Prerequisites

- **Python 3.10+** (3.12 recommended)
- **ffmpeg** installed and available on your system PATH
- A **Discord bot token** from the [Discord Developer Portal](https://discord.com/developers/applications)

### 0. Create Your Discord Bot Application

If you don't already have a bot set up, follow these steps:

1. **Go to the Discord Developer Portal**  
   Visit [https://discord.com/developers/applications](https://discord.com/developers/applications) and log in.

2. **Create a new application**  
   Click the **New Application** button (top right) → give it a name (e.g. "My Radio Bot") → click **Create**.

3. **Turn it into a bot**  
   In the left sidebar, click the **Bot** tab → click **Add Bot** → confirm by clicking **Yes, do it!**

4. **Copy your bot token**  
   Under the **Token** section, click **Copy** (or **Reset Token** → **Copy** if no token is shown).  
   This is your `DISCORD_TOKEN` — paste it into `.env` later.  
   > ⚠️ **Keep this secret!** Anyone with your token can control your bot.

5. **Enable required intents**  
   Still on the Bot tab, scroll down to **Privileged Gateway Intents** and toggle ON:
   - **Presence Intent**
   - **Server Members Intent**
   - **Message Content Intent** *(required to read `!commands`)*  
   Click **Save Changes**.

6. **Invite the bot to your server**  
   - Click **OAuth2** in the left sidebar → **URL Generator**
   - Under **Scopes**, check: `bot` and `applications.commands`
   - Under **Bot Permissions**, check: `Connect`, `Speak`, `Use Voice Activity`, `Send Messages`, `Embed Links`, `Attach Files`, `Read Message History`, `Manage Channel` (or `Set Voice Channel Status`)
   - Copy the generated URL at the bottom, open it in your browser, select your server, and click **Authorize**

   > For a detailed breakdown of why each permission is needed, see the [Discord Bot Permissions](#discord-bot-permissions) section below.

7. **Disable "Public Bot" (recommended)**  
   Back in the **Bot** tab, turn OFF **Public Bot** so only you can invite it. Leave **Requires OAuth2 Code Grant** OFF.

Your bot now appears in your server's member list (offline until you run `python bot.py`).

### 1. Clone & Enter the Project

```bash
git clone https://github.com/haywardgg/vibed-discord-bot.git
cd vibed-discord-bot
```

### 2. Create a Virtual Environment

**Linux / macOS:**
```bash
python3 -m venv venv
source venv/bin/activate
```

**Windows (Command Prompt):**
```cmd
python -m venv venv
venv\Scripts\activate
```

**Windows (PowerShell):**
```powershell
python -m venv venv
.\venv\Scripts\Activate.ps1
```

### 3. Install Python Dependencies

```bash
pip install -r requirements.txt
```

### 4. Install ffmpeg

This is the most common stumbling block. ffmpeg is a **system program** — not a Python package — that discord.py uses to encode and stream audio.

#### Linux (Debian / Ubuntu / Raspberry Pi OS)
```bash
sudo apt update && sudo apt install ffmpeg -y
```

#### macOS (Homebrew)
```bash
brew install ffmpeg
```

#### Windows
**Option A — winget (Windows 10/11):**
```cmd
winget install ffmpeg
```
Restart your terminal after installation.

**Option B — Manual download:**
1. Go to https://ffmpeg.org/download.html
2. Download the Windows build (e.g. from gyan.dev)
3. Extract the zip to a folder (e.g. `C:\ffmpeg`)
4. Add `C:\ffmpeg\bin` to your system PATH:
   - Open **System Properties** → **Environment Variables**
   - Under **System variables**, find `Path` → **Edit**
   - Add the full path to the `bin` folder
   - Click **OK** and restart your terminal

#### Verify ffmpeg is Working
Run this in any terminal:
```bash
ffmpeg -version
```
You should see something like:
```
ffmpeg version 6.1
built with gcc ...
```
If you see `command not found` or `'ffmpeg' is not recognized`, ffmpeg is not installed or not on your PATH.

### 5. Configure the Bot

```bash
# Copy the example config
cp .env.example .env
```

Open `.env` in any text editor and fill in your values:

```ini
# Required — your bot token from https://discord.com/developers
DISCORD_TOKEN=your_bot_token_here

# Discord IDs — see "Finding Discord IDs" below
GUILD_ID=1234567890123456789
VOICE_CHANNEL_ID=1234567890123456789

# Admin role — set to 0 to let everyone use admin commands
ADMIN_ROLE_ID=0

# Where your music files live
MUSIC_FOLDER=./music

# Volume (0.0 = silent, 1.0 = full)
DEFAULT_VOLUME=0.3

# Seconds of silence before the bot leaves the voice channel (when empty)
AFK_TIMEOUT_SECONDS=60

# Optional — path to a 1280×720 JPG banner image (defaults to ./vibed_header.jpg)
HEADER_IMAGE_PATH=./vibed_header.jpg

# Display toggles — set to "false" to hide elements from the Now Playing embed
SHOW_UP_NEXT=true
SHOW_NOW_PLAYING=true
SHOW_ALBUM_ART=true

# Show PREV/NEXT buttons on the embed. Set to "true" to let users skip via buttons.
SHOW_SKIP_BUTTONS=false

# Metadata format — customise the embed description line.
# Supports: {artist}  {title}  {album}  {duration}
METADATA_FORMAT={artist} · {album} · {duration}

# Voting — how long a vote stays open, and the YES ratio required to pass
VOTE_TIMEOUT_SECONDS=10
VOTE_THRESHOLD=0.75

# Bot messages and vote results auto-delete after this many seconds
AUTO_DELETE_TIMEOUT=20
```

### Finding Discord IDs

1. Open Discord → **User Settings** (gear icon)
2. Go to **Advanced** → toggle **Developer Mode** ON
3. Right-click any server, channel, or role → **Copy ID**

| What | Which Variable | How |
|------|--------------|-----|
| Server | `GUILD_ID` | Right-click server icon → Copy ID |
| Voice channel | `VOICE_CHANNEL_ID` | Right-click the voice channel → Copy ID |
| Admin role | `ADMIN_ROLE_ID` | Server Settings → Roles → right-click role → Copy ID |

### 6. Add Music Files

Create a `music` folder next to `bot.py` (or wherever you set `MUSIC_FOLDER` to) and drop in your `.mp3`, `.flac`, `.m4a`, or `.ogg` files:

```
vibed-discord-bot/
├── music/
│   ├── song-one.mp3
│   ├── song-two.flac
│   └── album-folder/
│       ├── track-01.mp3
│       └── cover.jpg       ← album art (optional)
├── bot.py
├── config.py
├── .env
└── requirements.txt
```

The bot scans subfolders too — organise your music however you like.

### 7. Run the Bot

```bash
# Make sure your virtual environment is activated, then:
python bot.py
```

You should see output like:
```
2025-01-01 12:00:00 [INFO] radio-bot: Logged in as RadioBot#1234
2025-01-01 12:00:01 [INFO] radio-bot: Music folder: /home/you/vibed-discord-bot/music
2025-01-01 12:00:02 [INFO] radio-bot: Connected to voice channel: Music
2025-01-01 12:00:03 [INFO] radio-bot: Playing: Queen - Bohemian Rhapsody at 30% volume
```

---

## Verifying the Bot is Working

### 1. Check the Console / Logs
The bot logs every action to stdout. Look for:
- `Logged in as ...` — the bot connected to Discord
- `Connected to voice channel: ...` — joined the voice channel
- `Playing: ...` — actively streaming a track
- `Channel empty for 60s – leaving voice` — bot left the channel (normal if empty)
- `Listener joined empty channel – rejoining and resuming` — bot came back

### 2. Check the Voice Channel Status
Look at the voice channel in Discord's sidebar. The grey sub-text should show the current song (e.g. "🎧 Bohemian Rhapsody"). When the bot leaves due to an empty channel, the status clears automatically (Discord removes channel statuses when the setter leaves).

### 3. Check the Voice Channel Text Chat
The bot sends a Now Playing embed to its voice channel's text chat every time a new song starts. If you see it, playback is active.

### 4. Use the `!hint` or `!now` Command
Type `!now` in the voice channel's text chat. It replies with the current track or "Nothing is playing right now."

### 5. Test FFmpeg Directly
If you suspect ffmpeg isn't working, run this while the bot is playing:
```bash
# Linux / macOS
ps aux | grep ffmpeg

# Windows
tasklist | findstr ffmpeg
```
You should see one or more ffmpeg processes if the bot is streaming audio.

### 6. Common Startup Warnings (and What They Mean)

| Log Message | Meaning |
|------------|---------|
| `GUILD_ID is 0` | You haven't set your server ID in `.env` |
| `VOICE_CHANNEL_ID is 0` | You haven't set your voice channel ID |
| `TEXT_CHANNEL_ID is 0` | (Deprecated — no longer used. The bot posts to the voice channel's text chat.) |
| `MUSIC_FOLDER '...' does not exist` | Create the folder and add audio files |
| `MISSING 'Manage Channel' permission` | Voice channel status won't update until fixed — grant **Set Voice Channel Status** (or **Manage Channel**) on the voice channel |
| `No music files found in ...` | The folder exists but has no supported audio files |

### Display Configuration

The Now Playing embed shows the song title as its heading, with metadata on a compact single line below. You can toggle each element on or off independently in `.env`:

| Variable | Default | What It Controls |
|----------|---------|-----------------|
| `SHOW_UP_NEXT` | `true` | Shows the next track in the embed footer |
| `SHOW_NOW_PLAYING` | `true` | Sends the embed at all — set to `false` to stop bot messages in chat (voice status still updates) |
| `SHOW_ALBUM_ART` | `true` | Shows the album art thumbnail on the right side of the embed — turning this off skips image extraction entirely for better performance |
| `SHOW_SKIP_BUTTONS` | `false` | Shows the PREV and NEXT buttons on the embed — set to `true` to let users skip tracks via buttons |
| `METADATA_FORMAT` | `{artist} · {album} · {duration}` | Controls the embed description line. Supports `{artist}`, `{title}`, `{album}`, `{duration}` placeholders |

**Examples:**

```ini
# Minimal embed — only the song title, nothing else
SHOW_ARTIST=false
SHOW_ALBUM=false
SHOW_DURATION=false
SHOW_UP_NEXT=false
SHOW_ALBUM_ART=false

# No chat embeds at all (voice channel status and logging still work)
SHOW_NOW_PLAYING=false
```

When enabled, artist, album, and duration appear on the same line separated by " · " for a clean, compact look:

```
Queen · A Night at the Opera · 3:45
```

All toggles default to `true`, so existing users who don't update their `.env` will see no change.

### Button Controls & Voting

The Now Playing embed can include up to three interactive buttons below the track info. PREV and NEXT are controlled by `SHOW_SKIP_BUTTONS` (default `false`); the rating buttons and STATS are always shown.

| Button | Behaviour |
|--------|-----------|
| **NEXT** (⏭) | Skips to the next track. Hidden unless `SHOW_SKIP_BUTTONS=true`. |
| **PREV** (⏮) | Goes back to the previous track (keeps a history of the last 50). Hidden unless `SHOW_SKIP_BUTTONS=true`. |
| **PAUSE / PLAY** (⏯️) | Toggles between paused and playing. |

#### How Voting Works

| People in Channel | What Happens |
|-------------------|-------------|
| **Just you** | Buttons work instantly — skip, pause, or go back with no delay. |
| **2 people** | Clicking NEXT or PREV starts a 10-second vote in the chat. Both users can click ✅ YES or ❌ NO. If ≥75% vote YES, the action executes. If the vote fails, nothing changes. |
| **3+ people** | Same as 2, but the PAUSE/PLAY button is **not shown** on the embed. |

If a second person joins while you're listening solo, the existing PAUSE/PLAY button stays visible but is silently ignored when clicked — it will be removed on the next track's embed.

All vote result messages and bot replies auto-delete after 20 seconds.

#### Voting Configuration

| Variable | Default | Description |
|----------|---------|-------------|
| `VOTE_TIMEOUT_SECONDS` | `10` | How long a vote stays open |
| `VOTE_THRESHOLD` | `0.75` | Required YES ratio (0.0–1.0) |
| `AUTO_DELETE_TIMEOUT` | `20` | Seconds before bot messages auto-delete |

Add these to your `.env` if you want to customise the defaults:

```ini
# Voting & auto-delete
VOTE_TIMEOUT_SECONDS=10
VOTE_THRESHOLD=0.75
AUTO_DELETE_TIMEOUT=20
```

### Multi-Folder Music Selection (Optional)

The bot can manage multiple music folders (e.g. by genre) and let users switch between them on the fly — great for servers with diverse music tastes.

#### Setup

Add these to your `.env`:

```ini
# Enable multi-folder selection
FOLDER_SELECTION_ENABLED=true

# Who can use !folders and !switch: "admin" or "all"
FOLDER_SELECTION_PERMISSION=admin

# JSON mapping of display name → folder path
MUSIC_FOLDERS_JSON={"Rock Music": "./music/Rock", "Lofi Beats": "./music/Lofi", "Classical": "./music/Classic"}
```

Then create the folders and add music:

```
vibed-discord-bot/
├── music/
│   ├── Rock/          ← your rock songs
│   ├── Lofi/          ← your lofi beats
│   └── Classic/       ← your classical pieces
```

#### How It Works

| Setting | Description |
|---------|-------------|
| `FOLDER_SELECTION_ENABLED` | Set to `true` to enable. When `false` (default), the bot uses `MUSIC_FOLDER` as normal — fully backward-compatible. |
| `FOLDER_SELECTION_PERMISSION` | `admin` = only admins can switch. `all` = anyone can switch. |
| `MUSIC_FOLDERS_JSON` | JSON object mapping display names to folder paths. Names are what users type in the `!switch` command. Paths can be relative (e.g. `./music/Rock`) or absolute. |

#### Commands

| Command | What It Does |
|---------|-------------|
| `!folders` | Lists all configured folders. The currently active folder is marked with ▶ and **bold**. |
| `!switch <name>` | Switches to the named folder. Supports partial name matching — `!switch rock` matches "Rock Music". |

Both commands and their responses auto-delete after `AUTO_DELETE_TIMEOUT` seconds. The user's original `!command` message is also deleted to keep the chat clean.

#### Configuration Table

| Variable | Default | Description |
|----------|---------|-------------|
| `FOLDER_SELECTION_ENABLED` | `false` | Enable/disable multi-folder selection |
| `FOLDER_SELECTION_PERMISSION` | `admin` | `admin` or `all` |
| `MUSIC_FOLDERS_JSON` | `{}` | JSON mapping of names → paths |

#### Now Playing Embed

When an alternate folder is selected, the embed footer shows the folder name:

> 📂 Rock Music • Up Next: Back in Black

---

## Running as a Background Service

### Linux / Raspberry Pi (systemd)

Create the service file:
```bash
sudo nano /etc/systemd/system/radio-bot.service
```

Paste this (adjust paths and user to match your system):
```ini
[Unit]
Description=Discord Radio Bot
After=network-online.target
Wants=network-online.target

[Service]
Type=idle
User=yourusername
Group=yourusername
WorkingDirectory=/home/yourusername/vibed-discord-bot
ExecStart=/home/yourusername/vibed-discord-bot/venv/bin/python /home/yourusername/vibed-discord-bot/bot.py
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
```

Enable and start:
```bash
sudo systemctl daemon-reload
sudo systemctl enable radio-bot.service
sudo systemctl start radio-bot.service
```

Check status and view logs:
```bash
sudo systemctl status radio-bot.service
journalctl -u radio-bot.service -f        # live logs
journalctl -u radio-bot.service -n 50     # last 50 lines
```

### Windows (Task Scheduler or NSSM)

For always-on Windows, use [NSSM (Non-Sucking Service Manager)](https://nssm.cc/):
```cmd
nssm install RadioBot
```
- **Application Path**: `C:\path\to\vibed-discord-bot\venv\Scripts\python.exe`
- **Arguments**: `C:\path\to\vibed-discord-bot\bot.py`
- **Startup Directory**: `C:\path\to\vibed-discord-bot`

---

## Discord Bot Permissions

When inviting your bot to a server, it needs these permissions:

| Permission | Why |
|-----------|-----|
| **Connect** | Join voice channels |
| **Speak** | Stream audio |
| **Use Voice Activity** | Required for voice |
| **Send Messages** | Post Now Playing embeds |
| **Embed Links** | The Now Playing embed won't render without this |
| **Attach Files** | Upload album art and banner images |
| **Read Message History** | Clean up old messages on startup |
| **Manage Channel** or **Set Voice Channel Status** | Update the voice channel status with the current song title. "Set Voice Channel Status" is the more targeted permission — use it if you prefer granting minimal permissions. |

Bot invite URL format:
```
https://discord.com/oauth2/authorize?client_id=YOUR_CLIENT_ID&permissions=36700160&scope=bot
```

---

## Troubleshooting

| Issue | Likely Fix |
|-------|-----------|
| `ffmpeg` not found / `'ffmpeg' is not recognized` | Install ffmpeg (see step 4 above) and verify with `ffmpeg -version` |
| `PyNaCl` / `libsodium` error during pip install | Run `pip install --upgrade discord.py[voice]`. On Linux, also try `sudo apt install libsodium23` |
| Bot not joining voice channel | Double-check `VOICE_CHANNEL_ID` in `.env`. Make sure the bot has Connect + Speak permissions on that channel |
| Bot joins but no audio | Verify ffmpeg is on your PATH: run `ffmpeg -version` from the same terminal you start the bot in |
| `No music files found` | Check `MUSIC_FOLDER` path. The bot looks for `.mp3`, `.flac`, `.m4a`, `.ogg` — other formats are ignored |
| Admin commands not working | Set `ADMIN_ROLE_ID=0` in `.env` to open commands to everyone (good for testing) |
| Voice channel status not updating | Grant the bot the **Set Voice Channel Status** permission (or the broader **Manage Channel** permission) on the voice channel |
| Old messages not purged on restart | The bot needs **Read Message History** permission on the voice channel |
| Bot disconnected and won't rejoin | If the bot was AFK-disconnected (empty channel), it will rejoin automatically when someone enters the voice channel. Otherwise, restart the bot service. |
| Module import errors (aiohttp, discord, etc.) | Make sure your virtual environment is activated before running `python bot.py` |
| `DISCORD_TOKEN is not set` | You forgot to create `.env` or the token is missing. Copy `.env.example` to `.env` and fill in your token |
| Permission errors when installing packages | On Linux, never use `sudo pip`. Make sure you're in a virtual environment |

---

## File Structure

```
vibed-discord-bot/
├── bot.py                # Main bot code — playback, commands, events
├── config.py             # Reads .env, validates settings, exports constants
├── .env                  # Your secrets and configuration (NOT committed)
├── .env.example          # Template — copy to .env and fill in
├── requirements.txt      # Python package dependencies + install guide
├── readme.md             # This file
├── LICENSE               # MIT license
├── .gitignore            # Keeps secrets and temp files out of git
├── music/                # Drop your audio files here (or wherever MUSIC_FOLDER points)
└── venv/                 # Python virtual environment (NOT committed)
```

---

## Dependencies Explained

| Package | Purpose |
|---------|---------|
| `discord.py[voice]` | The Discord API wrapper. The `[voice]` extra pulls in PyNaCl for encrypted voice streaming. |
| `python-dotenv` | Loads environment variables from `.env` so secrets stay out of source control. |
| `mutagen` | Reads/writes audio metadata (tags, album art) without needing ffmpeg. |
| `ffmpeg` (system) | Decodes audio files and converts them to raw PCM for Discord. Not a Python package. |

---

## Security Notes

- **Never commit `.env`** — it contains your bot token. The `.gitignore` already excludes it.
- **The bot token** is the only secret. Anyone with your token can control your bot.
- **No user-generated input is passed to shell commands**. All ffmpeg arguments are hardcoded.
- **Admin commands are gated** behind the `is_admin()` check — only users with the correct role or the Administrator permission can change state.
- **The bot's messages are self-deleted** on restart, so no chat history is leaked.
- **Album art is extracted locally** via Mutagen — no images are uploaded to external services.

## Screenshots 

<img width="656" height="1083" alt="Screenshot 2026-06-23 114414" src="https://github.com/user-attachments/assets/357b7bc8-d19c-46c8-a1fd-f0b9aefbef2b" />

