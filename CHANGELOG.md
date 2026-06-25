# Changelog

All notable changes to the Vibed Discord Radio Bot will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

---

## [Unreleased]

### Added

- **Multi-folder music selection** — Bot owners can now configure multiple music folders (e.g. Rock, Lofi, Classical) with custom display names in `.env` via `MUSIC_FOLDERS_JSON`. Users or admins can switch between them with the new `!folders` and `!switch` commands. Permission is controlled by `FOLDER_SELECTION_PERMISSION` (`admin` or `all`). Can be toggled on/off with `FOLDER_SELECTION_ENABLED`. The active folder name appears in the Now Playing embed footer. When disabled, the bot falls back to the default `MUSIC_FOLDER` — fully backward-compatible. User command messages and bot replies auto-delete after `AUTO_DELETE_TIMEOUT` seconds to keep the chat tidy. ([#PR])
- **Embed button controls** — The Now Playing embed now includes interactive buttons for track control. Three buttons appear below the embed: NEXT (⏭), PREV (⏮), and PAUSE/PLAY (⏯️). No commands required — just click to skip, go back, or pause. ([#PR])
- **Democratic voting system** — When 2 or more people are in the voice channel, clicking NEXT or PREV triggers a 10-second YES/NO vote. The action only executes if ≥75% of voters say YES. Single listeners can skip/pause freely with no vote. ([#PR])
- **PAUSE/PLAY visibility rule** — The PAUSE/PLAY button is hidden from the embed when 2+ humans are in the voice channel (it's a solo-listener feature). If a second person joins mid-track, clicking the button is silently ignored until the next embed refresh. ([#PR])
- **Track history with PREV** — The bot maintains a history of the last 50 played tracks. Clicking PREV pops the most recent track from history and queues it to play next, stopping the current track immediately. ([#PR])
- **Auto-delete bot replies** — All bot confirmation, error, and vote-result messages auto-delete after 20 seconds to keep the chat channel clean. Vote messages are also auto-deleted after their results are shown. ([#PR])
- **Persistent track ratings** — 👍/👎 buttons on the embed with per-user, per-track voting stored in a local SQLite database (`ratings.db`). Ratings survive bot restarts and accumulate across play sessions. The SQLite `PRIMARY KEY (track_path, user_id)` constraint prevents double-voting. Clicking the opposite button switches your vote; clicking the same button again removes it. ([#PR])
- **STATS button & play count tracking** — A 📊 STATS button displays every track in the music folder with its cumulative 👍/👎 votes and total play count in a monospace table, paginated at 25 tracks per embed. Play counts persist across restarts and are not incremented when going back via PREV. ([#PR])
- **Customisable metadata format** — The embed description line is now controlled by `METADATA_FORMAT` in `.env`, replacing the old `SHOW_ARTIST`, `SHOW_ALBUM`, and `SHOW_DURATION` toggles. Supports `{artist}`, `{title}`, `{album}`, and `{duration}` placeholders with arbitrary custom text and separators. ([#PR])
- **Auto-resume on join** — Paused music automatically resumes when a human joins the voice channel (handled in the 5-second polling loop). ([#PR])
- **.env auto-sync** — On first run after a `git pull`, the bot automatically merges any new configuration keys from `.env.example` into the user's `.env` file, preserving existing settings and notifying the admin via Discord to restart. ([#PR])
- **Pause/Resume support** — The bot now supports `voice_client.pause()` and `voice_client.resume()`, updating the embed to show the paused state (orange embed with ⏸️ prefix). Previously the bot had no pause capability. ([#PR])
- Three new `.env` settings: `VOTE_TIMEOUT_SECONDS` (default 10), `VOTE_THRESHOLD` (default 0.75), and `AUTO_DELETE_TIMEOUT` (default 20). ([#PR])
- Two new UI classes in `bot.py`: `VoteView` (YES/NO vote buttons with per-user duplicate prevention) and `PlayerControlsView` (persistent embed buttons with dynamic pause/play visibility). ([#PR])
- **Customisable command names** — Every bot command can now be renamed via `.env` to avoid collisions with other bots on the same server. Nine new variables (`COMMAND_NOW`, `COMMAND_VOLUME`, `COMMAND_SKIP`, `COMMAND_STOP`, `COMMAND_JOIN`, `COMMAND_REFRESH`, `COMMAND_QUEUE`, `COMMAND_RESUME`, `HELP_COMMAND`) let each bot instance use completely unique command names. The built-in `!help` command is now fully customisable via `HELP_COMMAND`. ([#PR])
- New `Custom command names` section added to `.env.example` with all nine variables documented.
- `readme.md` Commands table now includes a "Customisable Via" column showing which `.env` variable controls each command.

- **AFK leave & auto-rejoin** — When the voice channel is empty for the configured timeout, the bot now **disconnects from voice** entirely (seting the channel status to "🎧 DJ waiting..") instead of pausing playback. This avoids Discord's server-side idle-connection timeout which was causing random disconnects after variable periods of silence. The bot rejoins instantly when someone enters the channel, detected both via the 5-second polling loop and the `on_voice_state_update` event. ([#PR])
- **Display toggles** — Six new `.env` settings (`SHOW_ARTIST`, `SHOW_ALBUM`, `SHOW_DURATION`, `SHOW_UP_NEXT`, `SHOW_NOW_PLAYING`, `SHOW_ALBUM_ART`) allow granular control over which elements appear in the Now Playing embed. Each defaults to `true`. ([#PR])
  - Artist, album, and duration metadata are now shown in the embed description (previously only logged to console).
  - Album name is omitted when it equals "Unknown Album" to keep the embed clean.
  - Setting `SHOW_ALBUM_ART` to `false` skips album art extraction entirely for reduced disk I/O.
  - Setting `SHOW_NOW_PLAYING` to `false` suppresses all embed messages while keeping voice channel status updates and logging active.
  - Voice channel status now updates before the `SHOW_NOW_PLAYING` gate so it always runs regardless of embed visibility.
- `_env_bool()` helper function in `config.py` for parsing boolean-like environment variables (`true`/`false`/`yes`/`no`/`1`/`0`/`on`/`off`).
- `Display Configuration` section added to `readme.md` with toggle reference table and usage examples.

### Changed

- `afk_timeout()` calls `voice_client.disconnect()` instead of `voice_client.stop()` or `pause()`. The bot cleanly leaves the channel rather than staying connected with no audio flowing.
- `is_paused_for_afk` state variable renamed to `is_afk_disconnected` to reflect the new behaviour (bot leaves vs. pauses).
- `check_channel_activity()` monitor loop now also checks for humans joining an empty channel while AFK-disconnected, triggering a rejoin via `start_radio()`.
- `on_voice_state_update` now handles two cases: (1) the bot itself was unexpectedly disconnected → reconnects, and (2) a human joins the configured voice channel while AFK-disconnected → rejoins instantly (before the next monitor poll tick).
- `!resume` command now rejoins the voice channel (was: resumed a paused state).
- `!now` command now shows "Left channel - No listeners" instead of "Paused" when AFK-disconnected.
- `update_now_playing()` sets the voice channel status to "🎧 DJ waiting.." during AFK leave.
- `stop_radio()` cancels the monitor loop before disconnecting to prevent race conditions.
- `start_radio()` clears `is_afk_disconnected` at the start to handle rejoin-from-AFK cleanly.
- `update_now_playing()` embed description is now built line-by-line from enabled fields rather than being static.
- **Embed layout redesigned**: the embed title is now the song name itself (instead of "🎵 NOW PLAYING"), and artist/album/duration appear on a single line separated by " · " for a compact, elegant look. Emoji prefixes (👤, 💿, ⏱) were removed in favour of cleaner typography.
- `play_next()` and `afk_timeout()` conditionally extract album art based on `SHOW_ALBUM_ART` config.
- `.env.example` updated with the six new display toggle variables.
- `readme.md` Display Configuration section updated to reflect the new single-line metadata layout.

---

## [1.0.0] — 2026-06-15

### Initial Release

- Continuous 24/7 music playback from a local folder.
- Supports MP3, FLAC, M4A, and Ogg Vorbis audio formats.
- Now Playing embeds with album art thumbnails and optional header banner.
- Voice channel status updates showing the current track.
- AFK auto-pause/resume when the voice channel is empty.
- Auto-reconnect with exponential backoff on voice disconnects.
- Admin permission system with role-based access control.
- Commands: `!now`, `!volume`, `!skip`, `!stop`, `!join`, `!refresh`, `!queue`, `!resume`.
- Graceful shutdown via SIGINT/SIGTERM signal handling.
- Old message cleanup on startup.
- Cross-platform support (Windows, Linux, macOS).