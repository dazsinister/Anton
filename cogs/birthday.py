"""/birthday set, show, remove, list, and the daily announcement."""
from __future__ import annotations

import asyncio
from datetime import date, datetime
from typing import Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import discord
from discord import app_commands
from discord.ext import commands, tasks

from core import config, db

MONTHS = ["January", "February", "March", "April", "May", "June", "July",
          "August", "September", "October", "November", "December"]

try:
    TZ = ZoneInfo(config.BDAY_TZ)
except ZoneInfoNotFoundError:
    print(f"Unknown BDAY_TZ '{config.BDAY_TZ}', using America/New_York (pip install tzdata on Windows)")
    TZ = ZoneInfo("America/New_York")


def local_now() -> datetime:
    return datetime.now(TZ)


def is_leap(y: int) -> bool:
    return y % 4 == 0 and (y % 100 != 0 or y % 400 == 0)


def valid(month: int, day: int) -> bool:
    try:
        date(2000, month, day)  # 2000 is a leap year, so Feb 29 is allowed
        return True
    except ValueError:
        return False


def celebrates_on(month: int, day: int, today: date) -> bool:
    if month == 2 and day == 29 and not is_leap(today.year):
        return today.month == 2 and today.day == 28
    return (month, day) == (today.month, today.day)


def days_until(month: int, day: int, today: date) -> int:
    for year in (today.year, today.year + 1):
        d = 28 if (month, day) == (2, 29) and not is_leap(year) else day
        target = date(year, month, d)
        if target >= today:
            return (target - today).days
    return 366


class Birthdays(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.announcer.start()

    def cog_unload(self):
        self.announcer.cancel()

    group = app_commands.Group(name="birthday", description="Birthdays.", guild_only=True)

    @group.command(name="set", description="Save your birthday. The year is optional and never shown.")
    @app_commands.describe(month="Month", day="Day (1-31)", year="Year (optional)")
    @app_commands.choices(month=[app_commands.Choice(name=m, value=i + 1) for i, m in enumerate(MONTHS)])
    async def bday_set(self, interaction: discord.Interaction, month: app_commands.Choice[int],
                       day: app_commands.Range[int, 1, 31],
                       year: Optional[app_commands.Range[int, 1900, 2100]] = None):
        if not valid(month.value, day):
            return await interaction.response.send_message(f"{month.name} {day} isn't a real date.", ephemeral=True)
        if year and year > local_now().year:
            return await interaction.response.send_message("That year hasn't happened yet!", ephemeral=True)
        await asyncio.to_thread(db.set_birthday, interaction.user.id, month.value, day, year)
        note = " (in non-leap years I'll celebrate on Feb 28)" if (month.value, day) == (2, 29) else ""
        await interaction.response.send_message(f"Saved: **{month.name} {day}** 🎂{note}", ephemeral=True)

    @group.command(name="show", description="Show the birthday you saved.")
    async def bday_show(self, interaction: discord.Interaction):
        mine = [b for b in await asyncio.to_thread(db.all_birthdays) if b["user_id"] == interaction.user.id]
        if not mine:
            return await interaction.response.send_message("No birthday saved. Use `/birthday set`.", ephemeral=True)
        b = mine[0]
        yr = f", {b['year']}" if b["year"] else ""
        await interaction.response.send_message(f"Your birthday: **{MONTHS[b['month'] - 1]} {b['day']}{yr}**", ephemeral=True)

    @group.command(name="remove", description="Delete your saved birthday.")
    async def bday_remove(self, interaction: discord.Interaction):
        ok = await asyncio.to_thread(db.remove_birthday, interaction.user.id)
        await interaction.response.send_message("Removed your birthday." if ok else "You didn't have one saved.", ephemeral=True)

    @group.command(name="list", description="Everyone's birthdays in this server, soonest first.")
    async def bday_list(self, interaction: discord.Interaction):
        await interaction.response.defer()
        today = local_now().date()
        rows = []
        for b in await asyncio.to_thread(db.all_birthdays):
            member = interaction.guild.get_member(b["user_id"])
            if member is None:
                try:
                    member = await interaction.guild.fetch_member(b["user_id"])
                except discord.HTTPException:
                    continue  # not in this server
            rows.append((days_until(b["month"], b["day"], today), b, member))
        if not rows:
            return await interaction.followup.send("Nobody here has saved a birthday yet. Use `/birthday set`!")
        rows.sort(key=lambda r: r[0])
        lines = []
        for days, b, member in rows:
            when = "🎉 **today!**" if days == 0 else "tomorrow" if days == 1 else f"in {days} days"
            lines.append(f"**{MONTHS[b['month'] - 1][:3]} {b['day']}** — {member.display_name} ({when})")
        emb = discord.Embed(title=f"🎂 Birthdays in {interaction.guild.name}", color=0xF783AC)
        # Embed descriptions max out at 4096 characters
        desc = ""
        for i, line in enumerate(lines):
            if len(desc) + len(line) > 4000:
                desc += f"…and {len(lines) - i} more"
                break
            desc += line + "\n"
        emb.description = desc
        await interaction.followup.send(embed=emb, allowed_mentions=discord.AllowedMentions.none())

    # ── daily announcement ──────────────────────────────────

    @tasks.loop(minutes=5)
    async def announcer(self):
        now = local_now()
        if now.hour < config.BDAY_ANNOUNCE_HOUR:
            return
        today = now.date().isoformat()
        if db.get_setting("birthday:last_announced") == today:
            return
        db.set_setting("birthday:last_announced", today)  # mark first so a failure can't double-post

        celebrants = [b["user_id"] for b in await asyncio.to_thread(db.all_birthdays)
                      if celebrates_on(b["month"], b["day"], now.date())]
        if not celebrants or not config.BDAY_CHANNEL_ID:
            return
        ch = self.bot.get_channel(config.BDAY_CHANNEL_ID)
        if ch is None:
            try:
                ch = await self.bot.fetch_channel(config.BDAY_CHANNEL_ID)
            except discord.HTTPException as e:
                print(f"Birthday channel {config.BDAY_CHANNEL_ID} unavailable: {e}")
                return
        guild = getattr(ch, "guild", None)
        present = []
        for uid in celebrants:
            if guild is None or guild.get_member(uid):
                present.append(uid)
                continue
            try:
                await guild.fetch_member(uid)
                present.append(uid)
            except discord.HTTPException:
                pass
        if not present:
            return
        mentions = ", ".join(f"<@{u}>" for u in present)
        try:
            await ch.send(f"🎉 Happy birthday {mentions}! 🎂 Have a great one!",
                          allowed_mentions=discord.AllowedMentions(users=True))
        except discord.HTTPException as e:
            print(f"Birthday post failed: {e}")

    @announcer.before_loop
    async def _ready(self):
        await self.bot.wait_until_ready()


async def setup(bot: commands.Bot):
    await bot.add_cog(Birthdays(bot))
