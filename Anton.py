"""Anton: InStockTrades / HPB / free games watcher, plus birthdays, polls, price lookups and more.

Run with:  python Anton.py
"""
from __future__ import annotations

import logging
import traceback

import discord
from discord import app_commands
from discord.ext import commands

from core import config, db, feeds

COGS = (
    "cogs.feeds",
    "cogs.lookup",
    "cogs.birthday",
    "cogs.poll",
    "cogs.fun",
    "cogs.ai",
)


class Anton(commands.Bot):
    def __init__(self):
        intents = discord.Intents.default()
        intents.members = False          # not needed; keeps setup simple (no privileged intents)
        intents.message_content = False  # everything is slash commands
        super().__init__(command_prefix=commands.when_mentioned, intents=intents)

    async def setup_hook(self):
        # Runs once at startup (unlike on_ready, which fires again on every reconnect)
        db.init()
        moved = db.migrate_birthdays_json()
        if moved:
            print(f"Imported {moved} birthdays from data/birthdays.json")

        for ext in COGS:
            try:
                await self.load_extension(ext)
                print(f"Loaded {ext}")
            except Exception:
                print(f"Failed to load {ext}:")
                traceback.print_exc()

        self.tree.on_error = self.on_app_command_error
        if config.TEST_GUILD_ID:
            guild = discord.Object(id=config.TEST_GUILD_ID)
            self.tree.copy_global_to(guild=guild)
            synced = await self.tree.sync(guild=guild)
            print(f"Synced {len(synced)} slash commands to test server {config.TEST_GUILD_ID}")
        else:
            synced = await self.tree.sync()
            print(f"Synced {len(synced)} global slash commands (new ones can take a while to appear)")

    async def on_ready(self):
        print(f"Logged in as {self.user} (ID: {self.user.id}) in {len(self.guilds)} server(s)")

    async def on_app_command_error(self, interaction: discord.Interaction, error: app_commands.AppCommandError):
        if isinstance(error, app_commands.MissingPermissions):
            msg = "You don't have permission to use that."
        elif isinstance(error, app_commands.CommandOnCooldown):
            msg = f"Slow down! Try again in {error.retry_after:.0f}s."
        else:
            msg = "Something went wrong running that command."
            traceback.print_exception(type(error), error, error.__traceback__)
        try:
            if interaction.response.is_done():
                await interaction.followup.send(msg, ephemeral=True)
            else:
                await interaction.response.send_message(msg, ephemeral=True)
        except discord.HTTPException:
            pass


def print_config():
    print("Feeds:")
    for f in feeds.FEEDS:
        where = f.thread_id or f.channel_id or "-"
        print(f"  {f.label:<14} channel={where} every {f.interval_min}min  post_new={f.post_new}")
    print(f"AI (/ask): {'on, model ' + config.AI_MODEL if config.AI_ENABLED else 'off (no API key in .env)'}")
    print(f"Database: {config.DB_PATH}")


if __name__ == "__main__":
    if not config.DISCORD_TOKEN:
        raise SystemExit("Set DISCORD_TOKEN in .env (copy .env.example to .env first).")
    db.init()
    print_config()
    discord.utils.setup_logging(level=logging.INFO, root=False)
    Anton().run(config.DISCORD_TOKEN, log_handler=None)
