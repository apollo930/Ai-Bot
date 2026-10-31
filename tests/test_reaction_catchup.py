"""Emoji filter spelling, and the bounded catch-up after a restart."""

import asyncio
import datetime
import time

import discord

from reaction_stats import CATCHUP_MARGIN, ReactionStore, ReactionTracker
from test_reaction_stats import Channel, Client, Reaction, guild, message, user


def run(coro):
    return asyncio.run(coro)


def test_emoji_filter_matches_either_spelling(tmp_path):
    store = ReactionStore(tmp_path / "links.db")
    store.replace(1, 2, 100, 10, [(20, "unicode:❤️", "❤️"), (21, "unicode:❤️", "❤️")])
    store.replace(1, 2, 101, 11, [(20, "unicode:❤", "❤")])
    for typed in ("❤", "❤️"):
        rows = store.leaderboard(1, typed)
        assert [(author, total) for author, total, _ in rows] == [(10, 2), (11, 1)], typed
    assert store.leaderboard(1, "😂") == []


def snowflake(seconds_ago):
    return discord.utils.time_snowflake(datetime.datetime.fromtimestamp(time.time() - seconds_ago, datetime.timezone.utc))


class WindowChannel(Channel):
    """A channel whose history honours after=, and remembers what it was asked."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.asked_after = []

    async def history(self, *, limit, oldest_first=False, after=None):
        self.asked_after.append(after.id if after else None)
        for m in sorted(self.messages.values(), key=lambda m: m.id, reverse=True)[:limit]:
            if after is None or m.id > after.id:
                yield m


def test_catchup_rechecks_only_recent_messages_and_clears_the_warning(tmp_path):
    path = tmp_path / "links.db"
    old_id, gone_id, new_id = snowflake(3 * 86400), snowflake(CATCHUP_MARGIN + 600 - 3600), snowflake(60)
    first = ReactionTracker(Client(), lambda m: None, path)
    first.store.replace(1, 2, old_id, 10, [(20, "unicode:😂", "😂")])
    first.store.replace(1, 2, gone_id, 10, [(20, "unicode:😂", "😂")])  # deleted while the bot was away
    first.store.mark_alive([1], int(time.time()) - 3600)  # last alive an hour ago
    first.store.db.close()

    old = message(old_id, reactions=[Reaction("😂", normal=[user(20), user(21)])])  # changed, but too old
    new = message(new_id, reactions=[Reaction("🔥", normal=[user(22)])])
    channel = WindowChannel(2, [old, new])
    tracker = ReactionTracker(Client(channels=[channel]), lambda m: None, path)
    tracker.note_gap(1)
    assert "offline changes" in tracker.status(1) or "Live tracking" in tracker.status(1)

    async def flow():
        assert tracker.start_catchup([guild([channel])])
        assert "catching up after a restart" in tracker.status(1)
        await tracker.task

    run(flow())
    assert channel.asked_after == [snowflake_floor(time.time() - 3600 - CATCHUP_MARGIN, channel.asked_after[0])]
    totals = dict((e, n) for e, n in tracker.store.summary(1, 10)[1])
    assert totals == {"😂": 1, "🔥": 1}  # the old message kept its stored count; the new one counted
    assert tracker.store.owner(gone_id) is None  # pruned: inside the window and no longer in history
    assert 1 not in tracker.needs_scan


def snowflake_floor(when, asked):
    # time_snowflake of the same second: only the low bits can differ, so compare by timestamp
    assert abs(discord.utils.snowflake_time(asked).timestamp() - when) < 2
    return asked


def test_no_known_alive_time_means_no_catchup(tmp_path):
    tracker = ReactionTracker(Client(), lambda m: None, tmp_path / "links.db")
    tracker.note_gap(1)
    assert tracker.start_catchup([guild([])]) is False
    assert tracker.task is None and 1 in tracker.needs_scan


def test_heartbeat_is_what_the_next_start_reads(tmp_path):
    path = tmp_path / "links.db"
    tracker = ReactionTracker(Client(), lambda m: None, path)
    tracker.heartbeat([1])
    later = ReactionTracker(Client(), lambda m: None, path)
    assert abs(later.alive_at[1] - time.time()) < 5
    later.heartbeat([1])  # a heartbeat after start doesn't move the first gap's start
    later._boot_alive[1] = 1000
    later.note_gap(1)
    assert later.gap_since[1] == 1000


def test_catchup_waits_for_a_running_scan(tmp_path):
    path = tmp_path / "links.db"
    store = ReactionStore(path)
    store.mark_alive([1], int(time.time()))
    store.db.close()
    tracker = ReactionTracker(Client(), lambda m: None, path)
    tracker.note_gap(1)

    async def flow():
        slow = asyncio.Event()
        tracker.task = asyncio.create_task(slow.wait())
        assert tracker.start_catchup([guild([])]) is False
        slow.set()
        await tracker.task

    run(flow())


def test_catchup_covers_every_guild_one_after_another(tmp_path):
    path = tmp_path / "links.db"
    store = ReactionStore(path)
    store.mark_alive([1, 2], int(time.time()) - 60)
    store.db.close()
    a, b = WindowChannel(10, [message(snowflake(30))]), WindowChannel(20, [message(snowflake(30))])
    g1, g2 = guild([a]), guild([b])
    g2.id = 2
    tracker = ReactionTracker(Client(channels=[a, b]), lambda m: None, path)
    tracker.note_gap(1)
    tracker.note_gap(2)

    async def flow():
        assert tracker.start_catchup([g1, g2])
        await tracker.task

    run(flow())
    assert len(a.asked_after) == 1 and len(b.asked_after) == 1
    assert not tracker.needs_scan and set(tracker.caught_up) == {1, 2}
