# Anton

A self-hosted Discord bot for comic collectors. Anton watches InStockTrades, Half Price Books and free-game giveaways, posts what changes, and keeps everything in a local database you can search from Discord. It also does birthdays, polls, jokes, and (optionally) AI answers with live web search.

## What it does

**Feeds** (each posts to its own channel or thread, on its own timer)

| Feed | Watches | Posts by default |
|---|---|---|
| Red Tag | InStockTrades Red Tag sale | new listings, price changes |
| Damages | InStockTrades damaged-copy sale | new listings, price changes |
| Marvel / DC / Dark Horse / Image | InStockTrades publisher catalogs | price changes, restocks, sell-outs, removals |
| HPB Omnibuses | Half Price Books omnibus list | new listings, price changes |
| Free Games | Epic Games Store + GamerPower | new free games, end-date changes |

- **First run is silent.** A brand-new feed stores everything quietly, then posts only real changes after that, so turning a feed on never floods the channel.
- **Safe on bad scrapes.** If a site is down or returns a half-empty page, Anton doesn't mark everything as removed.
- **Posts are embeds** with cover art, price, MSRP and discount, sent up to 10 per message. Huge batches are capped (`MAX_POSTS_PER_RUN`) with a "plus N more" line.

**Slash commands**

| Command | Who | What |
|---|---|---|
| `/price <title>` | everyone | Current price, link and status of a title. Suggestions appear as you type. |
| `/history <title>` | everyone | Price over time: lowest, highest and every change. |
| `/watch add <keyword> [max_price]` | everyone | DMs you when a match is new, back, cheaper or restocked (optionally only under a price). |
| `/watch list` · `/watch remove` | everyone | Manage your watches. |
| `/birthday set / show / remove / list` | everyone | Save your birthday; `list` shows the whole server, soonest first. |
| `/poll <question> <answer1…5> [hours]` | everyone | Button poll; survives bot restarts; owner or mods can close it. |
| `/joke [kind]` | everyone | Dad jokes, puns, programming, spooky… |
| `/ask <question>` | everyone | AI answer using Claude with live web search (needs an API key, see below). |
| `/status` | everyone | Health of every feed: last run, next run, listings tracked. |
| `/feed run / channel / interval / toggle / reset` | admins | Run a feed now, or change its channel, timer or on/off without restarting. |
| `/welcome channel / message / dm / autorole / test / off` | admins | Greet new members with an embed (avatar and member number), optionally DM them, and give them a starting role. |
| `/leave channel / message / test / off` | admins | Post when someone leaves (same channel as welcomes unless you pick another). |
| `/colors setup / post` | admins | Creates (or reuses) the Yellow, Blue, Green, Purple, Red and Orange roles and posts a picker with buttons. The buttons keep working after restarts. |

`/feed`, `/welcome` and `/leave` are limited to members with **Manage Server** by default, and `/colors` to **Manage Roles**. Server admins can adjust that under *Server Settings → Integrations → Anton*.

Welcome, DM and leave messages can use `{user}` (mention), `{name}`, `{server}` and `{count}` (member count).

## Setup

1. Install Python 3.10 or newer.
2. Install the requirements. Newer Linux versions refuse a global `pip install` ("externally-managed-environment"), so use a virtual environment:
   ```bash
   python3 -m venv .venv
   source .venv/bin/activate
   pip install -r requirements.txt
   ```
   Run `source .venv/bin/activate` again in each new terminal before starting Anton. If creating the venv fails, run `sudo apt install python3-venv` first.
3. Copy `.env.example` to `.env` and fill in at least `DISCORD_TOKEN` and the channel IDs for the feeds you want. (Right-click a channel in Discord → *Copy Channel ID*; turn on Developer Mode in Discord's settings if you don't see it.)
4. Run it:
   ```bash
   python Anton.py
   ```

When inviting the bot, give it the `bot` and `applications.commands` scopes, with permission to **Send Messages, Embed Links, Read Message History** and **Manage Roles** (and **Create Public Threads** if a feed posts to a Forum).

**Two Discord settings for welcome messages and color roles:**

1. **Server Members Intent.** In the Developer Portal → your app → **Bot**, turn on **Server Members Intent** and save. Without it Discord doesn't tell Anton when people join or leave. If it's off, Anton still starts and prints a reminder, but welcome/leave messages and auto-role stay quiet.
2. **Role order.** In your server → Settings → **Roles**, drag Anton's role **above** the color roles (and the auto-role). Discord won't let a bot hand out roles ranked above its own. A member's name shows the color of their highest colored role, so other colored roles above the colors will hide them.

New slash commands can take a while to show up globally. For instant updates while testing, set `TEST_GUILD_ID` to your server's ID.

### Turning on `/ask`

1. Create a key at <https://console.anthropic.com> → **API Keys**. This is billed separately from a claude.ai subscription, pay as you go.
2. In `.env`, replace the placeholder:
   ```
   ANTHROPIC_API_KEY="HEY THIS IS WHERE YOUR KEY GOES!"
   ```
3. Restart Anton. Usage limits are in `.env` (`AI_DAILY_LIMIT`, `AI_DAILY_LIMIT_PER_USER`, `AI_USER_COOLDOWN_SEC`, `AI_MAX_SEARCHES`).

## Upgrading from the old Anton

The safest way is to set this version up in a **new folder** and leave the old one alone until you're happy:

```bash
git clone https://github.com/dazsinister/Anton.git Anton-new
cd Anton-new
git checkout anton-updates      # until it's merged into master
pip install -r requirements.txt
```

Then copy two files over from your old folder:

- **`.env`**: all the old setting names still work (`CHANNEL_ID_REDTAG`, `RUN_INTERVAL_MIN_MARVEL`, `MARVEL_POST_NEW`, `BDAY_CHANNEL_ID`…). Compare against `.env.example` for the new ones. `MARVEL_COOKIE`/`DC_COOKIE` are now a single `IST_COOKIE`.
- **`data/birthdays.json`**: imported automatically into the new database on first start.

The old `*_seen.json` files aren't needed. Each feed rebuilds its list quietly on its first run.

**Command changes:** `/redtag`, `/damages`, `/marvel`, `/dc` and `/freegames` are now `/feed run`. `/interval` is `/feed interval`. `/stats` is `/status`. `/pollclose` is the 🔒 button on the poll.

## Things to check after starting

- **Store URLs** are set in `.env` (`REDTAG_URL`, `DARKHORSE_URL`, `IMAGE_URL`…). The Red Tag URL points at the 2025 sale, and the Dark Horse and Image addresses are best guesses. If `/status` shows a feed finding **0 listings**, open the page in a browser and paste the right address into `.env`.
- **Health summaries** go to `LOG_CHANNEL_ID`. Leave it at `0` to skip them.

## Files

```
Anton.py            start here: loads everything and connects to Discord
core/config.py      reads .env
core/db.py          the SQLite database (data/anton.db) and change detection
core/feeds.py       the list of feeds and their settings
core/render.py      how posts look
cogs/               slash commands (feeds, lookup, birthday, poll, fun, ai)
scrapers/           one file per kind of site
tests/              offline tests: python -m unittest discover tests
```

Everything Anton remembers lives in `data/anton.db`. Back that file up if you care about price history. `data/` and `.env` are ignored by git, so they never get committed.
