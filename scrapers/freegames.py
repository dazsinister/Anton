"""Free games from the Epic Games Store and GamerPower (Steam, GOG and others)."""
from __future__ import annotations

import re
import time
from datetime import datetime, timezone
from typing import Optional

from scrapers.common import ScrapeError, get, make_session

EPIC_URL = ("https://store-site-backend-static-ipv4.ak.epicgames.com/freeGamesPromotions"
            "?locale=en-US&country=US&allowCountries=US")
GAMERPOWER_URL = "https://www.gamerpower.com/api/giveaways?type=game"


def _iso(s: Optional[str]) -> Optional[str]:
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:  # GamerPower sends "2026-11-13 23:59:00" with no zone; it's UTC
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds")


def _clean(t: str) -> str:
    return re.sub(r"\s+", " ", t or "").strip()


def _image(images: list, *preferred: str) -> Optional[str]:
    for kind in preferred:
        for im in images or []:
            if im.get("type") == kind and im.get("url"):
                return im["url"]
    return None


def fetch_epic(session) -> list[dict]:
    data = get(session, EPIC_URL).json()
    elements = data.get("data", {}).get("Catalog", {}).get("searchStore", {}).get("elements", []) or []
    out = []
    now = datetime.now(timezone.utc).isoformat()
    for el in elements:
        title = _clean(el.get("title"))
        if not title:
            continue
        offers = []
        for block in ((el.get("promotions") or {}).get("promotionalOffers") or []):
            for p in block.get("promotionalOffers", []):
                if (p.get("discountSetting") or {}).get("discountPercentage") == 0 or \
                        (p.get("discountSetting") or {}).get("discountType") == "PERCENTAGE":
                    offers.append(p)
        # Only count it if it's actually free right now
        price = ((el.get("price") or {}).get("totalPrice") or {})
        if not offers or price.get("discountPrice", 1) != 0:
            continue
        end = _iso(offers[0].get("endDate"))
        if end and end < now:
            continue
        slug = el.get("productSlug") or ((el.get("catalogNs") or {}).get("mappings") or [{}])[0].get("pageSlug") or ""
        orig = price.get("originalPrice")
        out.append({
            "key": f"epic:{slug or title.lower()}",
            "title": title,
            "url": f"https://store.epicgames.com/p/{slug}" if slug else "https://store.epicgames.com/free-games",
            "price_cents": 0,
            "msrp_cents": orig if isinstance(orig, int) and orig > 0 else None,
            "discount": "100% off",
            "available": None,
            "image": _image(el.get("keyImages"), "OfferImageWide", "DieselStoreFrontWide", "Thumbnail"),
            "extra": {"platform": "Epic Games Store", "source": "Epic", "end_date": end},
        })
    return out


def fetch_gamerpower(session) -> list[dict]:
    data = get(session, GAMERPOWER_URL).json()
    out = []
    for g in data if isinstance(data, list) else []:
        title = _clean(g.get("title"))
        url = g.get("open_giveaway_url") or g.get("giveaway_url") or ""
        if not title or not url:
            continue
        worth = g.get("worth") or ""
        end = g.get("end_date")
        out.append({
            "key": f"gamerpower:{g.get('id') or title.lower()}",
            "title": title,
            "url": url,
            "price_cents": 0,
            "msrp_cents": None if worth in ("", "N/A") else _worth_cents(worth),
            "discount": None,
            "available": None,
            "image": g.get("image") or g.get("thumbnail"),
            "extra": {"platform": _clean(g.get("platforms")) or "Various", "source": "GamerPower",
                      "end_date": None if end in (None, "", "N/A") else _iso(end)},
        })
    return out


def _worth_cents(worth: str) -> Optional[int]:
    m = re.search(r"(\d+(?:\.\d{1,2})?)", worth.replace(",", ""))
    return int(round(float(m.group(1)) * 100)) if m else None


def scrape(_base_url: str = "") -> list[dict]:
    items: list[dict] = []
    errors = []
    with make_session() as session:
        session.headers["Accept"] = "application/json"
        for fetch in (fetch_epic, fetch_gamerpower):
            try:
                items.extend(fetch(session))
            except Exception as e:
                errors.append(f"{fetch.__name__}: {e}")
            time.sleep(0.4)
    if errors and not items:
        raise ScrapeError("; ".join(errors))
    # Epic shows up in both sources; keep the Epic copy (it has the real store link)
    epic_titles = {it["title"].lower() for it in items if it["key"].startswith("epic:")}
    return [it for it in items
            if it["key"].startswith("epic:")
            or not ("epic" in it["extra"]["platform"].lower() and it["title"].lower().split(" (")[0] in epic_titles)]
