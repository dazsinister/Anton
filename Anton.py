import os
import asyncio
import functools
import traceback
from datetime import datetime, timezone
from dotenv import load_dotenv
import discord
from discord.ext import commands, tasks
import scrapers.redtag as redtag
import scrapers.damages as damages

load_dotenv()

DISCORD_TOKEN = os.getenv("DISCORD_TOKEN", "").strip()
if not DISCORD_TOKEN:
    raise SystemExit("Set DISCORD_TOKEN in .env")

CHANNEL_ID_REDTAG = int(os.getenv("CHANNEL_ID_REDTAG", "0").strip() or 0)
THREAD_ID_REDTAG  = int(os.getenv("THREAD_ID_REDTAG", "0").strip() or 0)
RUN_INTERVAL_MIN_REDTAG = int(os.getenv("RUN_INTERVAL_MIN_REDTAG", "60").strip() or 60)

CHANNEL_ID_DAMAGES = int(os.getenv("CHANNEL_ID_DAMAGES", "0").strip() or 0)
THREAD_ID_DAMAGES  = int(os.getenv("THREAD_ID_DAMAGES", "0").strip() or 0)
RUN_INTERVAL_MIN_DAMAGES = int(os.getenv("RUN_INTERVAL_MIN_DAMAGES", "60").strip() or 60)

POST_DELAY     = float(os.getenv("POST_DELAY", "0.25"))
BATCH_SIZE     = int(os.getenv("BATCH_SIZE", "50"))
BATCH_COOLDOWN = float(os.getenv("BATCH_COOLDOWN", "10.0"))

FORUM_AUTOCREATE   = os.getenv("FORUM_AUTOCREATE", "false").lower() in {"1","true","yes","on"}
FORUM_THREAD_TITLE = os.getenv("FORUM_THREAD_TITLE", "IST Updates")

LOG_CHANNEL_ID = int(os.getenv("LOG_CHANNEL_ID", "0").strip() or 0)
HEALTH_MESSAGES = os.getenv("HEALTH_MESSAGES", "true").lower() in {"1","true","yes","on"}
HEALTH_SUMMARY_PREFIX = os.getenv("HEALTH_SUMMARY_PREFIX", "[Health]")

TEST_GUILD_ID = int(os.getenv("TEST_GUILD_ID", "0").strip() or 0)
MY_GUILD = discord.Object(id=TEST_GUILD_ID) if TEST_GUILD_ID else None

intents = discord.Intents.default()
bot = commands.Bot(command_prefix="!", intents=intents)

redtag_lock = asyncio.Lock()
damages_lock = asyncio.Lock()

last_stats = {
    "Red Tag": {"last_run": None, "scraped": 0, "new": 0, "changed": 0, "pruned": 0, "interval_min": RUN_INTERVAL_MIN_REDTAG},
    "Damages": {"last_run": None, "scraped": 0, "new": 0, "changed": 0, "pruned": 0, "interval_min": RUN_INTERVAL_MIN_DAMAGES},
}

async def post_items(channel_like, render_fn, items: list[dict], label: str | None = None):
    """Post items with batching."""
    if not items:
        return
    total = len(items)
    batch_count = 0
    for idx, it in enumerate(items, start=1):
        body = render_fn(it)
        msg = (f"**[{label}]**\n{body}") if label else body
        if len(msg) > 1900:
            msg = msg[:1900] + "…"
        await channel_like.send(msg)
        await asyncio.sleep(POST_DELAY)
        batch_count += 1
        if BATCH_SIZE and batch_count == BATCH_SIZE and idx < total:
            await asyncio.sleep(BATCH_COOLDOWN)
            batch_count = 0

async def call_blocking(func, *args, timeout: float = 300.0, **kwargs):
    """Run a blocking function in a thread with a hard timeout."""
    bound = functools.partial(func, *args, **kwargs)
    return await asyncio.wait_for(asyncio.to_thread(bound), timeout=timeout)

def fmt_ts(dt: datetime | None) -> str:
    """Discord-friendly absolute + relative timestamp."""
    if not dt:
        return "—"
    epoch = int(dt.replace(tzinfo=timezone.utc).timestamp())
    return f"<t:{epoch}:f> (<t:{epoch}:R>)"

def get_next_run_dt(label: str) -> datetime | None:
    """Return the next scheduled datetime for the loop (UTC)."""
    if label == "Red Tag":
        return getattr(auto_redtag, "next_iteration", None)
    if label == "Damages":
        return getattr(auto_damages, "next_iteration", None)
    return None

def fmt_next_run(label: str) -> str:
    nxt = get_next_run_dt(label)
    return fmt_ts(nxt) if nxt else "—"

async def get_log_target(fallback):
    """Return the log channel if configured, else fallback."""
    if LOG_CHANNEL_ID:
        try:
            ch = bot.get_channel(LOG_CHANNEL_ID) or await bot.fetch_channel(LOG_CHANNEL_ID)
            if hasattr(ch, "send"):
                return ch
        except Exception as e:
            print(f"LOG_CHANNEL_ID fetch failed: {e}")
    return fallback

async def send_health(label: str, channel_like, scraped: int, new: int, changed: int, pruned: int):
    """Send a health summary as an embed (to LOG_CHANNEL_ID if set, else the same channel)."""
    if not HEALTH_MESSAGES:
        return
    target = await get_log_target(channel_like)

    now_utc = datetime.now(timezone.utc)
    last = last_stats[label]["last_run"] or now_utc
    interval_min = last_stats[label]["interval_min"]
    next_str = fmt_next_run(label)

    emb = discord.Embed(
        title=f"{HEALTH_SUMMARY_PREFIX} {label} run finished".strip(),
        timestamp=now_utc,
        color=0x43B581
    )
    emb.add_field(name="Scraped", value=str(scraped), inline=True)
    emb.add_field(name="New", value=str(new), inline=True)
    emb.add_field(name="Changed", value=str(changed), inline=True)
    emb.add_field(name="Pruned", value=str(pruned), inline=True)
    emb.add_field(name="Interval", value=f"{interval_min} min", inline=True)
    emb.add_field(name="Next run", value=next_str, inline=True)
    emb.add_field(name="Last run", value=fmt_ts(last), inline=False)

    try:
        await target.send(embed=emb)
    except Exception:
        traceback.print_exc()

async def resolve_channel_like(target_channel_id: int, target_thread_id: int):
    """
    Return an object we can .send() to for the given IDs:
      - If thread_id is set: use that thread
      - Else resolve channel_id:
          * TextChannel -> OK
          * ForumChannel -> if FORUM_AUTOCREATE, create a thread
    """
    if target_thread_id:
        try:
            th = await bot.fetch_channel(target_thread_id)
            if hasattr(th, "send"):
                return th
            print(f"THREAD_ID {target_thread_id} not sendable (type={type(th)})")
        except Exception as e:
            print(f"fetch_channel THREAD_ID={target_thread_id} failed: {e}")

    ch = bot.get_channel(target_channel_id) if target_channel_id else None
    if ch:
        if hasattr(ch, "send"):
            return ch
        if isinstance(ch, discord.ForumChannel) and FORUM_AUTOCREATE:
            try:
                thread = await ch.create_thread(name=FORUM_THREAD_TITLE)
                print(f"Created forum thread: {thread} (id={thread.id}) for channel {target_channel_id}")
                return thread
            except Exception as e:
                print(f"Failed to create forum thread in {ch}: {e}")
        print(f"CHANNEL_ID {target_channel_id} not sendable (type={type(ch)})")
        return None

    if target_channel_id:
        try:
            ch = await bot.fetch_channel(target_channel_id)
            if hasattr(ch, "send"):
                return ch
            if isinstance(ch, discord.ForumChannel) and FORUM_AUTOCREATE:
                try:
                    thread = await ch.create_thread(name=FORUM_THREAD_TITLE)
                    print(f"Created forum thread: {thread} (id={thread.id}) for channel {target_channel_id}")
                    return thread
                except Exception as e:
                    print(f"Failed to create forum thread in {ch}: {e}")
            print(f"Resolved CHANNEL_ID {target_channel_id} not sendable (type={type(ch)})")
            return None
        except discord.Forbidden:
            print(f"Forbidden: bot lacks permission to view channel {target_channel_id}")
        except discord.HTTPException as e:
            print(f"fetch_channel failed for {target_channel_id}: {e}")

    print("No channel/thread resolved for given IDs.")
    return None

async def print_startup_diagnostics():
    print("Guilds I’m in:")
    for g in bot.guilds:
        print(f"- {g.name} ({g.id})")
    # Red Tag
    if CHANNEL_ID_REDTAG:
        try:
            ch = await bot.fetch_channel(CHANNEL_ID_REDTAG)
            print(f"REDTAG CHANNEL_ID -> {getattr(ch, 'name', ch)} | type={type(ch)} | guild={getattr(ch, 'guild', None)}")
        except Exception as e:
            print(f"Immediate fetch failed for REDTAG channel {CHANNEL_ID_REDTAG}: {e}")
    if THREAD_ID_REDTAG:
        try:
            th = await bot.fetch_channel(THREAD_ID_REDTAG)
            print(f"REDTAG THREAD_ID -> {getattr(th, 'name', th)} | type={type(th)} | parent={getattr(th, 'parent', None)}")
        except Exception as e:
            print(f"Immediate fetch failed for REDTAG thread {THREAD_ID_REDTAG}: {e}")

    if CHANNEL_ID_DAMAGES:
        try:
            ch = await bot.fetch_channel(CHANNEL_ID_DAMAGES)
            print(f"DAMAGES CHANNEL_ID -> {getattr(ch, 'name', ch)} | type={type(ch)} | guild={getattr(ch, 'guild', None)}")
        except Exception as e:
            print(f"Immediate fetch failed for DAMAGES channel {CHANNEL_ID_DAMAGES}: {e}")
    if THREAD_ID_DAMAGES:
        try:
            th = await bot.fetch_channel(THREAD_ID_DAMAGES)
            print(f"DAMAGES THREAD_ID -> {getattr(th, 'name', th)} | type={type(th)} | parent={getattr(th, 'parent', None)}")
        except Exception as e:
            print(f"Immediate fetch failed for DAMAGES thread {THREAD_ID_DAMAGES}: {e}")

    if LOG_CHANNEL_ID:
        try:
            ch = await bot.fetch_channel(LOG_CHANNEL_ID)
            print(f"LOG CHANNEL_ID -> {getattr(ch, 'name', ch)} | type={type(ch)} | guild={getattr(ch, 'guild', None)}")
        except Exception as e:
            print(f"Immediate fetch failed for LOG channel {LOG_CHANNEL_ID}: {e}")

async def run_scraper_once(module, channel_like, label: str, lock: asyncio.Lock):
    """Run one scraper module safely (off the loop, with lock & timeout)."""
    async with lock:
        try:
            new_items, changed_items, pruned, total = await call_blocking(
                module.get_updates, timeout=300.0
            )
        except asyncio.TimeoutError:
            print(f"[{label}] ERROR: get_updates() timed out; skipping this tick.")
            return
        except Exception:
            print(f"[{label}] ERROR during get_updates():")
            traceback.print_exc()
            return

    await post_items(channel_like, module.render_item, new_items, label=label)
    await post_items(channel_like, module.render_item, changed_items, label=label)

    last_stats[label].update({
        "last_run": datetime.now(timezone.utc),
        "scraped": total,
        "new": len(new_items),
        "changed": len(changed_items),
        "pruned": pruned,
    })
    print(f"[{label}] scraped={total} new={len(new_items)} changed={len(changed_items)} pruned={pruned}")
    await send_health(label, channel_like, total, len(new_items), len(changed_items), pruned)

@bot.event
async def on_ready():
    print(f"Logged in as {bot.user} (ID: {bot.user.id})")
    await print_startup_diagnostics()

    try:
        await bot.load_extension("cogs.poll")
        print("Loaded cog: cogs.poll")
    except Exception:
        print("Failed to load cogs.poll:")
        traceback.print_exc()

    if MY_GUILD is not None:
        try:
            await bot.tree.sync(guild=MY_GUILD)
            print(f"Slash commands synced to guild {TEST_GUILD_ID} (fast).")
        except Exception:
            print("Guild slash sync failed:")
            traceback.print_exc()
    else:
        try:
            await bot.tree.sync()
            print("Global slash commands synced.")
        except Exception:
            print("Global slash sync failed:")
            traceback.print_exc()

    if not auto_redtag.is_running():
        auto_redtag.start()
    if not auto_damages.is_running():
        auto_damages.start()

    await asyncio.sleep(0.2)

@bot.event
async def on_disconnect():
    print("[EVENT] Disconnected from gateway.")

@bot.event
async def on_error(event, *args, **kwargs):
    print(f"[ERROR] in event '{event}':")
    traceback.print_exc()

@bot.tree.command(name="redtag", description="Run the Red Tag scrape now (posts to its channel).")
async def redtag_cmd(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    target = await resolve_channel_like(CHANNEL_ID_REDTAG, THREAD_ID_REDTAG) or interaction.channel
    if not target or not hasattr(target, "send"):
        return await interaction.followup.send("No sendable channel for Red Tag.", ephemeral=True)
    await run_scraper_once(redtag, target, "Red Tag", lock=redtag_lock)
    await interaction.followup.send("Red Tag done.", ephemeral=True)

@bot.tree.command(name="damages", description="Run the Damages scrape now (posts to its channel).")
async def damages_cmd(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    target = await resolve_channel_like(CHANNEL_ID_DAMAGES, THREAD_ID_DAMAGES) or interaction.channel
    if not target or not hasattr(target, "send"):
        return await interaction.followup.send("No sendable channel for Damages.", ephemeral=True)
    await run_scraper_once(damages, target, "Damages", lock=damages_lock)
    await interaction.followup.send("Damages done.", ephemeral=True)

@bot.tree.command(name="stats", description="Show last run stats and current intervals.")
async def stats_cmd(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)

    def build_embed(lbl: str) -> discord.Embed:
        s = last_stats[lbl]
        e = discord.Embed(title=f"Stats — {lbl}", color=0x5865F2, timestamp=datetime.now(timezone.utc))
        e.add_field(name="Last run", value=fmt_ts(s["last_run"]), inline=False)
        e.add_field(name="Next run", value=fmt_next_run(lbl), inline=False)
        e.add_field(name="Scraped", value=str(s["scraped"]), inline=True)
        e.add_field(name="New", value=str(s["new"]), inline=True)
        e.add_field(name="Changed", value=str(s["changed"]), inline=True)
        e.add_field(name="Pruned", value=str(s["pruned"]), inline=True)
        e.add_field(name="Interval", value=f"{s['interval_min']} min", inline=True)
        return e

    await interaction.followup.send(embed=build_embed("Red Tag"), ephemeral=True)
    await interaction.followup.send(embed=build_embed("Damages"), ephemeral=True)

@bot.tree.command(name="interval", description="Change run interval (minutes) for a feed.")
async def interval_cmd(
    interaction: discord.Interaction,
    feed: str,
    minutes: int
):
    """
    /interval feed:<redtag|damages> minutes:<1..10080>
    """
    await interaction.response.defer(ephemeral=True)
    feed_l = feed.strip().lower()
    if minutes < 1 or minutes > 10080:
        return await interaction.followup.send("Minutes must be between 1 and 10080.", ephemeral=True)

    try:
        if feed_l in ("redtag", "red", "rt"):
            auto_redtag.change_interval(minutes=minutes)
            last_stats["Red Tag"]["interval_min"] = minutes
            await interaction.followup.send(f"Set **Red Tag** interval to **{minutes}** minutes.", ephemeral=True)
        elif feed_l in ("damages", "damage", "dm"):
            auto_damages.change_interval(minutes=minutes)
            last_stats["Damages"]["interval_min"] = minutes
            await interaction.followup.send(f"Set **Damages** interval to **{minutes}** minutes.", ephemeral=True)
        else:
            await interaction.followup.send("Unknown feed. Use `redtag` or `damages`.", ephemeral=True)
    except Exception:
        traceback.print_exc()
        await interaction.followup.send("Failed to update interval due to an internal error.", ephemeral=True)

@tasks.loop(minutes=RUN_INTERVAL_MIN_REDTAG)
async def auto_redtag():
    target = await resolve_channel_like(CHANNEL_ID_REDTAG, THREAD_ID_REDTAG)
    if target is None or not hasattr(target, "send"):
        print("auto_redtag: channel not sendable; will retry next tick.")
        return
    await run_scraper_once(redtag, target, "Red Tag", lock=redtag_lock)

@tasks.loop(minutes=RUN_INTERVAL_MIN_DAMAGES)
async def auto_damages():
    target = await resolve_channel_like(CHANNEL_ID_DAMAGES, THREAD_ID_DAMAGES)
    if target is None or not hasattr(target, "send"):
        print("auto_damages: channel not sendable; will retry next tick.")
        return
    await run_scraper_once(damages, target, "Damages", lock=damages_lock)

@auto_redtag.before_loop
async def _stagger_redtag():
    await asyncio.sleep(2)

@auto_damages.before_loop
async def _stagger_damages():
    await asyncio.sleep(5)

if __name__ == "__main__":
    print(
        f"RedTag:   channel={CHANNEL_ID_REDTAG} thread={THREAD_ID_REDTAG} interval={RUN_INTERVAL_MIN_REDTAG}min\n"
        f"Damages:  channel={CHANNEL_ID_DAMAGES} thread={THREAD_ID_DAMAGES} interval={RUN_INTERVAL_MIN_DAMAGES}min\n"
        f"Pacing:   delay={POST_DELAY}s batch={BATCH_SIZE} cool={BATCH_COOLDOWN}s\n"
        f"Forum:    autocreate={FORUM_AUTOCREATE} title='{FORUM_THREAD_TITLE}'\n"
        f"Health:   enabled={HEALTH_MESSAGES} log_channel={LOG_CHANNEL_ID or 'same as feed'}"
    )
    bot.run(DISCORD_TOKEN)
