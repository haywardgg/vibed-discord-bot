"""Configuration module for the Discord Radio Bot.
All environment-specific settings are loaded from environment variables,
with sensible fallback defaults for development/testing.
"""

import json
import logging
import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

log = logging.getLogger("radio-bot.config")


def _env_int(name: str, default: int = 0) -> int:
    """Return an integer environment variable with a default."""
    val = os.getenv(name, str(default))
    try:
        return int(val)
    except ValueError:
        return default


def _env_float(name: str, default: float = 0.3) -> float:
    """Return a float environment variable with a default."""
    val = os.getenv(name, str(default))
    try:
        return float(val)
    except ValueError:
        return default


def _env_bool(name: str, default: bool = True) -> bool:
    """Return a boolean environment variable with a default.

    Accepts: "true", "yes", "1", "on" → True
             "false", "no", "0", "off", "" → False
    """
    val = os.getenv(name, "").strip().lower()
    if not val:
        return default
    if val in ("true", "yes", "1", "on"):
        return True
    if val in ("false", "no", "0", "off"):
        return False
    return default


# ---------------------------------------------------------------------------
# Discord settings
# ---------------------------------------------------------------------------
TOKEN = os.getenv("DISCORD_TOKEN")  # Required – must be set in .env

GUILD_ID = _env_int("GUILD_ID", 0)
VOICE_CHANNEL_ID = _env_int("VOICE_CHANNEL_ID", 0)

# ---------------------------------------------------------------------------
# Admin settings
# ---------------------------------------------------------------------------
# Set to 0 in .env (or leave unset) to allow everyone to use admin commands.
ADMIN_ROLE_ID = _env_int("ADMIN_ROLE_ID", 0)

# Discord user ID of the bot owner — used for DM notifications
# (e.g., when new .env keys are auto-added after a git pull).
ADMIN_USER_ID = _env_int("ADMIN_USER_ID", 0)

# Set to "true" if the bot is managed by systemd / NSSM / Docker etc.
# When new .env keys are auto-synced, the bot will shut down so the
# service manager restarts it.  If false, the owner gets a DM instead.
RUNNING_AS_SERVICE = _env_bool("RUNNING_AS_SERVICE", False)

# ---------------------------------------------------------------------------

# Music settings
# ---------------------------------------------------------------------------
# Path to the folder containing your audio files.
# Can be absolute (e.g. C:\Music on Windows, /home/pi/music on Linux)
# or relative to the bot's working directory (e.g. ./music).
_music_folder = os.getenv("MUSIC_FOLDER", "./music")
MUSIC_FOLDER = str(Path(_music_folder).resolve()) if _music_folder else ""
DEFAULT_VOLUME = _env_float("DEFAULT_VOLUME", 0.3)
AFK_TIMEOUT_SECONDS = _env_int("AFK_TIMEOUT_SECONDS", 60)
AFK_AUTO_LEAVE = _env_bool("AFK_AUTO_LEAVE", True)
AFK_PAUSE_ON_EMPTY = _env_bool("AFK_PAUSE_ON_EMPTY", True)
VOICE_IDLE_STATUS = os.getenv("VOICE_IDLE_STATUS", "PAUSED")

# ---------------------------------------------------------------------------
# Embed header image (optional)
# ---------------------------------------------------------------------------
# Path to a 1280×720 JPG image shown as a banner at the top of the
# now-playing embed.  Defaults to "./vibed_header.jpg" — place your
# banner image at that path, or set to empty to disable the header.
HEADER_IMAGE_PATH = os.getenv("HEADER_IMAGE_PATH", "./vibed_header.jpg")

# ---------------------------------------------------------------------------
# Display toggles — control which elements appear in the Now Playing embed.
# Set any of these to "false" to hide that element.
# ---------------------------------------------------------------------------
SHOW_UP_NEXT = _env_bool("SHOW_UP_NEXT", True)
SHOW_NOW_PLAYING = _env_bool("SHOW_NOW_PLAYING", True)
SHOW_ALBUM_ART = _env_bool("SHOW_ALBUM_ART", True)

# Show/hide the PREV and NEXT buttons on the Now Playing embed.
# Set to "false" to prevent non-admin users from skipping tracks via buttons.
SHOW_SKIP_BUTTONS = _env_bool("SHOW_SKIP_BUTTONS", False)

# ---------------------------------------------------------------------------
# Metadata format — controls how the embed description line is displayed.
# Supports these placeholders: {artist}  {title}  {album}  {duration}
# Duration uses M:SS format.  Leave empty to use the legacy template.
# ---------------------------------------------------------------------------
METADATA_FORMAT = os.getenv("METADATA_FORMAT", "{artist} · {album} · {duration}")

# ---------------------------------------------------------------------------
# Voting / button controls
# ---------------------------------------------------------------------------
VOTE_TIMEOUT_SECONDS = _env_int("VOTE_TIMEOUT_SECONDS", 10)
VOTE_THRESHOLD = _env_float("VOTE_THRESHOLD", 0.75)
AUTO_DELETE_TIMEOUT = _env_int("AUTO_DELETE_TIMEOUT", 20)

# ---------------------------------------------------------------------------
# Startup retry settings
# ---------------------------------------------------------------------------
# When Discord returns a 5xx error during login, retry with exponential
# backoff instead of crashing immediately.  This prevents systemd crash
# loops during brief Discord outages.
STARTUP_RETRY_MAX_ATTEMPTS = _env_int("STARTUP_RETRY_MAX_ATTEMPTS", 10)
STARTUP_RETRY_BASE_SECONDS = _env_float("STARTUP_RETRY_BASE_SECONDS", 2.0)
STARTUP_RETRY_MAX_SECONDS = _env_float("STARTUP_RETRY_MAX_SECONDS", 300.0)

# ---------------------------------------------------------------------------
# Multi-folder music selection
# ---------------------------------------------------------------------------
# When enabled, the bot owner can configure multiple music folders with
# display names. Users (or just admins, depending on permission) can
# switch between them with the !switch command.
#
# MUSIC_FOLDERS_JSON — JSON mapping of display name → folder path.
#   Example: {"Rock Music": "./music/Rock", "Lofi Beats": "./music/Lofi", "Classical": "./music/Classic"}
#   Leave as "{}" or empty to disable. Falls back to the main MUSIC_FOLDER.
FOLDER_SELECTION_ENABLED = _env_bool("FOLDER_SELECTION_ENABLED", False)

# Who can use !folders and !switch: "all" (anyone) or "admin" (admins only).
FOLDER_SELECTION_PERMISSION = os.getenv("FOLDER_SELECTION_PERMISSION", "admin")

# Parse the JSON mapping — robust parsing with graceful fallback

_raw_folders = os.getenv("MUSIC_FOLDERS_JSON", "{}")
MUSIC_FOLDERS: dict[str, str] = {}
try:
    _parsed = json.loads(_raw_folders)
    if isinstance(_parsed, dict):
        for _name, _path in _parsed.items():
            if isinstance(_name, str) and isinstance(_path, str):
                _resolved = str(Path(_path).resolve())
                MUSIC_FOLDERS[_name] = _resolved
except (json.JSONDecodeError, TypeError) as _exc:
    log.warning("MUSIC_FOLDERS_JSON is invalid JSON: %s", _exc)


# ---------------------------------------------------------------------------
# Auto-sync missing keys from .env.example into the user's .env file.
# This runs every time the bot starts, so users who `git pull` get new
# config options merged in automatically without breaking their setup.
# ---------------------------------------------------------------------------
NEW_ENV_KEYS_ADDED: bool = False
NEW_ENV_KEYS_LIST: list[str] = []


def _sync_env() -> None:
    global NEW_ENV_KEYS_ADDED, NEW_ENV_KEYS_LIST

    example_path = Path(".env.example")
    env_path = Path(".env")

    if not example_path.exists() or not env_path.exists():
        return

    # Parse keys from .env.example (ignore comments and blank lines)
    example_keys: dict[str, str] = {}  # key → raw line
    for line in example_path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if "=" not in stripped:
            continue
        key = stripped.split("=", 1)[0].strip()
        example_keys[key] = stripped

    # Parse keys already in .env
    env_keys: set[str] = set()
    for line in env_path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if "=" not in stripped:
            continue
        key = stripped.split("=", 1)[0].strip()
        env_keys.add(key)

    missing = {k: v for k, v in example_keys.items() if k not in env_keys}
    if not missing:
        return

    # Append missing keys to .env
    try:
        with open(env_path, "a", encoding="utf-8") as f:
            f.write("\n")
            f.write("# ---------------------------------------------------------------------------\n")
            f.write("# Auto-added from .env.example after update — restart the bot to apply.\n")
            f.write("# ---------------------------------------------------------------------------\n")
            for key, raw_line in missing.items():
                f.write(raw_line + "\n")
    except (PermissionError, OSError) as exc:
        log.warning(
            "Could not write new keys to .env (%s). "
            "The following keys need to be added manually: %s",
            exc,
            ", ".join(sorted(missing.keys())),
        )
        return

    NEW_ENV_KEYS_ADDED = True
    NEW_ENV_KEYS_LIST = sorted(missing.keys())
    log.info(
        "Added %d new key(s) to .env: %s",
        len(NEW_ENV_KEYS_LIST),
        ", ".join(NEW_ENV_KEYS_LIST),
    )



# ---------------------------------------------------------------------------
# Startup validation – warn about unconfigured values so the operator
# doesn't have to debug silent failures.
# ---------------------------------------------------------------------------
def _validate_config() -> None:
    """Log warnings for configuration values that are still at their defaults."""
    if not TOKEN:
        log.critical(
            "DISCORD_TOKEN is not set! Create a .env file from .env.example "
            "and paste your bot token from https://discord.com/developers"
        )

    if GUILD_ID == 0:
        log.warning(
            "GUILD_ID is 0 – the bot may not find the correct server. "
            "Set GUILD_ID in your .env file."
        )
    if VOICE_CHANNEL_ID == 0:
        log.warning(
            "VOICE_CHANNEL_ID is 0 – the bot cannot join a voice channel. "
            "Set VOICE_CHANNEL_ID in your .env file."
        )

    if MUSIC_FOLDER and not Path(MUSIC_FOLDER).is_dir():
        log.warning(
            "MUSIC_FOLDER '%s' does not exist. Create the folder and add some "
            "audio files (.mp3, .flac, .m4a, .ogg) before starting the bot.",
            MUSIC_FOLDER,
        )

    if FOLDER_SELECTION_ENABLED:
        if not MUSIC_FOLDERS:
            log.warning(
                "FOLDER_SELECTION_ENABLED is true but MUSIC_FOLDERS_JSON "
                "is empty or invalid. Multi-folder selection will not work."
            )
        for name, path in MUSIC_FOLDERS.items():
            if not Path(path).is_dir():
                log.warning(
                    "Music folder '%s' (path: '%s') does not exist. "
                    "Create the folder and add audio files.",
                    name, path,
                )


_sync_env()
_validate_config()
