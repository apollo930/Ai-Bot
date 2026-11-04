"""Reddit links: vxreddit for every post; when vxreddit can't show one, reddit.com's own card stays."""
import asyncio
import types

import discord
import pytest

from test_bot import Channel, Message

REDDIT = {"domain": "reddit.com", "replacement": "vxreddit.com"}
VIDEO = '<meta name="twitter:card" content="player"><meta property="og:video" content="https://vxreddit.com/redditvideo.mp4?v=1">'
GALLERY = ('<meta name="twitter:card" content="summary_large_image">'
           '<meta property="og:image" content="https://i.redd.it/a.jpeg"><meta property="og:image" content="https://i.redd.it/b.jpeg">')
IMAGE = '<meta name="twitter:card" content="summary_large_image"><meta property="og:image" content="https://i.redd.it/a.jpeg">'
LINK = '<meta name="twitter:card" content="summary"><meta property="og:image" content="https://external-preview.redd.it/x.jpeg">'


class FakeResponse:
    def __init__(self, url, status, body):
        self.url = types.SimpleNamespace(host=url.split("/")[2])
        self.status, self.content_type = status, "text/html"
        self.content = types.SimpleNamespace(iter_chunked=self._chunks)
        self._body = body.encode()

    async def _chunks(self, n):
        # like vxreddit: the page arrives in small pieces
        for i in range(0, len(self._body), 40):
            yield self._body[i:i + 40]

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class FakeSession:
    pages = {}
    requested = []

    def __init__(self, headers=None):
        self.closed = False

    def get(self, url, **kw):
        FakeSession.requested.append(url)
        status, body = next(((s, b) for key, (s, b) in FakeSession.pages.items() if key in url), (404, ""))
        return FakeResponse(url, status, body)


@pytest.fixture
def bot(bot_closures, monkeypatch):
    import bot as bot_module

    FakeSession.pages, FakeSession.requested = {}, []
    monkeypatch.setattr(bot_module.aiohttp, "ClientSession", FakeSession)
    return bot_closures


def resolve(bot, url, rule=REDDIT):
    steps = []
    return asyncio.run(bot["resolve"](url, rule, steps)), steps


TEXT = ('<meta name="twitter:card" content="summary"><meta property="og:site_name" content="u/x on r/AskReddit">'
        '<meta property="og:description" content="What is the coldest line?">')


@pytest.mark.parametrize("page, media", [(VIDEO, "video"), (GALLERY, "image"), (IMAGE, "image"), (TEXT, None), (LINK, None)])
def test_every_kind_of_post_uses_vxreddit(bot, page, media):
    FakeSession.pages = {"vxreddit.com/r/pics/comments/abc": (200, page)}
    (fixed, kind), steps = resolve(bot, "https://www.reddit.com/r/pics/comments/abc/x/")
    assert (fixed, kind) == ("https://vxreddit.com/r/pics/comments/abc/x/", media)
    assert steps == ["✓ `vxreddit.com`: used"]


def test_text_pages_only_count_for_reddit(bot):
    FakeSession.pages = {"ig.example": (200, TEXT)}
    (fixed, kind), _ = resolve(bot, "https://www.instagram.com/p/AbC/", {"domain": "instagram.com", "replacement": "ig.example"})
    assert (fixed, kind) == (None, None)


def test_vxreddit_error_page_keeps_the_reddit_link(bot):
    FakeSession.pages = {"vxreddit.com": (200, '<meta property="og:title" content="vxReddit" />'
                                                '<meta property="og:description" content="Internal server error: HTTPError: HTTP Error 403: Forbidden" />')}
    (fixed, media), steps = resolve(bot, "https://www.reddit.com/r/videos/comments/err/v/")
    assert (fixed, media) == (None, "original")
    assert steps == [
        "✗ `vxreddit.com`: it couldn't get the post (Internal server error: HTTPError: HTTP Error 403: Forbidden)",
        "✓ kept the original link: no fixer could show it, and its own embed works",
    ]


def test_vxreddit_down_keeps_the_reddit_link(bot):
    FakeSession.pages = {"vxreddit.com": (502, "")}
    (fixed, media), steps = resolve(bot, "https://www.reddit.com/r/pics/comments/ghi/z/")
    assert (fixed, media) == (None, "original")
    assert steps == ["✗ `vxreddit.com`: HTTP 502", "✓ kept the original link: no fixer could show it, and its own embed works"]


def test_other_sites_unchanged(bot):
    FakeSession.pages = {"ig.example": (200, IMAGE)}
    rule = {"domain": "instagram.com", "replacement": "ig.example"}
    (fixed, media), _ = resolve(bot, "https://www.instagram.com/p/AbC/", rule)
    assert fixed == "https://ig.example/p/AbC/" and media == "image"
    FakeSession.pages = {}
    (fixed, media), _ = resolve(bot, "https://www.instagram.com/p/Zzz/", rule)
    assert (fixed, media) == (None, None)


class LinkMessage(Message):
    _next_id = 1

    def __init__(self, content, channel):
        super().__init__(content, channel)
        self.id = LinkMessage._next_id = LinkMessage._next_id + 1
        self.guild = types.SimpleNamespace(id=7, filesize_limit=10_000_000)
        self.flags = types.SimpleNamespace(suppress_embeds=False)
        self.reactions = []

    async def add_reaction(self, emoji):
        self.reactions.append(emoji)

    async def remove_reaction(self, emoji, member):
        if emoji in self.reactions:
            self.reactions.remove(emoji)


def fix(bot, text):
    bot["url_rules"].clear()
    bot["url_rules"].append(REDDIT)
    channel = Channel()

    async def send(content=discord.utils.MISSING, **kw):
        channel.hook.sent.append((content, kw))
        return types.SimpleNamespace(id=5000 + len(channel.hook.sent), jump_url="https://discord.com/channels/7/1/5001")

    channel.hook.send = send
    msg = LinkMessage(text, channel)
    asyncio.run(bot["fix_links"](msg, text))
    return msg, channel


def test_vxreddit_down_leaves_the_message_alone_with_the_usual_warning(bot, monkeypatch):
    FakeSession.pages = {"vxreddit.com": (502, "")}
    msg, channel = fix(bot, "look https://www.reddit.com/r/AskReddit/comments/aaa/q/")
    assert not msg.deleted and channel.hook.sent == [] and msg.reactions == ["⚠️"]


def test_text_post_is_reposted_with_vxreddit(bot, monkeypatch):
    monkeypatch.setattr(discord, "Thread", type("FakeThread", (), {}))
    FakeSession.pages = {"vxreddit.com": (200, TEXT)}
    msg, channel = fix(bot, "look https://www.reddit.com/r/AskReddit/comments/aaa/q/")
    assert msg.deleted and channel.hook.sent[-1][0] == "look https://vxreddit.com/r/AskReddit/comments/aaa/q/"
    ids = [getattr(item, "custom_id", None) or "" for item in channel.hook.sent[-1][1]["view"].children]
    assert any(i.startswith("revert:") for i in ids) and any(i.startswith("dellink:") for i in ids)


def test_video_post_is_reposted_with_vxreddit(bot, monkeypatch):
    monkeypatch.setattr(discord, "Thread", type("FakeThread", (), {}))
    FakeSession.pages = {"vxreddit.com": (200, VIDEO)}
    msg, channel = fix(bot, "lol https://www.reddit.com/r/videos/comments/bbb/v/")
    assert msg.deleted
    content, _kw = channel.hook.sent[-1]
    assert content == "lol https://vxreddit.com/r/videos/comments/bbb/v/"
