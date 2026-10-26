"""Shared test setup. bot.py and utils.py live at the repo root, not in a package."""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


@pytest.fixture
def bot_closures(tmp_path, monkeypatch):
    """Everything run_discord_bot() defines (commands, handlers, stores), without connecting:
    discord.Client.run is replaced by one that grabs the function's locals. Files the bot writes
    (config.json, links.db, uploaded reactions) go to a temporary folder."""
    import discord

    import bot

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("BOT_TOKEN", "test")
    monkeypatch.setattr(bot, "REACTIONS_DIR", str(tmp_path / "reactions"))
    captured = {}
    monkeypatch.setattr(discord.Client, "run", lambda self, *a, **k: captured.update(sys._getframe(1).f_locals))
    bot.run_discord_bot()
    return captured
