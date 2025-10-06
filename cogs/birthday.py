from __future__ import annotations
import json
import os
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, Optional
import discord
from discord import app_commands
from discord.ext import commands, tasks

BDAY_CHANNEL_ID = int(os.getenv("BDAY_CHANNEL_ID", "0").strip() or 0)
BDAY_ANNOUNCE_HOUR = int(os.getenv("BDAY_ANNOUNCE_HOUR", "9").strip() or 9)
BDAY_TZ = os.getenv("BDAY_TZ", "America/New_York")

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"
DATA_DIR.mkdir(parents=True, exist_ok=True)

BIRTHDAYS_JSON = DATA_DIR / "birthdays.json"
ANNOUNCE_MARK  = DATA_DIR / "birthday_last_announce.txt"

@dataclass
class Birthday:
    month: int
    day: int
    year: Optional[int] = None 

def _load_birthdays() -> Dict[str, Birthday]:
    if not BIRTHDAYS_JSON.exists():
        return {}
    try:
        raw = json.loads(BIRTHDAYS_JSON.read_text(encoding="utf-8"))
        out: Dict[str, Birthday] = {}
        for uid, v in raw.items():
            out[str(uid)] = Birthday(
                month=int(v.get("month", 0)),
                day=int(v.get("day", 0)),
                year=(int(v["year"]) if v.get("year") else None)
            )
        return out
    except Exception:
        return {}

def _save_birthdays(book: Dict[str, Birthday]) -> None:
    payload = {
        uid: {"month": b.month, "day": b.day, "year": b.year} for uid, b in book.items()
    }
    BIRTHDAYS_JSON.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

def _is_valid_date(month: int, day: int, year: Optional[int]) -> bool:
    y = year if year else 2000
    try:
        _ = date(y, month, day)
        return True
    except ValueError:
        if month == 2 and day == 29:
            return True
        return False

def _today_local() -> date:
    return datetime.now().date()

def _is_leap_year(y: int) -> bool:
    return (y % 4 == 0 and y % 100 != 0) or (y % 400 == 0)

def _matches_today(b: Birthday, today: date) -> bool:
    if b.month == 2 and b.day == 29 and not _is_leap_year(today.year):
        return today.month == 2 and today.day == 28
    return (b.month == today.month and b.day == today.day)

def _load_announced_date() -> Optional[date]:
    if not ANNOUNCE_MARK.exists():
        return None
    try:
        s = ANNOUNCE_MARK.read_text(encoding="utf-8").strip()
        return date.fromisoformat(s)
    except Exception:
        return None

def _save_announced_date(d: date) -> None:
    ANNOUNCE_MARK.write_text(d.isoformat(), encoding="utf-8")

class BirthdayCog(commands.Cog):
    """
    /birthday set <month> <day> [year]
    /birthday show
    /birthday remove

    A background task checks daily at BDAY_ANNOUNCE_HOUR local time and posts
    a birthday message tagging users in BDAY_CHANNEL_ID.
    """
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.book: Dict[str, Birthday] = _load_birthdays()
        self.last_announced = _load_announced_date()
        self.bday_watcher.start()

    def cog_unload(self):
        try:
            self.bday_watcher.cancel()
        except Exception:
            pass

    group = app_commands.Group(name="birthday", description="Manage your birthday.")

    @group.command(name="set", description="Save your birthday (Month/Day/Year). Year is optional.")
    @app_commands.describe(
        month="Month (1-12)",
        day="Day (1-31)",
        year="Year (optional, e.g., 1990)"
    )
    async def birthday_set(self, interaction: discord.Interaction, month: int, day: int, year: Optional[int] = None):
        await interaction.response.defer(ephemeral=True)
        if not (1 <= month <= 12):
            return await interaction.followup.send("Month must be between 1 and 12.", ephemeral=True)
        if not (1 <= day <= 31):
            return await interaction.followup.send("Day must be between 1 and 31.", ephemeral=True)
        if year is not None and (year < 1900 or year > date.today().year):
            return await interaction.followup.send(f"Year must be between 1900 and {date.today().year}.", ephemeral=True)
        if not _is_valid_date(month, day, year):
            return await interaction.followup.send("That date doesn’t look valid. If you were born on Feb 29, we’ll announce on Feb 28 in non-leap years.", ephemeral=True)

        uid = str(interaction.user.id)
        self.book[uid] = Birthday(month=month, day=day, year=year)
        _save_birthdays(self.book)

        pretty_year = f", {year}" if year else ""
        await interaction.followup.send(f"Saved your birthday as **{month}/{day}{pretty_year}** 🎂", ephemeral=True)

    @group.command(name="show", description="Show your currently saved birthday.")
    async def birthday_show(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        b = self.book.get(str(interaction.user.id))
        if not b:
            return await interaction.followup.send("No birthday saved. Use `/birthday set` to add yours.", ephemeral=True)
        pretty = f"{b.month}/{b.day}" + (f"/{b.year}" if b.year else "")
        await interaction.followup.send(f"Your birthday on file: **{pretty}**", ephemeral=True)

    @group.command(name="remove", description="Remove your saved birthday.")
    async def birthday_remove(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        uid = str(interaction.user.id)
        if uid in self.book:
            self.book.pop(uid, None)
            _save_birthdays(self.book)
            return await interaction.followup.send("Removed your birthday.", ephemeral=True)
        await interaction.followup.send("You don’t have a birthday saved.", ephemeral=True)

    @tasks.loop(minutes=5.0)
    async def bday_watcher(self):
        """
        Wakes every 5 minutes. When local time crosses BDAY_ANNOUNCE_HOUR and we haven’t
        announced for today yet, post all birthdays for today.
        """
        today = _today_local()
        if self.last_announced == today:
            return

        now = datetime.now()
        if now.hour != BDAY_ANNOUNCE_HOUR:
            return 

        target = None
        if BDAY_CHANNEL_ID:
            try:
                target = self.bot.get_channel(BDAY_CHANNEL_ID) or await self.bot.fetch_channel(BDAY_CHANNEL_ID)
            except Exception:
                target = None
        if target is None:
            for g in self.bot.guilds:
                for ch in g.text_channels:
                    target = ch
                    break
                if target:
                    break
        if target is None or not hasattr(target, "send"):
            return  

        celebrants = [int(uid) for uid, b in self.book.items() if _matches_today(b, today)]
        if not celebrants:
            self.last_announced = today
            _save_announced_date(today)
            return

        mentions = " ".join(f"<@{uid}>" for uid in celebrants)
        try:
            await target.send(f"🎉 **Happy birthday** {mentions}! 🎂 ({BDAY_TZ})")
        except Exception:
            pass

        self.last_announced = today
        _save_announced_date(today)

    @bday_watcher.before_loop
    async def _wait_until_ready(self):
        await self.bot.wait_until_ready()

async def setup(bot: commands.Bot):
    cog = BirthdayCog(bot)
    await bot.add_cog(cog)

    if bot.tree.get_command("birthday") is None:
        bot.tree.add_command(BirthdayCog.group)
