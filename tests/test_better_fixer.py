"""After a repost, a fixer that shows more of the post (video over image over text) replaces the first one."""
import asyncio
import types

import pytest

from test_reddit import FakeSession
from test_watch_embeds import FakeHook, embed

IG_RULE = {"domain": "instagram.com", "replacement": ["ig.reaper113.xyz", "kkinstagram.com", "vxinstagram.com"]}
IG = "https://www.instagram.com/reel/AbC/"
OURS = "https://ig.reaper113.xyz/reel/AbC/"
VX = "https://vxinstagram.com/reel/AbC/"
COVER = '<meta name="twitter:card" content="summary_large_image"><meta property="og:image" content="https://cdn/cover.jpg">'
VIDEO = '<meta name="twitter:card" content="player"><meta property="og:video" content="https://cdn/v.mp4">'


@pytest.fixture
def bot(bot_closures, monkeypatch):
    import bot as bot_module

    monkeypatch.setattr(bot_module, "EMBED_CHECK_AFTER", 0)
    FakeSession.pages, FakeSession.requested = {}, []
    monkeypatch.setattr(bot_module.aiohttp, "ClientSession", FakeSession)
    return bot_closures


def after_repost(bot, previews, content=f"look {OURS}"):
    hook = FakeHook(content, previews)
    sent = types.SimpleNamespace(id=1, content=content, embeds=[])
    steps = {IG: []}
    asyncio.run(bot["after_repost"](hook, sent, {IG: OURS}, {IG: IG_RULE}, steps, None))
    return hook, steps[IG]


def test_a_cover_frame_is_replaced_by_the_video(bot):
    FakeSession.pages = {"ig.reaper113.xyz": (200, COVER), "kkinstagram.com": (200, COVER), "vxinstagram.com": (200, VIDEO)}
    hook, steps = after_repost(bot, [[embed(VX)]])
    assert hook.edits == [f"look {VX}"]
    assert steps == ["⬆️ `vxinstagram.com` shows the video where `ig.reaper113.xyz` showed an image: switched to it"]


def test_a_video_stays(bot):
    FakeSession.pages = {"ig.reaper113.xyz": (200, VIDEO), "vxinstagram.com": (200, VIDEO)}
    hook, steps = after_repost(bot, [[embed(OURS)]])
    assert hook.edits == [] and steps == []
    assert FakeSession.requested == ["https://ig.reaper113.xyz/reel/AbC/"]  # only the current fixer was asked


def test_known_rank_skips_the_recheck(bot):
    FakeSession.pages = {"ig.reaper113.xyz": (200, VIDEO)}
    bot["page_ranks"][OURS] = 3
    hook, steps = after_repost(bot, [[embed(OURS)]])
    assert FakeSession.requested == [] and hook.edits == []


def test_nothing_better_leaves_it(bot):
    FakeSession.pages = {"ig.reaper113.xyz": (200, COVER), "kkinstagram.com": (200, COVER)}
    hook, steps = after_repost(bot, [[embed(OURS)]])
    assert hook.edits == []


def test_edited_meanwhile_is_left_alone(bot):
    FakeSession.pages = {"ig.reaper113.xyz": (200, COVER), "vxinstagram.com": (200, VIDEO)}
    hook = FakeHook("someone changed it", [[]])
    sent = types.SimpleNamespace(id=1, content=f"look {OURS}", embeds=[])
    asyncio.run(bot["after_repost"](hook, sent, {IG: OURS}, {IG: IG_RULE}, {IG: []}, None))
    assert hook.edits == []
