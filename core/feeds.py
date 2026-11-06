"""The list of feeds Anton watches, and their settings.

Settings come from .env first; anything changed with /feed in Discord is saved in
the database and wins from then on.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Optional

from core import config, db
from scrapers import freegames, hpb, instocktrades


@dataclass(frozen=True)
class Feed:
    key: str                     # short id, e.g. "redtag"
    label: str                   # display name
    env: str                     # .env suffix, e.g. REDTAG → CHANNEL_ID_REDTAG
    scrape_fn: Callable[[str], list[dict]]
    default_url: str = ""
    post_new_default: bool = True
    post_removed: bool = False   # announce listings that vanish
    color: int = 0x5865F2
    store: str = ""

    # ── effective settings ──
    @property
    def url(self) -> str:
        return config.env_str(f"{self.env}_URL", self.default_url)

    @property
    def post_new(self) -> bool:
        return config.env_bool(f"{self.env}_POST_NEW", self.post_new_default)

    @property
    def channel_id(self) -> int:
        v = db.get_setting(f"feed:{self.key}:channel")
        return int(v) if v else config.env_int(f"CHANNEL_ID_{self.env}")

    @property
    def thread_id(self) -> int:
        if db.get_setting(f"feed:{self.key}:channel"):
            return 0  # a channel picked in Discord replaces the .env thread
        return config.env_int(f"THREAD_ID_{self.env}")

    @property
    def interval_min(self) -> int:
        v = db.get_setting(f"feed:{self.key}:interval")
        return int(v) if v else max(1, config.env_int(f"RUN_INTERVAL_MIN_{self.env}", 60))

    @property
    def enabled(self) -> bool:
        v = db.get_setting(f"feed:{self.key}:enabled")
        if v is not None:
            return v == "1"
        return bool(self.channel_id or self.thread_id)

    def scrape(self) -> list[dict]:
        return self.scrape_fn(self.url)


IST = "InStockTrades"

FEEDS: list[Feed] = [
    Feed("redtag", "Red Tag", "REDTAG", instocktrades.scrape,
         "https://www.instocktrades.com/specials/500/2025-red-tag-sale", True, False, 0xE03131, IST),
    Feed("damages", "Damages", "DAMAGES", instocktrades.scrape,
         "https://www.instocktrades.com/damages", True, False, 0xF08C00, IST),
    Feed("marvel", "Marvel", "MARVEL", instocktrades.scrape,
         "https://www.instocktrades.com/publishers/marvel", False, True, 0xC92A2A, IST),
    Feed("dc", "DC", "DC", instocktrades.scrape,
         "https://www.instocktrades.com/publishers/dc-comics", False, True, 0x1971C2, IST),
    Feed("darkhorse", "Dark Horse", "DARKHORSE", instocktrades.scrape,
         "https://www.instocktrades.com/publishers/dark-horse-comics", False, True, 0x495057, IST),
    Feed("image", "Image", "IMAGE", instocktrades.scrape,
         "https://www.instocktrades.com/publishers/image-comics", False, True, 0x2F9E44, IST),
    Feed("hpb", "HPB Omnibuses", "HPB", hpb.scrape,
         "https://www.hpb.com/books/trending-topics/omnibuses", True, False, 0x7048E8, "Half Price Books"),
    Feed("freegames", "Free Games", "FREEGAMES", freegames.scrape,
         "", True, False, 0x12B886, "Epic / GamerPower"),
]

BY_KEY: dict[str, Feed] = {f.key: f for f in FEEDS}


def get(key: str) -> Optional[Feed]:
    return BY_KEY.get(key)


def label(feed_key: str) -> str:
    f = BY_KEY.get(feed_key)
    return f.label if f else feed_key
