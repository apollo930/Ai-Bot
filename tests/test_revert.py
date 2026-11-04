"""↩️ on a repost: back to the original links."""

import asyncio
import types

import discord
import pytest

IG_RULE = {"domain": "instagram.com", "replacement": ["ig.reaper113.xyz", "kkinstagram.com"]}


@pytest.fixture
def bot(bot_closures):
    bot_closures["url_rules"].append(IG_RULE)
    return bot_closures


def button(custom_id=None, url=None, emoji="🗑️"):
    return {"type": 2, "style": 5 if url else 2, "emoji": {"name": emoji}, **({"url": url} if url else {"custom_id": custom_id})}


class Response:
    def __init__(self):
        self.edited, self.said = None, None

    async def edit_message(self, **kwargs):
        self.edited = kwargs

    async def send_message(self, text, **kwargs):
        self.said = text

    async def defer(self, **kwargs):
        pass


def press(bot, custom_id, content, user_id=42, mod=False):
    rows = [{"type": 1, "components": [
        button(url="https://instagram.com/reel/AbC/", emoji="📷"),
        button(custom_id="revert:42", emoji="↩️"),
        button(custom_id="dellink:42"),
    ]}]
    state = types.SimpleNamespace()
    message = discord.Message.__new__(discord.Message)
    message.id, message.content = 9, content
    message.components = [discord.ActionRow(row) for row in rows]
    i = types.SimpleNamespace(
        data={"custom_id": custom_id}, message=message, response=Response(),
        user=types.SimpleNamespace(id=user_id, display_name="VB", guild_permissions=types.SimpleNamespace(manage_messages=mod)),
    )
    asyncio.run(bot["on_interaction"](i))
    return i.response


def test_revert_puts_the_original_link_back_and_drops_the_button(bot):
    r = press(bot, "revert:42", "look https://ig.reaper113.xyz/reel/AbC/?r=1")
    assert r.edited["content"] == "look https://instagram.com/reel/AbC/"
    ids = [getattr(item, "custom_id", None) for item in r.edited["view"].children]
    assert "revert:42" not in ids and "dellink:42" in ids and len(ids) == 2


def test_only_the_poster_or_a_mod(bot):
    assert press(bot, "revert:42", "look https://ig.reaper113.xyz/reel/AbC/", user_id=7).said == "Only the poster or a mod can do that."
    assert press(bot, "revert:42", "look https://ig.reaper113.xyz/reel/AbC/", user_id=7, mod=True).edited is not None


def test_nothing_left_to_revert(bot):
    assert press(bot, "revert:42", "look https://instagram.com/reel/AbC/").said == "There's no fixed link left to revert."
