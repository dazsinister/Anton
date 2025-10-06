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
import scrapers.freegames as freegames
import scrapers.marvel_watch as marvel
import scrapers.dc_watch as dc  

load_dotenv()

DISCORD_TOKEN = os.getenv("DISCORD_TOKEN", "").strip()
if not DISCORD_TOKEN:
    raise SystemExit("Set DISCORD_TOKEN in .env")

CHANNEL_ID_REDTAG = int(os.getenv("CHANNEL_ID_REDTAG", "0") or 0)
THREAD_ID_REDTAG  = int(os.getenv("THREAD_ID_REDTAG", "0") or 0)
RUN_INTERVAL_MIN_REDTAG = int(os.getenv("RUN_INTERVAL_MIN_REDTAG", "60") or 60)

CHANNEL_ID_DAMAGES = int(os.getenv("CHANNEL_ID_DAMAGES", "0") or 0)
THREAD_ID_DAMAGES  = int(os.getenv("THREAD_ID_DAMAGES", "0") or 0)
RUN_INTERVAL_MIN_DAMAGES = int(os.getenv("RUN_INTERVAL_MIN_DAMAGES", "60") or 60)

CHANNEL_ID_FREEGAMES = int(os.getenv("CHANNEL_ID_FREEGAMES", "0") or 0)
THREAD_ID_FREEGAMES  = int(os.getenv("THREAD_ID_FREEGAMES", "0") or 0)
RUN_INTERVAL_MIN_FREEGAMES = int(os.getenv("RUN_INTERVAL_MIN_FREEGAMES", "60") or 60)

CHANNEL_ID_MARVEL = int(os.getenv("CHANNEL_ID_MARVEL", "0") or 0)
THREAD_ID_MARVEL  = int(os.getenv("THREAD_ID_MARVEL", "0") or 0)
RUN_INTERVAL_MIN_MARVEL = int(os.getenv("RUN_INTERVAL_MIN_MARVEL", "60") or 60)

CHANNEL_ID_DC = int(os.getenv("CHANNEL_ID_DC", "0") or 0)
THREAD_ID_DC  = int(os.getenv("THREAD_ID_DC", "0") or 0)
RUN_INTERVAL_MIN_DC = int(os.getenv("RUN_INTERVAL_MIN_DC", "60") or 60)

POST_DELAY     = float(os.getenv("POST_DELAY", "0.25"))
BATCH_SIZE     = int(os.getenv("BATCH_SIZE", "50"))
BATCH_COOLDOWN = float(os.getenv("BATCH_COOLDOWN", "10.0"))

FORUM_AUTOCREATE   = os.getenv("FORUM_AUTOCREATE", "false").lower() in {"1","true","yes","on"}
FORUM_THREAD_TITLE = os.getenv("FORUM_THREAD_TITLE", "IST Updates")

LOG_CHANNEL_ID = int(os.getenv("LOG_CHANNEL_ID", "0") or 0)
HEALTH_MESSAGES = os.getenv("HEALTH_MESSAGES", "true").lower() in {"1","true","yes","on"}
HEALTH_SUMMARY_PREFIX = os.getenv("HEALTH_SUMMARY_PREFIX", "[Health]")

TEST_GUILD_ID = int(os.getenv("TEST_GUILD_ID", "0") or 0)
MY_GUILD = discord.Object(id=TEST_GUILD_ID) if TEST_GUILD_ID else None

intents = discord.Intents.default()
bot = commands.Bot(command_prefix="!", intents=intents)

redtag_lock = asyncio.Lock()
damages_lock = asyncio.Lock()
freegames_lock = asyncio.Lock()
marvel_lock = asyncio.Lock()
dc_lock = asyncio.Lock()  

last_stats = {
    "Red Tag":    {"last_run": None, "scraped": 0, "new": 0, "changed": 0, "pruned": 0, "interval_min": RUN_INTERVAL_MIN_REDTAG},
    "Damages":    {"last_run": None, "scraped": 0, "new": 0, "changed": 0, "pruned": 0, "interval_min": RUN_INTERVAL_MIN_DAMAGES},
    "Free Games": {"last_run": None, "scraped": 0, "new": 0, "changed": 0, "pruned": 0, "interval_min": RUN_INTERVAL_MIN_FREEGAMES},
    "Marvel":     {"last_run": None, "scraped": 0, "new": 0, "changed": 0, "pruned": 0, "interval_min": RUN_INTERVAL_MIN_MARVEL},
    "DC":         {"last_run": None, "scraped": 0, "new": 0, "changed": 0, "pruned": 0, "interval_min": RUN_INTERVAL_MIN_DC},
}

async def post_items(channel_like, render_fn, items: list[dict], label: str | None = None):
    if not items: return
    total = len(items); batch_count = 0
    for idx, it in enumerate(items, start=1):
        body = render_fn(it)
        msg = (f"**[{label}]**\n{body}") if label else body
        if len(msg) > 1900: msg = msg[:1900] + "…"
        await channel_like.send(msg)
        await asyncio.sleep(POST_DELAY)
        batch_count += 1
        if BATCH_SIZE and batch_count == BATCH_SIZE and idx < total:
            await asyncio.sleep(BATCH_COOLDOWN)
            batch_count = 0

async def call_blocking(func, *args, timeout: float = 300.0, **kwargs):
    bound = functools.partial(func, *args, **kwargs)
    return await asyncio.wait_for(asyncio.to_thread(bound), timeout=timeout)

def fmt_ts(dt: datetime | None) -> str:
    if not dt: return "—"
    epoch = int(dt.replace(tzinfo=timezone.utc).timestamp())
    return f"<t:{epoch}:f> (<t:{epoch}:R>)"

def get_next_run_dt(label: str) -> datetime | None:
    return {
        "Red Tag": getattr(auto_redtag, "next_iteration", None),
        "Damages": getattr(auto_damages, "next_iteration", None),
        "Free Games": getattr(auto_freegames, "next_iteration", None),
        "Marvel": getattr(auto_marvel, "next_iteration", None),
        "DC": getattr(auto_dc, "next_iteration", None),
    }.get(label)

def fmt_next_run(label: str) -> str:
    nxt = get_next_run_dt(label)
    return fmt_ts(nxt) if nxt else "—"

async def get_log_target(fallback):
    if LOG_CHANNEL_ID:
        try:
            ch = bot.get_channel(LOG_CHANNEL_ID) or await bot.fetch_channel(LOG_CHANNEL_ID)
            if hasattr(ch, "send"): return ch
        except Exception as e:
            print(f"LOG_CHANNEL_ID fetch failed: {e}")
    return fallback

async def send_health(label: str, channel_like, scraped: int, new: int, changed: int, pruned: int):
    if not HEALTH_MESSAGES: return
    target = await get_log_target(channel_like)
    now_utc = datetime.now(timezone.utc)
    last = last_stats[label]["last_run"] or now_utc
    interval_min = last_stats[label]["interval_min"]
    emb = discord.Embed(
        title=f"{HEALTH_SUMMARY_PREFIX} {label} run finished".strip(),
        timestamp=now_utc, color=0x43B581
    )
    emb.add_field(name="Scraped", value=str(scraped), inline=True)
    emb.add_field(name="New", value=str(new), inline=True)
    emb.add_field(name="Changed", value=str(changed), inline=True)
    emb.add_field(name="Pruned", value=str(pruned), inline=True)
    emb.add_field(name="Interval", value=f"{interval_min} min", inline=True)
    emb.add_field(name="Next run", value=fmt_next_run(label), inline=True)
    emb.add_field(name="Last run", value=fmt_ts(last), inline=False)
    try: await target.send(embed=emb)
    except Exception: traceback.print_exc()

async def resolve_channel_like(target_channel_id: int, target_thread_id: int):
    if target_thread_id:
        try:
            th = await bot.fetch_channel(target_thread_id)
            if hasattr(th, "send"): return th
            print(f"THREAD_ID {target_thread_id} not sendable (type={type(th)})")
        except Exception as e:
            print(f"fetch_channel THREAD_ID={target_thread_id} failed: {e}")
    ch = bot.get_channel(target_channel_id) if target_channel_id else None
    if ch:
        if hasattr(ch, "send"): return ch
        if isinstance(ch, discord.ForumChannel) and FORUM_AUTOCREATE:
            try:
                thread = await ch.create_thread(name=FORUM_THREAD_TITLE)
                print(f"Created forum thread: {thread} (id={thread.id})")
                return thread
            except Exception as e:
                print(f"Failed forum thread create: {e}")
        print(f"CHANNEL_ID {target_channel_id} not sendable (type={type(ch)})")
        return None
    if target_channel_id:
        try:
            ch = await bot.fetch_channel(target_channel_id)
            if hasattr(ch, "send"): return ch
            if isinstance(ch, discord.ForumChannel) and FORUM_AUTOCREATE:
                try:
                    thread = await ch.create_thread(name=FORUM_THREAD_TITLE)
                    print(f"Created forum thread: {thread} (id={thread.id})")
                    return thread
                except Exception as e:
                    print(f"Failed forum thread create: {e}")
            print(f"Resolved CHANNEL_ID {target_channel_id} not sendable (type={type(ch)})")
            return None
        except discord.Forbidden:
            print(f"Forbidden: cannot view channel {target_channel_id}")
        except discord.HTTPException as e:
            print(f"fetch_channel failed {target_channel_id}: {e}")
    print("No channel/thread resolved for given IDs.")
    return None

async def print_startup_diagnostics():
    print("Guilds I’m in:")
    for g in bot.guilds:
        print(f"- {g.name} ({g.id})")
    for label, cid in [
        ("REDTAG", CHANNEL_ID_REDTAG),
        ("DAMAGES", CHANNEL_ID_DAMAGES),
        ("FREEGAMES", CHANNEL_ID_FREEGAMES),
        ("MARVEL", CHANNEL_ID_MARVEL),
        ("DC", CHANNEL_ID_DC),
    ]:
        if cid:
            try:
                ch = await bot.fetch_channel(cid)
                print(f"{label} CHANNEL_ID -> {getattr(ch,'name',ch)} | type={type(ch)} | guild={getattr(ch,'guild',None)}")
            except Exception as e:
                print(f"Immediate fetch failed for {label} channel {cid}: {e}")
    if LOG_CHANNEL_ID:
        try:
            ch = await bot.fetch_channel(LOG_CHANNEL_ID)
            print(f"LOG CHANNEL_ID -> {getattr(ch,'name',ch)} | type={type(ch)} | guild={getattr(ch,'guild',None)}")
        except Exception as e:
            print(f"Immediate fetch failed for LOG channel {LOG_CHANNEL_ID}: {e}")

async def run_scraper_once(module, channel_like, label: str, lock: asyncio.Lock):
    async with lock:
        try:
            per_job_timeout = 600.0 if label.lower() in ("marvel", "dc") else 300.0
            new_items, changed_items, pruned, total = await call_blocking(
                module.get_updates, timeout=per_job_timeout
            )
        except asyncio.TimeoutError:
            print(f"[{label}] ERROR: get_updates() timed out; skipping this tick.")
            return
        except Exception:
            print(f"[{label}] ERROR during get_updates():"); traceback.print_exc(); return
    await post_items(channel_like, module.render_item, changed_items, label=label)
    last_stats[label].update({
        "last_run": datetime.now(timezone.utc),
        "scraped": total, "new": len(new_items), "changed": len(changed_items), "pruned": pruned,
    })
    print(f"[{label}] scraped={total} new={len(new_items)} changed={len(changed_items)} pruned={pruned}")
    await send_health(label, channel_like, total, len(new_items), len(changed_items), pruned)

@bot.event
async def on_ready():
    print(f"Logged in as {bot.user} (ID: {bot.user.id})")
    await print_startup_diagnostics()

    for ext in ("cogs.poll", "cogs.birthday"):
        try:
            await bot.load_extension(ext)
            print(f"Loaded cog: {ext}")
        except Exception:
            print(f"Failed to load {ext}:"); traceback.print_exc()

    try:
        if MY_GUILD: await bot.tree.sync(guild=MY_GUILD); print(f"Slash synced to guild {TEST_GUILD_ID}")
        else:         await bot.tree.sync();               print("Global slash commands synced.")
    except Exception:
        print("Slash sync failed:"); traceback.print_exc()

    if not auto_redtag.is_running():   auto_redtag.start()
    if not auto_damages.is_running():  auto_damages.start()
    if not auto_freegames.is_running():auto_freegames.start()
    if not auto_marvel.is_running():   auto_marvel.start()
    if not auto_dc.is_running():       auto_dc.start()

    await asyncio.sleep(0.2)

@bot.tree.command(name="redtag", description="Run the Red Tag scrape now.")
async def redtag_cmd(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    target = await resolve_channel_like(CHANNEL_ID_REDTAG, THREAD_ID_REDTAG) or interaction.channel
    if not target or not hasattr(target,"send"): return await interaction.followup.send("No sendable channel.", ephemeral=True)
    await run_scraper_once(redtag, target, "Red Tag", lock=redtag_lock)
    await interaction.followup.send("Red Tag done.", ephemeral=True)

@bot.tree.command(name="damages", description="Run the Damages scrape now.")
async def damages_cmd(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    target = await resolve_channel_like(CHANNEL_ID_DAMAGES, THREAD_ID_DAMAGES) or interaction.channel
    if not target or not hasattr(target,"send"): return await interaction.followup.send("No sendable channel.", ephemeral=True)
    await run_scraper_once(damages, target, "Damages", lock=damages_lock)
    await interaction.followup.send("Damages done.", ephemeral=True)

@bot.tree.command(name="freegames", description="Run the Free Games scrape now.")
async def freegames_cmd(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    target = await resolve_channel_like(CHANNEL_ID_FREEGAMES, THREAD_ID_FREEGAMES) or interaction.channel
    if not target or not hasattr(target,"send"): return await interaction.followup.send("No sendable channel.", ephemeral=True)
    await run_scraper_once(freegames, target, "Free Games", lock=freegames_lock)
    await interaction.followup.send("Free Games done.", ephemeral=True)

@bot.tree.command(name="marvel", description="Run the Marvel publisher scrape now.")
async def marvel_cmd(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    target = await resolve_channel_like(CHANNEL_ID_MARVEL, THREAD_ID_MARVEL) or interaction.channel
    if not target or not hasattr(target,"send"): return await interaction.followup.send("No sendable channel.", ephemeral=True)
    await run_scraper_once(marvel, target, "Marvel", lock=marvel_lock)
    await interaction.followup.send("Marvel done.", ephemeral=True)

@bot.tree.command(name="dc", description="Run the DC publisher scrape now.")
async def dc_cmd(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    target = await resolve_channel_like(CHANNEL_ID_DC, THREAD_ID_DC) or interaction.channel
    if not target or not hasattr(target,"send"): return await interaction.followup.send("No sendable channel.", ephemeral=True)
    await run_scraper_once(dc, target, "DC", lock=dc_lock)
    await interaction.followup.send("DC done.", ephemeral=True)

@bot.tree.command(name="stats", description="Show last run stats and current intervals.")
async def stats_cmd(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    def emb(lbl: str):
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
    for name in ("Red Tag","Damages","Free Games","Marvel","DC"):
        await interaction.followup.send(embed=emb(name), ephemeral=True)

@bot.tree.command(name="interval", description="Change run interval (minutes) for a feed.")
async def interval_cmd(interaction: discord.Interaction, feed: str, minutes: int):
    await interaction.response.defer(ephemeral=True)
    if minutes < 1 or minutes > 10080:
        return await interaction.followup.send("Minutes must be between 1 and 10080.", ephemeral=True)
    f = feed.strip().lower()
    try:
        if   f in ("redtag","red","rt"):   auto_redtag.change_interval(minutes=minutes);   last_stats["Red Tag"]["interval_min"]=minutes;   label="Red Tag"
        elif f in ("damages","damage"):    auto_damages.change_interval(minutes=minutes);  last_stats["Damages"]["interval_min"]=minutes;   label="Damages"
        elif f in ("freegames","free","fg"):auto_freegames.change_interval(minutes=minutes);last_stats["Free Games"]["interval_min"]=minutes;label="Free Games"
        elif f in ("marvel","mv"):         auto_marvel.change_interval(minutes=minutes);   last_stats["Marvel"]["interval_min"]=minutes;    label="Marvel"
        elif f in ("dc","dccomics"):       auto_dc.change_interval(minutes=minutes);       last_stats["DC"]["interval_min"]=minutes;        label="DC"
        else:
            return await interaction.followup.send("Use one of: redtag, damages, freegames, marvel, dc", ephemeral=True)
        await interaction.followup.send(f"Set **{label}** interval to **{minutes}** minutes.", ephemeral=True)
    except Exception:
        traceback.print_exc()
        await interaction.followup.send("Failed to update interval.", ephemeral=True)

@tasks.loop(minutes=RUN_INTERVAL_MIN_REDTAG)
async def auto_redtag():
    target = await resolve_channel_like(CHANNEL_ID_REDTAG, THREAD_ID_REDTAG)
    if not target or not hasattr(target,"send"): print("auto_redtag: no sendable channel."); return
    await run_scraper_once(redtag, target, "Red Tag", lock=redtag_lock)

@tasks.loop(minutes=RUN_INTERVAL_MIN_DAMAGES)
async def auto_damages():
    target = await resolve_channel_like(CHANNEL_ID_DAMAGES, THREAD_ID_DAMAGES)
    if not target or not hasattr(target,"send"): print("auto_damages: no sendable channel."); return
    await run_scraper_once(damages, target, "Damages", lock=damages_lock)

@tasks.loop(minutes=RUN_INTERVAL_MIN_FREEGAMES)
async def auto_freegames():
    target = await resolve_channel_like(CHANNEL_ID_FREEGAMES, THREAD_ID_FREEGAMES)
    if not target or not hasattr(target,"send"): print("auto_freegames: no sendable channel."); return
    await run_scraper_once(freegames, target, "Free Games", lock=freegames_lock)

@tasks.loop(minutes=RUN_INTERVAL_MIN_MARVEL)
async def auto_marvel():
    target = await resolve_channel_like(CHANNEL_ID_MARVEL, THREAD_ID_MARVEL)
    if not target or not hasattr(target,"send"): print("auto_marvel: no sendable channel."); return
    await run_scraper_once(marvel, target, "Marvel", lock=marvel_lock)

@tasks.loop(minutes=RUN_INTERVAL_MIN_DC)
async def auto_dc():
    target = await resolve_channel_like(CHANNEL_ID_DC, THREAD_ID_DC)
    if not target or not hasattr(target,"send"): print("auto_dc: no sendable channel."); return
    await run_scraper_once(dc, target, "DC", lock=dc_lock)

@auto_redtag.before_loop
async def _s1():
    await asyncio.sleep(2)

@auto_damages.before_loop
async def _s2():
    await asyncio.sleep(5)

@auto_freegames.before_loop
async def _s3():
    await asyncio.sleep(8)

@auto_marvel.before_loop
async def _s4():
    await asyncio.sleep(11)

@auto_dc.before_loop
async def _s5():
    await asyncio.sleep(14)


if __name__ == "__main__":
    print(
        f"RedTag:   channel={CHANNEL_ID_REDTAG} interval={RUN_INTERVAL_MIN_REDTAG}min\n"
        f"Damages:  channel={CHANNEL_ID_DAMAGES} interval={RUN_INTERVAL_MIN_DAMAGES}min\n"
        f"FreeGames:channel={CHANNEL_ID_FREEGAMES} interval={RUN_INTERVAL_MIN_FREEGAMES}min\n"
        f"Marvel:   channel={CHANNEL_ID_MARVEL} interval={RUN_INTERVAL_MIN_MARVEL}min\n"
        f"DC:       channel={CHANNEL_ID_DC} interval={RUN_INTERVAL_MIN_DC}min\n"
        f"Pacing:   delay={POST_DELAY}s batch={BATCH_SIZE} cool={BATCH_COOLDOWN}s\n"
        f"Forum:    autocreate={FORUM_AUTOCREATE} title='{FORUM_THREAD_TITLE}'\n"
        f"Health:   enabled={HEALTH_MESSAGES} log_channel={LOG_CHANNEL_ID or 'same as feed'}"
    )
    bot.run(DISCORD_TOKEN)
