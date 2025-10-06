from __future__ import annotations
import json
import time
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Tuple, Optional
import requests

HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; AntonFreeGames/1.0)"}
TIMEOUT = (5, 15)
PER_SOURCE_DELAY = 0.4

BASE_DIR  = Path(__file__).resolve().parent.parent
DATA_DIR  = BASE_DIR / "data"
DATA_DIR.mkdir(parents=True, exist_ok=True)
SEEN_JSON = DATA_DIR / "freegames_seen.json"

if not SEEN_JSON.exists():
    SEEN_JSON.write_text("{}", encoding="utf-8")

def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()

def _parse_dt(s: Optional[str]) -> Optional[str]:
    """Return normalized ISO timestamp (UTC) if parseable, else None."""
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        return dt.astimezone(timezone.utc).isoformat()
    except Exception:
        return None

def _short_dt(s: Optional[str]) -> Optional[str]:
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(s)
        return dt.strftime("%Y-%m-%d %H:%M UTC")
    except Exception:
        return s

def _clean_title(t: str) -> str:
    return re.sub(r"\s+", " ", t).strip()

def _load_seen() -> Dict[str, dict]:
    try:
        return json.loads(SEEN_JSON.read_text(encoding="utf-8"))
    except Exception:
        return {}

def _save_seen(seen: Dict[str, dict]) -> None:
    SEEN_JSON.write_text(json.dumps(seen, ensure_ascii=False, indent=2), encoding="utf-8")

def fetch_epic() -> List[dict]:
    """
    Pull current Epic free games.
    Endpoint is known to expose weekly promos.
    """
    url = "https://store-site-backend-static-ipv4.ak.epicgames.com/freeGamesPromotions?locale=en-US&country=US&allowCountries=US"
    try:
        r = requests.get(url, headers=HEADERS, timeout=TIMEOUT)
        r.raise_for_status()
        data = r.json()
    except Exception:
        return []

    out: List[dict] = []
    elements = (
        data.get("data", {})
            .get("Catalog", {})
            .get("searchStore", {})
            .get("elements", [])
    )
    for el in elements:
        title = _clean_title(el.get("title") or "")
        if not title:
            continue

        slug = el.get("productSlug") or ""
        url = f"https://store.epicgames.com/p/{slug}" if slug else "https://store.epicgames.com/"

        promos = (el.get("promotions") or {})
        current = (promos.get("promotionalOffers") or [])
        upcoming = (promos.get("upcomingPromotionalOffers") or [])

        windows = []
        if current and isinstance(current, list):
            for block in current:
                for p in block.get("promotionalOffers", []):
                    discount = (p.get("discountSetting") or {}).get("discountType")
                    start = p.get("startDate")
                    end = p.get("endDate")
                    if discount == "PERCENTAGE":
                        windows.append({
                            "start_date": _parse_dt(start),
                            "end_date": _parse_dt(end),
                        })

        if not windows:
            continue

        end_date = windows[0].get("end_date")
        out.append({
            "key": f"epic::{slug or title.lower()}",
            "title": title,
            "platform": "Epic Games",
            "source": "Epic",
            "url": url,
            "start_date": windows[0].get("start_date"),
            "end_date": end_date,
        })

    return out

def fetch_gamerpower() -> List[dict]:
    """
    GamerPower aggregates giveaways (Steam/Epic/GOG/etc.).
    API docs: https://www.gamerpower.com/api-read
    We'll fetch the active giveaways list and normalize.
    """
    url = "https://www.gamerpower.com/api/giveaways"
    try:
        r = requests.get(url, headers=HEADERS, timeout=TIMEOUT)
        r.raise_for_status()
        data = r.json()
    except Exception:
        return []

    out: List[dict] = []
    for g in data if isinstance(data, list) else []:
        try:
            title = _clean_title(g.get("title") or "")
            if not title:
                continue
            platforms = _clean_title(g.get("platforms") or "")
            worth = g.get("worth") or ""
            end_str = g.get("end_date") or None
            end_iso = _parse_dt(end_str) if end_str else None
            url = g.get("open_giveaway_url") or g.get("giveaway_url") or g.get("url") or ""
            if not url:
                continue

            gid = str(g.get("id") or "")
            key = f"gamerpower::{gid}" if gid else f"gamerpower::{title.lower()}::{url}"

            gtype = (g.get("type") or "").lower()

            out.append({
                "key": key,
                "title": title,
                "platform": platforms or "Various",
                "source": "GamerPower",
                "url": url,
                "start_date": None,
                "end_date": end_iso,
                "worth": worth,
                "type": gtype,
            })
        except Exception:
            continue

    return out

def scrape_all_sources() -> List[dict]:
    items: List[dict] = []
    epic_items = fetch_epic()
    items.extend(epic_items)
    time.sleep(PER_SOURCE_DELAY)
    gp_items = fetch_gamerpower()
    items.extend(gp_items)

    uniq: Dict[str, dict] = {}
    for it in items:
        k = it["key"]
        if k in uniq:
            if it["source"] == "Epic":
                uniq[k] = it
        else:
            uniq[k] = it

    out = list(uniq.values())
    out.sort(key=lambda x: (x.get("platform", ""), x.get("title", "").lower()))
    return out

def get_updates() -> Tuple[List[dict], List[dict], int, int]:
    """
    Returns:
      new_items: unseen items (first time)
      changed_items: known items whose end_date OR url/title/platform changed
      pruned_count: items removed because they're no longer active this run
      total_scraped: total items gathered across sources this run
    """
    all_items = scrape_all_sources()
    total = len(all_items)
    current_keys = {it["key"] for it in all_items}

    seen = _load_seen()

    stale = set(seen.keys()) - current_keys
    for k in stale:
        seen.pop(k, None)
    pruned = len(stale)

    new_items: List[dict] = []
    changed_items: List[dict] = []

    for it in all_items:
        k = it["key"]
        snapshot = {
            "title": it.get("title"),
            "platform": it.get("platform"),
            "url": it.get("url"),
            "end_date": it.get("end_date"),
            "source": it.get("source"),
        }

        if k not in seen:
            new_items.append(it)
            seen[k] = snapshot
        else:
            prev = seen[k]
            changed = (
                (prev.get("end_date") or "") != (snapshot.get("end_date") or "") or
                (prev.get("url") or "")      != (snapshot.get("url") or "") or
                (prev.get("title") or "")    != (snapshot.get("title") or "") or
                (prev.get("platform") or "") != (snapshot.get("platform") or "")
            )
            if changed:
                it["_old_end"] = prev.get("end_date")
                it["_old_url"] = prev.get("url")
                it["_old_title"] = prev.get("title")
                it["_old_platform"] = prev.get("platform")
                changed_items.append(it)
                seen[k] = snapshot

    _save_seen(seen)
    return new_items, changed_items, pruned, total

def render_item(it: dict) -> str:
    """Discord message body."""
    parts = [f"**{it['title']}**"]
    meta = []

    if it.get("platform"):
        meta.append(f"Platform: {it['platform']}")
    if it.get("source"):
        meta.append(f"Source: {it['source']}")
    if it.get("end_date"):
        meta.append(f"Ends: {_short_dt(it['end_date'])}")
    if meta:
        parts.append(" • ".join(meta))

    if it.get("url"):
        parts.append(it["url"])

    ch = []
    if it.get("_old_end") is not None and (it.get("_old_end") != it.get("end_date")):
        ch.append(f"End: {_short_dt(it.get('_old_end'))} → {_short_dt(it.get('end_date'))}")
    if it.get("_old_url") is not None and (it.get("_old_url") != it.get("url")):
        ch.append("URL changed")
    if it.get("_old_title") is not None and (it.get("_old_title") != it.get("title")):
        ch.append("Title updated")
    if it.get("_old_platform") is not None and (it.get("_old_platform") != it.get("platform")):
        ch.append("Platform updated")
    if ch:
        parts.append("**Updated** → " + " | ".join(ch))

    return "\n".join(parts)
