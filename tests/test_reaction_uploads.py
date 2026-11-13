"""Saved Discord attachments follow the destination server's upload allowance."""

import asyncio
from types import SimpleNamespace

import pytest

from test_bot import Channel, interaction, said


URL = "https://cdn.discordapp.com/attachments/1/2/image.png"


class Download:
    def __init__(self, chunks, content_length=None):
        self.chunks = chunks
        self.content_length = content_length
        self.status = 200
        self.consumed = 0
        self.content = SimpleNamespace(iter_chunked=self.iter_chunked)

    async def iter_chunked(self, size):
        for chunk in self.chunks:
            self.consumed += 1
            yield chunk

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


def mock_download(monkeypatch, response):
    import bot

    session = SimpleNamespace(closed=False, get=lambda *a, **kw: response)
    monkeypatch.setattr(bot.aiohttp, "ClientSession", lambda **kw: session)


def test_addai_discord_link_uses_server_limit_and_preserves_old_file_on_failure(bot_closures, monkeypatch):
    bot = bot_closures
    channel = Channel()
    channel.guild.filesize_limit = 8
    response = Download([b"1234", b"5678"], content_length=8)
    mock_download(monkeypatch, response)
    i = interaction(channel)
    asyncio.run(bot["add_react"].callback(i, "image", URL))
    assert bot["reactions"]["image"] == "file:image.png"
    assert "Saved `image`" in said(i)

    response.content_length = 9
    i = interaction(channel)
    asyncio.run(bot["add_react"].callback(i, "image", URL))
    assert "upload limit" in said(i)
    assert response.consumed == 2  # rejected from the header without downloading again
    with open(bot["reaction_file"]("image"), "rb") as saved:
        assert saved.read() == b"12345678"


@pytest.mark.parametrize("content_length", [None, 0, 4])
def test_download_stops_when_body_exceeds_limit_despite_header(bot_closures, monkeypatch, content_length):
    response = Download([b"1234", b"5678", b"9", b"never read"], content_length)
    mock_download(monkeypatch, response)
    assert asyncio.run(bot_closures["download_upload"](URL, 8)) is None
    assert response.consumed == 3


def test_discord_link_above_old_cap_is_allowed_by_larger_server(bot_closures, monkeypatch):
    channel = Channel()
    channel.guild.filesize_limit = 50 * 2**20
    response = Download([b"image bytes"], content_length=15 * 2**20)
    mock_download(monkeypatch, response)
    i = interaction(channel)
    asyncio.run(bot_closures["add_react"].callback(i, "image", URL))
    assert "Saved `image`" in said(i)
