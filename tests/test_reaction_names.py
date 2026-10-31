"""Board names for people who left, webhook ids never counted as people, and clearer coverage."""

import asyncio
from types import SimpleNamespace as NS

import discord

from reaction_stats import ReactionTracker
from test_reaction_stats import Channel, Client, Reaction, guild, message, user


def run(coro):
    return asyncio.run(coro)


def test_board_names_people_who_left(bot_closures, monkeypatch):
    bot = bot_closures
    client = bot["client"]
    store = bot["reaction_tracker"].store
    store.replace(1, 2, 100, 10, [(20, "unicode:😂", "😂")])  # 10 is still here
    store.replace(1, 2, 101, 30, [(20, "unicode:😂", "😂")])  # 30 left; Discord still knows them
    store.replace(1, 2, 102, 40, [(20, "unicode:😂", "😂")])  # 40's account is gone
    monkeypatch.setattr(client, "get_channel", lambda cid: Channel(cid))
    monkeypatch.setattr(client, "get_guild", lambda gid: NS(id=gid, get_member=lambda uid: NS(id=uid) if uid == 10 else None))
    monkeypatch.setattr(client, "get_user", lambda uid: None)
    asked = []

    async def fetch_user(uid):
        asked.append(uid)
        if uid == 30:
            return NS(id=30, display_name="Old_Pal")
        raise discord.NotFound(NS(status=404, reason="x"), "Unknown User")

    monkeypatch.setattr(client, "fetch_user", fetch_user)

    class Response:
        async def send_message(self, **kwargs):
            self.kwargs = kwargs

    async def board():
        i = NS(guild_id=1, user=NS(id=10, display_name="VB"), response=Response())
        await bot["reaction_board"].callback(i)
        return i.response.kwargs["embed"].description

    text = run(board())
    assert "<@10> — **1**" in text and "Old\\_Pal (left) — **1**" in text and "former member — **1**" in text
    assert "<@30>" not in text and "<@40>" not in text
    run(board())
    assert sorted(asked) == [30, 40]  # looked up once


def test_old_repost_crediting_the_webhook_counts_for_nobody(tmp_path):
    webhook_id = 777
    repost = message(100, author=user(webhook_id), reactions=[Reaction("😂", [user(20)])], webhook_id=webhook_id)
    channel = Channel(2, [repost])
    tracker = ReactionTracker(Client(channels=[channel]), lambda m: webhook_id, tmp_path / "links.db")
    run(tracker.sync_message(channel, 100))
    assert tracker.store.owner(100) is None and tracker.store.leaderboard(1) == []


def test_bot_repost_owner_ignores_a_webhook_id_in_the_delete_button(bot_closures):
    button = NS(custom_id="dellink:777")
    old = NS(id=5, webhook_id=777, components=[NS(children=[button])])
    assert bot_closures["repost_owner"](old) is None
    button.custom_id = "dellink:42"
    assert bot_closures["repost_owner"](old) == 42


def test_coverage_separates_hidden_channels_from_errors(tmp_path):
    hidden = Channel(3, allowed=False)
    readable = Channel(2, [message(100, reactions=[Reaction("😂", [user(20)])])])
    tracker = ReactionTracker(Client([guild([readable, hidden])], [readable, hidden]), lambda m: None, tmp_path / "links.db")

    async def flow():
        tracker.start_scan(tracker.client.guilds[0])
        await tracker.task

    run(flow())
    status = tracker.status(1)
    assert "1 channel the bot can't read isn't counted" in status
    assert "error" not in status and "incomplete" not in status and "live updates enabled" in status
