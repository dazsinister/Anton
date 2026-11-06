"""Runs the scrapers on a schedule, posts what changed, pings watchers, and the
/feed (admin) and /status commands."""
from __future__ import annotations

import asyncio
import time
import traceback
from datetime import datetime, timedelta, timezone
from typing import Optional, Union

import discord
from discord import app_commands
from discord.ext import commands, tasks

from core import config, db, feeds, render
from core.feeds import Feed

FEED_CHOICES = [app_commands.Choice(name=f.label, value=f.key) for f in feeds.FEEDS]
WATCH_KINDS = {"new", "back", "price_drop", "restock"}
Sendable = Union[discord.TextChannel, discord.Thread]


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Feeds(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.locks = {f.key: asyncio.Lock() for f in feeds.FEEDS}
        start = utcnow()
        # Stagger the first runs so the bot doesn't hit every site at once on startup
        self.next_run: dict[str, datetime] = {
            f.key: start + timedelta(seconds=20 + i * 20) for i, f in enumerate(feeds.FEEDS)
        }
        self.scheduler.start()

    def cog_unload(self):
        self.scheduler.cancel()

    # ── scheduling ──────────────────────────────────────────

    @tasks.loop(seconds=30)
    async def scheduler(self):
        now = utcnow()
        for feed in feeds.FEEDS:
            if not feed.enabled or self.locks[feed.key].locked():
                continue
            if now >= self.next_run[feed.key]:
                asyncio.create_task(self.run_feed(feed))

    @scheduler.before_loop
    async def _before(self):
        await self.bot.wait_until_ready()

    # ── channels ────────────────────────────────────────────

    async def _fetch(self, cid: int):
        ch = self.bot.get_channel(cid)
        if ch is None:
            try:
                ch = await self.bot.fetch_channel(cid)
            except (discord.NotFound, discord.Forbidden, discord.HTTPException) as e:
                print(f"Can't open channel {cid}: {e}")
                return None
        return ch

    async def resolve_target(self, feed: Feed) -> Optional[discord.abc.Messageable]:
        if feed.thread_id:
            th = await self._fetch(feed.thread_id)
            if isinstance(th, discord.Thread):
                if th.archived:
                    try:
                        await th.edit(archived=False)
                    except discord.HTTPException:
                        pass
                return th
        if not feed.channel_id:
            return None
        ch = await self._fetch(feed.channel_id)
        if isinstance(ch, discord.ForumChannel):
            if not config.FORUM_AUTOCREATE:
                print(f"[{feed.label}] channel is a Forum; set FORUM_AUTOCREATE=true or pick a thread")
                return None
            # Reuse the thread we made last time instead of starting a new one every run
            saved = db.get_setting(f"feed:{feed.key}:forum_thread")
            if saved:
                th = await self._fetch(int(saved))
                if isinstance(th, discord.Thread):
                    return th
            created = await ch.create_thread(name=f"{config.FORUM_THREAD_TITLE} — {feed.label}",
                                             content=f"{feed.label} updates from Anton")
            db.set_setting(f"feed:{feed.key}:forum_thread", str(created.thread.id))
            return created.thread
        return ch if isinstance(ch, discord.abc.Messageable) else None

    # ── a single run ────────────────────────────────────────

    async def run_feed(self, feed: Feed, target_override: Optional[discord.abc.Messageable] = None) -> Optional[db.ScrapeResult]:
        lock = self.locks[feed.key]
        if lock.locked():
            return None
        async with lock:
            t0 = time.monotonic()
            res: Optional[db.ScrapeResult] = None
            error: Optional[str] = None
            try:
                res = await asyncio.wait_for(
                    asyncio.to_thread(lambda: db.apply_scrape(feed.key, feed.scrape(), feed.post_removed)),
                    timeout=config.SCRAPE_MAX_RUN_SECONDS + 120,
                )
            except asyncio.TimeoutError:
                error = "Timed out."
            except Exception as e:
                error = f"{type(e).__name__}: {e}"
                traceback.print_exc()
            duration = time.monotonic() - t0
            self.next_run[feed.key] = utcnow() + timedelta(minutes=feed.interval_min)
            await asyncio.to_thread(db.record_run, feed.key, res, duration, error)

            if res:
                print(f"[{feed.label}] scraped={res.scraped} events={len(res.events)}"
                      f"{' (first run, stored quietly)' if res.seeded_now else ''}")
                target = target_override or await self.resolve_target(feed)
                to_post = [e for e in res.events if feed.post_new or e.kind not in ("new", "back")]
                if target and to_post:
                    await self.post_events(target, feed, to_post)
                await self.notify_watchers(feed, res.events)
            else:
                print(f"[{feed.label}] ERROR: {error}")
            await self.send_health(feed, res, duration, error)
            return res

    async def post_events(self, target: discord.abc.Messageable, feed: Feed, events: list[db.Event]):
        shown = events[: config.MAX_POSTS_PER_RUN]
        embeds = [render.event_embed(e, feed.key) for e in shown]
        for i in range(0, len(embeds), 10):  # Discord allows 10 embeds per message
            try:
                await target.send(embeds=embeds[i:i + 10])
            except discord.HTTPException as e:
                print(f"[{feed.label}] post failed: {e}")
                return
            await asyncio.sleep(config.POST_DELAY)
        extra = len(events) - len(shown)
        if extra > 0:
            await target.send(f"…plus **{extra}** more {feed.label} updates. Use `/price` to look anything up.")

    async def notify_watchers(self, feed: Feed, events: list[db.Event]):
        watches = await asyncio.to_thread(db.list_watches)
        if not watches:
            return
        for w in watches:
            hits = []
            for e in events:
                if e.kind not in WATCH_KINDS or not db.watch_matches(w["keyword"], e.item.get("title", "")):
                    continue
                price = e.item.get("price_cents")
                if w["max_price_cents"] is not None and (price is None or price > w["max_price_cents"]):
                    continue
                hits.append(e)
            if not hits:
                continue
            embeds = [render.event_embed(e, feed.key) for e in hits[:10]]
            msg = f"👀 Your watch **{w['keyword']}** matched {len(hits)} {feed.label} update{'s' if len(hits) > 1 else ''}:"
            try:
                user = self.bot.get_user(w["user_id"]) or await self.bot.fetch_user(w["user_id"])
                await user.send(msg, embeds=embeds)
            except (discord.Forbidden, discord.HTTPException):
                # DMs closed: ping them where they created the watch instead
                ch = await self._fetch(w["channel_id"]) if w["channel_id"] else None
                if isinstance(ch, discord.abc.Messageable):
                    try:
                        await ch.send(f"<@{w['user_id']}> {msg}", embeds=embeds,
                                      allowed_mentions=discord.AllowedMentions(users=True))
                    except discord.HTTPException:
                        pass

    async def send_health(self, feed: Feed, res, duration: float, error: Optional[str]):
        if not (config.HEALTH_MESSAGES and config.LOG_CHANNEL_ID):
            return
        ch = await self._fetch(config.LOG_CHANNEL_ID)
        if isinstance(ch, discord.abc.Messageable):
            try:
                await ch.send(embed=render.health_embed(feed.key, res, duration, error, self.next_run.get(feed.key)))
            except discord.HTTPException:
                pass

    # ── /status (everyone) ──────────────────────────────────

    @app_commands.command(name="status", description="How each of Anton's feeds is doing.")
    async def status(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        counts = await asyncio.to_thread(db.feed_item_counts)
        emb = discord.Embed(title="Anton status", color=0x5865F2, timestamp=utcnow())
        for feed in feeds.FEEDS:
            run = await asyncio.to_thread(db.last_run, feed.key)
            active, _total = counts.get(feed.key, (0, 0))
            if not feed.enabled:
                icon, lines = "⚪", ["Off"]
            else:
                where = f"<#{feed.thread_id or feed.channel_id}>" if (feed.thread_id or feed.channel_id) else "no channel (watchlists only)"
                lines = [f"Posts to {where}", f"Every {feed.interval_min} min"]
                if self.locks[feed.key].locked():
                    lines.append("Running now…")
                else:
                    lines.append(f"Next run {render.ts(self.next_run[feed.key].isoformat(), 'R')}")
                icon = "🟢"
            if run:
                if run["error"]:
                    icon = "🔴"
                    lines.append(f"Last run failed {render.ts(run['finished_at'], 'R')}")
                else:
                    lines.append(f"Last run {render.ts(run['finished_at'], 'R')}: {run['scraped']} found")
                    if run["scraped"] == 0 and feed.enabled:
                        icon = "🟠"
                        lines.append("Found nothing: check the URL")
            lines.append(f"{active} listings tracked")
            emb.add_field(name=f"{icon} {feed.label}", value="\n".join(lines), inline=True)
        await interaction.followup.send(embed=emb, ephemeral=True)

    # ── /feed (admins) ──────────────────────────────────────

    feed_group = app_commands.Group(
        name="feed", description="Manage Anton's feeds (admins).",
        default_permissions=discord.Permissions(manage_guild=True), guild_only=True,
    )

    @feed_group.command(name="run", description="Run a feed right now.")
    @app_commands.choices(feed=FEED_CHOICES)
    async def feed_run(self, interaction: discord.Interaction, feed: app_commands.Choice[str]):
        f = feeds.get(feed.value)
        if self.locks[f.key].locked():
            return await interaction.response.send_message(f"{f.label} is already running.", ephemeral=True)
        await interaction.response.send_message(f"Running **{f.label}**… results will post shortly.", ephemeral=True)
        target = await self.resolve_target(f) or interaction.channel
        res = await self.run_feed(f, target_override=target)
        if res is None:
            msg = f"**{f.label}** failed. Check the log channel or console."
        elif res.seeded_now:
            msg = f"**{f.label}**: first run, stored {res.scraped} listings quietly. Changes post from now on."
        else:
            msg = f"**{f.label}** done: {res.scraped} found, {len(res.events)} changes."
        try:
            await interaction.followup.send(msg, ephemeral=True)
        except discord.HTTPException:
            pass

    @feed_group.command(name="channel", description="Choose where a feed posts.")
    @app_commands.choices(feed=FEED_CHOICES)
    async def feed_channel(self, interaction: discord.Interaction, feed: app_commands.Choice[str],
                           channel: Union[discord.TextChannel, discord.Thread, discord.ForumChannel]):
        f = feeds.get(feed.value)
        db.set_setting(f"feed:{f.key}:channel", str(channel.id))
        db.set_setting(f"feed:{f.key}:forum_thread", None)
        db.set_setting(f"feed:{f.key}:enabled", "1")
        note = ""
        if isinstance(channel, discord.ForumChannel) and not config.FORUM_AUTOCREATE:
            note = "\n⚠️ That's a Forum channel; set `FORUM_AUTOCREATE=true` in .env or pick a thread instead."
        await interaction.response.send_message(f"**{f.label}** now posts to {channel.mention} and is on.{note}", ephemeral=True)

    @feed_group.command(name="interval", description="How often a feed checks for changes.")
    @app_commands.choices(feed=FEED_CHOICES)
    @app_commands.describe(minutes="5 to 10080 (one week)")
    async def feed_interval(self, interaction: discord.Interaction, feed: app_commands.Choice[str],
                            minutes: app_commands.Range[int, 5, 10080]):
        f = feeds.get(feed.value)
        db.set_setting(f"feed:{f.key}:interval", str(minutes))
        self.next_run[f.key] = min(self.next_run[f.key], utcnow() + timedelta(minutes=minutes))
        await interaction.response.send_message(f"**{f.label}** now runs every **{minutes}** minutes.", ephemeral=True)

    @feed_group.command(name="toggle", description="Turn a feed on or off.")
    @app_commands.choices(feed=FEED_CHOICES)
    async def feed_toggle(self, interaction: discord.Interaction, feed: app_commands.Choice[str], on: bool):
        f = feeds.get(feed.value)
        db.set_setting(f"feed:{f.key}:enabled", "1" if on else "0")
        hint = "" if not on or f.channel_id or f.thread_id else \
            " It has no channel yet, so it will only feed `/price` and watchlists. Use `/feed channel` to post updates."
        await interaction.response.send_message(f"**{f.label}** is now **{'on' if on else 'off'}**.{hint}", ephemeral=True)

    @feed_group.command(name="reset", description="Clear the saved Discord settings for a feed and go back to .env.")
    @app_commands.choices(feed=FEED_CHOICES)
    async def feed_reset(self, interaction: discord.Interaction, feed: app_commands.Choice[str]):
        f = feeds.get(feed.value)
        for k in ("channel", "interval", "enabled", "forum_thread"):
            db.set_setting(f"feed:{f.key}:{k}", None)
        await interaction.response.send_message(f"**{f.label}** is back to the settings in .env.", ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(Feeds(bot))
