"""Settings read from .env, in one place."""
from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / ".env")

DATA_DIR = BASE_DIR / "data"
DATA_DIR.mkdir(parents=True, exist_ok=True)
DB_PATH = Path(os.getenv("ANTON_DB_PATH", str(DATA_DIR / "anton.db")))


def env_str(name: str, default: str = "") -> str:
    val = os.getenv(name)
    if val is None:
        return default
    # Allow trailing "# comments" after a value, as in .env.example
    val = val.split(" #")[0].strip().strip('"').strip("'")
    return val if val else default


def env_int(name: str, default: int = 0) -> int:
    try:
        return int(float(env_str(name, str(default))))
    except ValueError:
        return default


def env_float(name: str, default: float = 0.0) -> float:
    try:
        return float(env_str(name, str(default)))
    except ValueError:
        return default


def env_bool(name: str, default: bool = False) -> bool:
    return env_str(name, "true" if default else "false").lower() in {"1", "true", "yes", "on"}


DISCORD_TOKEN = env_str("DISCORD_TOKEN")
TEST_GUILD_ID = env_int("TEST_GUILD_ID")

# AI
AI_KEY_PLACEHOLDER = "HEY THIS IS WHERE YOUR KEY GOES!"
ANTHROPIC_API_KEY = env_str("ANTHROPIC_API_KEY")
AI_ENABLED = bool(ANTHROPIC_API_KEY) and ANTHROPIC_API_KEY != AI_KEY_PLACEHOLDER
AI_MODEL = env_str("AI_MODEL", "claude-sonnet-5-5")
AI_MAX_SEARCHES = env_int("AI_MAX_SEARCHES", 3)
AI_USER_COOLDOWN_SEC = env_int("AI_USER_COOLDOWN_SEC", 30)
AI_DAILY_LIMIT_PER_USER = env_int("AI_DAILY_LIMIT_PER_USER", 20)
AI_DAILY_LIMIT = env_int("AI_DAILY_LIMIT", 100)

# Scraping
SCRAPE_PAGE_DELAY = env_float("SCRAPE_PAGE_DELAY", 0.8)
SCRAPE_MAX_PAGES = env_int("SCRAPE_MAX_PAGES", 200)
SCRAPE_TIMEOUT = env_int("SCRAPE_TIMEOUT", 25)
SCRAPE_MAX_RUN_SECONDS = env_float("SCRAPE_MAX_RUN_SECONDS", 540)

# Posting
MAX_POSTS_PER_RUN = env_int("MAX_POSTS_PER_RUN", 30)
POST_DELAY = env_float("POST_DELAY", 1.0)
FORUM_AUTOCREATE = env_bool("FORUM_AUTOCREATE", False)
FORUM_THREAD_TITLE = env_str("FORUM_THREAD_TITLE", "Anton Updates")

# Logs
LOG_CHANNEL_ID = env_int("LOG_CHANNEL_ID")
HEALTH_MESSAGES = env_bool("HEALTH_MESSAGES", True)

# Birthdays
BDAY_CHANNEL_ID = env_int("BDAY_CHANNEL_ID")
BDAY_ANNOUNCE_HOUR = env_int("BDAY_ANNOUNCE_HOUR", 9)
BDAY_TZ = env_str("BDAY_TZ", "America/New_York")
