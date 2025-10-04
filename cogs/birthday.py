# cogs/birthday.py
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

# ---------- Config via env ----------
BDAY_CHANNEL_ID = int(os.getenv("BDAY_CHANNEL_ID", "0").strip() or 0)
# Hour (0–23) in your server's local time when announcements should post
BDAY_ANNOUNCE_HOUR = int(os.getenv("BDAY_ANNOUNCE_HOUR", "9").strip() or 9)
# Optional timezone name (only used for rendering; scheduling is host-local time)
BDAY_TZ = os.getenv("BDAY_TZ", "America/New_York")

# ---------- Storage paths ----------
BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"
DATA_DIR.mkdir(parents=True, exist_ok=True)

BIRTHDAYS_JSON = DATA_DIR / "birthdays.json"
ANNOUNCE_MARK  = DATA_DIR / "birthday_last_announce.txt"

# ---------- Helpers ----------
@dataclass
class Birthday:
    month: int
    day: int
    year: Optional[int] = None  # year is optional for privacy; age calc not required

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
    # Accept month/day without year; if year missing, validate using a leap-safe dummy (e.g., 2000).
    y = year if year else 2000
    try:
        _ = date(y, month, day)
        return True
    except ValueError:
        # Allow Feb 29 entries; we’ll announce them on Feb 29 (or Feb 28 on non-leap years).
        if month == 2 and day == 29:
            return True
        return False

def _today_local() -> date:
    # Use system local date for scheduling; simplest + reliable with Task Scheduler / systemd.
    # (We still show BDAY_TZ label in messages for clarity.)
    return datetime.now().date()

def _is_leap_year(y: int) -> bool:
    return (y % 4 == 0 and y % 100 != 0) or (y % 400 == 0)

def _matches_today(b: Birthday, today: date) -> bool:
    if b.month == 2 and b.day == 29 and not _is_leap_year(today.year):
        # Policy: celebrate Feb 29 birthdays on Feb 28 in non-leap years
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

# ---------- Cog ----------
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
        # a lightweight loop that wakes frequently until we hit the target hour, then announces once per day
        self.last_announced = _load_announced_date()
        self.bday_watcher.start()

    def cog_unload(self):
        try:
            self.bday_watcher.cancel()
        except Exception:
            pass

    # ---------- Slash commands ----------
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

    # ---------- Daily announcer ----------
    @tasks.loop(minutes=5.0)
    async def bday_watcher(self):
        """
        Wakes every 5 minutes. When local time crosses BDAY_ANNOUNCE_HOUR and we haven’t
        announced for today yet, post all birthdays for today.
        """
        today = _today_local()
        # If we already announced today, nothing to do
        if self.last_announced == today:
            return

        now = datetime.now()
        if now.hour != BDAY_ANNOUNCE_HOUR:
            return  # Not time yet

        # Resolve channel
        target = None
        if BDAY_CHANNEL_ID:
            try:
                target = self.bot.get_channel(BDAY_CHANNEL_ID) or await self.bot.fetch_channel(BDAY_CHANNEL_ID)
            except Exception:
                target = None
        if target is None:
            # Fallback: try the first text channel of the first guild
            for g in self.bot.guilds:
                for ch in g.text_channels:
                    target = ch
                    break
                if target:
                    break
        if target is None or not hasattr(target, "send"):
            return  # nowhere to post

        # Build list of celebrants
        celebrants = [int(uid) for uid, b in self.book.items() if _matches_today(b, today)]
        if not celebrants:
            # Mark as announced for the day to avoid waking again
            self.last_announced = today
            _save_announced_date(today)
            return

        # Post one message tagging everyone with birthday today
        mentions = " ".join(f"<@{uid}>" for uid in celebrants)
        # Optional: include the timezone label in message
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
    # Attach the cog itself
    await bot.add_cog(cog)

    # Add the slash group once (skip if it's already registered)
    if bot.tree.get_command("birthday") is None:
        bot.tree.add_command(BirthdayCog.group)
