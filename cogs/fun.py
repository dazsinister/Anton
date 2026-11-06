"""/joke"""
from __future__ import annotations

import random
from typing import Optional

import aiohttp
import discord
from discord import app_commands
from discord.ext import commands

JOKEAPI = "https://v2.jokeapi.dev/joke/{cat}?safe-mode&blacklistFlags=nsfw,religious,political,racist,sexist,explicit"
DADJOKE = "https://icanhazdadjoke.com/"
UA = "Anton Discord bot (https://github.com/dazsinister/Anton)"

# Used if the joke sites are down
BACKUP = [
    ("Why did the comic book go to therapy?", "It had too many issues."),
    ("Why don't skeletons fight each other?", "They don't have the guts."),
    ("What do you call a fake noodle?", "An impasta."),
    ("Why did the scarecrow win an award?", "He was outstanding in his field."),
    ("How does Batman's mom call him to dinner?", "Dinner dinner dinner dinner dinner dinner dinner dinner… BATMAN!"),
    ("Why can't you trust an atom?", "They make up everything."),
    ("What do you call a bear with no teeth?", "A gummy bear."),
    ("Why did the bicycle fall over?", "It was two tired."),
    ("Why do programmers prefer dark mode?", "Because light attracts bugs."),
    ("What's Thor's favourite food?", "Thor-tilla chips."),
]

CATEGORIES = [
    app_commands.Choice(name="Anything", value="Any"),
    app_commands.Choice(name="Dad jokes", value="dad"),
    app_commands.Choice(name="Puns", value="Pun"),
    app_commands.Choice(name="Programming", value="Programming"),
    app_commands.Choice(name="Spooky", value="Spooky"),
    app_commands.Choice(name="Christmas", value="Christmas"),
    app_commands.Choice(name="Misc", value="Misc"),
]


class Fun(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    async def fetch(self, cat: str) -> Optional[tuple[str, Optional[str]]]:
        timeout = aiohttp.ClientTimeout(total=8)
        async with aiohttp.ClientSession(timeout=timeout, headers={"User-Agent": UA, "Accept": "application/json"}) as s:
            if cat == "dad":
                async with s.get(DADJOKE) as r:
                    data = await r.json(content_type=None)
                    return (data["joke"], None) if data.get("joke") else None
            async with s.get(JOKEAPI.format(cat=cat)) as r:
                data = await r.json(content_type=None)
                if data.get("error"):
                    return None
                if data.get("type") == "twopart":
                    return data["setup"], data["delivery"]
                return data.get("joke"), None

    @app_commands.command(name="joke", description="Tell a joke.")
    @app_commands.choices(kind=CATEGORIES)
    @app_commands.checks.cooldown(1, 5)
    async def joke(self, interaction: discord.Interaction, kind: Optional[app_commands.Choice[str]] = None):
        cat = kind.value if kind else "Any"
        try:
            got = await self.fetch(cat)
        except Exception:
            got = None
        setup, punch = got if got and got[0] else random.choice(BACKUP)
        text = setup if not punch else f"{setup}\n\n||{punch}||"
        await interaction.response.send_message(text[:2000])


async def setup(bot: commands.Bot):
    await bot.add_cog(Fun(bot))
