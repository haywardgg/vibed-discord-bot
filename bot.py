import asyncio
import glob
import logging
import os
import random
import signal
import sqlite3
from pathlib import Path

import discord
from discord.ext import commands
from mutagen import File as MutagenFile, MutagenError # type: ignore
from mutagen.flac import FLAC
from mutagen.id3 import APIC, ID3 # type: ignore

import config

# ---------------------------------------------------------------------------
# Logging — writes timestamped messages to stdout so you can watch the bot
# work in real time.  Set logging.DEBUG for more verbose output.
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("radio-bot")

# ---------------------------------------------------------------------------
# Bot setup
# ---------------------------------------------------------------------------
# 'message_content' intent lets the bot read text commands (!skip etc.).
# 'voice_states' intent lets it track who is in the voice channel.
intents = discord.Intents.default()
intents.message_content = True
intents.voice_states = True

bot = commands.Bot(command_prefix="!", intents=intents, help_command=None)


# =======================================================================
# Ratings Database (SQLite — persists across bot restarts)
# =======================================================================
#
# Stores two tables in a local 'ratings.db' file:
#   ratings  — cumulative up / down / play counts per track
#   votes    — which user voted which way (enforces 1-vote-per-track)
#
# All writes use WAL (write-ahead logging) for safe concurrent access.
# =======================================================================

class RatingsDB:
    """Persistent SQLite store for track thumb ratings and play counts."""

    def __init__(self, db_path: str = str(Path(__file__).parent / "ratings.db")) -> None:
        self.db_path = db_path
        # connect() creates the file if it doesn't exist yet
        self._conn = sqlite3.connect(db_path)
        self._conn.execute("PRAGMA journal_mode=WAL")

        # Cumulative counts for every track ever seen
        self._conn.execute(
            "CREATE TABLE IF NOT EXISTS ratings ("
            "  track_path TEXT PRIMARY KEY,"
            "  up_count INTEGER NOT NULL DEFAULT 0,"
            "  down_count INTEGER NOT NULL DEFAULT 0,"
            "  play_count INTEGER NOT NULL DEFAULT 0"
            ")"
        )
        # Migrate older DBs that didn't have the play_count column
        try:
            self._conn.execute(
                "ALTER TABLE ratings ADD COLUMN play_count INTEGER NOT NULL DEFAULT 0"
            )
        except sqlite3.OperationalError:
            pass  # column already exists — safe to ignore

        # Per-user voting records — PRIMARY KEY prevents double-voting
        self._conn.execute(
            "CREATE TABLE IF NOT EXISTS votes ("
            "  track_path TEXT NOT NULL,"
            "  user_id INTEGER NOT NULL,"
            "  vote_type TEXT NOT NULL,"
            "  PRIMARY KEY (track_path, user_id)"
            ")"
        )
        self._conn.commit()

    # -------------------------------------------------------------------
    # get_counts() — returns (up, down) for a single track
    # -------------------------------------------------------------------
    def get_counts(self, track_path: str) -> tuple[int, int]:
        row = self._conn.execute(
            "SELECT up_count, down_count FROM ratings WHERE track_path = ?",
            (track_path,),
        ).fetchone()
        if row is None:
            return (0, 0)
        return (row[0], row[1])

    # -------------------------------------------------------------------
    # get_user_vote() — returns 'up', 'down', or None
    # -------------------------------------------------------------------
    def get_user_vote(self, track_path: str, user_id: int) -> str | None:
        row = self._conn.execute(
            "SELECT vote_type FROM votes WHERE track_path = ? AND user_id = ?",
            (track_path, user_id),
        ).fetchone()
        return row[0] if row else None

    # -------------------------------------------------------------------
    # set_vote() — atomically replaces a user's old vote with the new one.
    # vote_type = 'up' | 'down' | None (None removes the vote entirely).
    # -------------------------------------------------------------------
    def set_vote(self, track_path: str, user_id: int, vote_type: str | None) -> None:
        old = self.get_user_vote(track_path, user_id)

        # Decrement old vote from the cumulative counts
        if old == "up":
            self._conn.execute(
                "UPDATE ratings SET up_count = MAX(0, up_count - 1) "
                "WHERE track_path = ?",
                (track_path,),
            )
        elif old == "down":
            self._conn.execute(
                "UPDATE ratings SET down_count = MAX(0, down_count - 1) "
                "WHERE track_path = ?",
                (track_path,),
            )

        # Remove the old per-user row
        self._conn.execute(
            "DELETE FROM votes WHERE track_path = ? AND user_id = ?",
            (track_path, user_id),
        )

        # Insert the new vote (if any)
        if vote_type is not None:
            self._conn.execute(
                "INSERT OR REPLACE INTO votes (track_path, user_id, vote_type) "
                "VALUES (?, ?, ?)",
                (track_path, user_id, vote_type),
            )
            # Make sure a ratings row exists so counts don't start NULL
            self._conn.execute(
                "INSERT OR IGNORE INTO ratings (track_path, up_count, down_count) "
                "VALUES (?, 0, 0)",
                (track_path,),
            )
            if vote_type == "up":
                self._conn.execute(
                    "UPDATE ratings SET up_count = up_count + 1 "
                    "WHERE track_path = ?",
                    (track_path,),
                )
            elif vote_type == "down":
                self._conn.execute(
                    "UPDATE ratings SET down_count = down_count + 1 "
                    "WHERE track_path = ?",
                    (track_path,),
                )

        self._conn.commit()

    # -------------------------------------------------------------------
    # increment_play_count() — called every time a track starts playing.
    # -------------------------------------------------------------------
    def increment_play_count(self, track_path: str) -> None:
        self._conn.execute(
            "INSERT OR IGNORE INTO ratings (track_path, up_count, down_count) "
            "VALUES (?, 0, 0)",
            (track_path,),
        )
        self._conn.execute(
            "UPDATE ratings SET play_count = play_count + 1 WHERE track_path = ?",
            (track_path,),
        )
        self._conn.commit()

    # -------------------------------------------------------------------
    # get_all_stats() — only tracks that have been played at least once.
    # -------------------------------------------------------------------
    def get_all_stats(self) -> list[tuple[str, int, int, int]]:
        rows = self._conn.execute(
            "SELECT track_path, up_count, down_count, play_count "
            "FROM ratings WHERE play_count > 0 "
            "ORDER BY play_count DESC"
        ).fetchall()
        return [(r[0], r[1], r[2], r[3]) for r in rows]

    # -------------------------------------------------------------------
    # get_all_tracks_stats() — ensures every known track has a ratings
    # row, then fetches all of them in the caller's order (used by
    # STATS to show the current shuffle order).
    # -------------------------------------------------------------------
    def get_all_tracks_stats(
        self, all_track_paths: list[str],
    ) -> list[tuple[str, int, int, int]]:
        for path in all_track_paths:
            self._conn.execute(
                "INSERT OR IGNORE INTO ratings (track_path, up_count, down_count) "
                "VALUES (?, 0, 0)",
                (path,),
            )
        self._conn.commit()

        placeholders = ",".join("?" for _ in all_track_paths)
        rows = self._conn.execute(
            f"SELECT track_path, up_count, down_count, play_count "
            f"FROM ratings WHERE track_path IN ({placeholders})",
            all_track_paths,
        ).fetchall()

        # Preserve caller order (e.g. shuffle order from get_all_music_files)
        stats_map = {r[0]: (r[0], r[1], r[2], r[3]) for r in rows}
        return [stats_map[path] for path in all_track_paths if path in stats_map]

    # -------------------------------------------------------------------
    # get_voters() — loads the in-memory up/down sets for the current track.
    # -------------------------------------------------------------------
    def get_voters(self, track_path: str) -> tuple[set[int], set[int]]:
        up: set[int] = set()
        down: set[int] = set()
        for row in self._conn.execute(
            "SELECT user_id, vote_type FROM votes WHERE track_path = ?",
            (track_path,),
        ):
            if row[1] == "up":
                up.add(row[0])
            elif row[1] == "down":
                down.add(row[0])
        return up, down

    def close(self) -> None:
        self._conn.close()


# =======================================================================
# Player Control Button Views
# =======================================================================
#
# Discord's "Views" are the framework for interactive buttons on embeds.
# Each View subclass defines one or more @discord.ui.button callbacks.
#
# Two views are used:
#   VoteView           — temporary YES/NO buttons for voting
#   PlayerControlsView — persistent embed buttons (PREV/NEXT/PAUSE/etc.)
# =======================================================================

class VoteView(discord.ui.View):
    """Temporary YES/NO vote buttons shown during multi-user decisions.

    This view is sent as a standalone message (not attached to the embed).
    It tracks who voted and counts YES/NO.  After VOTE_TIMEOUT_SECONDS
    the _close_vote() coroutine tallies the result.
    """

    def __init__(
        self,
        radio: "RadioManager",
        action: str,
        yes_callback,
        timeout: float | None = None,
    ):
        super().__init__(timeout=timeout)
        self.radio = radio
        self.action = action    # "skip", "prev", "pause"
        self.yes_callback = yes_callback  # called if vote passes
        self.voters: set[int] = set()
        self.yes_count: int = 0
        self.no_count: int = 0

    # ------------------------------------------------------------------
    # YES button — green with thumbs-up emoji
    # ------------------------------------------------------------------
    @discord.ui.button(label="YES", style=discord.ButtonStyle.green, emoji="👍")
    async def yes_button(
        self, interaction: discord.Interaction, button: discord.ui.Button,
    ) -> None:
        if interaction.user.id in self.voters:
            await interaction.response.send_message(
                "You already voted!", ephemeral=True,
            )
            return
        self.voters.add(interaction.user.id)
        self.yes_count += 1
        await interaction.response.send_message(
            f"✅ You voted **YES** for {self.action}.", ephemeral=True,
        )

    # ------------------------------------------------------------------
    # NO button — red with thumbs-down emoji
    # ------------------------------------------------------------------
    @discord.ui.button(label="NO", style=discord.ButtonStyle.red, emoji="👎")
    async def no_button(
        self, interaction: discord.Interaction, button: discord.ui.Button,
    ) -> None:
        if interaction.user.id in self.voters:
            await interaction.response.send_message(
                "You already voted!", ephemeral=True,
            )
            return
        self.voters.add(interaction.user.id)
        self.no_count += 1
        await interaction.response.send_message(
            f"❌ You voted **NO** for {self.action}.", ephemeral=True,
        )


class PlayerControlsView(discord.ui.View):
    """Persistent control buttons attached to every Now Playing embed.

    Row 0: [PREV] [NEXT] [PAUSE/PLAY]
    Row 1: [👍] [👎] [📊 STATS]

    The PAUSE/PLAY button is hidden when 2+ humans are in the channel.
    """

    def __init__(self, radio: "RadioManager"):
        # timeout=None means the buttons never expire (persistent view)
        super().__init__(timeout=None)
        self.radio = radio

    # ------------------------------------------------------------------
    # Helper: count non-bot members in the voice channel
    # ------------------------------------------------------------------
    def _count_humans(self) -> int:
        if not self.radio.voice_client:
            return 0
        ch = self.radio.voice_client.channel
        if ch is None:
            return 0
        return sum(1 for m in ch.members if not m.bot)

    # ------------------------------------------------------------------
    # Auto-delete reply helpers — sends a message then schedules deletion
    # ------------------------------------------------------------------
    async def _reply_autodelete(
        self,
        interaction: discord.Interaction,
        content: str,
        delete_after: int | None = None,
    ) -> None:
        if delete_after is None:
            delete_after = config.AUTO_DELETE_TIMEOUT
        try:
            await interaction.response.send_message(content)
            msg = await interaction.original_response()
            asyncio.create_task(self._delete_after(msg, delete_after))
        except discord.HTTPException:
            pass

    async def _send_autodelete(
        self,
        channel: discord.TextChannel,
        content: str,
        view: discord.ui.View | None = None,
        delete_after: int | None = None,
    ) -> None:
        if delete_after is None:
            delete_after = config.AUTO_DELETE_TIMEOUT
        try:
            kwargs = {}
            if view is not None:
                kwargs["view"] = view
            msg = await channel.send(content, **kwargs)
            asyncio.create_task(self._delete_after(msg, delete_after))
        except discord.HTTPException:
            pass

    @staticmethod
    async def _delete_after(msg: discord.Message, delay: int) -> None:
        await asyncio.sleep(delay)
        try:
            await msg.delete()
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            pass  # message already deleted — not an error

    # ------------------------------------------------------------------
    # Helper: quick access to the configured text channel
    # ------------------------------------------------------------------
    async def _get_text_channel(self) -> discord.TextChannel | None:
        return await self.radio.get_text_channel()

    # ------------------------------------------------------------------
    # Core voting dispatcher — solo users skip the vote entirely;
    # 2+ users trigger a 10-second YES/NO vote.
    # ------------------------------------------------------------------
    async def _maybe_vote(
        self,
        interaction: discord.Interaction,
        action: str,
        action_name: str,
        execute_callback,
    ) -> None:
        humans = self._count_humans()

        if humans == 0:
            await self._reply_autodelete(
                interaction, "❌ Nobody is listening right now.",
            )
            return

        if humans == 1:
            # Solo listener — run immediately, no voting needed
            await execute_callback(interaction)
            return

        # 2+ humans — open a vote
        channel = await self._get_text_channel()
        if channel is None:
            await self._reply_autodelete(
                interaction, "❌ Could not find text channel.",
            )
            return

        # Don't stack votes on top of each other
        if self.radio.active_vote is not None:
            await interaction.response.defer()
            return

        # Acknowledge button press so Discord doesn't show
        # "This interaction failed" during the vote.
        await interaction.response.defer()

        vote_view = VoteView(
            self.radio,
            action=action,
            yes_callback=execute_callback,
            timeout=config.VOTE_TIMEOUT_SECONDS,
        )

        vote_msg = await channel.send(
            f"🗳️ **Vote: {action_name}?**\n"
            f"Need **{int(config.VOTE_THRESHOLD * 100)}% YES** to pass.\n"
            f"Vote closes in {config.VOTE_TIMEOUT_SECONDS}s",
            view=vote_view,
        )

        self.radio.active_vote = {
            "view": vote_view,
            "message": vote_msg,
            "action": action,
            "callback": execute_callback,
            "interaction": interaction,
        }

        # Schedule the vote closing coroutine
        asyncio.create_task(
            self._close_vote(vote_msg, vote_view, action, interaction),
        )

    async def _close_vote(
        self,
        vote_msg: discord.Message,
        vote_view: VoteView,
        action: str,
        original_interaction: discord.Interaction,
    ) -> None:
        """Called after VOTE_TIMEOUT_SECONDS to disable buttons and tally."""
        await asyncio.sleep(config.VOTE_TIMEOUT_SECONDS)

        # Disable all buttons so no further votes can be cast
        for child in vote_view.children:
            if isinstance(child, discord.ui.Button):
                child.disabled = True
        try:
            await vote_msg.edit(view=vote_view)
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            pass

        if self.radio.active_vote is not None:
            self.radio.active_vote = None

        total = vote_view.yes_count + vote_view.no_count

        if total == 0:
            channel = await self._get_text_channel()
            if channel:
                await self._send_autodelete(
                    channel, "❌ **Vote failed** — nobody voted.",
                )
            return

        ratio = vote_view.yes_count / total

        if ratio >= config.VOTE_THRESHOLD:
            channel = await self._get_text_channel()
            if channel:
                await self._send_autodelete(
                    channel,
                    f"✅ **Vote passed!** ({vote_view.yes_count}/{total} YES) "
                    f"— {action}ing now.",
                )
            await vote_view.yes_callback(original_interaction)
        else:
            channel = await self._get_text_channel()
            if channel:
                await self._send_autodelete(
                    channel,
                    f"❌ **Vote failed** — only {vote_view.yes_count}/{total} YES "
                    f"({ratio:.0%}), needed {config.VOTE_THRESHOLD:.0%}.",
                )

        # Clean up the vote message after a short delay
        asyncio.create_task(self._delete_after(vote_msg, config.AUTO_DELETE_TIMEOUT))

    # ------------------------------------------------------------------
    # PREV button — moves backward through track history
    # ------------------------------------------------------------------
    @discord.ui.button(
        label="PREV", style=discord.ButtonStyle.blurple, emoji="⏮", row=0,
    )
    async def prev_button(
        self, interaction: discord.Interaction, button: discord.ui.Button,
    ) -> None:
        action_name = "Go back to previous track"

        async def execute(inter: discord.Interaction) -> None:
            if not self.radio.track_history:
                await self._reply_autodelete(
                    inter, "❌ No previous track in history.",
                )
                return

            if self.radio.voice_client is None:
                await self._reply_autodelete(
                    inter, "❌ Bot is not connected to voice.",
                )
                return

            # Pop the most recent entry from history, save current to it
            current_path = self.radio.current_song_path
            prev_path = self.radio.track_history.pop()

            if current_path and current_path != prev_path:
                self.radio.track_history.append(current_path)

            self.radio.music_queue.insert(0, prev_path)

            # Flag prevents play_count from incrementing on PREV revisits
            self.radio._preved = True

            if (
                self.radio.voice_client.is_playing()
                or self.radio.voice_client.is_paused()
            ):
                self.radio.voice_client.stop()

            await self._reply_autodelete(
                inter, "⏮ Going back to previous track",
            )

        await self._maybe_vote(interaction, "prev", action_name, execute)

    # ------------------------------------------------------------------
    # NEXT button — skips to the next track in the queue
    # ------------------------------------------------------------------
    @discord.ui.button(
        label="NEXT", style=discord.ButtonStyle.blurple, emoji="⏭", row=0,
    )
    async def next_button(
        self, interaction: discord.Interaction, button: discord.ui.Button,
    ) -> None:
        action_name = "Skip to next track"

        async def execute(inter: discord.Interaction) -> None:
            # Save current to history before skipping away
            if (
                self.radio.current_song_path
                and self.radio.current_song_path not in self.radio.track_history
            ):
                self.radio.track_history.append(self.radio.current_song_path)

            if self.radio.voice_client and self.radio.voice_client.is_playing():
                self.radio.voice_client.stop()
                await self._reply_autodelete(inter, "⏭ Skipped to next song")
            elif (
                self.radio.voice_client
                and self.radio.voice_client.is_paused()
            ):
                self.radio.voice_client.stop()
                await self._reply_autodelete(inter, "⏭ Skipped to next song")
            elif self.radio.is_afk_disconnected:
                await self._reply_autodelete(
                    inter, "❌ Bot is not in voice channel — nothing to skip.",
                )
            else:
                await self._reply_autodelete(
                    inter, "❌ Nothing is playing right now.",
                )

        await self._maybe_vote(interaction, "skip", action_name, execute)

    # ------------------------------------------------------------------
    # PAUSE / PLAY button — toggles pause state (solo-listeners only)
    # ------------------------------------------------------------------
    @discord.ui.button(
        label="PAUSE / PLAY",
        style=discord.ButtonStyle.grey,
        emoji="⏯️",
        row=0,
    )
    async def pause_play_button(
        self, interaction: discord.Interaction, button: discord.ui.Button,
    ) -> None:
        humans = self._count_humans()

        # Pause is inherently a solo feature — reject with >1 human
        if humans > 1:
            await interaction.response.send_message(
                "⏯️ Pause/Play not available with multiple listeners.",
                ephemeral=True,
            )
            return

        action_name = "Pause" if (
            self.radio.voice_client
            and self.radio.voice_client.is_playing()
        ) else "Resume"

        async def execute(inter: discord.Interaction) -> None:
            await self.radio.toggle_pause()
            status = (
                "⏸️ Paused"
                if (
                    self.radio.voice_client
                    and self.radio.voice_client.is_paused()
                )
                else "▶️ Resumed"
            )
            await self._reply_autodelete(inter, status)

        await self._maybe_vote(interaction, "pause", action_name, execute)

    # ------------------------------------------------------------------
    # 👍 / 👎 Track rating buttons — one vote per user per track.
    # Clicking the opposite button switches your vote.
    # Clicking the same button again removes your vote entirely.
    # ------------------------------------------------------------------
    @discord.ui.button(
        label="", style=discord.ButtonStyle.green, emoji="👍", row=1,
    )
    async def thumbs_up_button(
        self, interaction: discord.Interaction, button: discord.ui.Button,
    ) -> None:
        # defer() prevents Discord's "This interaction failed" timeout
        await interaction.response.defer()

        user_id = interaction.user.id
        track = self.radio.current_song_path
        if track is None:
            return

        current_vote = self.radio.ratings_db.get_user_vote(track, user_id)

        if current_vote == "down":
            self.radio.ratings_db.set_vote(track, user_id, "up")
            self.radio.rating_down.discard(user_id)
            self.radio.rating_up.add(user_id)
        elif current_vote == "up":
            self.radio.ratings_db.set_vote(track, user_id, None)
            self.radio.rating_up.discard(user_id)
        else:
            self.radio.ratings_db.set_vote(track, user_id, "up")
            self.radio.rating_up.add(user_id)

        await self._update_rating_labels(interaction)

    @discord.ui.button(
        label="", style=discord.ButtonStyle.red, emoji="👎", row=1,
    )
    async def thumbs_down_button(
        self, interaction: discord.Interaction, button: discord.ui.Button,
    ) -> None:
        await interaction.response.defer()

        user_id = interaction.user.id
        track = self.radio.current_song_path
        if track is None:
            return

        current_vote = self.radio.ratings_db.get_user_vote(track, user_id)

        if current_vote == "up":
            self.radio.ratings_db.set_vote(track, user_id, "down")
            self.radio.rating_up.discard(user_id)
            self.radio.rating_down.add(user_id)
        elif current_vote == "down":
            self.radio.ratings_db.set_vote(track, user_id, None)
            self.radio.rating_down.discard(user_id)
        else:
            self.radio.ratings_db.set_vote(track, user_id, "down")
            self.radio.rating_down.add(user_id)

        await self._update_rating_labels(interaction)

    # ------------------------------------------------------------------
    # STATS button — prints the full track list with vote/play stats.
    # Uses a monospace code block for perfect column alignment.
    # Only one stats embed can exist at a time (guarded by stats_message).
    # ------------------------------------------------------------------
    @discord.ui.button(
        label="STATS", style=discord.ButtonStyle.grey, emoji="📊", row=1,
    )
    async def stats_button(
        self, interaction: discord.Interaction, button: discord.ui.Button,
    ) -> None:
        # Prevent spamming — reject if stats are already being loaded or visible.
        # Set _stats_loading immediately (before any await) to close the race
        # window where two concurrent button presses both see False and proceed.
        if self.radio._stats_loading or self.radio.stats_message is not None:
            await interaction.response.defer()
            return
        self.radio._stats_loading = True

        await interaction.response.defer()

        channel = await self._get_text_channel()
        if channel is None:
            self.radio._stats_loading = False
            return

        # Get every audio file in the music folder
        all_paths = RadioManager.get_all_music_files()
        if not all_paths:
            await channel.send(
                "📊 No music files found.", delete_after=60,
            )
            self.radio._stats_loading = False
            return

        stats = self.radio.ratings_db.get_all_tracks_stats(all_paths)

        # ---- Build the monospace table ----
        # Step 1: find the longest title so we can left-pad consistently
        titles: list[str] = []
        for path, up, down, plays in stats:
            info = self.radio.get_audio_info(path)
            titles.append(info['title'])

        max_title_len = max(len(t) for t in titles) if titles else 0

        # Step 2: build each line with right-aligned numbers
        all_lines: list[str] = []
        for i, (path, up, down, plays) in enumerate(stats):
            title = titles[i]
            padded = title.ljust(max_title_len + 2)
            all_lines.append(
                f"{padded} │ 👍 {str(up).rjust(3)} │ 👎 {str(down).rjust(3)} "
                f"│ ▶ {str(plays).rjust(3)}"
            )

        # Step 3: paginate — 25 tracks per embed page
        per_page = 25
        pages: list[str] = []
        for idx in range(0, len(all_lines), per_page):
            chunk = "\n".join(all_lines[idx:idx + per_page])
            pages.append(f"```\n{chunk}\n```")

        # Step 4: send page 1, track the message for the "one at a time" guard
        embed = discord.Embed(
            title="📊 Full Track List",
            description=pages[0],
            color=discord.Color.gold(),
        )
        embed.set_footer(
            text=f"{len(stats)} tracks  •  Page 1/{len(pages)}  •  Auto-deletes in 60s"
        )
        self.radio.stats_message = await channel.send(embed=embed)

        # Remaining pages as follow-up embeds
        for page_num, page_desc in enumerate(pages[1:], 2):
            embed2 = discord.Embed(
                title="📊 Full Track List (continued)",
                description=page_desc,
                color=discord.Color.gold(),
            )
            embed2.set_footer(
                text=f"Page {page_num}/{len(pages)}  •  Auto-deletes in 60s"
            )
            await channel.send(embed=embed2, delete_after=60)

        # Clean up after 1 minute
        asyncio.create_task(self._delete_after(self.radio.stats_message, 60))
        asyncio.create_task(self._clear_stats_tracker(60))

    async def _clear_stats_tracker(self, delay: int) -> None:
        await asyncio.sleep(delay)
        self.radio.stats_message = None
        self.radio._stats_loading = False

    # ------------------------------------------------------------------
    # _update_rating_labels() — refreshes the 👍/👎 button text with counts
    # ------------------------------------------------------------------
    async def _update_rating_labels(
        self, interaction: discord.Interaction,
    ) -> None:
        up_count = len(self.radio.rating_up)
        down_count = len(self.radio.rating_down)

        for child in self.children:
            if isinstance(child, discord.ui.Button):
                if child.emoji and str(child.emoji) == "👍":
                    child.label = str(up_count) if up_count > 0 else ""
                elif child.emoji and str(child.emoji) == "👎":
                    child.label = str(down_count) if down_count > 0 else ""

        try:
            if interaction.message is not None:
                await interaction.message.edit(view=self)
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            pass


# =======================================================================
# RadioManager — the single source of truth for all runtime state.
# =======================================================================
# There is exactly one RadioManager instance (global 'radio').
# It holds the queue, current track, volume, voice client, embed
# reference, AFK state, and rating sets.  All playback transitions
# flow through play_next() under asyncio.Lock for thread safety.
# =======================================================================

class RadioManager:
    """Holds all mutable radio state and the core playback / AFK logic."""

    def __init__(self) -> None:
        # ---- Queue & current track ----
        self.music_queue: list[str] = []
        self.current_song: str | None = None        # "Artist - Title"
        self.current_song_path: str | None = None     # filesystem path
        self.current_song_art: bytes | None = None    # embedded cover art

        # ---- PREV navigation ----
        self.track_history: list[str] = []    # max 50 entries

        # ---- Playback controls ----
        self.volume: float = config.DEFAULT_VOLUME
        self.voice_client: discord.VoiceClient | None = None
        self.current_embed_message: discord.Message | None = None

        # ---- Embedded button view reference ----
        self.controls_view: PlayerControlsView | None = None

        # ---- AFK state ----
        self.is_afk_disconnected: bool = False
        self.afk_timer_task: asyncio.Task | None = None
        self._monitor_task: asyncio.Task | None = None

        # ---- Concurrency safety ----
        self._lock = asyncio.Lock()

        # ---- Connection state ----
        self._is_connecting: bool = False
        self._last_reconnect_time: float = 0.0
        self._reconnect_cooldown: float = 10.0

        # ---- Voting state ----
        self.active_vote: dict | None = None

        # ---- Stats output tracking ----
        self.stats_message: discord.Message | None = None
        self._stats_loading: bool = False  # guards the stats button against races

        # ---- PREV tracking (prevents double-counting play stats) ----
        self._preved: bool = False

        # ---- Pause state (prevents auto-resume from undoing manual pause) ----
        self._manual_pause: bool = False

        # ---- Multi-folder selection ----
        self.active_folder_name: str | None = None
        self.active_folder_path: str | None = None

        # ---- Track ratings (in-memory, loaded from DB on track change) ----
        self.ratings_db = RatingsDB(str(Path(__file__).parent / "ratings.db"))
        self.rating_up: set[int] = set()
        self.rating_down: set[int] = set()

    # -------------------------------------------------------------------
    # Resolve the current music folder — active selection wins, else default
    # -------------------------------------------------------------------
    def _get_current_music_folder(self) -> str:
        """Return the path to use for scanning music files."""
        if (
            config.FOLDER_SELECTION_ENABLED
            and self.active_folder_path
            and self.active_folder_path != config.MUSIC_FOLDER
        ):
            return self.active_folder_path
        return config.MUSIC_FOLDER

    # -------------------------------------------------------------------
    # Album art — tries embedded tags first, then common filenames
    # -------------------------------------------------------------------

    @staticmethod
    def get_album_art(file_path: str) -> bytes | None:
        """Extract embedded album art from FLAC/MP3 or a local image file."""
        try:
            audio = MutagenFile(file_path)
        except (MutagenError, OSError, ValueError):
            log.warning("Mutagen could not open %s", file_path)
            return None

        if audio is None:
            return None

        try:
            if isinstance(audio, FLAC) and audio.pictures:
                return audio.pictures[0].data

            if isinstance(audio, ID3):
                for tag in audio.values():
                    if isinstance(tag, APIC):
                        return getattr(tag, "data", None)

            elif hasattr(audio, "tags") and audio.tags:
                values_method = getattr(audio.tags, "values", None)  # type: ignore[attr-defined]
                if callable(values_method):
                    for tag in values_method():  # type: ignore
                        if isinstance(tag, APIC):
                            return getattr(tag, "data", None)
        except (KeyError, AttributeError, IndexError):
            pass

        # Fallback: look for image files in the same folder
        folder = Path(file_path).parent
        for name in (
            "cover.jpg", "cover.png", "folder.jpg", "albumart.jpg", "front.jpg",
        ):
            cover = folder / name
            if cover.is_file():
                try:
                    return cover.read_bytes()
                except OSError:
                    log.debug("Could not read %s", cover)

        return None

    # -------------------------------------------------------------------
    # Audio metadata — reads artist / title / album / duration from tags
    # -------------------------------------------------------------------

    @staticmethod
    def _tag_str(tags, keys) -> str:
        """Return the first non-empty text value from *tags* for *keys*."""
        for key in keys:
            val = tags.get(key)
            if val is None:
                continue
            if hasattr(val, "text"):
                texts = val.text
                if isinstance(texts, (list, tuple)):
                    return ", ".join(str(t) for t in texts if t)
                return str(texts)
            return str(val)
        return ""

    @staticmethod
    def get_audio_info(file_path: str) -> dict:
        """Return {artist, title, album, duration} for *file_path*."""
        try:
            audio = MutagenFile(file_path)
        except (MutagenError, OSError, ValueError):
            log.warning("Mutagen cannot read %s", file_path)
            audio = None

        filename = Path(file_path).stem

        if audio is None or not hasattr(audio, "tags") or audio.tags is None:
            return {
                "artist": "Unknown Artist",
                "title": filename,
                "album": "Unknown Album",
                "duration": 0,
            }

        artist = (
            RadioManager._tag_str(audio.tags, ["TPE1", "artist", "ARTIST", "©ART"])
            or "Unknown Artist"
        )
        title = (
            RadioManager._tag_str(audio.tags, ["TIT2", "title", "TITLE", "©nam"])
            or filename
        )
        album = (
            RadioManager._tag_str(audio.tags, ["TALB", "album", "ALBUM", "©alb"])
            or "Unknown Album"
        )

        try:
            duration = int(audio.info.length) if audio.info else 0
        except (AttributeError, TypeError, ValueError):
            duration = 0

        return {
            "artist": artist,
            "title": title,
            "album": album,
            "duration": duration,
        }

    # -------------------------------------------------------------------
    # Helpers — format durations, scan for music files, resolve channels
    # -------------------------------------------------------------------

    @staticmethod
    def format_duration(seconds: int) -> str:
        return f"{seconds // 60}:{seconds % 60:02d}"

    @staticmethod
    def get_all_music_files(folder_path: str | None = None) -> list[str]:
        """Glob *folder_path* (or the default MUSIC_FOLDER) for audio files (shuffled)."""
        if folder_path is None:
            folder_path = config.MUSIC_FOLDER
        music_files: list[str] = []
        extensions = [
            "*.mp3", "*.flac", "*.m4a", "*.ogg",
        ]
        for ext in extensions:
            music_files.extend(
                glob.glob(
                    os.path.join(folder_path, ext),
                ),
            )
            # Also match uppercase variants on Unix (Windows glob is
            # already case-insensitive).
            music_files.extend(
                glob.glob(
                    os.path.join(folder_path, ext.upper()),
                ),
            )
        # Deduplicate — prevents the same file from appearing twice
        # in the queue, which would skew play counts 2× for some tracks.
        music_files = list(dict.fromkeys(music_files))
        random.shuffle(music_files)
        return music_files

    async def get_text_channel(self) -> discord.TextChannel | None:
        """Resolve the configured text channel from guild + channel IDs."""
        guild = bot.get_guild(config.GUILD_ID)
        if guild is None:
            log.warning("Guild %s not found", config.GUILD_ID)
            return None
        channel = guild.get_channel(config.TEXT_CHANNEL_ID)
        if channel is None:
            log.warning("Text channel %s not found", config.TEXT_CHANNEL_ID)
        # Pylance can't statically narrow GuildChannel → TextChannel,
        # but the configured channel is always a text channel at runtime.
        return channel  # type: ignore[return-value]

    # -------------------------------------------------------------------
    # Embed management — sends updates to the text channel
    # -------------------------------------------------------------------

    async def delete_current_embed(self) -> None:
        """Safely delete the currently pinned embed message."""
        if self.current_embed_message is not None:
            try:
                await self.current_embed_message.delete()
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                log.debug("Could not delete previous embed (already gone?)")
            self.current_embed_message = None

    async def update_now_playing(
        self,
        song_path: str,
        song_info: dict,
        artwork_data: bytes | None = None,
        is_paused: bool = False,
        status_message: str | None = None,
    ) -> None:
        """Post/refresh the Now Playing embed and update voice channel status."""
        channel = await self.get_text_channel()
        if channel is None:
            return

        # Choose colours: blue = playing, orange = paused/AFK
        if is_paused:
            color = discord.Color.orange()
            voice_status = config.VOICE_IDLE_STATUS
        else:
            color = discord.Color.blue()
            voice_status = f"🎧 {song_info['title']}"

        # Update the Discord voice-channel status line
        if self.voice_client and self.voice_client.channel:
            try:
                await self.voice_client.channel.edit(status=voice_status)  # type: ignore[call-overload]
            except (discord.Forbidden, discord.HTTPException) as exc:
                log.warning("Could not update voice channel status: %s", exc)

        log.info("Now Playing: %s - %s", song_info["artist"], song_info["title"])

        if not config.SHOW_NOW_PLAYING:
            await self.delete_current_embed()
            return

        await self.delete_current_embed()

        # Build the embed title — prefix with ⏸ if paused
        if is_paused:
            title_text = f"⏸️ {song_info['title']}"
        elif (
            self.voice_client
            and self.voice_client.is_paused()
            and not self.is_afk_disconnected
        ):
            title_text = f"⏸️ {song_info['title']}"
        else:
            title_text = song_info['title']

        # Build the metadata description from the configurable format string.
        # Operators can set METADATA_FORMAT in .env to control what appears
        # on the metadata line.  Supported placeholders:
        #   {artist}  {title}  {album}  {duration}
        duration_str = ""
        if song_info.get("duration", 0) > 0:
            duration_str = self.format_duration(song_info['duration'])

        fmt = config.METADATA_FORMAT
        desc = (
            fmt.replace("{artist}", song_info['artist'])
               .replace("{title}", song_info['title'])
               .replace("{album}", song_info['album'])
               .replace("{duration}", duration_str)
        ).strip()

        # Remove any " · " spacer artifacts when fields resolve to empty
        desc = desc.replace(" ·  · ", " · ").replace("  · ", " · ").replace(" ·  ·", " · ")
        while " ·  · " in desc:
            desc = desc.replace(" ·  · ", " · ")

        if is_paused:
            if status_message:
                note = status_message
            else:
                note = "Waiting for listeners…"
            if desc:
                desc = f"{desc}\n{note}"
            else:
                desc = note

        # Footer: "Up Next: …"
        footer_text: str | None = None
        if config.SHOW_UP_NEXT:
            if self.music_queue:
                next_path = self.music_queue[0]
                next_info = self.get_audio_info(next_path)
                footer_text = f"Up Next: {next_info['title']}"
            else:
                footer_text = "Up Next: Queue empty (refilling...)"

        embed = discord.Embed(
            title=title_text,
            description=desc,
            color=color,
        )
        embed.set_author(
            name="Vibed Discord Bot",
            url="https://github.com/haywardgg/vibed-discord-bot",
        )
        # Build footer text — includes "Up Next:" and optional folder indicator
        footer_parts: list[str] = []
        if config.FOLDER_SELECTION_ENABLED and self.active_folder_name:
            footer_parts.append(f"📂 {self.active_folder_name}")
        if footer_text:
            footer_parts.append(footer_text)
        if footer_parts:
            embed.set_footer(text="  •  ".join(footer_parts))

        # Attach banner image and/or album art
        files: list[discord.File] = []
        temp_files: list[Path] = []
        temp_dir = Path(os.environ.get("TEMP", "/tmp"))

        if config.HEADER_IMAGE_PATH:
            header_path = Path(config.HEADER_IMAGE_PATH)
            if header_path.is_file():
                try:
                    header_file = discord.File(str(header_path), filename="header.jpg")
                    files.append(header_file)
                    embed.set_image(url="attachment://header.jpg")
                except OSError:
                    log.warning("Could not read header image from %s", header_path)

        if config.SHOW_ALBUM_ART and artwork_data:
            temp_art = temp_dir / "radio_bot_album_art.png"
            try:
                temp_art.write_bytes(artwork_data)
                temp_files.append(temp_art)
                art_file = discord.File(str(temp_art), filename="albumart.png")
                files.append(art_file)
                embed.set_thumbnail(url="attachment://albumart.png")
            except Exception:
                log.exception("Failed to write artwork temp file; skipping thumbnail")

        # Build the interactive button view
        view = await self._build_controls_view()

        try:
            self.current_embed_message = await channel.send(
                embed=embed, files=files, view=view,
            )
        except Exception:
            log.exception("Failed to send embed; falling back without attachments")
            self.current_embed_message = await channel.send(
                embed=embed, view=view,
            )
        finally:
            for tf in temp_files:
                try:
                    tf.unlink(missing_ok=True)
                except OSError:
                    pass

    async def _build_controls_view(self) -> PlayerControlsView:
        """Build a PlayerControlsView, hiding PAUSE/PLAY when 2+ humans."""
        view = PlayerControlsView(self)
        self.controls_view = view

        humans = 0
        if self.voice_client and self.voice_client.channel:
            humans = sum(
                1 for m in self.voice_client.channel.members if not m.bot
            )

        # Pre-load the rating button labels from the in-memory sets
        up_count = len(self.rating_up)
        down_count = len(self.rating_down)
        for child in view.children:
            if isinstance(child, discord.ui.Button):
                if child.emoji and str(child.emoji) == "👍":
                    child.label = str(up_count) if up_count > 0 else ""
                elif child.emoji and str(child.emoji) == "👎":
                    child.label = str(down_count) if down_count > 0 else ""

        # PAUSE/PLAY is a solo-listener feature — hide it with multiple users
        if humans > 1:
            for child in list(view.children):
                if isinstance(child, discord.ui.Button) and getattr(getattr(child, "callback", None), "__name__", "") == "pause_play_button":
                    view.remove_item(child)
                    break

        return view

    # -------------------------------------------------------------------
    # Track history — used by the PREV button
    # -------------------------------------------------------------------

    def add_to_history(self, file_path: str) -> None:
        """Push *file_path* onto the history stack (max 50 entries)."""
        if file_path and (
            not self.track_history
            or self.track_history[-1] != file_path
        ):
            self.track_history.append(file_path)
            if len(self.track_history) > 50:
                self.track_history = self.track_history[-50:]

    # -------------------------------------------------------------------
    # Auto-delete helper — sends a message that disappears after N seconds
    # -------------------------------------------------------------------

    async def send_autodelete(
        self,
        channel: discord.TextChannel,
        content: str,
        **kwargs,
    ) -> None:
        try:
            msg = await channel.send(content, **kwargs)
            asyncio.create_task(self._delete_after(msg, config.AUTO_DELETE_TIMEOUT))
        except discord.HTTPException:
            pass

    @staticmethod
    async def _delete_after(msg: discord.Message, delay: int) -> None:
        await asyncio.sleep(delay)
        try:
            await msg.delete()
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            pass

    # ===================================================================
    # PLAYBACK CORE
    # ===================================================================
    # play_next() is the central playback function.  Every track
    # transition — natural end, NEXT, PREV — flows through here.
    # It dequeues the next file, reads metadata, increments the play
    # count (unless _preved is True), posts the embed, and starts
    # FFmpeg streaming with an after= callback that loops back.
    # ===================================================================

    async def play_next(self) -> None:
        """Dequeue the next track and start FFmpeg playback.

        CALLERS MUST HOLD self._lock before calling this method.
        """

        if self.is_afk_disconnected:
            return

        if not (self.voice_client and self.voice_client.is_connected()):
            log.warning("Voice client is not connected – cannot start playback")
            return

        if not self.music_queue:
            folder = self._get_current_music_folder()
            self.music_queue = self.get_all_music_files(folder)
            if not self.music_queue:
                log.error("No music files found in %s", folder)
                channel = await self.get_text_channel()
                if channel:
                    await channel.send("❌ No music files found!")
                return

        # Move current track to history before replacing it
        if self.current_song_path:
            self.add_to_history(self.current_song_path)

        self.current_song_path = self.music_queue.pop(0)

        # Restore any historical votes from the database
        self.rating_up, self.rating_down = self.ratings_db.get_voters(
            self.current_song_path,
        )

        song_info = self.get_audio_info(self.current_song_path)
        artwork_data = (
            self.get_album_art(self.current_song_path)
            if config.SHOW_ALBUM_ART
            else None
        )

        self.current_song = f"{song_info['artist']} - {song_info['title']}"
        self.current_song_art = artwork_data

        # Clear any manual pause flag when a new track starts
        self._manual_pause = False

        # Track the play — skip increment if we came here via PREV
        if not self._preved:
            self.ratings_db.increment_play_count(self.current_song_path)
        self._preved = False

        await self.update_now_playing(
            self.current_song_path, song_info, artwork_data,
        )

        log.info(
            "Playing: %s at %d%% volume",
            self.current_song,
            int(self.volume * 100),
        )

        # FFmpeg source — converts any format to raw PCM for Discord
        source = discord.FFmpegPCMAudio(
            self.current_song_path,
            before_options="-nostdin",
            options="-vn -af aresample=48000",
        )
        source = discord.PCMVolumeTransformer(source, volume=self.volume)

        def after_playing(error: Exception | None) -> None:
            if error:
                log.error("Playback error: %s", error)
            # Schedule the next track on the asyncio event loop
            asyncio.run_coroutine_threadsafe(
                self._locked_play_next(), bot.loop,
            )

        self.voice_client.play(source, after=after_playing)

    async def _locked_play_next(self) -> None:
        """Acquire the lock, then call play_next()."""
        async with self._lock:
            await self.play_next()

    # -------------------------------------------------------------------
    # Pause / Resume — toggles the voice client and refreshes the embed
    # -------------------------------------------------------------------

    async def toggle_pause(self) -> None:
        if self.voice_client is None:
            return

        if self.voice_client.is_playing():
            self.voice_client.pause()
            self._manual_pause = True
            log.info("Playback paused")
            if self.current_song_path:
                info = self.get_audio_info(self.current_song_path)
                art = (
                    self.get_album_art(self.current_song_path)
                    if config.SHOW_ALBUM_ART
                    else None
                )
                await self.update_now_playing(
                    self.current_song_path, info, art, is_paused=True,
                )
        elif self.voice_client.is_paused():
            self.voice_client.resume()
            self._manual_pause = False
            log.info("Playback resumed")
            if self.current_song_path:
                info = self.get_audio_info(self.current_song_path)
                art = (
                    self.get_album_art(self.current_song_path)
                    if config.SHOW_ALBUM_ART
                    else None
                )
                await self.update_now_playing(
                    self.current_song_path, info, art, is_paused=False,
                )

    # -------------------------------------------------------------------
    # AFK — if the channel is empty for AFK_TIMEOUT_SECONDS, disconnect
    # -------------------------------------------------------------------

    async def afk_timeout(self) -> None:
        await asyncio.sleep(config.AFK_TIMEOUT_SECONDS)

        if (
            not self.is_afk_disconnected
            and self.voice_client
            and self.voice_client.is_connected()
        ):
            ch = self.voice_client.channel
            if ch and not any(not m.bot for m in ch.members):
                log.info(
                    "Channel empty for %ds – pausing playback",
                    config.AFK_TIMEOUT_SECONDS,
                )

                try:
                    await ch.edit(status=config.VOICE_IDLE_STATUS)  # type: ignore[call-overload]
                except (discord.Forbidden, discord.HTTPException) as exc:
                    log.warning("Could not update channel status: %s", exc)

                # Pause playback
                if self.voice_client.is_playing():
                    self.voice_client.pause()

                # Post one final embed showing the paused state
                channel = await self.get_text_channel()
                if channel and self.current_song_path:
                    info = self.get_audio_info(self.current_song_path)
                    art = (
                        self.get_album_art(self.current_song_path)
                        if config.SHOW_ALBUM_ART
                        else None
                    )
                    await self.update_now_playing(
                        self.current_song_path, info, art, is_paused=True,
                    )

                if config.AFK_AUTO_LEAVE:
                    self.is_afk_disconnected = True
                    if self._monitor_task and not self._monitor_task.done():
                        self._monitor_task.cancel()
                        self._monitor_task = None
                    await self.voice_client.disconnect()
                    self.voice_client = None

                    self._monitor_task = asyncio.create_task(
                        self.check_channel_activity(),
                    )
                else:
                    log.info(
                        "AFK_AUTO_LEAVE is false – staying in channel while paused",
                    )

    async def check_channel_activity(self) -> None:
        """5-second polling loop that manages AFK timers and auto-resume."""
        while True:
            await asyncio.sleep(5)

            guild = bot.get_guild(config.GUILD_ID)
            if guild is None:
                continue

            voice_channel = guild.get_channel(config.VOICE_CHANNEL_ID)
            if voice_channel is None:
                continue
            if not isinstance(voice_channel, (discord.VoiceChannel, discord.StageChannel)):
                continue

            has_humans = any(not m.bot for m in voice_channel.members)

            if not has_humans:
                if (
                    self.voice_client
                    and self.voice_client.is_connected()
                    and not self.is_afk_disconnected
                ):
                    if (
                        config.AFK_PAUSE_ON_EMPTY
                        and (
                            self.afk_timer_task is None
                            or self.afk_timer_task.done()
                        )
                    ):
                        log.info(
                            "Channel empty – starting %ds AFK timer",
                            config.AFK_TIMEOUT_SECONDS,
                        )
                        self.afk_timer_task = asyncio.create_task(
                            self.afk_timeout(),
                        )
            else:
                # Cancel AFK timer if someone joined
                if self.afk_timer_task and not self.afk_timer_task.done():
                    self.afk_timer_task.cancel()
                    self.afk_timer_task = None
                    log.info("AFK timer cancelled (listener joined)")

                # Auto-resume playback if it was paused and someone joins
                # (only if the pause was NOT a manual user pause)
                if (
                    self.voice_client
                    and self.voice_client.is_connected()
                    and self.voice_client.is_paused()
                    and not self.is_afk_disconnected
                    and not self._manual_pause
                ):
                    log.info(
                        "Listener joined while paused – auto-resuming playback",
                    )
                    self.voice_client.resume()
                    if self.current_song_path:
                        info = self.get_audio_info(self.current_song_path)
                        art = (
                            self.get_album_art(self.current_song_path)
                            if config.SHOW_ALBUM_ART
                            else None
                        )
                        await self.update_now_playing(
                            self.current_song_path, info, art, is_paused=False,
                        )

                # Rejoin if we left due to AFK
                if self.is_afk_disconnected:
                    log.info(
                        "Listener joined empty channel – rejoining and resuming",
                    )
                    self.is_afk_disconnected = False
                    await self.start_radio()

    # -------------------------------------------------------------------
    # Multi-folder switching — called by the !switch command
    # -------------------------------------------------------------------

    async def switch_folder(self, display_name: str) -> str:
        """Switch to a different music folder. Returns a human-readable result.

        Looks up *display_name* (case-insensitive) in config.MUSIC_FOLDERS,
        sets active_folder_name/path, refills the queue, and stops current
        playback so play_next() starts from the new folder.
        """
        if not config.FOLDER_SELECTION_ENABLED:
            return "❌ Folder selection is not enabled."

        if not config.MUSIC_FOLDERS:
            return "❌ No alternate music folders are configured."

        # Case-insensitive lookup; also support partial prefix matching
        lookup = display_name.strip().lower()
        match: str | None = None
        for name in config.MUSIC_FOLDERS:
            if name.lower() == lookup:
                match = name
                break
        if match is None:
            # Try prefix matching as a fallback
            candidates = [
                n for n in config.MUSIC_FOLDERS if n.lower().startswith(lookup)
            ]
            if len(candidates) == 1:
                match = candidates[0]
            elif len(candidates) > 1:
                return (
                    f"❌ Ambiguous: did you mean one of: "
                    f"{', '.join(candidates)}?"
                )

        if match is None:
            available = ", ".join(f"`{n}`" for n in config.MUSIC_FOLDERS)
            return f"❌ Unknown folder. Available: {available}"

        target_path = config.MUSIC_FOLDERS[match]

        if self.active_folder_name == match:
            return f"✅ Already playing from **{match}**."

        old_name = self.active_folder_name
        self.active_folder_name = match
        self.active_folder_path = target_path

        log.info(
            "Switched music folder: %s → %s (%s)",
            old_name or "(default)", match, target_path,
        )

        async with self._lock:
            # Refill queue from the new folder
            self.music_queue = self.get_all_music_files(target_path)
            self.track_history.clear()

            # Stop current playback so play_next() picks up the new queue
            if (
                self.voice_client
                and (self.voice_client.is_playing() or self.voice_client.is_paused())
            ):
                self.voice_client.stop()

        return f"📂 Switched to **{match}** ({len(self.music_queue)} tracks loaded)"

    # -------------------------------------------------------------------
    # Connection management — start / stop the radio
    # -------------------------------------------------------------------

    async def _purge_old_bot_messages(self) -> None:
        """Clean up recent bot messages from the text channel on startup."""
        channel = await self.get_text_channel()
        if channel is None:
            return

        try:
            deleted = await channel.purge(
                limit=50, check=lambda m: m.author == bot.user,
            )
            if deleted:
                log.info(
                    "Purged %d old bot message(s) from #%s",
                    len(deleted), channel.name,
                )
        except (discord.Forbidden, discord.HTTPException) as exc:
            log.warning("Could not purge old bot messages: %s", exc)

    async def start_radio(self) -> None:
        """Connect to voice and begin streaming."""
        self.is_afk_disconnected = False

        if (
            self.voice_client is not None
            and self.voice_client.is_connected()
            and self.voice_client.is_playing()
        ):
            log.info("Radio already connected and playing.")
            return

        if (
            self.voice_client is not None
            and self.voice_client.is_connected()
            and not self.voice_client.is_playing()
        ):
            log.info("Radio connected but not playing – restarting playback")
            async with self._lock:
                await self.play_next()
            self._is_connecting = False
            return

        self._is_connecting = True
        await self._purge_old_bot_messages()

        log.info("Attempting to connect/reconnect to voice channel...")
        voice_channel = bot.get_channel(config.VOICE_CHANNEL_ID)
        if voice_channel is None:
            log.error(
                "Voice channel %s not found. Check VOICE_CHANNEL_ID in config.",
                config.VOICE_CHANNEL_ID,
            )
            self._is_connecting = False
            return

        if not isinstance(voice_channel, (discord.VoiceChannel, discord.StageChannel)):
            log.error(
                "Channel %s is not a voice-capable channel (type=%s).",
                config.VOICE_CHANNEL_ID,
                type(voice_channel).__name__,
            )
            self._is_connecting = False
            return

        try:
            self.voice_client = await voice_channel.connect()
            log.info("Connected to voice channel: %s", voice_channel.name)
        except discord.Forbidden:
            log.error("Bot cannot join voice channel. Check permissions.")
            self._is_connecting = False
            return
        except (discord.HTTPException, asyncio.TimeoutError) as exc:
            log.exception("Unexpected error connecting to voice channel: %s", exc)
            self._is_connecting = False
            return

        if self._monitor_task and not self._monitor_task.done():
            self._monitor_task.cancel()
        self._monitor_task = asyncio.create_task(self.check_channel_activity())

        async with self._lock:
            await self.play_next()

        self._is_connecting = False
        self._last_reconnect_time = asyncio.get_running_loop().time()

    async def stop_radio(self) -> None:
        """Gracefully disconnect and tear down all background tasks."""
        if self.voice_client:
            if self.afk_timer_task and not self.afk_timer_task.done():
                self.afk_timer_task.cancel()
            if self._monitor_task and not self._monitor_task.done():
                self._monitor_task.cancel()
            await self.voice_client.disconnect()
            self.voice_client = None
            self.is_afk_disconnected = False
            await self.delete_current_embed()
        self.ratings_db.close()


# =======================================================================
# Singleton instance — everything references 'radio'
# =======================================================================
radio = RadioManager()


# =======================================================================
# Permission helpers
# =======================================================================
def is_admin(member: discord.Member | discord.User) -> bool:
    """Return True if *member* is allowed to use admin-only commands."""
    if not isinstance(member, discord.Member):
        return False
    if config.ADMIN_ROLE_ID == 0:
        return True
    if member.guild_permissions.administrator:
        return True
    return config.ADMIN_ROLE_ID in {r.id for r in member.roles}


def can_switch_folder(member: discord.Member | discord.User) -> bool:
    """Return True if *member* is allowed to use folder selection commands."""
    if not config.FOLDER_SELECTION_ENABLED:
        return False
    if config.FOLDER_SELECTION_PERMISSION == "all":
        return True
    return is_admin(member)


# =======================================================================
# Voice reconnection — exponential backoff on unexpected disconnect
# =======================================================================
async def _attempt_reconnect(
    max_retries: int = 3,
    base_delay: float = 3.0,
) -> None:
    for attempt in range(1, max_retries + 1):
        log.info("Reconnect attempt %d/%d – calling start_radio()", attempt, max_retries)
        await radio.start_radio()

        if radio.voice_client is not None and radio.voice_client.is_connected():
            log.info("Reconnect succeeded on attempt %d", attempt)
            return

        if attempt < max_retries:
            delay = base_delay * (2 ** (attempt - 1))
            log.info("Waiting %.1fs before next reconnect attempt", delay)
            await asyncio.sleep(delay)

    log.error("All %d reconnect attempts exhausted", max_retries)


# =======================================================================
# Event Handlers
# =======================================================================
# on_ready() fires once when the bot logs in.
# on_voice_state_update() fires whenever ANY user joins/leaves a channel.
# =======================================================================

@bot.event
async def on_ready() -> None:
    """Called once when the bot has logged in and is ready."""
    log.info("Logged in as %s", bot.user)
    log.info("Music folder: %s", config.MUSIC_FOLDER)
    log.info("AFK timeout : %s s", config.AFK_TIMEOUT_SECONDS)

    _install_signal_handlers()

    # Handle .env auto-sync: notify owner and optionally restart
    if config.NEW_ENV_KEYS_ADDED:
        if config.RUNNING_AS_SERVICE:
            # Service-managed: DM the owner, then shut down so systemd/NSSM restarts
            if config.ADMIN_USER_ID != 0:
                try:
                    owner = await bot.fetch_user(config.ADMIN_USER_ID)
                    if owner:
                        await owner.send(
                            f"🔄 **{len(config.NEW_ENV_KEYS_LIST)} new configuration option(s) "
                            f"were added to `.env`.**\n"
                            f"The bot will now restart to apply them.\n"
                            f"Added: `{', '.join(config.NEW_ENV_KEYS_LIST)}`"
                        )
                except (discord.HTTPException, discord.NotFound) as exc:
                    log.warning("Could not DM owner about new .env keys: %s", exc)
            log.info("New .env keys synced — shutting down for service-manager restart")
            await radio.stop_radio()
            await bot.close()
            return
        else:
            # Not service-managed: just DM the owner to restart manually
            if config.ADMIN_USER_ID != 0:
                try:
                    owner = await bot.fetch_user(config.ADMIN_USER_ID)
                    if owner:
                        await owner.send(
                            f"🔄 **{len(config.NEW_ENV_KEYS_LIST)} new configuration option(s) "
                            f"were added to `.env`.**\n"
                            f"Please restart the bot to apply them.\n"
                            f"Added: `{', '.join(config.NEW_ENV_KEYS_LIST)}`"
                        )
                except (discord.HTTPException, discord.NotFound) as exc:
                    log.warning("Could not DM owner about new .env keys: %s", exc)

    # Verify voice-channel permissions early
    voice_channel_obj = bot.get_channel(config.VOICE_CHANNEL_ID)
    if voice_channel_obj is not None and isinstance(
        voice_channel_obj, (discord.VoiceChannel, discord.StageChannel)
    ):
        try:
            await voice_channel_obj.edit(status="")  # type: ignore[call-overload]
            log.info(
                "Voice channel permission check passed – status updates will"
                " work on %s",
                voice_channel_obj.name,
            )
        except discord.Forbidden:
            log.error(
                "MISSING 'Manage Channel' permission on voice channel %s – "
                "the voice channel status will NEVER be updated! "
                "Grant the bot role the 'Manage Channel' permission, and if "
                "the channel has custom permissions, add it there too.",
                voice_channel_obj.name,
            )
        except discord.HTTPException:
            log.warning(
                "HTTP error during permission check on voice channel %s – "
                "status may or may not work",
                voice_channel_obj.name,
            )
    elif voice_channel_obj is None:
        log.warning(
            "Voice channel %s not found – cannot check channel status permissions",
            config.VOICE_CHANNEL_ID,
        )
    else:
        log.warning(
            "Channel %s is not a voice-capable channel (type=%s) – "
            "cannot check channel status permissions",
            config.VOICE_CHANNEL_ID,
            type(voice_channel_obj).__name__,
        )

    await radio.start_radio()


@bot.event
async def on_voice_state_update(
    member: discord.Member,
    before: discord.VoiceState,
    after: discord.VoiceState,
) -> None:
    """Handle voice disconnects and human-join detection."""
    # --- Bot's own voice state changed ---
    if bot.user is not None and member.id == bot.user.id:
        if radio._is_connecting:
            return

        now = asyncio.get_running_loop().time()
        if now - radio._last_reconnect_time < radio._reconnect_cooldown:
            log.debug(
                "Ignoring disconnect – %.1fs since last reconnect (cooldown %.1fs)",
                now - radio._last_reconnect_time,
                radio._reconnect_cooldown,
            )
            return

        if after.channel is None and before.channel is not None:
            if radio.is_afk_disconnected:
                return  # intentional AFK leave — don't reconnect

            log.info(
                "Disconnected from voice (channel=%s) – starting reconnection",
                before.channel.id,
            )

            await asyncio.sleep(2)
            await _attempt_reconnect(max_retries=4, base_delay=3.0)

            if not (radio.voice_client and radio.voice_client.is_connected()):
                log.critical("Unable to reconnect after backoff – bot may be stuck")
                channel = await radio.get_text_channel()
                if channel:
                    await channel.send(
                        "⚠️ Radio failed to reconnect to voice channel. "
                        "Admins, use `!join` to manually restart.",
                    )

    # --- A human joined our voice channel ---
    else:
        if member.bot:
            return
        if after.channel is None:
            return
        if after.channel.id != config.VOICE_CHANNEL_ID:
            return

        if radio.is_afk_disconnected:
            log.info("Detected human join via voice state – rejoining channel")
            if radio.afk_timer_task and not radio.afk_timer_task.done():
                radio.afk_timer_task.cancel()
                radio.afk_timer_task = None
            radio.is_afk_disconnected = False
            await radio.start_radio()


# =======================================================================
# Bot Commands
# =======================================================================

@bot.command(name=config.COMMAND_NOW)
async def now_playing_cmd(ctx: commands.Context) -> None:
    """Show the currently playing song.  Anyone can use this."""
    if radio.current_song:
        if radio.is_afk_disconnected:
            status = " (Left channel - No listeners)"
        elif radio.voice_client and radio.voice_client.is_paused():
            status = " (Paused)"
        else:
            status = ""
        embed = discord.Embed(
            title="🎵 Currently Playing",
            description=radio.current_song + status,
            color=discord.Color.green(),
        )
        embed.set_footer(text=f"Volume: {int(radio.volume * 100)}%")
        await ctx.send(embed=embed)
    else:
        await ctx.send("❌ Nothing is playing right now.")


@bot.command(name=config.COMMAND_VOLUME)
async def set_volume(ctx: commands.Context, vol: int) -> None:
    """Set the playback volume (0-100).  Admin only."""
    if not is_admin(ctx.author):
        await ctx.send("❌ Admin only command!")
        return

    if not 0 <= vol <= 100:
        await ctx.send("❌ Volume must be between 0 and 100!")
        return

    old = int(radio.volume * 100)
    radio.volume = vol / 100
    await ctx.send(
        f"🔊 Volume changed from {old}% to {vol}% (applies to next song)",
    )
    log.info("Volume set to %d%%", vol)


@bot.command(name=config.COMMAND_SKIP)
async def skip(ctx: commands.Context) -> None:
    """Skip the current track.  Admin only."""
    if not is_admin(ctx.author):
        await ctx.send("❌ Admin only command!")
        return

    async with radio._lock:
        if radio.voice_client and radio.voice_client.is_playing():
            radio.voice_client.stop()
            await ctx.send("⏭ Skipped to next song")
        else:
            await ctx.send("❌ Nothing playing!")


@bot.command(name=config.COMMAND_STOP)
async def stop_radio(ctx: commands.Context) -> None:
    """Stop playback and disconnect from voice.  Admin only."""
    if not is_admin(ctx.author):
        await ctx.send("❌ Admin only command!")
        return

    await radio.stop_radio()
    await ctx.send("🛑 Radio stopped")


@bot.command(name=config.COMMAND_JOIN)
async def join_radio(ctx: commands.Context) -> None:
    """Start the radio / rejoin voice channel.  Admin only."""
    if not is_admin(ctx.author):
        await ctx.send("❌ Admin only command!")
        return

    await radio.start_radio()
    await ctx.send("📻 Radio started!")


@bot.command(name=config.COMMAND_REFRESH)
async def refresh_embed(ctx: commands.Context) -> None:
    """Re-send the Now Playing embed.  Admin only."""
    if not is_admin(ctx.author):
        await ctx.send("❌ Admin only command!")
        return

    if radio.current_song_path:
        info = radio.get_audio_info(radio.current_song_path)
        art = radio.get_album_art(radio.current_song_path)
        await radio.update_now_playing(
            radio.current_song_path, info, art,
            is_paused=radio.is_afk_disconnected,
        )
        await ctx.send("🔄 Refreshed display")
    else:
        await ctx.send("❌ No song playing")


@bot.command(name=config.COMMAND_QUEUE)
async def show_queue(ctx: commands.Context) -> None:
    """Show the next 10 upcoming tracks.  Admin only."""
    if not is_admin(ctx.author):
        await ctx.send("❌ Admin only command!")
        return

    if not radio.music_queue:
        await ctx.send("📋 Queue is empty, refilling...")
        return

    lines: list[str] = []
    for i, path in enumerate(radio.music_queue[:10], 1):
        info = radio.get_audio_info(path)
        lines.append(f"{i}. {info['artist']} - {info['title']}")

    text = "\n".join(lines)
    if len(radio.music_queue) > 10:
        text += f"\n... and {len(radio.music_queue) - 10} more"

    embed = discord.Embed(
        title=f"📋 Queue ({len(radio.music_queue)} songs)",
        description=text,
        color=discord.Color.blue(),
    )
    await ctx.send(embed=embed)


@bot.command(name=config.COMMAND_RESUME)
async def resume_radio(ctx: commands.Context) -> None:
    """Rejoin the voice channel and resume playback.  Admin only."""
    if not is_admin(ctx.author):
        await ctx.send("❌ Admin only command!")
        return

    if radio.is_afk_disconnected:
        log.info("Manual resume – rejoining voice channel")
        radio.is_afk_disconnected = False
        await radio.start_radio()
        await ctx.send("▶️ Playback resumed")
    elif radio.voice_client and radio.voice_client.is_connected():
        await ctx.send("✅ Bot is already connected to voice")
    else:
        await radio.start_radio()
        await ctx.send("▶️ Playback resumed")


# -----------------------------------------------------------------------
# Folder selection commands — only available when FOLDER_SELECTION_ENABLED
# -----------------------------------------------------------------------

@bot.command(name=config.COMMAND_FOLDERS)
async def list_folders(ctx: commands.Context) -> None:
    """List all configured music folders and show the currently active one."""
    try:
        await ctx.message.delete()
    except (discord.Forbidden, discord.NotFound, discord.HTTPException):
        pass

    if not can_switch_folder(ctx.author):
        await ctx.send(
            "❌ You don't have permission to use this command!",
            delete_after=config.AUTO_DELETE_TIMEOUT,
        )
        return

    if not config.MUSIC_FOLDERS:
        await ctx.send(
            "❌ No alternate music folders are configured.",
            delete_after=config.AUTO_DELETE_TIMEOUT,
        )
        return

    lines: list[str] = []
    active_name = radio.active_folder_name
    for name in config.MUSIC_FOLDERS:
        if name == active_name:
            lines.append(f"▶ **{name}** ← current")
        else:
            lines.append(f"  {name}")

    embed = discord.Embed(
        title="📂 Music Folders",
        description="\n".join(lines),
        color=discord.Color.blurple(),
    )
    embed.set_footer(
        text=f"Use {bot.command_prefix}{config.COMMAND_SWITCH} <name> to switch"
    )
    await ctx.send(embed=embed, delete_after=config.AUTO_DELETE_TIMEOUT)


@bot.command(name=config.COMMAND_SWITCH)
async def switch_music_folder(ctx: commands.Context, *, folder_name: str = "") -> None:
    """Switch to a different music folder. Provide the folder display name."""
    try:
        await ctx.message.delete()
    except (discord.Forbidden, discord.NotFound, discord.HTTPException):
        pass

    if not can_switch_folder(ctx.author):
        await ctx.send(
            "❌ You don't have permission to use this command!",
            delete_after=config.AUTO_DELETE_TIMEOUT,
        )
        return

    if not folder_name:
        await ctx.send(
            f"❌ Please specify a folder name. "
            f"Use `{bot.command_prefix}{config.COMMAND_FOLDERS}` to see available folders.",
            delete_after=config.AUTO_DELETE_TIMEOUT,
        )
        return

    result = await radio.switch_folder(folder_name)
    await ctx.send(result, delete_after=config.AUTO_DELETE_TIMEOUT)


# -----------------------------------------------------------------------
# Custom help command — lists all available commands
# -----------------------------------------------------------------------
@bot.command(name=config.HELP_COMMAND)
async def custom_help(ctx: commands.Context) -> None:
    prefix = bot.command_prefix
    lines = [
        f"**{prefix}{config.HELP_COMMAND}** — Show this help message (anyone)",
        f"**{prefix}{config.COMMAND_NOW}** — Show the currently playing song (anyone)",
        f"**{prefix}{config.COMMAND_VOLUME} <0-100>** — Set playback volume (admin)",
        f"**{prefix}{config.COMMAND_SKIP}** — Skip to the next track (admin)",
        f"**{prefix}{config.COMMAND_STOP}** — Stop playback and disconnect (admin)",
        f"**{prefix}{config.COMMAND_JOIN}** — Start the radio / rejoin voice (admin)",
        f"**{prefix}{config.COMMAND_REFRESH}** — Re-send the Now Playing embed (admin)",
        f"**{prefix}{config.COMMAND_QUEUE}** — Show upcoming tracks (admin)",
        f"**{prefix}{config.COMMAND_RESUME}** — Resume playback after AFK leave (admin)",
    ]
    if config.FOLDER_SELECTION_ENABLED:
        perm_label = "(anyone)" if config.FOLDER_SELECTION_PERMISSION == "all" else "(admin)"
        lines.append(
            f"**{prefix}{config.COMMAND_FOLDERS}** — List available music folders {perm_label}"
        )
        lines.append(
            f"**{prefix}{config.COMMAND_SWITCH} <name>** — Switch music folder {perm_label}"
        )
    embed = discord.Embed(
        title="📻 Vibed Discord Bot — Commands",
        description="\n".join(lines),
        color=discord.Color.blue(),
    )
    embed.set_footer(text="Prefix: !  •  Customise command names in .env")
    await ctx.send(embed=embed)


# =======================================================================
# Graceful shutdown — handles Ctrl+C / SIGTERM
# =======================================================================
async def shutdown_handler() -> None:
    log.info("Shutdown signal received – disconnecting voice and cancelling tasks")
    await radio.stop_radio()


def _install_signal_handlers() -> None:
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(
                sig,
                lambda s=sig: asyncio.ensure_future(_signal_handler(s)),
            )
        except NotImplementedError:
            signal.signal(
                sig,
                lambda _s, _f, s=sig: asyncio.ensure_future(_signal_handler(s)),
            )


async def _signal_handler(sig: signal.Signals) -> None:
    log.info("Received signal %s", sig.name)
    await shutdown_handler()
    await bot.close()


# =======================================================================
# Entry point — start the bot with the token from .env
# =======================================================================
if __name__ == "__main__":
    if not config.TOKEN:
        log.critical("DISCORD_TOKEN is not set – check your .env file")
        raise SystemExit(1)

    bot.run(config.TOKEN)