"""Hall of fame: a message enough people react to gets reposted to a chosen channel.

Runs on the reaction counts reaction_stats already keeps, and only on live reaction events,
so history scans and catch-ups never flood the channel with old messages.
"""

import asyncio
import logging
import sqlite3
import time

import discord

log = logging.getLogger(__name__)

MAX_AGE = 7 * 86400  # only messages from the last week get in (an old one getting one more reaction doesn't)
GOLD = 0xF1C40F


def message_age(message_id):
    return time.time() - discord.utils.snowflake_time(message_id).timestamp()


class HallOfFame:
    def __init__(self, client, store, path="links.db"):
        self.client, self.store = client, store
        self.db = sqlite3.connect(path)
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS hall_settings (
                guild_id INTEGER PRIMARY KEY,
                channel_id INTEGER NOT NULL,
                threshold INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS hall_posts (
                message_id INTEGER PRIMARY KEY,
                guild_id INTEGER NOT NULL,
                hall_message_id INTEGER NOT NULL
            );
        """)
        self.db.commit()
        self.lock = asyncio.Lock()  # one check at a time: two reactions at once mustn't post twice

    # ----- settings ------------------------------------------------------------
    def settings(self, guild_id):
        """(channel id, threshold) for a guild, or None when the hall is off."""
        return self.db.execute("SELECT channel_id, threshold FROM hall_settings WHERE guild_id = ?", (guild_id,)).fetchone()

    def configure(self, guild_id, channel_id, threshold):
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO hall_settings VALUES (?, ?, ?)", (guild_id, channel_id, threshold))

    def disable(self, guild_id):
        with self.db:
            self.db.execute("DELETE FROM hall_settings WHERE guild_id = ?", (guild_id,))

    def posted(self, message_id):
        row = self.db.execute("SELECT hall_message_id FROM hall_posts WHERE message_id = ?", (message_id,)).fetchone()
        return row[0] if row else None

    def forget(self, message_id):
        with self.db:
            self.db.execute("DELETE FROM hall_posts WHERE message_id = ?", (message_id,))

    # ----- live reactions ------------------------------------------------------
    async def on_change(self, guild_id, channel_id, message_id):
        """Called after a live reaction event changed a message's counts."""
        settings = self.settings(guild_id)
        if not settings or channel_id == settings[0]:
            return
        hall_channel_id, threshold = settings
        async with self.lock:
            people = self.store.reactors(message_id)
            hall_message_id = self.posted(message_id)
            if hall_message_id is None and (people < threshold or message_age(message_id) > MAX_AGE):
                return
            channel = self.client.get_channel(channel_id) or await self.client.fetch_channel(channel_id)
            hall = self.client.get_channel(hall_channel_id)
            if hall is None:
                return
            # every channel counts, private and NSFW ones too (the owner's choice)
            message = await channel.fetch_message(message_id)
            embed, view = self.card(message, channel, people)
            if hall_message_id is None:
                sent = await hall.send(embed=embed, view=view, allowed_mentions=discord.AllowedMentions.none())
                with self.db:
                    self.db.execute("INSERT OR REPLACE INTO hall_posts VALUES (?, ?, ?)", (message_id, guild_id, sent.id))
            else:
                try:
                    await hall.get_partial_message(hall_message_id).edit(embed=embed, view=view)
                except discord.NotFound:
                    self.forget(message_id)  # someone deleted the hall post: leave it gone

    def card(self, message, channel, people):
        author_id = self.store.owner(message.id)
        member = channel.guild.get_member(author_id) if author_id else None
        name = member.display_name if member else message.author.display_name
        avatar = (member.display_avatar.url if member else message.author.display_avatar.url)
        text = message.content or ""
        embed = discord.Embed(description=text[:1500] + ("…" if len(text) > 1500 else ""), color=GOLD,
                              timestamp=message.created_at)
        embed.set_author(name=name, icon_url=avatar)
        image = next((a.url for a in message.attachments if (a.content_type or "").startswith("image/")), None)
        if image is None:  # a fixed link's preview (Instagram, X...) still gives the card a picture
            for e in message.embeds:
                picture = (e.image and e.image.url) or (e.thumbnail and e.thumbnail.url)
                if picture:
                    image = picture
                    break
        if image:
            embed.set_image(url=image)
        counts = " · ".join(f"{emoji} **{n}**" for emoji, n in self.store.message_breakdown(message.id)[:8])
        embed.add_field(name=f"{people} people reacted", value=counts or "—", inline=False)
        embed.set_footer(text=f"#{getattr(channel, 'name', 'channel')}")
        view = discord.ui.View(timeout=None)
        view.add_item(discord.ui.Button(label="Jump to message", url=message.jump_url))
        return embed, view
