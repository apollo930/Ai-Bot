"""/fixlinks: recheck the latest reposts in a channel and fix links Discord didn't preview."""
import asyncio
import types

import discord
import pytest

from test_watch_embeds import embed

IG_RULE = {"domain": "instagram.com", "replacement": ["ig.reaper113.xyz", "kkinstagram.com"]}
HOOK_ID = 77


class Hook:
    name, id = "IG Cleaner", HOOK_ID

    def __init__(self, messages):
        self.messages = {m.id: m for m in messages}
        self.edits = []

    async def fetch_message(self, mid, **kw):
        return self.messages[mid]

    async def edit_message(self, mid, content=None, **kw):
        self.edits.append((mid, content))
        m = self.messages[mid]
        m.content = content
        m.embeds = [embed(content.split()[-1])]  # Discord previews the new URL


class Channel:
    def __init__(self, messages, hook):
        self.messages, self.hook = messages, hook

    async def webhooks(self):
        return [self.hook]

    async def history(self, limit):
        for m in self.messages[:limit]:
            yield m


def repost(mid, url, previewed, webhook_id=HOOK_ID):
    return types.SimpleNamespace(id=mid, webhook_id=webhook_id, content=f"look {url}", embeds=[embed(url)] if previewed else [],
                                 jump_url=f"https://discord.com/channels/7/1/{mid}")


@pytest.fixture
def bot(bot_closures, monkeypatch):
    import bot as bot_module

    monkeypatch.setattr(bot_module, "EMBED_CHECK_AFTER", 0)
    bot_closures["url_rules"].append(IG_RULE)
    return bot_closures


def run_fixlinks(bot, messages, count=10):
    hook = Hook(messages)
    channel = Channel(messages, hook)
    said = []

    async def defer(**kw):
        pass

    async def send(text, **kw):
        said.append(text)

    i = types.SimpleNamespace(channel=channel, response=types.SimpleNamespace(defer=defer), followup=types.SimpleNamespace(send=send))
    asyncio.run(bot["fixlinks"].callback(i, count))
    return hook, said[-1]


def test_fixes_only_the_reposts_without_a_preview(bot):
    messages = [
        repost(3, "https://ig.reaper113.xyz/reel/C3/", previewed=False),
        types.SimpleNamespace(id=2, webhook_id=None, content="hi https://ig.reaper113.xyz/reel/X/", embeds=[], jump_url=""),  # not ours
        repost(1, "https://ig.reaper113.xyz/reel/A1/", previewed=True),
    ]
    hook, said = run_fixlinks(bot, messages)
    assert hook.edits == [(3, "look https://ig.reaper113.xyz/reel/C3/?r=1")]
    assert said == "Checked the last 2 reposts here: 1 fine, 1 fixed."
    details = bot["fix_details"][3]["https://instagram.com/reel/C3/"]
    assert details[0] == "🔎 /fixlinks: checked again" and details[-1] == "✓ Discord showed the preview this time"


def test_count_limits_how_far_back(bot):
    messages = [repost(i, f"https://ig.reaper113.xyz/reel/P{i}/", previewed=True) for i in range(5, 0, -1)]
    hook, said = run_fixlinks(bot, messages, count=2)
    assert said.startswith("Checked the last 2 reposts here: 2 fine")


def test_nothing_to_check(bot):
    hook, said = run_fixlinks(bot, [types.SimpleNamespace(id=1, webhook_id=None, content="hello", embeds=[], jump_url="")])
    assert said.startswith("No reposts of mine")
