# scrapers/hpb_omnibuses.py
from __future__ import annotations

import json, os, re, time
from pathlib import Path
from typing import Dict, List, Tuple, Optional
from urllib.parse import urljoin, urlparse, urlencode

import requests
from bs4 import BeautifulSoup

# ---------- Config ----------
BASE_URL = "https://www.hpb.com/books/trending-topics/omnibuses"  # HPB Omnibuses landing

PAGE_SIZE       = int(os.getenv("HPBOMNI_PAGE_SIZE", "80"))       # HPB supports 80
MAX_PAGES       = int(os.getenv("HPBOMNI_MAX_PAGES", "200"))
PER_PAGE_DELAY  = float(os.getenv("HPBOMNI_PER_PAGE_DELAY", "1.0"))
TIMEOUT_CONN    = int(os.getenv("HPBOMNI_TIMEOUT_CONN", "8"))
TIMEOUT_READ    = int(os.getenv("HPBOMNI_TIMEOUT_READ", "25"))

# Anti-bot helpers (optional but useful)
HPBOMNI_COOKIE  = os.getenv("HPBOMNI_COOKIE", "").strip()         # paste browser Cookie string if needed
HPBOMNI_REFERER = os.getenv("HPBOMNI_REFERER", "https://www.hpb.com/").strip()

# ---------- Paths ----------
BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"
DATA_DIR.mkdir(parents=True, exist_ok=True)
SEEN_JSON = DATA_DIR / "hpb_omni_seen.json"
if not SEEN_JSON.exists():
    SEEN_JSON.write_text("{}", encoding="utf-8")

# ---------- HTTP ----------
BASE_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
    "Cache-Control": "no-cache",
    "Pragma": "no-cache",
    "DNT": "1",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "same-origin",
    "Sec-Fetch-User": "?1",
    "sec-ch-ua": '"Chromium";v="124", "Not:A-Brand";v="99", "Google Chrome";v="124"',
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-platform": '"Windows"',
}

def _session() -> requests.Session:
    s = requests.Session()
    headers = dict(BASE_HEADERS)
    if HPBOMNI_REFERER:
        headers["Referer"] = HPBOMNI_REFERER
    if HPBOMNI_COOKIE:
        headers["Cookie"] = HPBOMNI_COOKIE
    s.headers.update(headers)
    return s

def _page_url_offset(page_index: int) -> str:
    # HPB likes offset style: ?sz=80&srule=most-popular&start=0,80,160…
    offset = page_index * PAGE_SIZE
    params = {"sz": PAGE_SIZE, "srule": "most-popular", "start": offset}
    sep = "&" if "?" in BASE_URL else "?"
    return f"{BASE_URL}{sep}{urlencode(params)}"

def _page_url_hash(page_index: int) -> str:
    # Fallback style: #pgN (still add sz= to encourage full page size)
    params = {"sz": PAGE_SIZE, "srule": "most-popular"}
    sep = "&" if "?" in BASE_URL else "?"
    return f"{BASE_URL}{sep}{urlencode(params)}#pg{page_index+1}"

def fetch_doc(session: requests.Session, url: str) -> Optional[BeautifulSoup]:
    try:
        r = session.get(url, timeout=(TIMEOUT_CONN, TIMEOUT_READ))
        r.raise_for_status()
        return BeautifulSoup(r.text, "html.parser")
    except Exception:
        return None

def _txt(node) -> Optional[str]:
    return node.get_text(strip=True) if node else None

def normalize_url(href: str) -> str:
    absu = urljoin(BASE_URL + "/", href)
    u = urlparse(absu)
    return f"{u.scheme}://{u.netloc}{u.path}"

def parse_page(doc: BeautifulSoup) -> List[dict]:
    """
    HPB uses a few templates. Try multiple selectors.
    """
    items: Dict[str, dict] = {}
    tiles = (
        doc.select("div.product")
        or doc.select("div.grid-tile")
        or doc.select("li.product")
        or doc.select("li.grid-tile")
    )
    if not tiles:
        return []

    for item in tiles:
        a_title = (item.select_one("a.link")
                   or item.select_one("a.name-link")
                   or item.select_one("a.product-name"))
        title = _txt(a_title)
        if not title:
            continue

        a_image = (item.select_one("div.image-container a")
                   or item.select_one("a.thumb-link")
                   or item.select_one("a.link")
                   or item.select_one("a.name-link")
                   or item.select_one("a.product-name"))
        href = a_image.get("href") if a_image else None
        if not href:
            continue
        url = normalize_url(href)

        price_div = (item.select_one("div.price")
                     or item.select_one("div.product-pricing")
                     or item.select_one("div.pricing"))
        spans = price_div.select("span.sales") if price_div else []
        min_price = _txt(spans[0]) if spans else None
        max_price = _txt(spans[1]) if spans and len(spans) > 1 else min_price

        # metadata (optional)
        isbn = ""
        img = (item.select_one("img.tile-image")
               or item.select_one("img.product-image")
               or item.select_one("img"))
        src = img.get("src") if img else None
        if src:
            m = re.search(r"/normal/(\w+)/", src)
            if m:
                isbn = m.group(1)

        item_id = item.get("data-pid", "") or item.get("data-itemid", "")

        items[url] = {
            "title": title,
            "url": url,
            "isbn": isbn,
            "id": item_id,
            "minPrice": min_price,
            "maxPrice": max_price,
        }

    return list(items.values())

def parse_total_status(doc: BeautifulSoup) -> tuple[int, int, int] | None:
    # Parse "X - Y of Z results" if present
    try:
        el = (doc.select_one("div.result-count span")
              or doc.select_one("div.result-count.mb-2.mb-lg-0 span"))
        if not el: return None
        txt = el.get_text(strip=True)
        m = re.search(r"(\d+)\s*-\s*(\d+)\s*of\s*(\d+)", txt)
        if not m: return None
        return int(m.group(1)), int(m.group(2)), int(m.group(3))
    except Exception:
        return None

def scrape_all_pages() -> List[dict]:
    all_items: List[dict] = []
    seen_urls = set()

    with _session() as session:
        used_hash_fallback = False
        for pg in range(0, MAX_PAGES):
            url = _page_url_offset(pg)
            doc = fetch_doc(session, url)
            if not doc:
                break

            page_items = parse_page(doc)
            if not page_items:
                # try #pg fallback for first couple pages
                if pg < 2 and not used_hash_fallback:
                    used_hash_fallback = True
                    all_items.clear(); seen_urls.clear()
                    for pg2 in range(0, MAX_PAGES):
                        url2 = _page_url_hash(pg2)
                        doc2 = fetch_doc(session, url2)
                        if not doc2: break
                        items2 = parse_page(doc2)
                        if not items2: break
                        for it in items2:
                            if it["url"] in seen_urls: continue
                            seen_urls.add(it["url"]); all_items.append(it)
                        status2 = parse_total_status(doc2)
                        if status2:
                            _x, y, z = status2
                            if y >= z: break
                        elif len(items2) < PAGE_SIZE:
                            break
                        time.sleep(PER_PAGE_DELAY)
                break  # end offset loop

            for it in page_items:
                if it["url"] in seen_urls: continue
                seen_urls.add(it["url"]); all_items.append(it)

            status = parse_total_status(doc)
            if status:
                _x, y, z = status
                if y >= z: break
            elif len(page_items) < PAGE_SIZE:
                break

            time.sleep(PER_PAGE_DELAY)

    all_items.sort(key=lambda x: x["title"].lower())
    return all_items

# ---------- Seen / Update logic ----------
def price_to_cents(s: Optional[str]) -> Optional[int]:
    if not s: return None
    try: return int(round(float(s.replace("$","").replace(",","").strip())*100))
    except: return None

def cents_to_str(c: Optional[int]) -> Optional[str]:
    return None if c is None else f"${c/100:.2f}"

def load_seen() -> Dict[str, dict]:
    try:
        return json.loads(SEEN_JSON.read_text(encoding="utf-8"))
    except Exception:
        return {}

def save_seen(seen: Dict[str, dict]) -> None:
    SEEN_JSON.write_text(json.dumps(seen, ensure_ascii=False, indent=2), encoding="utf-8")

def get_updates() -> Tuple[List[dict], List[dict], int, int]:
    all_items = scrape_all_pages()
    total_scraped = len(all_items)
    current_urls = {it["url"] for it in all_items}
    seen = load_seen()

    # prune not present
    stale = set(seen.keys()) - current_urls
    for u in stale: seen.pop(u, None)
    pruned_count = len(stale)

    new_items, changed_items = [], []
    for it in all_items:
        url = it["url"]
        min_c = price_to_cents(it.get("minPrice"))
        max_c = price_to_cents(it.get("maxPrice"))
        if url not in seen:
            new_items.append(it)
            seen[url] = {
                "min_cents": min_c,
                "max_cents": max_c,
                "title": it.get("title",""),
                "isbn":  it.get("isbn",""),
                "id":    it.get("id",""),
            }
        else:
            prev = seen[url]
            min_changed = (min_c is not None and min_c != prev.get("min_cents"))
            max_changed = (max_c is not None and max_c != prev.get("max_cents"))
            if min_changed or max_changed:
                it["_old_min_str"] = cents_to_str(prev.get("min_cents"))
                it["_old_max_str"] = cents_to_str(prev.get("max_cents"))
                changed_items.append(it)
                if min_c is not None: prev["min_cents"] = min_c
                if max_c is not None: prev["max_cents"] = max_c
                prev["title"] = it.get("title", prev.get("title",""))
                prev["isbn"]  = it.get("isbn",  prev.get("isbn",""))
                prev["id"]    = it.get("id",    prev.get("id",""))

    save_seen(seen)
    return new_items, changed_items, pruned_count, total_scraped

def render_item(it: dict) -> str:
    parts = [f"**{it['title']}**"]
    meta = []
    if it.get("isbn"): meta.append(f"ISBN: {it['isbn']}")
    if it.get("id"):   meta.append(f"ID: {it['id']}")
    if meta: parts.append(" • ".join(meta))

    pb = []
    if it.get("minPrice"): pb.append(f"Min: {it['minPrice']}")
    if it.get("maxPrice") and it.get("maxPrice") != it.get("minPrice"):
        pb.append(f"Max: {it['maxPrice']}")
    if pb: parts.append(" | ".join(pb))

    ch = []
    if it.get("_old_min_str") is not None and it.get("minPrice"):
        ch.append(f"Min: {it.get('_old_min_str','?')} → {it['minPrice']}")
    if it.get("_old_max_str") is not None and it.get("maxPrice"):
        ch.append(f"Max: {it.get('_old_max_str','?')} → {it['maxPrice']}")
    if ch: parts.append("**Price changed** → " + " | ".join(ch))

    if it.get("url"): parts.append(it["url"])
    return "\n".join(parts)
