"""Discord embeds for feed posts, health summaries and lookups."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

import discord

from core import feeds
from core.db import Event, ScrapeResult, cents_to_str

EVENT_STYLE = {
    "new":         ("🆕", "New listing", None),
    "back":        ("🔁", "Back on the list", None),
    "price_drop":  ("📉", "Price drop", 0x2F9E44),
    "price_up":    ("📈", "Price went up", 0xE8590C),
    "msrp_change": ("🏷️", "MSRP changed", None),
    "restock":     ("✅", "Back in stock", 0x2F9E44),
    "oos":         ("❌", "Sold out", 0x868E96),
    "removed":     ("🗑️", "Removed from listing", 0x868E96),
    "updated":     ("⏰", "Offer updated", None),
}


def ts(iso: Optional[str], style: str = "f") -> str:
    """Discord timestamp that shows in each reader's own time zone."""
    if not iso:
        return "—"
    try:
        dt = datetime.fromisoformat(iso)
    except ValueError:
        return iso
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return f"<t:{int(dt.timestamp())}:{style}>"


def pct_off(price: Optional[int], msrp: Optional[int]) -> Optional[str]:
    if price is None or not msrp or price >= msrp:
        return None
    return f"{round((1 - price / msrp) * 100)}% off"


def _cut(s: str, n: int) -> str:
    return s if len(s) <= n else s[: n - 1] + "…"


def item_embed(it: dict, feed_key: str, headline: str = "", color: Optional[int] = None) -> discord.Embed:
    feed = feeds.get(feed_key)
    emb = discord.Embed(
        title=_cut(it.get("title") or "Unknown", 256),
        url=it.get("url") or None,
        description=headline or None,
        color=color if color is not None else (feed.color if feed else 0x5865F2),
    )
    price, msrp = it.get("price_cents"), it.get("msrp_cents")
    if price == 0:
        emb.add_field(name="Price", value="**FREE**", inline=True)
    elif price is not None:
        emb.add_field(name="Price", value=f"**{cents_to_str(price)}**", inline=True)
    if msrp:
        emb.add_field(name="MSRP" if price else "Worth", value=cents_to_str(msrp), inline=True)
    off = it.get("discount") or pct_off(price, msrp)
    if off and price != 0:
        emb.add_field(name="Discount", value=off, inline=True)
    extra = it.get("extra") or {}
    if extra.get("platform"):
        emb.add_field(name="Platform", value=extra["platform"], inline=True)
    if extra.get("end_date"):
        emb.add_field(name="Ends", value=f"{ts(extra['end_date'])} ({ts(extra['end_date'], 'R')})", inline=True)
    if it.get("image"):
        emb.set_thumbnail(url=it["image"])
    footer = feed.label if feed else feed_key
    if feed and feed.store and feed.store != feed.label:
        footer += f" · {feed.store}"
    emb.set_footer(text=footer)
    return emb


def event_embed(ev: Event, feed_key: str) -> discord.Embed:
    emoji, label, color = EVENT_STYLE.get(ev.kind, ("", ev.kind, None))
    it = ev.item
    detail = ""
    if ev.kind in ("price_drop", "price_up") and ev.old_price is not None:
        detail = f" — {cents_to_str(ev.old_price)} → **{cents_to_str(it.get('price_cents'))}**"
    elif ev.kind == "msrp_change" and ev.old_msrp is not None:
        detail = f" — {cents_to_str(ev.old_msrp)} → {cents_to_str(it.get('msrp_cents'))}"
    elif ev.kind == "updated" and ev.old_extra.get("end_date"):
        detail = f" — now ends {ts((it.get('extra') or {}).get('end_date'), 'R')}"
    elif ev.kind == "new" and feed_key == "freegames":
        emoji, label = "🎮", "Free to keep"
    return item_embed(it, feed_key, headline=f"{emoji} **{label}**{detail}", color=color)


def health_embed(feed_key: str, res: Optional[ScrapeResult], duration_s: float,
                 error: Optional[str], next_run: Optional[datetime]) -> discord.Embed:
    label = feeds.label(feed_key)
    if error:
        emb = discord.Embed(title=f"⚠️ {label} run failed", description=_cut(error, 1000), color=0xE03131)
    else:
        assert res is not None
        title = f"{label} run finished"
        color = 0x43B581
        desc = None
        if res.seeded_now:
            desc = f"First run: stored {res.scraped} listings quietly. Changes get posted from now on."
        elif res.removal_skipped:
            color, desc = 0xF59F00, "Scrape came back much smaller than usual, so nothing was marked removed."
        elif res.scraped == 0:
            color, desc = 0xF59F00, "Found 0 listings. The page may have moved. Check the URL in .env."
        emb = discord.Embed(title=title, description=desc, color=color)
        emb.add_field(name="Scraped", value=str(res.scraped))
        emb.add_field(name="New", value=str(res.count("new", "back")))
        emb.add_field(name="Price changes", value=str(res.count("price_drop", "price_up", "msrp_change")))
        emb.add_field(name="Stock changes", value=str(res.count("restock", "oos")))
        emb.add_field(name="Removed", value=str(res.count("removed")))
        emb.add_field(name="Took", value=f"{duration_s:.0f}s")
    if next_run:
        emb.add_field(name="Next run", value=ts(next_run.isoformat(), "R"), inline=False)
    emb.timestamp = datetime.now(timezone.utc)
    return emb
