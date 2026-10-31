"""Reaction accounting and recovery use stand-ins; no Discord connection is needed."""

import asyncio
import sqlite3
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock

import discord
import pytest

from reaction_stats import ReactionStore, ReactionTracker, emoji_key, parse_scan_limit


def run(coro):
    return asyncio.run(coro)


def user(uid, bot=False):
    return NS(id=uid, bot=bot)


def http_error(status):
    return discord.HTTPException(NS(status=status, reason="test"), "test") if status != 404 else discord.NotFound(NS(status=status, reason="test"), "test")


class Reaction:
    def __init__(self, emoji, normal=(), burst=(), error=False):
        self.emoji, self.normal, self.burst, self.error = emoji, normal, burst, error
        self.normal_count, self.burst_count = len(normal), len(burst)

    async def users(self, *, limit, type):
        for member in self.burst if type == discord.ReactionType.burst else self.normal:
            yield member
        if self.error:
            raise http_error(403)


class Channel:
    def __init__(self, cid, messages=(), allowed=True, fail_history=False):
        self.id, self.allowed, self.fail_history = cid, allowed, fail_history
        self.messages = {m.id: m for m in messages}
        self.fetched = []
        self.mention = f"<#{cid}>"

    def permissions_for(self, member):
        return NS(view_channel=self.allowed, read_message_history=self.allowed, manage_threads=False)

    async def fetch_message(self, mid):
        self.fetched.append(mid)
        if mid not in self.messages:
            raise http_error(404)
        return self.messages[mid]

    async def history(self, *, limit, oldest_first=False):
        assert not oldest_first
        for message in sorted(self.messages.values(), key=lambda m: m.id, reverse=True)[:limit]:
            yield message
        if self.fail_history:
            raise http_error(403)


def message(mid=100, author=None, reactions=(), webhook_id=None):
    return NS(id=mid, guild=NS(id=1), author=author or user(10),
              reactions=list(reactions), webhook_id=webhook_id, components=[])


class Client:
    def __init__(self, guilds=(), channels=()):
        self.guilds = guilds
        self.channels = {c.id: c for c in channels}

    def get_channel(self, cid):
        return self.channels.get(cid)

    async def fetch_channel(self, cid):
        if cid not in self.channels:
            raise http_error(404)
        return self.channels[cid]


def guild(channels=(), threads=()):
    class Guild:
        id = 1
        me = user(99, bot=True)

        async def active_threads(self):
            return threads

    g = Guild()
    g.channels, g.threads = list(channels), list(threads)
    return g


@pytest.mark.parametrize("text, expected", [("1", 1), ("1000", 1000), (" 250 ", 250), ("inf", None), (" INF ", None)])
def test_scan_limit_accepts_positive_counts_and_inf(text, expected):
    assert parse_scan_limit(text) == expected


@pytest.mark.parametrize("text", ["0", "-1", "1.5", "nan", "", "all", str(2**63)])
def test_scan_limit_rejects_invalid_values(text):
    with pytest.raises(ValueError):
        parse_scan_limit(text)


def test_scan_metadata_migrates_existing_database_and_keeps_totals(tmp_path):
    path = tmp_path / "links.db"
    db = sqlite3.connect(path)
    db.execute("CREATE TABLE reaction_scans (guild_id INTEGER PRIMARY KEY, finished_at INTEGER NOT NULL, problems INTEGER NOT NULL)")
    db.execute("INSERT INTO reaction_scans VALUES (1, 123, 0)")
    db.commit()
    db.close()
    store = ReactionStore(path)
    store.replace(1, 2, 100, 10, [(20, "unicode:❤️", "❤️")])
    assert store.db.execute("SELECT channel_id, message_limit, checked FROM reaction_scans").fetchone() == (None, None, 0)
    store.finish_scan(1, 0, channel_id=2, limit=1000, checked=50)
    store.db.close()
    store = ReactionStore(path)
    assert store.db.execute("SELECT channel_id, message_limit, checked FROM reaction_scans").fetchone() == (2, 1000, 50)
    assert store.summary(1, 10)[0] == 1


def test_store_persists_scopes_by_guild_and_filters_renamed_custom_emoji(tmp_path):
    path = tmp_path / "links.db"
    store = ReactionStore(path)
    entries = [(20, "unicode:❤️", "❤️"), (21, "custom:123", "<:old:123>")]
    store.replace(1, 2, 100, 10, entries + entries)
    store.replace(1, 2, 100, 10, entries)  # replay
    store.replace(2, 3, 200, 30, [(20, "unicode:❤️", "❤️")])
    store.finish_scan(1, 0)
    store.db.close()

    store = ReactionStore(path)
    assert store.summary(1, 10)[0] == 2
    assert store.summary(1, 30)[0] == 0
    assert store.leaderboard(1) == [(10, 2, "<:old:123>")]
    assert store.leaderboard(1, "❤️") == [(10, 1, "❤️")]
    for emoji in ("<:new:123>", "<a:new:123>", "123", NS(id=123)):
        assert emoji_key(emoji) == "custom:123"
        assert store.leaderboard(1, emoji) == [(10, 1, "<:old:123>")]
    assert store.leaderboard(1, "😂") == []
    assert store.summary(1, 10)[2] == (2, 100, 2)


def test_unchanged_snapshot_does_not_write_database_again(tmp_path):
    store = ReactionStore(tmp_path / "links.db")
    entries = [(20, "unicode:❤️", "❤️")]
    store.replace(1, 2, 100, 10, entries)
    changes = store.db.total_changes
    store.replace(1, 2, 100, 10, entries + entries)
    assert store.db.total_changes == changes
    store.replace(1, 2, 100, 10, entries + [(21, "unicode:😂", "😂")])
    assert store.summary(1, 10)[0] == 2
    assert store.db.total_changes > changes
    store.replace(1, 2, 100, 10, [])
    assert store.summary(1, 10)[0] == 0


def test_channel_filter_scopes_totals_breakdown_top_message_and_emoji_board(tmp_path):
    store = ReactionStore(tmp_path / "links.db")
    store.replace(1, 2, 100, 10, [(20, "unicode:❤️", "❤️"), (21, "unicode:😂", "😂")])
    store.replace(1, 3, 101, 10, [(20, "unicode:😂", "😂")])
    store.replace(1, 3, 102, 30, [(20, "unicode:❤️", "❤️"), (21, "unicode:❤️", "❤️")])
    store.replace(9, 3, 103, 50, [(20, "unicode:😂", "😂")])

    assert store.summary(1, 10)[0] == 3
    assert store.summary(1, 10, channel_id=3) == (1, [("😂", 1)], (3, 101, 1))
    assert store.summary(1, 10, channel_id=4) == (0, [], None)
    assert store.leaderboard(1) == [(10, 3, "😂"), (30, 2, "❤️")]
    assert store.leaderboard(1, channel_id=3) == [(30, 2, "❤️"), (10, 1, "😂")]
    assert store.leaderboard(1, "😂", channel_id=3) == [(10, 1, "😂")]
    assert store.leaderboard(1, "❤️", channel_id=3) == [(30, 2, "❤️")]
    assert store.leaderboard(1, channel_id=4) == []


def test_snapshot_excludes_bots_and_self_and_unions_super_reactions(tmp_path):
    m = message(reactions=[Reaction("😂", [user(10), user(20), user(99, True)], [user(20), user(21)]),
                           Reaction("❤️", [user(20)])])
    channel = Channel(2, [m])
    tracker = ReactionTracker(Client(channels=[channel]), lambda m: None, tmp_path / "links.db")
    payload = NS(guild_id=1, channel_id=2, message_id=100)

    async def flow():
        await tracker.on_reaction(payload)
        await tracker.on_reaction(payload)
        assert tracker.store.summary(1, 10)[0] == 3
        m.reactions = [Reaction("❤️", [user(20)])]
        await tracker.on_reaction(payload)
        assert tracker.store.summary(1, 10)[0] == 1
        m.reactions = []
        await tracker.on_reaction(payload)
        assert tracker.store.summary(1, 10)[0] == 0

    run(flow())


def test_webhook_ownership_survives_owner_table_pruning_and_ignores_other_bots(tmp_path):
    owners = {100: 10}
    m = message(author=user(99, True), webhook_id=7,
                reactions=[Reaction("❤️", [user(10), user(20)])])
    channel = Channel(2, [m])
    tracker = ReactionTracker(Client(), lambda m: owners.get(m.id), tmp_path / "links.db")

    async def flow():
        await tracker.sync_message(channel, 100)
        owners.clear()
        await tracker.sync_message(channel, 100)
        assert tracker.store.summary(1, 10)[0] == 1
        # Foreign webhook and ordinary bot messages don't earn reactions.
        channel.messages[101] = message(101, user(99, True), m.reactions, webhook_id=8)
        channel.messages[102] = message(102, user(99, True), m.reactions)
        await tracker.sync_message(channel, 101)
        await tracker.sync_message(channel, 102)
        assert tracker.store.leaderboard(1) == [(10, 1, "❤️")]

    run(flow())


def test_partial_snapshot_does_not_erase_previous_counts_and_missing_message_does(tmp_path):
    m = message(reactions=[Reaction("❤️", [user(20)])])
    channel = Channel(2, [m])
    tracker = ReactionTracker(Client(channels=[channel]), lambda m: None, tmp_path / "links.db")

    async def flow():
        await tracker.sync_message(channel, 100)
        m.reactions = [Reaction("😂", [user(21)], error=True)]
        await tracker.on_reaction(NS(guild_id=1, channel_id=2, message_id=100))
        assert tracker.store.summary(1, 10)[:2] == (1, [("❤️", 1)])
        channel.messages.clear()
        await tracker.sync_message(channel, 100)
        assert tracker.store.summary(1, 10)[0] == 0

    run(flow())


def test_live_error_is_reported_until_a_successful_reconciliation(tmp_path):
    m = message(reactions=[Reaction("❤️", [user(20)], error=True)])
    channel = Channel(2, [m])
    tracker = ReactionTracker(Client([guild([channel])], [channel]), lambda m: None, tmp_path / "links.db")
    tracker.store.finish_scan(1, 0)

    async def flow():
        await tracker.on_reaction(NS(guild_id=1, channel_id=2, message_id=100))
        assert "live updates failed" in tracker.status(1)
        m.reactions[0].error = False
        tracker.start_scan(tracker.client.guilds[0])
        await tracker.task
        assert "live updates enabled" in tracker.status(1)
        assert tracker.store.summary(1, 10)[0] == 1

    run(flow())


def test_deleted_uncached_channel_clears_counts_and_dms_are_ignored(tmp_path):
    tracker = ReactionTracker(Client(), lambda m: None, tmp_path / "links.db")
    tracker.store.replace(1, 2, 100, 10, [(20, "unicode:❤️", "❤️")])

    async def flow():
        await tracker.on_reaction(NS(guild_id=None))
        assert tracker.store.summary(1, 10)[0] == 1
        await tracker.on_reaction(NS(guild_id=1, channel_id=2, message_id=100))
        assert tracker.store.summary(1, 10)[0] == 0

    run(flow())


def test_schema_creation_preserves_existing_repost_records(tmp_path):
    import utils

    path = tmp_path / "links.db"
    owners = utils.RepostOwners(path)
    owners.add(100, 10)
    store = ReactionStore(path)
    store.replace(1, 2, 100, 10, [(20, "unicode:❤️", "❤️")])
    ReactionStore(path)  # idempotent startup
    assert owners.owner(100) == 10
    assert store.summary(1, 10)[0] == 1


def test_history_reconciles_offline_changes_deletions_and_archived_threads(tmp_path):
    old = message(100, reactions=[])  # its reaction was removed while offline
    new = message(102, reactions=[Reaction("❤️", [user(20)])])
    archived = Channel(3, [message(103, reactions=[Reaction("😂", [user(21)])])])

    class Parent(Channel):
        async def archived_threads(self, **kwargs):
            assert kwargs == {"limit": None}
            yield archived

    channel = Parent(2, [old, new, message(104)])
    active = Channel(4, [message(105, reactions=[Reaction("❤️", [user(22)])])])
    g = guild([channel], [active])
    tracker = ReactionTracker(Client([g], [channel, archived, active]), lambda m: None, tmp_path / "links.db")
    for cid, mid in [(2, 100), (2, 101), (50, 106)]:
        tracker.store.replace(1, cid, mid, 10, [(20, "unicode:❤️", "❤️")])

    async def flow():
        assert tracker.start_scan(g)
        task = tracker.task
        assert not tracker.start_scan(g)  # duplicate commands must not queue extra scans
        assert tracker.task is task
        await task
        assert tracker.store.summary(1, 10)[0] == 3
        assert tracker.store.message_ids(2) == {102}
        assert tracker.store.message_ids(50) == set()  # channel deleted offline
        assert 104 not in channel.fetched  # no API fetch for unreacted, unknown messages
        assert "live updates enabled" in tracker.status(1)

    run(flow())


def test_scan_reuses_history_without_individual_message_fetches(tmp_path):
    channel = Channel(2, [message(100 + i, reactions=[Reaction("❤️", [user(20)])]) for i in range(80)])
    tracker = ReactionTracker(Client(channels=[channel]), lambda m: None, tmp_path / "links.db")

    async def flow():
        tracker.start_scan(guild([channel]), channel=channel)
        await tracker.task
        assert tracker.store.summary(1, 10)[0] == 80
        assert channel.fetched == []  # previously this scan made 80 redundant message requests

    run(flow())


def test_live_event_processed_before_stale_history_cannot_be_overwritten(tmp_path):
    stale = message(100, reactions=[Reaction("❤️", [user(20)])])
    current = message(100, reactions=[Reaction("😂", [user(21)])])

    class ChangingChannel(Channel):
        async def history(self, **kwargs):
            # A history page was fetched, then a live update completed before we visited its message.
            self.messages[100] = current
            await tracker.on_reaction(NS(guild_id=1, channel_id=2, message_id=100))
            yield stale

    channel = ChangingChannel(2, [stale])
    tracker = ReactionTracker(Client(channels=[channel]), lambda m: None, tmp_path / "links.db")

    async def flow():
        tracker.start_scan(guild([channel]), channel=channel)
        await tracker.task
        assert tracker.store.summary(1, 10)[:2] == (1, [("😂", 1)])
        assert channel.fetched == [100, 100]  # live event + fresh fetch for the changed snapshot

    run(flow())


@pytest.mark.parametrize("delete_channel", [False, True])
def test_deletions_before_stale_history_do_not_resurrect_counts(tmp_path, delete_channel):
    stale = message(100, reactions=[Reaction("❤️", [user(20)])])

    class DeletingChannel(Channel):
        async def history(self, **kwargs):
            self.messages.clear()
            if delete_channel:
                await tracker.delete_channel(self.id)
            else:
                await tracker.delete_messages([100])
            yield stale

    channel = DeletingChannel(2, [stale])
    tracker = ReactionTracker(Client(channels=[channel]), lambda m: None, tmp_path / "links.db")
    tracker.store.replace(1, 2, 100, 10, [(20, "unicode:❤️", "❤️")])

    async def flow():
        tracker.start_scan(guild([channel]), channel=channel)
        await tracker.task
        assert tracker.store.summary(1, 10)[0] == 0
        assert tracker._scan_changed is None
        assert tracker._scan_deleted_channels is None

    run(flow())


def test_incomplete_history_keeps_unseen_records_and_reports_partial_coverage(tmp_path):
    channel = Channel(2, fail_history=True)
    hidden = Channel(3, allowed=False)
    tracker = ReactionTracker(Client([guild([channel, hidden])], [channel, hidden]), lambda m: None, tmp_path / "links.db")
    tracker.store.replace(1, 2, 100, 10, [(20, "unicode:❤️", "❤️")])

    async def flow():
        tracker.start_scan(tracker.client.guilds[0])
        await tracker.task

    run(flow())
    assert tracker.store.summary(1, 10)[0] == 1
    assert "2 unavailable channels or scan errors" in tracker.status(1)


def test_bounded_scan_keeps_older_totals_and_prunes_only_deleted_messages_in_range(tmp_path):
    channel = Channel(2, [message(100), message(103, reactions=[Reaction("😂", [user(21)])]), message(105)])
    tracker = ReactionTracker(Client(channels=[channel]), lambda m: None, tmp_path / "links.db")
    for mid in (100, 104, 105):
        tracker.store.replace(1, 2, mid, 10, [(20, "unicode:❤️", "❤️")])
    tracker.note_gap(1)

    async def flow():
        assert tracker.start_scan(guild([channel]), limit=2, channel=channel)
        assert "last 2 messages" in tracker.status(1)
        await tracker.task
        assert tracker.store.message_ids(2) == {100, 103}
        assert tracker.store.summary(1, 10)[0] == 2
        assert 100 not in channel.fetched  # untouched old reaction
        assert "last 2 messages" in tracker.status(1)
        assert "2 checked" in tracker.status(1)
        assert 1 in tracker.needs_scan

    run(flow())


def test_bounded_scan_that_exhausts_history_can_remove_deleted_older_records(tmp_path):
    channel = Channel(2, [message(105)])
    tracker = ReactionTracker(Client(), lambda m: None, tmp_path / "links.db")
    tracker.store.replace(1, 2, 100, 10, [(20, "unicode:❤️", "❤️")])

    async def flow():
        tracker.start_scan(guild([channel]), limit=10, channel=channel)
        await tracker.task
        assert tracker.store.message_ids(2) == set()

    run(flow())


def test_server_scan_limit_applies_per_channel_and_does_not_touch_other_guilds(tmp_path):
    one = Channel(2, [message(100), message(101, reactions=[Reaction("😂", [user(20)])])])
    two = Channel(3, [message(102), message(103, reactions=[Reaction("❤️", [user(21)])])])
    other = guild([Channel(4, [message(104)])])
    other.id = 9
    g = guild([one, two])
    tracker = ReactionTracker(Client([g, other], [one, two]), lambda m: None, tmp_path / "links.db")
    tracker.store.replace(9, 4, 104, 30, [(20, "unicode:❤️", "❤️")])

    async def flow():
        tracker.start_scan(g, limit=1)
        await tracker.task
        assert tracker.store.summary(1, 10)[0] == 2
        assert tracker.store.summary(9, 30)[0] == 1
        assert "last 1 per channel/thread" in tracker.status(1)
        assert "2 checked" in tracker.status(1)

    run(flow())


def test_single_channel_scan_does_not_discover_or_scan_threads(tmp_path):
    class Parent(Channel):
        async def archived_threads(self, **kwargs):
            raise AssertionError("A single channel scan must not enumerate threads")
            yield

    channel = Parent(2, [message(100, reactions=[Reaction("❤️", [user(20)])])])
    tracker = ReactionTracker(Client(), lambda m: None, tmp_path / "links.db")
    g = guild([channel])

    async def active_threads():
        raise AssertionError("A single channel scan must not enumerate active threads")

    g.active_threads = active_threads

    async def flow():
        tracker.start_scan(g, channel=channel)
        await tracker.task
        assert tracker.store.summary(1, 10)[0] == 1
        assert "channel/thread 2" in tracker.status(1)
        assert "all history" in tracker.status(1)

    run(flow())


def test_reconnect_during_scan_keeps_offline_warning(tmp_path):
    tracker = ReactionTracker(Client(), lambda m: None, tmp_path / "links.db")

    class ReconnectingChannel(Channel):
        async def history(self, **kwargs):
            tracker.note_gap(1)
            yield message(100)

    channel = ReconnectingChannel(2)
    tracker.note_gap(1)

    async def flow():
        tracker.start_scan(guild([channel]))
        await tracker.task
        assert "offline changes" in tracker.status(1)

    run(flow())


def test_archived_private_threads_are_discovered_with_available_permissions(tmp_path, monkeypatch):
    class TextChannel(Channel):
        def __init__(self, *args, manage_threads=False, **kwargs):
            super().__init__(*args, **kwargs)
            self.manage_threads, self.options = manage_threads, []

        def permissions_for(self, member):
            return NS(view_channel=True, read_message_history=True, manage_threads=self.manage_threads)

        async def archived_threads(self, **kwargs):
            self.options.append(kwargs)
            if kwargs.get("private"):
                yield archived

    archived = Channel(3, [message(103, reactions=[Reaction("😂", [user(21)])])])
    monkeypatch.setattr(discord, "TextChannel", TextChannel)
    channel = TextChannel(2)
    tracker = ReactionTracker(Client([guild([channel])], [channel]), lambda m: None, tmp_path / "links.db")

    async def flow():
        tracker.start_scan(tracker.client.guilds[0])
        await tracker.task
        assert {"limit": None, "private": True, "joined": True} in channel.options
        channel.options.clear()
        channel.manage_threads = True
        tracker.start_scan(tracker.client.guilds[0])
        await tracker.task
        assert {"limit": None, "private": True, "joined": False} in channel.options
        assert tracker.store.summary(1, 10)[0] == 1

    run(flow())


def test_live_message_added_during_scan_is_not_pruned(tmp_path):
    tracker = ReactionTracker(Client(), lambda m: None, tmp_path / "links.db")

    class GrowingChannel(Channel):
        async def history(self, **kwargs):
            tracker.store.replace(1, 2, 101, 10, [(20, "unicode:❤️", "❤️")])
            yield message(100)

    async def flow():
        tracker.progress[1] = 0
        await tracker._scan_channel(GrowingChannel(2), 1)
        assert tracker.store.summary(1, 10)[0] == 1

    run(flow())


@pytest.mark.parametrize("from_history", [False, True])
def test_live_removal_during_history_fetch_cannot_be_overwritten(tmp_path, from_history):
    async def flow():
        started, release = asyncio.Event(), asyncio.Event()

        class SlowReaction(Reaction):
            async def users(self, **kwargs):
                started.set()
                await release.wait()
                yield user(20)

        m = message(reactions=[SlowReaction("❤️", [user(20)])])
        channel = Channel(2, [m])
        tracker = ReactionTracker(Client(channels=[channel]), lambda m: None, tmp_path / "links.db")
        if from_history:
            tracker.start_scan(guild([channel]), channel=channel)
            scan = tracker.task
        else:
            scan = asyncio.create_task(tracker.sync_message(channel, 100))
        await started.wait()
        m.reactions = []
        event = asyncio.create_task(tracker.on_reaction(NS(guild_id=1, channel_id=2, message_id=100)))
        release.set()
        await asyncio.gather(scan, event)
        assert tracker.store.summary(1, 10)[0] == 0

    run(flow())


def test_commands_and_raw_event_registration(bot_closures, monkeypatch):
    bot = bot_closures
    tracker, client = bot["reaction_tracker"], bot["client"]
    tracker.store.replace(1, 2, 100, 10, [(20, "unicode:❤️", "❤️"), (21, "unicode:😂", "😂")])
    tracker.store.replace(1, 2, 101, 30, [(20, "unicode:❤️", "❤️")])
    tracker.store.replace(9, 2, 102, 50, [(20, "unicode:❤️", "❤️")])
    monkeypatch.setattr(client, "get_channel", lambda cid: Channel(cid))

    class Response:
        async def send_message(self, **kwargs):
            self.kwargs = kwargs

    i = NS(guild_id=1, user=NS(id=10, display_name="VB"), response=Response())

    async def flow():
        await bot["reaction_totals"].callback(i)
        embed = i.response.kwargs["embed"]
        assert "**2** reactions received" in embed.description
        fields = {field.name: field.value for field in embed.fields}
        assert "/1/2/100" in fields["Most-reacted message"]
        assert "Live tracking only" in fields["History coverage"]
        assert not i.response.kwargs["allowed_mentions"].users
        await bot["reaction_totals"].callback(i, NS(id=99, display_name="Nobody"))
        assert "No reactions counted yet" in i.response.kwargs["embed"].description
        await bot["reaction_board"].callback(i, "❤️")
        embed = i.response.kwargs["embed"]
        assert "<@10>" in embed.description and "<@30>" in embed.description
        assert "<@10> — **1** · ❤️" in embed.description
        assert "<@30> — **1** · ❤️" in embed.description
        assert "<@50>" not in embed.description
        assert "**2**" not in embed.description

        calls = []

        async def on_reaction(payload):
            calls.append(payload)

        monkeypatch.setattr(tracker, "on_reaction", on_reaction)
        for event in ("on_raw_reaction_add", "on_raw_reaction_remove", "on_raw_reaction_clear", "on_raw_reaction_clear_emoji"):
            assert getattr(client, event) is bot[event]
            await bot[event](event)
        assert len(calls) == 4
        await bot["on_raw_bulk_message_delete"](NS(message_ids={100, 101}))
        assert tracker.store.leaderboard(1) == []
        tracker.store.replace(1, 2, 103, 10, [(20, "unicode:❤️", "❤️")])
        bot["repost_owners"].add(103, 10)
        await bot["on_raw_message_delete"](NS(message_id=103))
        assert tracker.store.leaderboard(1) == []
        assert bot["repost_owners"].owner(103) is None
        await bot["on_raw_thread_delete"](NS(thread_id=2))
        assert tracker.store.leaderboard(9) == []
        called = []
        monkeypatch.setattr(tracker, "start_scan", lambda: called.append(True))
        monkeypatch.setattr(client._connection, "_guilds", {1: guild()})
        await bot["on_resumed"]()
        assert called == [] and tracker.task is None
        assert 1 in tracker.needs_scan

    run(flow())
    assert bot["reaction_totals"].guild_only and bot["reaction_board"].guild_only


def test_most_reacted_message_is_hidden_without_channel_access(bot_closures, monkeypatch):
    bot = bot_closures
    bot["reaction_tracker"].store.replace(1, 2, 100, 10, [(20, "unicode:❤️", "❤️")])
    monkeypatch.setattr(bot["client"], "get_channel", lambda cid: Channel(cid, allowed=False))

    class Response:
        async def send_message(self, **kwargs):
            assert all(field.name != "Most-reacted message" for field in kwargs["embed"].fields)

    run(bot["reaction_totals"].callback(NS(guild_id=1, user=NS(id=10, display_name="VB"), response=Response())))


def test_commands_combine_channel_and_emoji_filters_and_show_scope(bot_closures):
    bot = bot_closures
    store = bot["reaction_tracker"].store
    store.replace(1, 2, 100, 10, [(20, "unicode:❤️", "❤️"), (21, "unicode:😂", "😂")])
    store.replace(1, 3, 101, 10, [(20, "unicode:😂", "😂")])
    store.replace(1, 3, 102, 30, [(20, "unicode:❤️", "❤️")])

    class Response:
        async def send_message(self, **kwargs):
            self.kwargs = kwargs

    i = NS(guild_id=1, user=NS(id=10, display_name="VB"), response=Response())

    async def flow():
        await bot["reaction_totals"].callback(i, channel=Channel(3))
        embed = i.response.kwargs["embed"]
        assert "**1** reactions received" in embed.description
        fields = {field.name: field.value for field in embed.fields}
        assert "😂" in fields["By emoji"] and "❤️" not in fields["By emoji"]
        assert "<#3>" in embed.description
        assert "/1/3/101" in fields["Most-reacted message"]
        await bot["reaction_board"].callback(i, "❤️", Channel(3))
        embed = i.response.kwargs["embed"]
        assert "<@30>" in embed.description and "<@10>" not in embed.description
        assert "<@30> — **1** · ❤️" in embed.description
        assert embed.fields[0].value == "❤️" and embed.fields[1].value == "<#3>"
        await bot["reaction_board"].callback(i, channel=Channel(4))
        assert "No reactions counted" in i.response.kwargs["embed"].description
        await bot["reaction_totals"].callback(i, NS(id=30, display_name="Alex"), Channel(3))
        assert "Alex" in i.response.kwargs["embed"].title
        assert "❤️" in next(field.value for field in i.response.kwargs["embed"].fields if field.name == "By emoji")

    run(flow())
    for command in (bot["reaction_totals"], bot["reaction_board"]):
        param = command.get_parameter("channel")
        assert not param.required
        assert param.type is discord.AppCommandOptionType.channel
        for kind in (discord.ChannelType.text, discord.ChannelType.public_thread, discord.ChannelType.private_thread):
            assert kind in param.channel_types


def test_channel_filter_requires_view_and_history_permissions(bot_closures):
    bot = bot_closures

    class Response:
        async def send_message(self, content, **kwargs):
            assert "read message history" in content
            assert kwargs["ephemeral"]

    class NoHistory(Channel):
        def permissions_for(self, member):
            return NS(view_channel=True, read_message_history=False)

    i = NS(guild_id=1, user=NS(id=10, display_name="VB"), response=Response())

    async def flow():
        for channel in (Channel(2, allowed=False), NoHistory(3)):
            await bot["reaction_totals"].callback(i, channel=channel)
            await bot["reaction_board"].callback(i, channel=channel)

    run(flow())


def scan_interaction(channel, admin=True):
    class Response:
        async def send_message(self, content, **kwargs):
            self.content, self.kwargs = content, kwargs

    return NS(guild_id=1, guild=guild([channel]), channel=channel,
              user=NS(id=10, guild_permissions=NS(administrator=admin)), response=Response())


def test_admin_scan_defaults_to_current_channel_and_rejects_overlap(bot_closures):
    bot = bot_closures
    tracker = bot["reaction_tracker"]
    channel = Channel(2, [message(100), message(101, reactions=[Reaction("❤️", [user(20)])])])
    i = scan_interaction(channel)
    j = scan_interaction(channel)

    async def flow():
        await bot["reaction_scan"].callback(i, "1")
        assert "last 1 messages" in i.response.content
        assert "<#2>" in i.response.content and i.response.kwargs["ephemeral"]
        assert "running" in tracker.status(1)
        task = tracker.task
        await bot["reaction_scan"].callback(j, "inf")
        assert "already running" in j.response.content
        assert tracker.task is task
        await task
        assert tracker.store.summary(1, 10)[0] == 1
        assert "last 1 messages" in tracker.status(1)

    run(flow())
    assert bot["reaction_scan"].guild_only
    assert bot["reaction_scan"].default_permissions.administrator


def test_admin_scan_accepts_selected_thread_and_server_wide_inf(bot_closures):
    bot = bot_closures
    tracker = bot["reaction_tracker"]
    channel = Channel(2, [message(100)])
    thread = Channel(3, [message(101, reactions=[Reaction("❤️", [user(20)])])])
    i = scan_interaction(channel)
    i.guild.threads = [thread]

    async def active_threads():
        return [thread]

    i.guild.active_threads = active_threads

    async def flow():
        await bot["reaction_scan"].callback(i, "inf", thread)
        assert "all history in <#3>" in i.response.content
        await tracker.task
        assert tracker.store.summary(1, 10)[0] == 1
        tracker.note_gap(1)
        await bot["reaction_scan"].callback(i, "inf", all_channels=True)
        assert "all history in every accessible channel/thread" in i.response.content
        await tracker.task
        assert 1 not in tracker.needs_scan
        assert "all server history" in tracker.status(1)
        await bot["reaction_scan"].callback(i, "1", all_channels=True)
        assert "per channel/thread" in i.response.content
        await tracker.task

    run(flow())


@pytest.mark.parametrize("limit, admin, all_channels, selected, expected", [
    ("1000", False, False, False, "Only server administrators"),
    ("0", True, False, False, "positive message count"),
    ("-5", True, False, False, "positive message count"),
    ("oops", True, False, False, "positive message count"),
    ("inf", True, True, True, "either a channel or all_channels"),
])
def test_admin_scan_rejects_unauthorized_or_invalid_requests(bot_closures, limit, admin, all_channels, selected, expected):
    channel = Channel(2)
    i = scan_interaction(channel, admin=admin)
    run(bot_closures["reaction_scan"].callback(i, limit, channel if selected else None, all_channels))
    assert expected in i.response.content
    assert i.response.kwargs["ephemeral"]
    assert bot_closures["reaction_tracker"].task is None


def test_scan_refuses_a_channel_the_bot_cannot_read(bot_closures):
    class BotHiddenChannel(Channel):
        def permissions_for(self, member):
            return NS(view_channel=not getattr(member, "bot", False), read_message_history=True)

    i = scan_interaction(BotHiddenChannel(2))
    run(bot_closures["reaction_scan"].callback(i, "1000"))
    assert "I need View Channel" in i.response.content
    assert bot_closures["reaction_tracker"].task is None


def test_scan_does_not_fall_back_to_whole_server_if_current_channel_is_missing(bot_closures):
    i = scan_interaction(Channel(2))
    i.channel = None
    run(bot_closures["reaction_scan"].callback(i, "inf"))
    assert "Choose a channel" in i.response.content
    assert bot_closures["reaction_tracker"].task is None


def test_ready_and_resumed_never_start_history_scans(bot_closures, monkeypatch):
    import bot as bot_module

    bot = bot_closures
    g = guild()
    g.system_channel = None
    monkeypatch.setattr(bot["client"]._connection, "_guilds", {1: g})
    monkeypatch.setattr(bot["client"], "change_presence", AsyncMock())
    monkeypatch.setattr(bot["client"], "fetch_application_emojis", AsyncMock(return_value=[]))
    monkeypatch.setattr(bot["tree"], "sync", AsyncMock(return_value=[]))
    monkeypatch.setattr(bot["update_presence"], "is_running", lambda: True)
    monkeypatch.setattr(bot["check_reactions"], "is_running", lambda: True)
    monkeypatch.setattr(bot_module.utils, "get_cpu_temp", lambda: 42)
    monkeypatch.setattr(bot_module.glob, "glob", lambda pattern: [])

    def forbidden_scan(*args, **kwargs):
        raise AssertionError("Startup and reconnects must never scan history")

    monkeypatch.setattr(bot["reaction_tracker"], "start_scan", forbidden_scan)

    async def flow():
        await bot["on_ready"]()
        await bot["on_resumed"]()
        assert bot["reaction_tracker"].task is None
        assert 1 in bot["reaction_tracker"].needs_scan

    run(flow())
