from __future__ import annotations
import json, os, re, time
from random import random
from pathlib import Path
from typing import Dict, List, Tuple, Optional
from urllib.parse import urljoin, urlparse
import requests
from bs4 import BeautifulSoup

BASE_URL = "https://www.instocktrades.com/publishers/dc-comics"

DC_DEBUG   = os.getenv("DC_DEBUG", "0").lower() in {"1","true","yes","on"}
DC_POST_NEW = os.getenv("DC_POST_NEW", "false").lower() in {"1","true","yes","on"}
PER_PAGE_DELAY = float(os.getenv("DC_PER_PAGE_DELAY", "0.8"))
MAX_PAGES      = int(os.getenv("DC_MAX_PAGES", "200"))
TIMEOUT        = int(os.getenv("DC_TIMEOUT", "25"))
MAX_RUN_SECONDS= float(os.getenv("DC_MAX_RUN_SECONDS", "540"))

DC_COOKIE  = os.getenv("DC_COOKIE", "").strip()
DC_REFERER = os.getenv("DC_REFERER", "https://www.instocktrades.com/").strip()

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"
DATA_DIR.mkdir(parents=True, exist_ok=True)
SEEN_JSON = DATA_DIR / "dc_seen.json"
if not SEEN_JSON.exists():
    SEEN_JSON.write_text("{}", encoding="utf-8")

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Cache-Control": "no-cache",
    "Pragma": "no-cache",
    "Referer": DC_REFERER,
}
if DC_COOKIE:
    HEADERS["Cookie"] = DC_COOKIE

money_rx = re.compile(r"\$\s?\d{1,3}(?:[,\d]{3})*(?:\.\d{2})?")
add_rx   = re.compile(r"add\s*to\s*cart", re.I)

def _session() -> requests.Session:
    s = requests.Session()
    s.headers.update(HEADERS)
    return s

def _get_with_retries(session: requests.Session, url: str, tries: int = 3):
    last_err = None
    for i in range(tries):
        try:
            r = session.get(url, timeout=TIMEOUT)
            r.raise_for_status()
            return r
        except Exception as e:
            last_err = e
            time.sleep(0.5 + i*0.75 + random()*0.25)
    raise last_err

def _debug_dump(html: str, pg: int):
    if not DC_DEBUG: return
    p = DATA_DIR / f"debug_dc_pg{pg}.html"
    p.write_text(html, encoding="utf-8")
    print(f"[DC] wrote {p}")

def _page_url(pg: int) -> str:
    return BASE_URL if pg == 1 else f"{BASE_URL}?pg={pg}"

def normalize_url(href: str) -> str:
    absu = urljoin(BASE_URL + "/", href)
    u = urlparse(absu)
    return f"{u.scheme}://{u.netloc}{u.path}"

def parse_page(doc: BeautifulSoup) -> List[dict]:
    """Anchor → climb to card → detect 'Add to Cart' presence."""
    items: Dict[str, dict] = {}
    product_links = [a for a in doc.select('a[href*="/products/"]') if a.get_text(strip=True)]

    for a in product_links:
        title = a.get_text(strip=True)
        url = normalize_url(a.get("href",""))

        node = a; container = None
        for _ in range(8):
            node = node.parent
            if not hasattr(node, "get_text"): break
            txt = node.get_text(" ", strip=True)
            if money_rx.search(txt) or add_rx.search(txt):
                container = node; break
        if not container:
            container = a.parent if hasattr(a.parent, "get_text") else None
            if not container: continue

        text = container.get_text(" ", strip=True).lower()
        available = "add to cart" in text

        if url not in items:
            items[url] = {"title": title, "url": url, "available": available}

    return list(items.values())

def scrape_all_pages() -> List[dict]:
    start = time.time()
    all_items, seen_urls = [], set()
    with _session() as session:
        for pg in range(1, MAX_PAGES + 1):
            if time.time() - start > MAX_RUN_SECONDS:
                if DC_DEBUG: print(f"[DC] stopping due to run budget {MAX_RUN_SECONDS}s")
                break
            url = _page_url(pg)
            try:
                r = _get_with_retries(session, url, tries=3)
                html = r.text
            except Exception as e:
                if DC_DEBUG: print(f"[DC] page fetch error (pg {pg}): {e}")
                break

            doc = BeautifulSoup(html, "html.parser")
            if DC_DEBUG and pg <= 2:
                _debug_dump(doc.prettify(), pg)

            items = parse_page(doc)
            if DC_DEBUG: print(f"[DC] page {pg}: parsed {len(items)} items")
            if not items: break

            for it in items:
                if it["url"] in seen_urls: continue
                seen_urls.add(it["url"]); all_items.append(it)

            time.sleep(PER_PAGE_DELAY)

    all_items.sort(key=lambda x: x["title"].lower())
    if DC_DEBUG: print(f"[DC] total collected: {len(all_items)}")
    return all_items

def load_seen() -> Dict[str, dict]:
    try:
        return json.loads(SEEN_JSON.read_text(encoding="utf-8"))
    except Exception:
        return {}

def save_seen(seen: Dict[str, dict]) -> None:
    SEEN_JSON.write_text(json.dumps(seen, ensure_ascii=False, indent=2), encoding="utf-8")

def get_updates() -> Tuple[List[dict], List[dict], int, int]:
    current = scrape_all_pages()
    total_scraped = len(current)
    current_map = {it["url"]: it for it in current}
    seen = load_seen()

    removed = set(seen.keys()) - set(current_map.keys())
    pruned_count = len(removed)

    new_items: List[dict] = []
    changed_items: List[dict] = []

    for u in removed:
        prev = seen.get(u, {})
        changed_items.append({"title": prev.get("title","(unknown)"), "url": u, "_event": "removed"})
        seen.pop(u, None)

    for url, it in current_map.items():
        avail_now = bool(it.get("available"))
        if url not in seen:
            seen[url] = {"title": it["title"], "available": avail_now}
            if DC_POST_NEW:
                it["_event"] = "new"; changed_items.append(it)
            else:
                new_items.append(it)
        else:
            prev = seen[url]
            was = bool(prev.get("available"))
            if was and not avail_now:
                it["_event"] = "oos"; changed_items.append(it); prev["available"] = avail_now
            elif (not was) and avail_now:
                it["_event"] = "restock"; changed_items.append(it); prev["available"] = avail_now
            else:
                prev["title"] = it.get("title", prev.get("title",""))

    save_seen(seen)
    return new_items, changed_items, pruned_count, total_scraped

def render_item(it: dict) -> str:
    event = it.get("_event")
    title = it.get("title","Unknown")
    url = it.get("url","")
    if event == "oos":
        return f"❌ **OOS**: {title}\n{url}\n(‘Add to Cart’ no longer present)"
    if event == "restock":
        return f"✅ **Back in stock**: {title}\n{url}\n(‘Add to Cart’ is back)"
    if event == "removed":
        return f"🗑️ **Removed from listing**: {title}\n{url}"
    if event == "new":
        return f"🆕 **New item**: {title}\n{url}"
    return f"**{title}**\n{url}"
