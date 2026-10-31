"""Persistent counts of reactions received, plus reconciliation with Discord history."""

import asyncio
import logging
import re
import sqlite3
import time

import discord

log = logging.getLogger(__name__)


def parse_scan_limit(value):
    value = value.strip().lower()
    if value == "inf":
        return None
    if not re.fullmatch(r"[1-9][0-9]*", value):
        raise ValueError("Use a positive message count, such as 1000, or inf for all history.")
    limit = int(value)
    if limit > 2**63 - 1:
        raise ValueError("For all history, use inf instead of a very large number.")
    return limit


def emoji_key(emoji):
    """Custom emoji IDs survive renames; Unicode emoji keep Discord's exact spelling."""
    if getattr(emoji, "id", None):
        return f"custom:{emoji.id}"
    text = str(emoji).strip()
    match = re.fullmatch(r"<a?:\w+:(\d+)>|(\d+)", text)
    return f"custom:{match.group(1) or match.group(2)}" if match else f"unicode:{text}"


class ReactionStore:
    def __init__(self, path):
        self.db = sqlite3.connect(path)
        self.db.execute("PRAGMA foreign_keys = ON")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS reaction_messages (
                message_id INTEGER PRIMARY KEY,
                guild_id INTEGER NOT NULL,
                channel_id INTEGER NOT NULL,
                author_id INTEGER NOT NULL
            );
            CREATE INDEX IF NOT EXISTS reaction_authors
                ON reaction_messages (guild_id, author_id);
            CREATE TABLE IF NOT EXISTS reaction_entries (
                message_id INTEGER NOT NULL REFERENCES reaction_messages ON DELETE CASCADE,
                user_id INTEGER NOT NULL,
                emoji_key TEXT NOT NULL,
                emoji_display TEXT NOT NULL,
                PRIMARY KEY (message_id, user_id, emoji_key)
            );
            CREATE TABLE IF NOT EXISTS reaction_scans (
                guild_id INTEGER PRIMARY KEY,
                finished_at INTEGER NOT NULL,
                problems INTEGER NOT NULL
            );
        """)
        columns = {row[1] for row in self.db.execute("PRAGMA table_info(reaction_scans)")}
        for name, definition in (("channel_id", "INTEGER"), ("message_limit", "INTEGER"),
                                 ("checked", "INTEGER NOT NULL DEFAULT 0")):
            if name not in columns:
                self.db.execute(f"ALTER TABLE reaction_scans ADD COLUMN {name} {definition}")
        self.db.commit()

    def owner(self, message_id):
        row = self.db.execute("SELECT author_id FROM reaction_messages WHERE message_id = ?", (message_id,)).fetchone()
        return row[0] if row else None

    def replace(self, guild_id, channel_id, message_id, author_id, entries):
        """Replace one complete snapshot atomically; retries never add duplicate votes."""
        with self.db:
            self.db.execute("DELETE FROM reaction_messages WHERE message_id = ?", (message_id,))
            if not entries:
                return
            self.db.execute("INSERT INTO reaction_messages VALUES (?, ?, ?, ?)", (message_id, guild_id, channel_id, author_id))
            self.db.executemany("INSERT OR IGNORE INTO reaction_entries VALUES (?, ?, ?, ?)",
                                [(message_id, user, key, display) for user, key, display in entries])

    def delete_messages(self, message_ids):
        with self.db:
            self.db.executemany("DELETE FROM reaction_messages WHERE message_id = ?", [(mid,) for mid in message_ids])

    def delete_channel(self, channel_id):
        with self.db:
            self.db.execute("DELETE FROM reaction_messages WHERE channel_id = ?", (channel_id,))

    def message_ids(self, channel_id):
        return {row[0] for row in self.db.execute("SELECT message_id FROM reaction_messages WHERE channel_id = ?", (channel_id,))}

    def channel_ids(self, guild_id):
        return {row[0] for row in self.db.execute("SELECT DISTINCT channel_id FROM reaction_messages WHERE guild_id = ?", (guild_id,))}

    def summary(self, guild_id, author_id, channel_id=None):
        args = [guild_id, author_id]
        where = "FROM reaction_entries e JOIN reaction_messages m USING (message_id) WHERE m.guild_id = ? AND m.author_id = ?"
        if channel_id is not None:
            where += " AND m.channel_id = ?"
            args.append(channel_id)
        breakdown = self.db.execute("SELECT MAX(e.emoji_display), COUNT(*) " + where + " GROUP BY e.emoji_key ORDER BY COUNT(*) DESC, e.emoji_key", args).fetchall()
        top = self.db.execute("SELECT m.channel_id, m.message_id, COUNT(*) " + where + " GROUP BY m.message_id ORDER BY COUNT(*) DESC, m.message_id DESC LIMIT 1", args).fetchone()
        return sum(count for _, count in breakdown), breakdown, top

    def leaderboard(self, guild_id, emoji=None, channel_id=None):
        sql = """SELECT m.author_id, COUNT(*) FROM reaction_entries e
                 JOIN reaction_messages m USING (message_id) WHERE m.guild_id = ?"""
        args = [guild_id]
        if emoji is not None:
            sql += " AND e.emoji_key = ?"
            args.append(emoji_key(emoji))
        if channel_id is not None:
            sql += " AND m.channel_id = ?"
            args.append(channel_id)
        return self.db.execute(sql + " GROUP BY m.author_id ORDER BY COUNT(*) DESC, m.author_id LIMIT 20", args).fetchall()

    def finish_scan(self, guild_id, problems, channel_id=None, limit=None, checked=0):
        with self.db:
            self.db.execute("""INSERT OR REPLACE INTO reaction_scans
                (guild_id, finished_at, problems, channel_id, message_limit, checked)
                VALUES (?, ?, ?, ?, ?, ?)""", (guild_id, int(time.time()), problems, channel_id, limit, checked))


class ReactionTracker:
    def __init__(self, client, repost_owner, path="links.db"):
        self.client, self.repost_owner = client, repost_owner
        self.store = ReactionStore(path)
        # Fetch and commit under the same lock: live events cannot be overwritten by an older scan.
        self.lock = asyncio.Lock()
        self.task = None
        self.progress = {}
        self.live_errors = {}
        self.needs_scan = {}
        self.scope = None

    async def sync_message(self, channel, message_id):
        async with self.lock:
            try:
                message = await channel.fetch_message(message_id)
                if not message.guild:
                    return
                if message.webhook_id:
                    author_id = self.repost_owner(message) or self.store.owner(message.id)
                else:
                    author_id = None if message.author.bot else message.author.id
                entries = set()
                if author_id is not None:
                    for reaction in message.reactions:
                        # Normal + super reactions from the same user/emoji count once.
                        for kind, count in ((discord.ReactionType.normal, reaction.normal_count),
                                            (discord.ReactionType.burst, reaction.burst_count)):
                            if not count:
                                continue
                            async for user in reaction.users(limit=None, type=kind):
                                if not user.bot and user.id != author_id:
                                    entries.add((user.id, emoji_key(reaction.emoji), str(reaction.emoji)))
                self.store.replace(message.guild.id, channel.id, message.id, author_id, entries)
            except discord.NotFound:
                self.store.delete_messages([message_id])

    async def on_reaction(self, payload):
        if not payload.guild_id:
            return
        try:
            channel = self.client.get_channel(payload.channel_id) or await self.client.fetch_channel(payload.channel_id)
            await self.sync_message(channel, payload.message_id)
        except discord.NotFound:
            await self.delete_channel(payload.channel_id)
        except discord.HTTPException:
            self.live_errors[payload.guild_id] = self.live_errors.get(payload.guild_id, 0) + 1
            log.warning("Could not reconcile reactions on message %s", payload.message_id, exc_info=True)

    async def delete_messages(self, message_ids):
        async with self.lock:
            self.store.delete_messages(message_ids)

    async def delete_channel(self, channel_id):
        async with self.lock:
            self.store.delete_channel(channel_id)

    def note_gap(self, guild_id):
        """Startup/reconnection marks possible offline changes without fetching history."""
        self.needs_scan[guild_id] = self.needs_scan.get(guild_id, 0) + 1

    def start_scan(self, guild, limit=None, channel=None):
        if self.task is not None and not self.task.done():
            return False
        self.progress[guild.id] = 0
        self.scope = (channel.id if channel else None, limit)
        self.task = asyncio.create_task(self._scan(guild, limit, channel), name="reaction-history")
        return True

    def scope_text(self, channel_id, limit):
        channel = self.client.get_channel(channel_id) if channel_id is not None else None
        location = f"#{channel.name}" if channel is not None and hasattr(channel, "name") else f"channel/thread {channel_id}"
        if channel_id is None:
            return "all server history" if limit is None else f"last {limit:,} per channel/thread"
        return f"{location} · " + ("all history" if limit is None else f"last {limit:,} messages")

    def status(self, guild_id):
        progress = self.progress.get(guild_id)
        if progress is not None:
            return f"History scan running · {self.scope_text(*self.scope)} · {progress:,} checked · totals are provisional"
        row = self.store.db.execute("SELECT finished_at, problems, channel_id, message_limit, checked FROM reaction_scans WHERE guild_id = ?", (guild_id,)).fetchone()
        if not row:
            if self.live_errors.get(guild_id):
                return "Live tracking only · some live updates failed; admins: /reactionscan to reconcile"
            return "Live tracking only · admins: /reactionscan to include older or offline reactions"
        stamp, problems, channel_id, limit, checked = row
        date = time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(stamp))
        result = f"Last scan: {date} · {self.scope_text(channel_id, limit)} · {checked:,} checked"
        if self.live_errors.get(guild_id):
            return result + " · some live updates failed; admins can rescan"
        if problems:
            return result + f" · {problems} unavailable channels or scan errors · totals may be incomplete"
        if guild_id in self.needs_scan:
            return result + " · offline changes may need a manual rescan"
        return result + " · live updates enabled"

    async def _scan_channel(self, channel, guild_id, limit=None):
        # Only prune records that existed before this scan; new live reactions may arrive meanwhile.
        missing = self.store.message_ids(channel.id)
        checked, oldest_id = 0, None
        async for message in channel.history(limit=limit, oldest_first=False):
            checked += 1
            oldest_id = message.id if oldest_id is None else min(oldest_id, message.id)
            known = message.id in missing
            missing.discard(message.id)
            if message.reactions or known:
                await self.sync_message(channel, message.id)
            self.progress[guild_id] += 1
            await asyncio.sleep(0)  # long histories must not block live events or commands
        if limit is not None and checked >= limit:
            # A bounded scan says nothing about older records outside the inspected range.
            missing = {mid for mid in missing if mid >= oldest_id}
        await self.delete_messages(missing)

    async def _scan_guild(self, guild, limit, selected_channel):
        problems, seen = 0, set()

        async def scan(channel):
            nonlocal problems
            if channel.id in seen:
                return
            seen.add(channel.id)
            perms = channel.permissions_for(guild.me)
            if not perms.view_channel or not perms.read_message_history:
                problems += 1
                return
            try:
                await self._scan_channel(channel, guild.id, limit)
            except discord.HTTPException:
                problems += 1
                log.warning("Reaction history unavailable in channel %s", channel.id, exc_info=True)

        if selected_channel is not None:
            await scan(selected_channel)
            return problems

        for channel in guild.channels:
            if hasattr(channel, "history"):
                await scan(channel)
            if not hasattr(channel, "archived_threads"):
                continue
            perms = channel.permissions_for(guild.me)
            if not perms.view_channel or not perms.read_message_history:
                if channel.id not in seen:
                    problems += 1
                continue
            options = [{}]
            if isinstance(channel, discord.TextChannel):
                options.append({"private": True, "joined": not perms.manage_threads})
            for kwargs in options:
                try:
                    async for thread in channel.archived_threads(limit=None, **kwargs):
                        await scan(thread)
                except discord.HTTPException:
                    problems += 1
                    log.warning("Archived reaction history unavailable in channel %s", channel.id, exc_info=True)

        threads = {thread.id: thread for thread in guild.threads}
        try:
            threads.update({thread.id: thread for thread in await guild.active_threads()})
        except discord.HTTPException:
            problems += 1
            log.warning("Active threads unavailable in guild %s", guild.id, exc_info=True)
        for thread in threads.values():
            await scan(thread)

        # Previously counted channels/threads may have been deleted or hidden while offline.
        for channel_id in self.store.channel_ids(guild.id) - seen:
            try:
                channel = await self.client.fetch_channel(channel_id)
                await scan(channel)
            except discord.NotFound:
                await self.delete_channel(channel_id)
            except discord.HTTPException:
                problems += 1
                log.warning("Previously tracked channel %s is unavailable", channel_id, exc_info=True)
        return problems

    async def _scan(self, guild, limit, channel):
        live_errors_before = self.live_errors.get(guild.id, 0)
        gap_before = self.needs_scan.get(guild.id, 0)
        finished, problems = False, 0
        try:
            problems = await self._scan_guild(guild, limit, channel)
            finished = True
            if not problems and limit is None and channel is None:
                if self.live_errors.get(guild.id, 0) == live_errors_before:
                    self.live_errors.pop(guild.id, None)
                if self.needs_scan.get(guild.id, 0) == gap_before:
                    self.needs_scan.pop(guild.id, None)
        except Exception:
            problems, finished = 1, True
            log.exception("Reaction history scan failed in guild %s", guild.id)
        finally:
            checked = self.progress.pop(guild.id, 0)
            self.scope = None
            if finished:
                self.store.finish_scan(guild.id, problems, channel.id if channel else None, limit, checked)
