"""Persistent counts of reactions received, plus reconciliation with Discord history."""

import asyncio
import datetime
import logging
import re
import sqlite3
import time

import discord

log = logging.getLogger(__name__)

# After a restart or reconnect, messages from this long before the bot was last seen alive are
# rechecked too: reactions keep coming in on recent messages, and the last heartbeat is up to a
# minute old. Older messages are left to a manual /reactionscan.
CATCHUP_MARGIN = 2 * 3600


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


def same_emoji(key):
    """An emoji key without U+FE0F (the "show as emoji" mark), for matching what people type:
    ❤ and ❤️ look alike, and keyboards differ on which one they send."""
    return key.replace("\ufe0f", "")


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
            CREATE TABLE IF NOT EXISTS reaction_alive (
                guild_id INTEGER PRIMARY KEY,
                seen_at INTEGER NOT NULL
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
        entries = set(entries)
        previous = self.db.execute("SELECT guild_id, channel_id, author_id FROM reaction_messages WHERE message_id = ?", (message_id,)).fetchone()
        if not entries and previous is None:
            return
        if previous == (guild_id, channel_id, author_id):
            saved = set(self.db.execute("SELECT user_id, emoji_key, emoji_display FROM reaction_entries WHERE message_id = ?", (message_id,)))
            if saved == entries:
                return  # repeated scans/events need no disk write when the snapshot is unchanged
        with self.db:
            self.db.execute("DELETE FROM reaction_messages WHERE message_id = ?", (message_id,))
            if not entries:
                return
            self.db.execute("INSERT INTO reaction_messages VALUES (?, ?, ?, ?)", (message_id, guild_id, channel_id, author_id))
            self.db.executemany("INSERT OR IGNORE INTO reaction_entries VALUES (?, ?, ?, ?)",
                                [(message_id, user, key, display) for user, key, display in entries])

    def add_entry(self, guild_id, channel_id, message_id, author_id, user_id, key, display):
        """One reaction added (a live event): no Discord request needed. A normal and a super
        reaction with the same emoji from the same person stay one entry."""
        with self.db:
            self.db.execute("INSERT OR IGNORE INTO reaction_messages VALUES (?, ?, ?, ?)", (message_id, guild_id, channel_id, author_id))
            self.db.execute("INSERT OR IGNORE INTO reaction_entries VALUES (?, ?, ?, ?)", (message_id, user_id, key, display))

    def replace_emoji(self, message_id, key, entries):
        """Replace one emoji's entries on a message (after a removal or a cleared emoji); a message
        left with no counted reactions is dropped, as replace() does."""
        with self.db:
            self.db.execute("DELETE FROM reaction_entries WHERE message_id = ? AND emoji_key = ?", (message_id, key))
            self.db.executemany("INSERT OR IGNORE INTO reaction_entries VALUES (?, ?, ?, ?)",
                                [(message_id, user, k, display) for user, k, display in entries])
            if self.db.execute("SELECT 1 FROM reaction_entries WHERE message_id = ? LIMIT 1", (message_id,)).fetchone() is None:
                self.db.execute("DELETE FROM reaction_messages WHERE message_id = ?", (message_id,))

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

    @staticmethod
    def _only_channels(channels, args):
        """SQL limiting results to these channel ids (the ones the asker can read); None = all."""
        if channels is None:
            return ""
        channels = sorted(channels)
        args.extend(channels)
        return f" AND m.channel_id IN ({','.join('?' * len(channels))})" if channels else " AND 0"

    def summary(self, guild_id, author_id, channel_id=None, channels=None):
        args = [guild_id, author_id]
        where = "FROM reaction_entries e JOIN reaction_messages m USING (message_id) WHERE m.guild_id = ? AND m.author_id = ?"
        if channel_id is not None:
            where += " AND m.channel_id = ?"
            args.append(channel_id)
        where += self._only_channels(channels, args)
        breakdown = self.db.execute("SELECT MAX(e.emoji_display), COUNT(*) " + where + " GROUP BY e.emoji_key ORDER BY COUNT(*) DESC, e.emoji_key", args).fetchall()
        top = self.db.execute("SELECT m.channel_id, m.message_id, COUNT(*) " + where + " GROUP BY m.message_id ORDER BY COUNT(*) DESC, m.message_id DESC LIMIT 1", args).fetchone()
        return sum(count for _, count in breakdown), breakdown, top

    def sent_summary(self, guild_id, user_id, channel_id=None, channels=None, top=5):
        """(reactions user_id has given, their `top` most-used emoji as (emoji, count)), counting
        the same saved reactions as everything else (people's messages, no self-reactions)."""
        args = [guild_id, user_id]
        where = "FROM reaction_entries e JOIN reaction_messages m USING (message_id) WHERE m.guild_id = ? AND e.user_id = ?"
        if channel_id is not None:
            where += " AND m.channel_id = ?"
            args.append(channel_id)
        where += self._only_channels(channels, args)
        total = self.db.execute("SELECT COUNT(*) " + where, args).fetchone()[0]
        # ❤ and ❤️ (with/without U+FE0F) are one emoji here, as in the emoji filter
        used = self.db.execute("SELECT MAX(e.emoji_display), COUNT(*) " + where +
                               " GROUP BY replace(e.emoji_key, char(65039), '') ORDER BY COUNT(*) DESC, MIN(e.emoji_key) LIMIT ?",
                               [*args, top]).fetchall()
        return total, used

    def leaderboard(self, guild_id, emoji=None, channel_id=None, channels=None, *, sort="received"):
        """Return (person, total, top emoji), ranking authors or reacting users.
        channels, when given, limits the count to those channel/thread ids."""
        if sort not in ("received", "sent"):
            raise ValueError("sort must be received or sent")
        person = "e.user_id" if sort == "sent" else "m.author_id"
        where = "WHERE m.guild_id = ?"
        args = [guild_id]
        if emoji is not None:
            where += " AND replace(e.emoji_key, char(65039), '') = ?"
            args.append(same_emoji(emoji_key(emoji)))
        if channel_id is not None:
            where += " AND m.channel_id = ?"
            args.append(channel_id)
        where += self._only_channels(channels, args)
        return self.db.execute(f"""
            WITH emoji_counts AS (
                SELECT {person} AS person_id, e.emoji_key, MAX(e.emoji_display) AS display, COUNT(*) AS n
                FROM reaction_entries e JOIN reaction_messages m USING (message_id)
                {where}
                GROUP BY {person}, e.emoji_key
            ), ranked AS (
                SELECT person_id, display,
                    SUM(n) OVER (PARTITION BY person_id) AS total,
                    ROW_NUMBER() OVER (PARTITION BY person_id ORDER BY n DESC, emoji_key) AS priority
                FROM emoji_counts
            )
            SELECT person_id, total, display FROM ranked WHERE priority = 1
            ORDER BY total DESC, person_id LIMIT 20
        """, args).fetchall()

    def mark_alive(self, guild_ids, when):
        with self.db:
            self.db.executemany("INSERT OR REPLACE INTO reaction_alive VALUES (?, ?)", [(gid, when) for gid in guild_ids])

    def last_alive(self):
        """guild id -> when the bot was last seen running there: its heartbeat, or else the end of
        the last history scan (databases from before heartbeats)."""
        alive = dict(self.db.execute("SELECT guild_id, finished_at FROM reaction_scans"))
        alive.update(self.db.execute("SELECT guild_id, seen_at FROM reaction_alive"))
        return alive

    def finish_scan(self, guild_id, problems, channel_id=None, limit=None, checked=0):
        with self.db:
            self.db.execute("""INSERT OR REPLACE INTO reaction_scans
                (guild_id, finished_at, problems, channel_id, message_limit, checked)
                VALUES (?, ?, ?, ?, ?, ?)""", (guild_id, int(time.time()), problems, channel_id, limit, checked))


class ReactionTracker:
    def __init__(self, client, repost_owner, path="links.db"):
        self.client, self.repost_owner = client, repost_owner
        # repost owner by message id alone (the bot sets it), so live reactions on reposts need no fetch
        self.repost_owner_id = None
        self.store = ReactionStore(path)
        # Fetch and commit under the same lock: live events cannot be overwritten by an older scan.
        self.lock = asyncio.Lock()
        self.task = None
        self.progress = {}
        self.live_errors = {}
        self.needs_scan = {}
        # When each guild was last seen alive: read once at start, before the first heartbeat,
        # so a restart knows how long it was away; then kept current by heartbeat().
        self.alive_at = self.store.last_alive()
        self._boot_alive = dict(self.alive_at)  # the first gap after start goes back to before it
        self.gap_since = {}  # guild id -> start of the time events may have been missed
        self.caught_up = {}  # guild id -> when the last catch-up finished
        self._looked_up = {}  # channel id -> channel/thread fetched from Discord (archived threads), or None if gone
        self.scope = None
        self._scan_changed = None
        self._scan_deleted_channels = None

    async def sync_message(self, channel, message_id, *, snapshot=None):
        async with self.lock:
            try:
                if self._scan_deleted_channels is not None and channel.id in self._scan_deleted_channels:
                    return
                # History already contains the author and reactions. Only fetch again if a live
                # event/deletion may have made that history snapshot stale while the scan ran.
                changed = self._scan_changed is not None and message_id in self._scan_changed
                message = snapshot if snapshot is not None and not changed else await channel.fetch_message(message_id)
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
        if self._scan_changed is not None:
            self._scan_changed.add(payload.message_id)
        try:
            if await self._apply_event(payload):
                return
            channel = self.client.get_channel(payload.channel_id) or await self.client.fetch_channel(payload.channel_id)
            await self.sync_message(channel, payload.message_id)
        except discord.NotFound:
            await self.delete_channel(payload.channel_id)
        except discord.HTTPException:
            self.live_errors[payload.guild_id] = self.live_errors.get(payload.guild_id, 0) + 1
            log.warning("Could not reconcile reactions on message %s", payload.message_id, exc_info=True)

    _BOT_AUTHOR = object()

    def _author(self, payload):
        """Who a reacted message credits, without asking Discord: its saved author, our repost's
        owner, or a known member (_BOT_AUTHOR for bots). None when unsure (another webhook, someone
        who left), so the caller reads the message instead."""
        owner = self.store.owner(payload.message_id)
        if owner is not None:
            return owner
        if self.repost_owner_id is not None:
            owner = self.repost_owner_id(payload.message_id)
            if owner is not None:
                return owner
        author_id = getattr(payload, "message_author_id", None)
        guild = self.client.get_guild(payload.guild_id) if hasattr(self.client, "get_guild") else None
        member = guild.get_member(author_id) if guild is not None and author_id is not None else None
        if member is None:
            return None
        return self._BOT_AUTHOR if member.bot else member.id

    async def _apply_event(self, payload):
        """Apply a live reaction event straight to the counts when that's safe, instead of reading
        the whole message and every reactor again. True when handled."""
        kind = getattr(payload, "event_type", None)
        if isinstance(payload, discord.RawReactionClearEvent):
            await self.delete_messages([payload.message_id])
            return True
        if isinstance(payload, discord.RawReactionClearEmojiEvent):
            async with self.lock:
                self.store.replace_emoji(payload.message_id, emoji_key(payload.emoji), set())
            return True
        if kind == "REACTION_ADD":
            if payload.member is not None and payload.member.bot:
                return True  # bots' reactions (⏳, ⚠️, 🎵) never count
            async with self.lock:
                author = self._author(payload)
                if author is None:
                    return False
                if author is not self._BOT_AUTHOR and author != payload.user_id:
                    self.store.add_entry(payload.guild_id, payload.channel_id, payload.message_id, author,
                                         payload.user_id, emoji_key(payload.emoji), str(payload.emoji))
            return True
        if kind == "REACTION_REMOVE":
            guild = self.client.get_guild(payload.guild_id) if hasattr(self.client, "get_guild") else None
            remover = guild.get_member(payload.user_id) if guild is not None else None
            if remover is not None and remover.bot:
                return True
            if self.store.owner(payload.message_id) is None:
                return True  # nothing counted on that message, so a removal changes nothing
            # The person may still have the other kind (normal/super) of the same emoji: re-read
            # just this emoji's reactors, not the whole message.
            await self._refresh_emoji(payload)
            return True
        return False

    async def _refresh_emoji(self, payload):
        key = emoji_key(payload.emoji)
        async with self.lock:
            if self._scan_deleted_channels is not None and payload.channel_id in self._scan_deleted_channels:
                return
            author = self.store.owner(payload.message_id)
            if author is None:
                return
            channel = self.client.get_channel(payload.channel_id) or await self.client.fetch_channel(payload.channel_id)
            try:
                message = await channel.fetch_message(payload.message_id)
            except discord.NotFound:
                self.store.delete_messages([payload.message_id])
                return
            entries = set()
            for reaction in message.reactions:
                if emoji_key(reaction.emoji) != key:
                    continue
                for kind, count in ((discord.ReactionType.normal, reaction.normal_count),
                                    (discord.ReactionType.burst, reaction.burst_count)):
                    if count:
                        async for user in reaction.users(limit=None, type=kind):
                            if not user.bot and user.id != author:
                                entries.add((user.id, key, str(reaction.emoji)))
            self.store.replace_emoji(payload.message_id, key, entries)

    async def delete_messages(self, message_ids):
        message_ids = list(message_ids)
        if self._scan_changed is not None:
            self._scan_changed.update(message_ids)
        async with self.lock:
            self.store.delete_messages(message_ids)

    async def delete_channel(self, channel_id):
        if self._scan_deleted_channels is not None:
            self._scan_deleted_channels.add(channel_id)
        async with self.lock:
            self.store.delete_channel(channel_id)

    def readable_now(self, guild_id, member):
        """(ids of counted channels/threads member can read, ids not in the bot's memory). Server-wide
        totals only count what the asker could see; archived threads need looking up first."""
        readable, unknown = set(), set()
        for channel_id in self.store.channel_ids(guild_id):
            channel = self.client.get_channel(channel_id) or self._looked_up.get(channel_id)
            if channel is None:
                if channel_id not in self._looked_up:
                    unknown.add(channel_id)
                continue
            if self._can_read(channel, member):
                readable.add(channel_id)
        return readable, unknown

    async def readable_after_lookup(self, unknown, member):
        """Look up channels/threads not in the bot's memory (once per run) and return those member can read."""
        readable = set()
        for channel_id in unknown:
            try:
                channel = await self.client.fetch_channel(channel_id)
            except discord.HTTPException:
                channel = None  # deleted, or hidden from the bot
            self._looked_up[channel_id] = channel
            if channel is not None and self._can_read(channel, member):
                readable.add(channel_id)
        return readable

    @staticmethod
    def _can_read(channel, member):
        try:
            perms = channel.permissions_for(member)
        except (discord.ClientException, AttributeError):
            return False  # a thread whose parent channel is unknown
        if not perms.view_channel or not perms.read_message_history:
            return False
        if getattr(channel, "type", None) == discord.ChannelType.private_thread:
            # private threads: members only; membership isn't known here, so the creator and moderators
            return perms.manage_threads or getattr(channel, "owner_id", None) == member.id
        return True

    def note_gap(self, guild_id):
        """Startup/reconnection marks possible offline changes without fetching history."""
        self.needs_scan[guild_id] = self.needs_scan.get(guild_id, 0) + 1
        since = self._boot_alive.pop(guild_id, None) or self.alive_at.get(guild_id)
        if since is not None:
            self.gap_since[guild_id] = min(since, self.gap_since.get(guild_id, since))

    def heartbeat(self, guild_ids):
        """Record that the bot is running (every minute), for catch_up after the next restart."""
        now = int(time.time())
        self.store.mark_alive(guild_ids, now)
        self.alive_at.update({gid: now for gid in guild_ids})

    def start_catchup(self, guilds):
        """After a restart or reconnect: recheck only messages posted since shortly before the bot
        was last alive (never full history), then drop the "offline changes" warning. Guilds go
        one after another in one background task. Skipped for a guild whose time is unknown, and
        entirely while another scan is running."""
        todo = [g for g in guilds if self.gap_since.get(g.id) is not None]
        if not todo or (self.task is not None and not self.task.done()):
            return False
        self._scan_changed, self._scan_deleted_channels = set(), set()
        # show progress right away: reports run before the task gets its first turn
        self.progress[todo[0].id] = 0
        self.scope = (None, None, self.gap_since[todo[0].id] - CATCHUP_MARGIN)
        self.task = asyncio.create_task(self._catchup(todo), name="reaction-catchup")
        return True

    async def _catchup(self, guilds):
        try:
            for guild in guilds:
                after = self.gap_since[guild.id] - CATCHUP_MARGIN
                gap_before = self.needs_scan.get(guild.id, 0)
                self.progress[guild.id] = 0
                self.scope = (None, None, after)
                try:
                    problems = await self._scan_guild(guild, None, None, after=after)
                except Exception:
                    log.exception("Reaction catch-up failed in guild %s", guild.id)
                    continue
                finally:
                    checked = self.progress.pop(guild.id, 0)
                    self.scope = None
                # print, like the bot's other status lines: discord.py only sets up its own logger
                print(f"Reaction catch-up in guild {guild.id}: {checked} messages since "
                      f"{time.strftime('%Y-%m-%d %H:%M UTC', time.gmtime(after))} checked, {problems} problems", flush=True)
                if not problems:
                    self.caught_up[guild.id] = int(time.time())
                    if self.needs_scan.get(guild.id, 0) == gap_before:
                        self.needs_scan.pop(guild.id, None)
                        self.gap_since.pop(guild.id, None)
        finally:
            self._scan_changed = self._scan_deleted_channels = None

    def start_scan(self, guild, limit=None, channel=None):
        if self.task is not None and not self.task.done():
            return False
        self.progress[guild.id] = 0
        self.scope = (channel.id if channel else None, limit)
        self._scan_changed, self._scan_deleted_channels = set(), set()
        self.task = asyncio.create_task(self._scan(guild, limit, channel), name="reaction-history")
        return True

    def scope_text(self, channel_id, limit, after=None):
        if after is not None:
            return "catching up after a restart: messages since " + time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(after))
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
        if guild_id in self.caught_up:
            return result + " · caught up after the last restart · live updates enabled"
        return result + " · live updates enabled"

    async def _scan_channel(self, channel, guild_id, limit=None, after=None):
        # Only prune records that existed before this scan; new live reactions may arrive meanwhile.
        missing = self.store.message_ids(channel.id)
        window = {}
        if after is not None:
            # a catch-up only looks at (and prunes) messages posted since `after`
            after_id = discord.utils.time_snowflake(datetime.datetime.fromtimestamp(after, datetime.timezone.utc))
            missing = {mid for mid in missing if mid >= after_id}
            window = {"after": discord.Object(after_id)}
        checked, oldest_id = 0, None
        async for message in channel.history(limit=limit, oldest_first=False, **window):
            checked += 1
            oldest_id = message.id if oldest_id is None else min(oldest_id, message.id)
            known = message.id in missing
            missing.discard(message.id)
            if message.reactions or known:
                await self.sync_message(channel, message.id, snapshot=message)
            self.progress[guild_id] += 1
            await asyncio.sleep(0)  # long histories must not block live events or commands
        if limit is not None and checked >= limit:
            # A bounded scan says nothing about older records outside the inspected range.
            missing = {mid for mid in missing if mid >= oldest_id}
        await self.delete_messages(missing)

    async def _scan_guild(self, guild, limit, selected_channel, after=None):
        problems, seen = 0, set()

        async def scan(channel):
            nonlocal problems
            if channel.id in seen:
                return
            seen.add(channel.id)
            perms = channel.permissions_for(guild.me)
            if not perms.view_channel or not perms.read_message_history:
                if after is None:  # a catch-up has nothing to refresh where the bot can't read
                    problems += 1
                return
            try:
                await self._scan_channel(channel, guild.id, limit, after)
            except discord.HTTPException:
                problems += 1
                log.warning("Reaction history unavailable in channel %s", channel.id, exc_info=True)

        if selected_channel is not None:
            await scan(selected_channel)
            return problems

        for channel in guild.channels:
            if hasattr(channel, "history"):
                await scan(channel)
            if not hasattr(channel, "archived_threads") or after is not None:
                continue  # a catch-up skips archived threads: nothing new can be posted there
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

        if after is not None:
            return problems  # deleted channels are left to a full scan
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
                    self.gap_since.pop(guild.id, None)
        except Exception:
            problems, finished = 1, True
            log.exception("Reaction history scan failed in guild %s", guild.id)
        finally:
            checked = self.progress.pop(guild.id, 0)
            self.scope = None
            self._scan_changed = self._scan_deleted_channels = None
            if finished:
                self.store.finish_scan(guild.id, problems, channel.id if channel else None, limit, checked)
