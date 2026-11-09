"""Hall of fame: enough people react, the message is reposted (once) to the chosen channel."""

import asyncio
import datetime
from types import SimpleNamespace as NS

import discord

from hall_of_fame import HallOfFame
from reaction_stats import ReactionStore


def run(coro):
    return asyncio.run(coro)


def snowflake(seconds_ago):
    return discord.utils.time_snowflake(datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(seconds=seconds_ago))


class Hall:
    id, name, nsfw = 900, "hall-of-fame", False

    def __init__(self):
        self.sent, self.edits = [], []

    async def send(self, **kwargs):
        self.sent.append(kwargs)
        return NS(id=5000 + len(self.sent))

    def get_partial_message(self, mid):
        hall = self

        class Partial:
            async def edit(self, **kwargs):
                hall.edits.append((mid, kwargs))

        return Partial()


class Source:
    def __init__(self, cid=2, public=True, nsfw=False):
        self.id, self.name, self.nsfw, self.public = cid, "memes", nsfw, public
        self.guild = NS(default_role=NS(), get_member=lambda uid: NS(display_name="VB", display_avatar=NS(url="https://a/vb.png")))
        self.messages = {}

    def permissions_for(self, role):
        return NS(view_channel=self.public, read_message_history=self.public)

    async def fetch_message(self, mid):
        return self.messages[mid]


def message(mid, text="lol", image=None):
    attachments = [NS(url=image, content_type="image/png")] if image else []
    return NS(id=mid, content=text, attachments=attachments, embeds=[], created_at=discord.utils.snowflake_time(mid),
              jump_url=f"https://discord.com/channels/1/2/{mid}", author=NS(display_name="webhook", display_avatar=NS(url="x")))


def setup(tmp_path, threshold=3, **source_kwargs):
    store = ReactionStore(tmp_path / "links.db")
    hall, source = Hall(), Source(**source_kwargs)
    channels = {hall.id: hall, source.id: source}
    client = NS(get_channel=channels.get)
    hof = HallOfFame(client, store, tmp_path / "links.db")
    hof.configure(1, hall.id, threshold)
    return store, hof, hall, source


def react(store, hof, source, mid, people):
    store.replace(1, source.id, mid, 10, [(20 + i, "unicode:😂", "😂") for i in range(people)])
    run(hof.on_change(1, source.id, mid))


def test_posts_once_when_enough_people_react_then_updates(tmp_path):
    store, hof, hall, source = setup(tmp_path)
    mid = snowflake(60)
    source.messages[mid] = message(mid, "funny thing", image="https://cdn/x.png")
    react(store, hof, source, mid, 2)
    assert hall.sent == []
    react(store, hof, source, mid, 3)
    assert len(hall.sent) == 1
    card = hall.sent[0]["embed"]
    assert card.description == "funny thing" and card.author.name == "VB" and card.image.url == "https://cdn/x.png"
    assert card.fields[0].name == "3 people reacted" and card.fields[0].value == "😂 **3**"
    assert hall.sent[0]["view"].children[0].url.endswith(f"/{mid}")
    react(store, hof, source, mid, 4)
    assert len(hall.sent) == 1 and hall.edits[-1][0] == 5001
    assert hall.edits[-1][1]["embed"].fields[0].name == "4 people reacted"


def test_old_private_nsfw_and_hall_messages_stay_out(tmp_path):
    store, hof, hall, source = setup(tmp_path)
    old = snowflake(8 * 86400)
    source.messages[old] = message(old)
    react(store, hof, source, old, 5)  # older than a week
    for kwargs in ({"public": False}, {"nsfw": True}):
        store2, hof2, hall2, source2 = setup(_sub(tmp_path, kwargs), **kwargs)
        mid = snowflake(60)
        source2.messages[mid] = message(mid)
        react(store2, hof2, source2, mid, 5)
        assert hall2.sent == []
    mid = snowflake(60)
    store.replace(1, hall.id, mid, 10, [(20 + i, "unicode:😂", "😂") for i in range(5)])
    run(hof.on_change(1, hall.id, mid))  # reactions in the hall channel itself
    assert hall.sent == []


def _sub(tmp_path, kwargs):
    path = tmp_path / ("_".join(kwargs))
    path.mkdir()
    return path


def test_off_means_off(tmp_path):
    store, hof, hall, source = setup(tmp_path)
    hof.disable(1)
    mid = snowflake(60)
    source.messages[mid] = message(mid)
    react(store, hof, source, mid, 9)
    assert hall.sent == [] and hof.settings(1) is None
