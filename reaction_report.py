"""Compact Discord presentation of reaction totals, with pages for longer breakdowns."""

import discord


def add_scan_status(embed, tracker, guild_id):
    checked = tracker.progress.get(guild_id)
    if checked is not None:
        embed.add_field(name="Scan in progress", inline=False, value=(
            f"**{checked:,}** messages checked\n{tracker.scope_text(*tracker.scope)}\n"
            "Totals are still updating."))
    else:
        parts = tracker.status(guild_id).split(" · ")
        embed.add_field(name="History coverage", value="\n".join(parts), inline=False)


class ReactionReport(discord.ui.View):
    PAGE_SIZE = 8

    def __init__(self, interaction, user, total, breakdown, channel, most_reacted, tracker):
        super().__init__(timeout=300)
        self.interaction, self.user = interaction, user
        self.total, self.breakdown = total, breakdown
        self.channel, self.most_reacted, self.tracker = channel, most_reacted, tracker
        self.page = 0
        self.pages = max(1, (len(breakdown) + self.PAGE_SIZE - 1) // self.PAGE_SIZE)
        self._update_buttons()

    def _update_buttons(self):
        self.previous_page.disabled = self.page == 0
        self.next_page.disabled = self.page == self.pages - 1

    def embed(self):
        scope = f"In {self.channel.mention}" if self.channel is not None else "Across this server"
        embed = discord.Embed(title=f"{self.user.display_name} · Reactions", color=0x5865F2,
                              description=f"**{self.total:,}** reactions received\n{scope}")
        if self.breakdown:
            start = self.page * self.PAGE_SIZE
            rows = self.breakdown[start:start + self.PAGE_SIZE]
            # Keep custom emoji markup intact, including emoji rendered by client extensions.
            embed.add_field(name="By emoji", inline=False,
                            value="\n".join(f"{emoji}  **{count:,}**" for emoji, count in rows))
        else:
            embed.description += "\n\nNo reactions counted yet."
        if self.most_reacted:
            url, count = self.most_reacted
            embed.add_field(name="Most-reacted message", inline=False,
                            value=f"[Jump to message]({url}) · **{count:,}** reactions")
        add_scan_status(embed, self.tracker, self.interaction.guild_id)
        footer = "Bots and self-reactions excluded"
        if self.pages > 1:
            footer = f"Page {self.page + 1}/{self.pages} · {len(self.breakdown):,} emoji · " + footer
        embed.set_footer(text=footer)
        return embed

    async def interaction_check(self, interaction):
        if interaction.user.id == self.interaction.user.id:
            return True
        await interaction.response.send_message("Run /reactions to open your own report.", ephemeral=True)
        return False

    @discord.ui.button(label="Previous", style=discord.ButtonStyle.secondary)
    async def previous_page(self, interaction, button):
        self.page = max(0, self.page - 1)
        self._update_buttons()
        await interaction.response.edit_message(embed=self.embed(), view=self)

    @discord.ui.button(label="Next", style=discord.ButtonStyle.secondary)
    async def next_page(self, interaction, button):
        self.page = min(self.pages - 1, self.page + 1)
        self._update_buttons()
        await interaction.response.edit_message(embed=self.embed(), view=self)

    async def on_timeout(self):
        for item in self.children:
            item.disabled = True
        try:
            await self.interaction.edit_original_response(view=self)
        except discord.HTTPException:
            pass  # the report may have been deleted, or the interaction token expired
