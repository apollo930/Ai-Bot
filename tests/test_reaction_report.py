import asyncio
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock

from reaction_report import ReactionReport
from reaction_stats import ReactionTracker


def test_long_breakdown_remains_readable_and_every_emoji_is_reachable(tmp_path):
    async def flow():
        tracker = ReactionTracker(NS(get_channel=lambda cid: None), lambda m: None, tmp_path / "links.db")
        tracker.progress[1], tracker.scope = 4487, (None, None)
        interaction = NS(guild_id=1, user=NS(id=10), edit_original_response=AsyncMock())
        user = NS(id=10, display_name="Actual Zombie")
        # Custom emoji remain exact, even when they belong to another server.
        breakdown = [("❤️", 1055)] + [(f"<a:external_{i}:1234567890123456{i:02}>", 1) for i in range(25)]
        report = ReactionReport(interaction, user, 1080, breakdown, None, None, tracker)
        component = NS(user=NS(id=10), response=NS(edit_message=AsyncMock(), send_message=AsyncMock()))
        assert await report.interaction_check(component)
        assert report.previous_page.disabled and not report.next_page.disabled
        seen = []
        for page in range(report.pages):
            embed = report.embed()
            field = next(field for field in embed.fields if field.name == "By emoji")
            seen.extend(line.split("  **")[0] for line in field.value.splitlines())
            assert len(field.value.splitlines()) <= 8
            assert len(embed) <= 6000 and all(len(field.value) <= 1024 for field in embed.fields)
            progress = next(field.value for field in embed.fields if field.name == "Scan in progress")
            assert "4,487" in progress and "all server history" in progress
            assert "4,487" not in embed.footer.text
            if page < report.pages - 1:
                await report.next_page.callback(component)
        assert seen == [emoji for emoji, count in breakdown]
        assert report.next_page.disabled
        await report.next_page.callback(component)
        assert report.page == report.pages - 1
        await report.previous_page.callback(component)
        assert report.page == report.pages - 2
        assert component.response.edit_message.await_count == report.pages + 1

        stranger = NS(user=NS(id=99), response=NS(send_message=AsyncMock()))
        assert not await report.interaction_check(stranger)
        assert stranger.response.send_message.call_args.kwargs["ephemeral"]
        await report.on_timeout()
        assert all(button.disabled for button in report.children)
        interaction.edit_original_response.assert_awaited_once()
        report.stop()

    asyncio.run(flow())
