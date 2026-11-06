"""/price, /history and /watch: look things up in Anton's database."""
from __future__ import annotations

import asyncio
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands

from core import db, feeds, render
from core.db import cents_to_str

SPARK = "▁▂▃▄▅▆▇█"


def _choice_label(it: dict) -> str:
    price = "FREE" if it.get("price_cents") == 0 else (cents_to_str(it.get("price_cents")) or "no price")
    tail = f" · {feeds.label(it['feed'])} · {price}" + ("" if it["active"] else " · gone")
    return it["title"][: 100 - len(tail)] + tail


def _choice_value(it: dict) -> str:
    v = f"{it['feed']}|{it['key']}"
    return v if len(v) <= 100 else it["title"][:100]


async def title_autocomplete(_: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
    if len(current.strip()) < 2:
        return []
    items = await asyncio.to_thread(db.search_items, current, 25)
    return [app_commands.Choice(name=_choice_label(it), value=_choice_value(it)) for it in items]


async def find(query: str) -> tuple[Optional[dict], list[dict]]:
    """Resolve an autocomplete pick or free text to (best match, other matches)."""
    if "|" in query:
        feed_key, key = query.split("|", 1)
        it = await asyncio.to_thread(db.get_item, feed_key, key)
        if it:
            others = await asyncio.to_thread(db.search_items, it["title"], 6)
            return it, [o for o in others if (o["feed"], o["key"]) != (it["feed"], it["key"])]
    results = await asyncio.to_thread(db.search_items, query, 6)
    return (results[0], results[1:]) if results else (None, [])


def _status_line(it: dict) -> str:
    if not it["active"]:
        return f"No longer listed (last seen {render.ts(it['last_seen'], 'R')})"
    if it.get("available") is False:
        return f"Listed but **sold out** (checked {render.ts(it['last_seen'], 'R')})"
    return f"Listed (checked {render.ts(it['last_seen'], 'R')})"


def _other_lines(others: list[dict]) -> str:
    lines = []
    for o in others[:5]:
        price = "FREE" if o.get("price_cents") == 0 else (cents_to_str(o.get("price_cents")) or "—")
        lines.append(f"• [{o['title'][:70]}]({o['url']}) — {price} · {feeds.label(o['feed'])}"
                     + ("" if o["active"] else " (gone)"))
    return "\n".join(lines)[:1024]


def sparkline(values: list[int]) -> str:
    if len(values) < 2:
        return ""
    lo, hi = min(values), max(values)
    if lo == hi:
        return SPARK[3] * len(values)
    return "".join(SPARK[round((v - lo) / (hi - lo) * (len(SPARK) - 1))] for v in values)


class Lookup(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @app_commands.command(name="price", description="Look up a title's current price in Anton's database.")
    @app_commands.describe(title="Start typing a title, e.g. Batman by James Tynion vol 2")
    @app_commands.autocomplete(title=title_autocomplete)
    async def price(self, interaction: discord.Interaction, title: str):
        await interaction.response.defer()
        it, others = await find(title)
        if not it:
            return await interaction.followup.send(
                f"I couldn't find anything matching **{title[:100]}**. I only know titles from the pages I watch "
                f"({', '.join(f.label for f in feeds.FEEDS if f.key != 'freegames')}).")
        emb = render.item_embed(it, it["feed"], headline=_status_line(it))
        hist = await asyncio.to_thread(db.price_history, it["feed"], it["key"])
        prices = [h["price_cents"] for h in hist if h["price_cents"] is not None]
        if len(prices) > 1:
            emb.add_field(name="Lowest seen", value=cents_to_str(min(prices)), inline=True)
        if others:
            emb.add_field(name="Other matches", value=_other_lines(others), inline=False)
        await interaction.followup.send(embed=emb)

    @app_commands.command(name="history", description="See how a title's price has changed over time.")
    @app_commands.describe(title="Start typing a title")
    @app_commands.autocomplete(title=title_autocomplete)
    async def history(self, interaction: discord.Interaction, title: str):
        await interaction.response.defer()
        it, _ = await find(title)
        if not it:
            return await interaction.followup.send(f"I couldn't find anything matching **{title[:100]}**.")
        hist = await asyncio.to_thread(db.price_history, it["feed"], it["key"])
        hist = [h for h in hist if h["price_cents"] is not None]
        emb = discord.Embed(title=it["title"][:256], url=it.get("url"), color=0x5865F2)
        if it.get("image"):
            emb.set_thumbnail(url=it["image"])
        if not hist:
            emb.description = "No prices recorded for this one yet."
        else:
            prices = [h["price_cents"] for h in hist]
            emb.description = (f"`{sparkline(prices)}`\n" if len(prices) > 1 else "") + \
                f"Tracking since {render.ts(it['first_seen'], 'D')} · {_status_line(it)}"
            emb.add_field(name="Now", value=cents_to_str(prices[-1]), inline=True)
            emb.add_field(name="Lowest", value=cents_to_str(min(prices)), inline=True)
            emb.add_field(name="Highest", value=cents_to_str(max(prices)), inline=True)
            lines, prev = [], None
            for h in hist[-15:]:
                arrow = "" if prev is None else (" 📉" if h["price_cents"] < prev else " 📈" if h["price_cents"] > prev else "")
                lines.append(f"{render.ts(h['recorded_at'], 'd')} — {cents_to_str(h['price_cents'])}{arrow}")
                prev = h["price_cents"]
            emb.add_field(name=f"Changes ({len(hist)} total)", value="\n".join(lines)[:1024], inline=False)
        emb.set_footer(text=feeds.label(it["feed"]))
        await interaction.followup.send(embed=emb)

    # ── watchlists ──────────────────────────────────────────

    watch = app_commands.Group(name="watch", description="Get a DM when something you want shows up or drops in price.")

    @watch.command(name="add", description="Watch for a title or keyword.")
    @app_commands.describe(keyword="Words that must all be in the title, e.g. saga compendium",
                           max_price="Only tell me when it's at or under this price (optional), e.g. 25")
    async def watch_add(self, interaction: discord.Interaction, keyword: app_commands.Range[str, 2, 80],
                        max_price: Optional[app_commands.Range[float, 0, 10000]] = None):
        if len(await asyncio.to_thread(db.list_watches, interaction.user.id)) >= 25:
            return await interaction.response.send_message("You already have 25 watches. Remove one first.", ephemeral=True)
        cap = int(round(max_price * 100)) if max_price is not None else None
        added = await asyncio.to_thread(db.add_watch, interaction.user.id, interaction.guild_id,
                                        interaction.channel_id, keyword, cap)
        cap_txt = f" at **{cents_to_str(cap)}** or less" if cap is not None else ""
        msg = (f"👀 Watching **{keyword}**{cap_txt}." if added else f"Updated your watch on **{keyword}**{cap_txt}.") + \
            " I'll DM you when a match is new, back, cheaper, or restocked."
        now = [it for it in await asyncio.to_thread(db.search_items, keyword, 25, True)
               if db.watch_matches(keyword, it["title"])
               and (cap is None or (it.get("price_cents") is not None and it["price_cents"] <= cap))]
        if now:
            msg += "\n\n**Already listed right now:**\n" + _other_lines(now)
        await interaction.response.send_message(msg[:2000], ephemeral=True)

    @watch.command(name="list", description="Show what you're watching.")
    async def watch_list(self, interaction: discord.Interaction):
        ws = await asyncio.to_thread(db.list_watches, interaction.user.id)
        if not ws:
            return await interaction.response.send_message("You're not watching anything. Try `/watch add`.", ephemeral=True)
        lines = [f"• **{w['keyword']}**" + (f" (≤ {cents_to_str(w['max_price_cents'])})" if w["max_price_cents"] is not None else "")
                 for w in ws]
        await interaction.response.send_message("Your watches:\n" + "\n".join(lines), ephemeral=True)

    async def _my_watches(self, interaction: discord.Interaction, current: str):
        ws = await asyncio.to_thread(db.list_watches, interaction.user.id)
        return [app_commands.Choice(name=w["keyword"], value=w["keyword"])
                for w in ws if current.lower() in w["keyword"].lower()][:25]

    @watch.command(name="remove", description="Stop watching something.")
    async def watch_remove(self, interaction: discord.Interaction, keyword: str):
        ok = await asyncio.to_thread(db.remove_watch, interaction.user.id, keyword)
        await interaction.response.send_message(
            f"Stopped watching **{keyword}**." if ok else f"You weren't watching **{keyword}**.", ephemeral=True)

    @watch_remove.autocomplete("keyword")
    async def _watch_remove_ac(self, interaction: discord.Interaction, current: str):
        return await self._my_watches(interaction, current)


async def setup(bot: commands.Bot):
    await bot.add_cog(Lookup(bot))
