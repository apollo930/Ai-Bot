"""YouTube duplicates match across URL formats without replacing ordinary posts."""

import asyncio
from types import SimpleNamespace as NS

import discord
import pytest

import utils
from test_bot import Channel, MEMBER
from test_reddit import FakeSession, LinkMessage, REDDIT, TEXT


VIDEO = "dQw4w9WgXcQ"
OTHER = "jNQXAC9IVRw"
WATCH = f"https://www.youtube.com/watch?v={VIDEO}"


@pytest.mark.parametrize("url", [
    WATCH,
    f"{WATCH}&t=42&list=playlist&index=3&si=share",
    f"https://m.youtube.com/watch?v={VIDEO}#t=80",
    f"https://youtu.be/{VIDEO}?t=10&si=share",
    f"https://youtube.com/shorts/{VIDEO}",
    f"https://youtube.com/live/{VIDEO}?start=100",
    f"https://www.youtube.com/embed/{VIDEO}",
    f"https://www.youtube-nocookie.com/embed/{VIDEO}",
    f"https://music.youtube.com/watch?v={VIDEO}&list=playlist",
])
def test_video_identity_ignores_url_format_and_playback_parameters(url):
    assert utils.youtube_video_id(url) == VIDEO
    assert utils.link_key(url) == f"ytmusic:{VIDEO}"
    assert utils.find_youtube_links(f"look ({url})") == [url]


@pytest.mark.parametrize("url", [
    "https://youtube.com/playlist?list=playlist",
    "https://youtube.com/@someone",
    "https://youtube.com/watch?v=short",
    f"https://youtube.com/watch?v={VIDEO}EXTRA",
    f"https://youtube.com.evil.example/watch?v={VIDEO}",
    f"https://notyoutube.com/watch?v={VIDEO}",
    f"https://youtube.com@evil.example/watch?v={VIDEO}",
    f"https://youtu.be/{VIDEO}/extra",
    f"ftp://youtube.com/watch?v={VIDEO}",
])
def test_non_video_and_lookalike_links_are_not_tracked(url):
    assert utils.youtube_video_id(url) is None
    assert utils.find_youtube_links(url) == []


class VideoMessage(LinkMessage):
    def __init__(self, content=WATCH, guild_id=7):
        super().__init__(content, Channel())
        self.guild.id = guild_id
        self.jump_url = f"https://discord.com/channels/{guild_id}/1/{self.id}"
        self.webhook_id = None
        self.author = NS(**vars(MEMBER), name="person", bot=False)

        async def send(content, **kwargs):
            self.channel.hook.sent.append((content, kwargs))
            return NS(id=10_000 + self.id, jump_url=f"https://discord.com/channels/{guild_id}/1/{10_000 + self.id}")

        self.channel.hook.send = send

    async def reply(self, text, **kwargs):
        self.replies.append((text, kwargs))


def post(bot, message):
    asyncio.run(bot["on_message"](message))
    return message


def test_first_video_stays_intact_and_duplicate_replies_without_pinging(bot_closures):
    bot = bot_closures
    first = post(bot, VideoMessage())
    assert not first.deleted and not first.replies and not first.channel.hook.sent
    later = post(bot, VideoMessage(f"https://youtu.be/{VIDEO}?t=30"))
    assert not later.deleted and not later.channel.hook.sent
    text, kwargs = later.replies[0]
    assert f"[posted before](<{first.jump_url}>)" in text
    assert "VB" in text
    assert kwargs["mention_author"] is False
    assert kwargs["suppress_embeds"] is True
    assert kwargs["allowed_mentions"].to_dict() == {"parse": []}
    assert bot["seen_links"].earlier(7, [utils.link_key(WATCH)])[utils.link_key(WATCH)][0] == first.jump_url


def test_other_videos_and_servers_do_not_collide(bot_closures):
    post(bot_closures, VideoMessage())
    other = post(bot_closures, VideoMessage(f"https://youtube.com/watch?v={OTHER}"))
    elsewhere = post(bot_closures, VideoMessage(WATCH, guild_id=8))
    assert not other.replies and not elsewhere.replies


def test_repeated_links_and_multiple_videos_from_one_post_get_one_notice(bot_closures):
    first = post(bot_closures, VideoMessage(f"{WATCH} https://youtu.be/{OTHER}"))
    later = post(bot_closures, VideoMessage(f"{WATCH} {WATCH}&t=99 https://youtube.com/shorts/{OTHER}"))
    assert not first.replies
    assert len(later.replies) == 1 and later.replies[0][0].count("posted before") == 1


def test_existing_youtube_music_history_matches_regular_videos(bot_closures, tmp_path):
    store = utils.SeenLinks(str(tmp_path / "links.db"))
    old = "https://discord.com/channels/7/1/123"
    store.remember(7, [f"ytmusic:{VIDEO}"], 123, old, "Original")
    store.db.close()
    later = post(bot_closures, VideoMessage())
    assert old in later.replies[0][0] and "Original" in later.replies[0][0]


def test_deleted_first_share_is_forgotten(bot_closures):
    first = post(bot_closures, VideoMessage())
    asyncio.run(bot_closures["on_raw_message_delete"](NS(message_id=first.id)))
    next_first = post(bot_closures, VideoMessage())
    assert not next_first.replies
    duplicate = post(bot_closures, VideoMessage())
    assert next_first.jump_url in duplicate.replies[0][0]


@pytest.mark.parametrize("text", [f"<{WATCH}>", f"`{WATCH}`", f"```\n{WATCH}\n```"])
def test_suppressed_and_code_links_are_not_tracked(bot_closures, text):
    post(bot_closures, VideoMessage(text))
    first = post(bot_closures, VideoMessage())
    assert not first.replies


def test_suppressed_messages_and_bot_posts_are_not_tracked(bot_closures):
    hidden = VideoMessage()
    hidden.flags.suppress_embeds = True
    post(bot_closures, hidden)
    bot_post = VideoMessage()
    bot_post.author.bot = True
    post(bot_closures, bot_post)
    webhook_post = VideoMessage()
    webhook_post.webhook_id = 99
    post(bot_closures, webhook_post)
    assert not post(bot_closures, VideoMessage()).replies


def test_link_fixer_includes_video_in_history_and_does_not_send_second_notice(bot_closures, monkeypatch):
    import bot

    monkeypatch.setattr(bot.aiohttp, "ClientSession", FakeSession)
    FakeSession.pages = {"vxreddit.com": (200, TEXT)}
    bot_closures["url_rules"].append(REDDIT)
    first = post(bot_closures, VideoMessage())
    mixed = post(bot_closures, VideoMessage(f"https://reddit.com/r/test/comments/abc/title {WATCH}"))
    assert mixed.deleted and not mixed.replies
    assert first.jump_url in mixed.channel.hook.sent[0][0]

    new_video = post(bot_closures, VideoMessage(f"https://reddit.com/r/test/comments/def/title https://youtu.be/{OTHER}"))
    next_share = post(bot_closures, VideoMessage(f"https://youtube.com/shorts/{OTHER}"))
    expected = f"https://discord.com/channels/7/1/{10_000 + new_video.id}"
    assert new_video.deleted and expected in next_share.replies[0][0]


def test_music_lookup_failure_still_tracks_the_video(bot_closures, monkeypatch):
    import bot

    monkeypatch.setattr(bot.aiohttp, "ClientSession", FakeSession)
    FakeSession.pages = {}
    first = post(bot_closures, VideoMessage(f"https://music.youtube.com/watch?v={VIDEO}"))
    later = post(bot_closures, VideoMessage())
    assert not first.deleted and first.jump_url in later.replies[0][0]


def test_reply_failure_keeps_original_history_and_does_not_crash(bot_closures):
    first = post(bot_closures, VideoMessage())
    later = VideoMessage()

    async def forbidden(*args, **kwargs):
        raise discord.Forbidden(NS(status=403, reason="Forbidden"), "Cannot send")

    later.reply = forbidden
    post(bot_closures, later)
    assert not later.deleted
    following = post(bot_closures, VideoMessage())
    assert first.jump_url in following.replies[0][0]
