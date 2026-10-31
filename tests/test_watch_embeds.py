"""watch_embeds: a repost whose link Discord didn't preview gets the link fetched again."""
import asyncio
import types

import discord
import pytest

from test_reddit import FakeSession, IMAGE

IG_RULE = {"domain": "instagram.com", "replacement": ["ig.reaper113.xyz", "kkinstagram.com"]}
REDDIT = {"domain": "reddit.com", "replacement": "vxreddit.com"}


def embed(url, provider="InstaFix"):
    return discord.Embed.from_dict({"type": "video", "url": url, "provider": {"name": provider}})


class FakeHook:
    """A webhook whose message gets Discord's previews from `previews`: one list of embeds per check."""

    def __init__(self, content, previews):
        self.content, self.previews, self.edits, self.fetches = content, list(previews), [], 0

    async def fetch_message(self, mid, **kw):
        embeds = self.previews[min(self.fetches, len(self.previews) - 1)]
        self.fetches += 1
        return types.SimpleNamespace(id=mid, content=self.content, embeds=embeds)

    async def edit_message(self, mid, content=None, **kw):
        self.content = content
        self.edits.append(content)


@pytest.fixture
def bot(bot_closures, monkeypatch):
    import bot as bot_module

    monkeypatch.setattr(bot_module, "EMBED_CHECK_AFTER", 0)
    FakeSession.pages, FakeSession.requested = {}, []
    monkeypatch.setattr(bot_module.aiohttp, "ClientSession", FakeSession)
    return bot_closures


def watch(bot, url, fixed, rule, previews, content=None):
    content = content or f"look {fixed}"
    hook = FakeHook(content, previews)
    sent = types.SimpleNamespace(id=1, content=content, embeds=[])
    steps = {url: []}
    asyncio.run(bot["watch_embeds"](hook, sent, {url: fixed}, {url: rule}, steps, None))
    return hook, steps[url]


IG = "https://www.instagram.com/reel/AbC/"
FIXED = "https://ig.reaper113.xyz/reel/AbC/"


def test_preview_there_nothing_happens(bot):
    hook, steps = watch(bot, IG, FIXED, IG_RULE, [[embed(FIXED)]])
    assert hook.edits == [] and steps == [] and hook.fetches == 1


def test_no_preview_asks_again_with_a_new_url(bot):
    hook, steps = watch(bot, IG, FIXED, IG_RULE, [[], [embed(FIXED + "?r=1")]])
    assert hook.edits == [f"look {FIXED}?r=1"]
    assert steps == [
        "↻ Discord showed no preview for `ig.reaper113.xyz` after 0s: asked it again",
        "✓ Discord showed the preview this time",
    ]


def test_instagram_login_card_counts_as_no_preview(bot):
    hook, steps = watch(bot, IG, FIXED, IG_RULE, [[embed(FIXED, "Instagram")], [embed(FIXED + "?r=1")]])
    assert hook.edits == [f"look {FIXED}?r=1"]


def test_then_the_next_fixer_then_gives_up(bot):
    FakeSession.pages = {"kkinstagram.com": (200, IMAGE)}
    hook, steps = watch(bot, IG, FIXED, IG_RULE, [[]])
    assert hook.edits == [f"look {FIXED}?r=1", "look https://kkinstagram.com/reel/AbC/"]
    assert steps[1:] == ["↻ switched to `kkinstagram.com`", "✗ Discord still showed no preview"]
    assert hook.fetches == 3


def test_no_other_fixer_leaves_it(bot):
    FakeSession.pages = {}
    hook, steps = watch(bot, IG, FIXED, IG_RULE, [[]])
    assert hook.edits == [f"look {FIXED}?r=1"]
    assert steps[-2:] == ["✗ `kkinstagram.com`: HTTP 404", "✗ no other fixer works for it"]


def test_reddit_goes_back_to_its_own_card(bot):
    url = "https://www.reddit.com/r/pics/comments/x/y/"
    fixed = "https://vxreddit.com/r/pics/comments/x/y/"
    hook, steps = watch(bot, url, fixed, REDDIT, [[], [], [embed(url, "Reddit")]])
    assert hook.edits[-1] == f"look {url}"
    assert steps[-2:] == ["↻ switched back to the original link (its own embed)", "✓ Discord showed the preview this time"]


def test_edited_repost_is_left_alone(bot):
    hook = FakeHook("someone changed this", [[]])
    sent = types.SimpleNamespace(id=1, content=f"look {FIXED}", embeds=[])
    asyncio.run(bot["watch_embeds"](hook, sent, {IG: FIXED}, {IG: IG_RULE}, {IG: []}, None))
    assert hook.edits == []


def test_deleted_repost_stops(bot):
    class Gone(FakeHook):
        async def fetch_message(self, mid, **kw):
            raise discord.NotFound(types.SimpleNamespace(status=404, reason="x"), "Unknown Message")

    hook = Gone("x", [[]])
    sent = types.SimpleNamespace(id=1, content="x", embeds=[])
    asyncio.run(bot["watch_embeds"](hook, sent, {IG: FIXED}, {IG: IG_RULE}, {IG: []}, None))
    assert hook.edits == []
