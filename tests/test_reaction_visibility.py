"""Server-wide reaction totals only count channels the person asking can read."""

import asyncio
from types import SimpleNamespace as NS

import discord

from reaction_stats import ReactionTracker
from test_reaction_stats import Channel, Client


def run(coro):
    return asyncio.run(coro)


class Response:
    def __init__(self):
        self.kwargs, self.deferred = None, False

    async def send_message(self, **kwargs):
        self.kwargs = kwargs

    async def defer(self, **kwargs):
        self.deferred = True


def asking(user_id=10):
    response = Response()
    i = NS(guild_id=1, user=NS(id=user_id, display_name="VB"), response=response)

    async def edit_original_response(**kwargs):
        response.kwargs = kwargs

    i.edit_original_response = edit_original_response
    return i


def seed(store):
    store.replace(1, 2, 100, 10, [(20, "unicode:😂", "😂")])               # public channel
    store.replace(1, 3, 101, 10, [(20, "unicode:❤️", "❤️"), (21, "unicode:❤️", "❤️")])  # hidden from the asker
    store.replace(1, 4, 102, 30, [(20, "unicode:🔥", "🔥")])               # archived thread, not in memory


def test_board_and_totals_leave_out_channels_the_asker_cannot_read(bot_closures, monkeypatch):
    bot = bot_closures
    tracker, client = bot["reaction_tracker"], bot["client"]
    seed(tracker.store)
    channels = {2: Channel(2), 3: Channel(3, allowed=False)}
    monkeypatch.setattr(client, "get_channel", lambda cid: channels.get(cid))
    fetched = []

    async def fetch_channel(cid):
        fetched.append(cid)
        return Channel(cid)

    monkeypatch.setattr(client, "fetch_channel", fetch_channel)

    async def flow():
        i = asking()
        await bot["reaction_board"].callback(i)
        board = i.response.kwargs["embed"]
        assert i.response.deferred  # thread 4 had to be looked up first
        assert "<@10> — **1** · 😂" in board.description and "<@30> — **1** · 🔥" in board.description
        assert "only channels you can read" in board.footer.text
        sent = asking()
        await bot["reaction_board"].callback(sent, sort="sent")
        sent_board = sent.response.kwargs["embed"]
        assert "<@20> — **2** · 🔥" in sent_board.description
        assert "<@21>" not in sent_board.description  # only reacted in the hidden channel
        assert "❤️" not in sent_board.description
        filtered = asking()
        await bot["reaction_board"].callback(filtered, emoji="❤", sort="sent")
        assert "No reactions counted" in filtered.response.kwargs["embed"].description
        j = asking()
        await bot["reaction_totals"].callback(j)
        report = j.response.kwargs["embed"]
        assert not j.response.deferred  # looked up once per run
        assert "**1** reactions received" in report.description  # the 2 ❤️ in the hidden channel don't count
        assert fetched == [4]

    run(flow())


def test_private_threads_count_for_their_creator_and_moderators(tmp_path):
    class Thread(Channel):
        type = discord.ChannelType.private_thread

        def __init__(self, cid, owner_id, manage=False):
            super().__init__(cid)
            self.owner_id, self.manage = owner_id, manage

        def permissions_for(self, member):
            return NS(view_channel=True, read_message_history=True, manage_threads=self.manage)

    tracker = ReactionTracker(Client(channels=[Thread(5, owner_id=10)]), lambda m: None, tmp_path / "links.db")
    tracker.store.replace(1, 5, 100, 30, [(20, "unicode:😂", "😂")])
    assert tracker.readable_now(1, NS(id=10)) == ({5}, set())
    assert tracker.readable_now(1, NS(id=11)) == (set(), set())
    tracker.client.channels[5].manage = True
    assert tracker.readable_now(1, NS(id=11)) == ({5}, set())


def test_store_filters(tmp_path):
    tracker = ReactionTracker(Client(), lambda m: None, tmp_path / "links.db")
    seed(tracker.store)
    assert tracker.store.summary(1, 10, channels={2})[0] == 1
    assert tracker.store.summary(1, 10, channels=set())[0] == 0
    assert tracker.store.summary(1, 10)[0] == 3  # no filter: everything, as before
    assert [r[0] for r in tracker.store.leaderboard(1, channels={4})] == [30]
    assert tracker.store.leaderboard(1, channels={2}, sort="sent") == [(20, 1, "😂")]
    assert tracker.store.leaderboard(1, channels=set(), sort="sent") == []
    assert tracker.store.leaderboard(1, emoji="❤", channels={3}, sort="sent") == [(20, 1, "❤️"), (21, 1, "❤️")]
