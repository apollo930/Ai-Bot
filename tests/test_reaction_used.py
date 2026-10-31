"""/reactions shows the 5 emoji a person reacts with most."""

import asyncio
from types import SimpleNamespace as NS

from reaction_stats import ReactionTracker
from test_reaction_stats import Channel, Client


def run(coro):
    return asyncio.run(coro)


def test_sent_summary_top_five(tmp_path):
    t = ReactionTracker(Client(), lambda m: None, tmp_path / "links.db")
    emojis = ["😂"] * 4 + ["❤️"] * 2 + ["❤"] + ["🔥"] * 3 + ["🐳", "😮", "➕"]
    for i, e in enumerate(emojis):
        t.store.replace(1, 2 if i % 2 else 3, 100 + i, 10, [(20, f"unicode:{e}", e)])
    t.store.replace(1, 2, 200, 20, [(10, "unicode:😂", "😂")])  # given by someone else
    total, used = t.store.sent_summary(1, 20)
    assert total == len(emojis)
    assert used[0] == ("😂", 4) and used[1][1] == 3 and len(used) == 5
    assert dict(used)["🔥"] == 3 and dict(used).get("❤️", dict(used).get("❤")) == 3  # ❤ and ❤️ together
    assert t.store.sent_summary(1, 20, channel_id=2)[0] == len(emojis) // 2
    assert t.store.sent_summary(1, 20, channels=set()) == (0, [])
    assert t.store.sent_summary(1, 99) == (0, [])


def test_reactions_command_shows_what_they_use_most(bot_closures, monkeypatch):
    bot = bot_closures
    store = bot["reaction_tracker"].store
    store.replace(1, 2, 100, 30, [(10, "unicode:😂", "😂")])
    store.replace(1, 2, 101, 30, [(10, "unicode:🔥", "🔥")])
    store.replace(1, 3, 102, 30, [(10, "unicode:😂", "😂")])  # in a channel the asker can't read
    channels = {2: Channel(2), 3: Channel(3, allowed=False)}
    monkeypatch.setattr(bot["client"], "get_channel", lambda cid: channels.get(cid))

    class Response:
        async def send_message(self, **kwargs):
            self.kwargs = kwargs

    i = NS(guild_id=1, user=NS(id=10, display_name="VB"), response=Response())
    run(bot["reaction_totals"].callback(i))
    fields = {f.name: f.value for f in i.response.kwargs["embed"].fields}
    lines = fields["Reacts with most"].splitlines()
    assert lines[0] == "**2** reactions given"  # the 😂 in the hidden channel doesn't count
    assert sorted(lines[1:]) == sorted(["😂  **1**", "🔥  **1**"])  # a tie: either order
    j = NS(guild_id=1, user=NS(id=30, display_name="Alex"), response=Response())
    run(bot["reaction_totals"].callback(j))
    assert {f.name: f.value for f in j.response.kwargs["embed"].fields}["Reacts with most"] == "No reactions given yet."
