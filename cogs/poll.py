"""/poll with up to 5 answers. Votes are saved in the database, so the buttons keep
working after the bot restarts."""
from __future__ import annotations

import asyncio
import re
from datetime import datetime, timedelta, timezone
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands, tasks

from core import db, render

NUMBERS = ["1️⃣", "2️⃣", "3️⃣", "4️⃣", "5️⃣"]


def poll_embed(poll: dict) -> discord.Embed:
    counts = poll["counts"]
    total = sum(counts)
    closed = bool(poll["closed"])
    lines = []
    top = max(counts) if counts else 0
    for i, (opt, n) in enumerate(zip(poll["options"], counts)):
        pct = n / total if total else 0
        bar = "█" * round(pct * 12) + "░" * (12 - round(pct * 12))
        crown = " 🏆" if closed and total and n == top else ""
        lines.append(f"{NUMBERS[i]} **{opt}**{crown}\n`{bar}` {pct:.0%} ({n})")
    emb = discord.Embed(title=f"📊 {poll['question']}"[:256], description="\n\n".join(lines),
                        color=0x868E96 if closed else 0x5865F2)
    if closed:
        foot = f"Poll closed · {total} vote{'s' if total != 1 else ''}"
    else:
        foot = f"{total} vote{'s' if total != 1 else ''} · click again to take your vote back"
        if poll["ends_at"]:
            emb.add_field(name="Closes", value=render.ts(poll["ends_at"], "R"))
    emb.set_footer(text=foot)
    return emb


class PollButton(discord.ui.DynamicItem[discord.ui.Button], template=r"anton:poll:(?P<poll>\d+):(?P<opt>\d|close)"):
    def __init__(self, poll_id: int, opt: str, label: str = "", disabled: bool = False):
        is_close = opt == "close"
        super().__init__(discord.ui.Button(
            label=label or ("Close poll" if is_close else "Vote"),
            emoji="🔒" if is_close else None,
            style=discord.ButtonStyle.secondary if is_close or disabled else discord.ButtonStyle.primary,
            custom_id=f"anton:poll:{poll_id}:{opt}",
            disabled=disabled,
        ))
        self.poll_id = poll_id
        self.opt = opt

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item: discord.ui.Button, match: re.Match[str], /):
        return cls(int(match["poll"]), match["opt"], label=item.label or "")

    async def callback(self, interaction: discord.Interaction):
        poll = await asyncio.to_thread(db.get_poll, self.poll_id)
        if not poll:
            return await interaction.response.send_message("This poll no longer exists.", ephemeral=True)
        if poll["closed"]:
            return await interaction.response.send_message("This poll is closed.", ephemeral=True)
        if self.opt == "close":
            perms = getattr(interaction.user, "guild_permissions", None)
            if interaction.user.id != poll["owner_id"] and not (perms and perms.manage_messages):
                return await interaction.response.send_message("Only the person who made the poll (or a mod) can close it.",
                                                               ephemeral=True)
            await asyncio.to_thread(db.close_poll, self.poll_id)
        else:
            await asyncio.to_thread(db.cast_vote, self.poll_id, interaction.user.id, int(self.opt))
        poll = await asyncio.to_thread(db.get_poll, self.poll_id)
        await interaction.response.edit_message(embed=poll_embed(poll), view=build_view(poll))


def build_view(poll: dict) -> discord.ui.View:
    view = discord.ui.View(timeout=None)
    closed = bool(poll["closed"])
    for i, opt in enumerate(poll["options"]):
        view.add_item(PollButton(poll["id"], str(i), label=f"{i + 1}. {opt}"[:80], disabled=closed))
    if not closed:
        view.add_item(PollButton(poll["id"], "close"))
    return view


class Polls(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.expirer.start()

    def cog_unload(self):
        self.expirer.cancel()

    @app_commands.command(name="poll", description="Ask a question with up to 5 answers.")
    @app_commands.describe(question="What are you asking?", answer1="Answer 1", answer2="Answer 2",
                           answer3="Answer 3 (optional)", answer4="Answer 4 (optional)", answer5="Answer 5 (optional)",
                           hours="Close automatically after this many hours (optional, up to 168)")
    async def poll(self, interaction: discord.Interaction,
                   question: app_commands.Range[str, 1, 250],
                   answer1: app_commands.Range[str, 1, 75], answer2: app_commands.Range[str, 1, 75],
                   answer3: Optional[app_commands.Range[str, 1, 75]] = None,
                   answer4: Optional[app_commands.Range[str, 1, 75]] = None,
                   answer5: Optional[app_commands.Range[str, 1, 75]] = None,
                   hours: Optional[app_commands.Range[float, 0.1, 168]] = None):
        options = [a.strip() for a in (answer1, answer2, answer3, answer4, answer5) if a and a.strip()]
        if len(options) < 2:
            return await interaction.response.send_message("A poll needs at least two answers.", ephemeral=True)
        ends = None
        if hours:
            ends = (datetime.now(timezone.utc) + timedelta(hours=hours)).isoformat(timespec="seconds")
        poll_id = await asyncio.to_thread(db.create_poll, interaction.guild_id, interaction.channel_id,
                                          interaction.user.id, question.strip(), options, ends)
        poll = await asyncio.to_thread(db.get_poll, poll_id)
        try:
            await interaction.response.send_message(embed=poll_embed(poll), view=build_view(poll))
            msg = await interaction.original_response()
        except discord.HTTPException:
            await asyncio.to_thread(db.delete_poll, poll_id)
            raise
        await asyncio.to_thread(db.set_poll_message, poll_id, msg.id)

    @tasks.loop(seconds=30)
    async def expirer(self):
        due = await asyncio.to_thread(db.due_polls, db.now_iso())
        for poll_id in due:
            await asyncio.to_thread(db.close_poll, poll_id)
            poll = await asyncio.to_thread(db.get_poll, poll_id)
            if not poll or not poll["message_id"] or not poll["channel_id"]:
                continue
            ch = self.bot.get_channel(poll["channel_id"])
            if ch is None:
                try:
                    ch = await self.bot.fetch_channel(poll["channel_id"])
                except discord.HTTPException:
                    continue
            try:
                await ch.get_partial_message(poll["message_id"]).edit(embed=poll_embed(poll), view=build_view(poll))
            except (discord.HTTPException, AttributeError):
                pass

    @expirer.before_loop
    async def _ready(self):
        await self.bot.wait_until_ready()


async def setup(bot: commands.Bot):
    bot.add_dynamic_items(PollButton)
    await bot.add_cog(Polls(bot))
