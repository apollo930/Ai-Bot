"""Live reaction events update the counts directly, reading from Discord only when needed."""

import asyncio
from types import SimpleNamespace as NS

import discord

from reaction_stats import ReactionTracker
from test_reaction_stats import Channel, Client, Reaction, message, user


def run(coro):
    return asyncio.run(coro)


class GuildClient(Client):
    """A client that knows the server's members (the bot has the members intent)."""

    def __init__(self, members, channels=()):
        super().__init__(channels=channels)
        self.members = {m.id: m for m in members}

    def get_guild(self, gid):
        return NS(id=gid, get_member=self.members.get)


PEOPLE = [user(10), user(20), user(21), user(50, bot=True)]


def tracker_with(tmp_path, channel=None, members=PEOPLE):
    t = ReactionTracker(GuildClient(members, channels=[channel] if channel else []), lambda m: None, tmp_path / "links.db")
    t.repost_owner_id = {500: 10}.get  # message 500 is our repost of person 10's link
    return t


def add(mid, by, emoji="😂", author=10, member_bot=False):
    return NS(guild_id=1, channel_id=2, message_id=mid, user_id=by, emoji=discord.PartialEmoji(name=emoji),
              event_type="REACTION_ADD", member=NS(id=by, bot=member_bot), message_author_id=author, burst=False)


def remove(mid, by, emoji="😂"):
    return NS(guild_id=1, channel_id=2, message_id=mid, user_id=by, emoji=discord.PartialEmoji(name=emoji),
              event_type="REACTION_REMOVE", member=None, burst=False)


def counts(t, author=10):
    return dict(t.store.summary(1, author)[1])


def test_adds_need_no_discord_request(tmp_path):
    channel = Channel(2)
    t = tracker_with(tmp_path, channel)
    run(t.on_reaction(add(100, by=20)))
    run(t.on_reaction(add(100, by=21)))
    run(t.on_reaction(add(100, by=20)))  # the same again (or the super version): still one
    assert counts(t) == {"😂": 2} and channel.fetched == []


def test_adds_that_never_count(tmp_path):
    channel = Channel(2)
    t = tracker_with(tmp_path, channel)
    run(t.on_reaction(add(100, by=50, member_bot=True)))  # a bot's ⏳
    run(t.on_reaction(add(100, by=10)))  # the author on their own message
    run(t.on_reaction(add(101, by=20, author=50)))  # on a bot's message
    assert t.store.leaderboard(1) == [] and channel.fetched == []


def test_reactions_on_our_reposts_credit_the_poster(tmp_path):
    t = tracker_with(tmp_path)
    run(t.on_reaction(add(500, by=20, author=999)))  # message_author_id is the webhook
    assert counts(t, 10) == {"😂": 1}


def test_unknown_author_reads_the_message(tmp_path):
    channel = Channel(2, [message(102, author=user(77), reactions=[Reaction("😂", [user(20)])])])
    t = tracker_with(tmp_path, channel)
    run(t.on_reaction(add(102, by=20, author=77)))  # 77 isn't a known member (left, or another webhook)
    assert channel.fetched == [102] and counts(t, 77) == {"😂": 1}


def test_remove_rereads_only_that_emoji(tmp_path):
    msg = message(100, reactions=[Reaction("😂", normal=[user(21)], burst=[user(20)]), Reaction("🔥", [user(20)])])
    channel = Channel(2, [msg])
    t = tracker_with(tmp_path, channel)
    for event in (add(100, by=20), add(100, by=21), add(100, by=20, emoji="🔥")):
        run(t.on_reaction(event))
    # 20 removed a normal 😂 but still has a super 😂 (Discord's list above), so 20 still counts once
    run(t.on_reaction(remove(100, by=20)))
    assert counts(t) == {"😂": 2, "🔥": 1} and channel.fetched == [100]
    msg.reactions[0] = Reaction("😂", normal=[user(21)])  # now 20's super 😂 is gone too
    run(t.on_reaction(remove(100, by=20)))
    assert counts(t) == {"😂": 1, "🔥": 1}


def test_removes_that_change_nothing_ask_nothing(tmp_path):
    channel = Channel(2)
    t = tracker_with(tmp_path, channel)
    run(t.on_reaction(remove(100, by=20)))  # nothing counted on that message
    run(t.on_reaction(add(100, by=21)))
    run(t.on_reaction(remove(100, by=50)))  # a bot removing its own reaction
    assert channel.fetched == [] and counts(t) == {"😂": 1}


def test_clears(tmp_path):
    t = tracker_with(tmp_path)
    for event in (add(100, by=20), add(100, by=21, emoji="🔥"), add(101, by=20)):
        run(t.on_reaction(event))
    run(t.on_reaction(discord.RawReactionClearEmojiEvent({"message_id": 100, "channel_id": 2, "guild_id": 1},
                                                         discord.PartialEmoji(name="🔥"))))
    assert counts(t) == {"😂": 2}
    run(t.on_reaction(discord.RawReactionClearEvent({"message_id": 101, "channel_id": 2, "guild_id": 1})))
    assert counts(t) == {"😂": 1}
    run(t.on_reaction(discord.RawReactionClearEmojiEvent({"message_id": 100, "channel_id": 2, "guild_id": 1},
                                                         discord.PartialEmoji(name="😂"))))
    assert t.store.owner(100) is None  # nothing left on it, so the message is dropped


def test_events_during_a_scan_still_mark_the_message(tmp_path):
    t = tracker_with(tmp_path)
    t._scan_changed = set()
    run(t.on_reaction(add(100, by=20)))
    assert t._scan_changed == {100}
