# Anton 2.0 — Discord Price Watcher Bot

Anton 2.0 is a self-hosted Discord bot that monitors **InStockTrades** deals and automatically posts updates to your Discord server.  
It scrapes both the **Red Tag Sale** and the **Damages** section, detecting:

- 🆕 New items  
- 🔄 Price changes (MSRP or Sale price updates)  
- 🗑️ Pruned items (disappeared listings, allowing re-post if they return)  

The bot runs on a configurable schedule, posts directly into your channels or threads, and provides health summaries after each run.

---

## ✨ Features

- **Two independent scrapers**  
  - Red Tag Sale  
  - Damages  
- **Automatic updates** on a loop (default: every 60 minutes, configurable)  
- **Price-change detection** with old → new value comparison  
- **Pruning** of stale items so reposting works if they return later  
- **Discord slash commands**  
  - `/redtag` → run Red Tag scrape now  
  - `/damages` → run Damages scrape now  
  - `/stats` → view last-run stats & next run time  
  - `/interval` → change scrape interval at runtime  
- **Health summaries** sent as embeds to your log channel  
- **Supports text channels, threads, or forum threads**  

---

## 🛠️ Requirements

- Python 3.10+  
- A Discord bot application + token ([create one here](https://discord.com/developers/applications))  
- Dependencies:
  - `discord.py`
  - `requests`
  - `beautifulsoup4`
  - `python-dotenv`

Install them with:

```bash
pip install -r requirements.txt
