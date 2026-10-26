"""Ai-Bot's commands and handlers, driven with stand-ins for Discord objects (no network)."""
import asyncio
import json
import os
import types

import discord
import pytest


class Hook:
    name = "IG Cleaner"

    def __init__(self):
        self.sent, self.edits = [], []

    async def send(self, content=discord.utils.MISSING, **kw):
        self.sent.append((content, kw))
        return types.SimpleNamespace(id=1000 + len(self.sent))

    async def edit_message(self, message_id, **kw):
        self.edits.append((message_id, kw))


class Channel:
    def __init__(self, has_hook=True):
        self.hook, self.has_hook, self.created = Hook(), has_hook, False

    async def webhooks(self):
        return [self.hook] if self.has_hook else []

    async def create_webhook(self, name):
        self.created = self.has_hook = True
        return self.hook


class FakeThread:
    def __init__(self, parent=None):
        self.parent = parent


class Response:
    def __init__(self):
        self.messages, self.modal = [], None

    async def defer(self, **kw):
        pass

    async def send_message(self, content=None, **kw):
        self.messages.append((content, kw))

    async def send_modal(self, modal):
        self.modal = modal


class Followup:
    def __init__(self, response):
        self.response = response

    async def send(self, content=None, **kw):
        self.response.messages.append((content, kw))


MEMBER = types.SimpleNamespace(id=42, display_name="VB", display_avatar=types.SimpleNamespace(url="https://a/av.png"), mention="<@42>")


def interaction(channel=None, user=MEMBER):
    response = Response()
    return types.SimpleNamespace(channel=channel, user=user, response=response, followup=Followup(response))


def said(i):
    return i.response.messages[-1][0]


class Upload:
    def __init__(self, filename, data):
        self.filename, self.data, self.size = filename, data, len(data)

    async def read(self):
        return self.data


class Message:
    def __init__(self, content, channel, attachments=(), reference=None):
        self.content, self.channel, self.attachments, self.reference = content, channel, list(attachments), reference
        self.author, self.deleted, self.replies = MEMBER, False, []

    async def delete(self):
        self.deleted = True

    async def reply(self, text, **kw):
        self.replies.append(text)


@pytest.fixture
def bot(bot_closures, monkeypatch):
    monkeypatch.setattr(discord, "Thread", FakeThread)  # the bot checks isinstance(channel, discord.Thread)
    return bot_closures


def run(coro):
    return asyncio.run(coro)


def add_file_reaction(bot, name="doakes", data=b"GIF89a" + b"\0" * 64, filename="Doakes Stare.GIF"):
    i = interaction()
    run(bot["add_react"].callback(i, name, None, Upload(filename, data)))
    return i


def test_addai_upload_is_kept_as_a_file(bot):
    i = add_file_reaction(bot)
    assert bot["reactions"]["doakes"] == "file:doakes.gif"
    assert "Saved `doakes`" in said(i)
    import bot as bot_module

    with open(os.path.join(bot_module.REACTIONS_DIR, "doakes.gif"), "rb") as f:
        assert f.read().startswith(b"GIF89a")
    assert json.load(open("config.json"))["reactions"] == {"doakes": "file:doakes.gif"}


def test_addai_refuses_bad_names_missing_input_and_big_files(bot):
    for args in [("bad name", "https://a/b.gif", None), ("ok", None, None)]:
        i = interaction()
        run(bot["add_react"].callback(i, *args))
        assert bot["reactions"] == {}
    import bot as bot_module

    add_file_reaction(bot, "big", b"0" * (bot_module.REACTION_MAX_BYTES + 1))
    assert "big" not in bot["reactions"]


def test_ai_sends_the_file_in_a_thread_through_the_parent_webhook(bot):
    add_file_reaction(bot)
    parent = Channel(has_hook=False)
    thread = FakeThread(parent)
    i = interaction(thread)
    run(bot["react"].callback(i, "doakes", types.SimpleNamespace(mention="<@7>")))
    content, kw = parent.hook.sent[-1]
    assert parent.created and kw["thread"] is thread
    assert content == "<@7>" and isinstance(kw["file"], discord.File)
    assert kw["username"] == "VB" and said(i) == "✅"
    assert bot["repost_owners"].owner(1001) == 42  # recorded for "Edit repost"


def test_ai_unknown_name(bot):
    i = interaction(Channel())
    run(bot["react"].callback(i, "nope", None))
    assert "No reaction named" in said(i)


def test_semicolon_trigger(bot):
    add_file_reaction(bot)
    channel = Channel()
    message = Message(";DOAKES", channel)
    assert run(bot["reaction_from_message"](message)) and message.deleted
    assert isinstance(channel.hook.sent[-1][1]["file"], discord.File)

    channel = Channel()
    message = Message(";doakes <@555>", channel)
    run(bot["reaction_from_message"](message))
    assert channel.hook.sent[-1][0] == "<@555>"

    for text, attachments in [(";nope", ()), ("; doakes", ()), ("lol ;doakes", ()), (";doakes", [object()])]:
        channel = Channel()
        message = Message(text, channel, attachments)
        assert not run(bot["reaction_from_message"](message))
        assert not message.deleted and not channel.hook.sent


def test_semicolon_trigger_as_a_reply(bot, monkeypatch):
    add_file_reaction(bot)
    monkeypatch.setattr(discord, "Message", types.SimpleNamespace)  # lets the stand-in pass isinstance
    replied = types.SimpleNamespace(author=types.SimpleNamespace(display_name="Friend"), jump_url="https://discord.com/channels/1/2/3")
    channel = Channel()
    run(bot["reaction_from_message"](Message(";doakes", channel, reference=types.SimpleNamespace(resolved=replied))))
    assert channel.hook.sent[-1][0] == "-# ↪ replying to [Friend](https://discord.com/channels/1/2/3)"


def test_listai_marks_files_and_dead_links_and_fits_discord_limits(bot):
    add_file_reaction(bot)
    bot["reactions"]["gone"] = "https://example.com/gone.gif"
    bot["dead_reactions"]["gone"] = "HTTP 404"
    for n in range(400):
        bot["reactions"][f"meme_{n:03d}_padding_padding"] = "https://example.com/a.gif"
    i = interaction()
    run(bot["list_reacts"].callback(i))
    embed = i.response.messages[-1][1]["embed"]
    assert embed.description.startswith("`gone` ⚠️ dead (HTTP 404)")  # dead ones first
    assert len(embed.description) <= 4096 and len(embed) <= 6000 and embed.description.endswith("more")


def test_removeai_deletes_entry_and_file(bot):
    add_file_reaction(bot)
    import bot as bot_module

    i = interaction()
    run(bot["remove_react"].callback(i, "doakes"))
    assert "doakes" not in bot["reactions"]
    assert not os.path.exists(os.path.join(bot_module.REACTIONS_DIR, "doakes.gif"))


def repost(mid, content="hi https://ig.reaper113.xyz/p/X/", webhook_id=77, components=(), channel=None, attachments=()):
    return types.SimpleNamespace(id=mid, content=content, webhook_id=webhook_id, components=list(components), channel=channel or object(), attachments=list(attachments), embeds=[])


def test_edit_repost(bot, monkeypatch):
    hook = Hook()

    async def fetch_webhook(webhook_id):
        return hook

    monkeypatch.setattr(bot["client"], "fetch_webhook", fetch_webhook)
    bot["repost_owners"].add(1, 42)

    async def flow():
        i = interaction()
        await bot["edit_repost"].callback(i, repost(1))
        modal = i.response.modal
        assert modal.text.default.startswith("hi") and modal.text.required
        modal.text._value = "fixed"
        j = interaction()
        await modal.on_submit(j)
        assert hook.edits[-1] == (1, {"content": "fixed", "thread": discord.utils.MISSING}) and said(j) == "✏️ Edited."

        i = interaction(user=types.SimpleNamespace(id=7))
        await bot["edit_repost"].callback(i, repost(1))
        assert i.response.modal is None and "Only the person" in said(i)

        for message in (repost(2, webhook_id=None), repost(3)):
            i = interaction()
            await bot["edit_repost"].callback(i, message)
            assert "isn't one of my reposts" in said(i)

        # reposts from before owners were recorded: the poster id in the Delete button
        row = types.SimpleNamespace(children=[types.SimpleNamespace(custom_id="dellink:42")])
        i = interaction()
        await bot["edit_repost"].callback(i, repost(4, components=[row]))
        assert i.response.modal is not None

        thread = FakeThread()
        bot["repost_owners"].add(5, 42)
        i = interaction()
        await bot["edit_repost"].callback(i, repost(5, channel=thread, attachments=[object()]))
        assert not i.response.modal.text.required  # a repost with files may lose its text
        i.response.modal.text._value = ""
        await i.response.modal.on_submit(interaction())
        assert hook.edits[-1][1]["thread"] is thread

    run(flow())
