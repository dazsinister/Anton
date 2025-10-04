from __future__ import annotations
import json, re, time
from pathlib import Path
from typing import Dict, List, Tuple, Optional
from urllib.parse import urljoin, urlparse
import requests
from bs4 import BeautifulSoup

BASE_URL = "https://www.instocktrades.com/specials/500/2025-red-tag-sale"
HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; IST-RedTagBot/2.3)"}
TIMEOUT = 20
PER_PAGE_DELAY = 1.0
MAX_PAGES = 200

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"
DATA_DIR.mkdir(parents=True, exist_ok=True)
SEEN_JSON = DATA_DIR / "redtag_seen.json"

money_rx = re.compile(r"\$\s?\d{1,3}(?:[,\d]{3})*(?:\.\d{2})?")
pct_rx   = re.compile(r"\b\d{1,3}%\s*off!?$", re.I)

def price_to_cents(s: Optional[str]) -> Optional[int]:
    if not s: return None
    try: return int(round(float(s.replace("$","").replace(",","").strip())*100))
    except: return None

def cents_to_str(c: Optional[int]) -> Optional[str]:
    return None if c is None else f"${c/100:.2f}"

def normalize_url(href: str) -> str:
    absu = urljoin(BASE_URL, href)
    u = urlparse(absu)
    return f"{u.scheme}://{u.netloc}{u.path}"

def fetch_doc(url: str) -> Optional[BeautifulSoup]:
    try:
        r = requests.get(url, headers=HEADERS, timeout=TIMEOUT)
        r.raise_for_status()
        return BeautifulSoup(r.text, "html.parser")
    except Exception:
        return None

def parse_page(doc: BeautifulSoup) -> List[dict]:
    items: Dict[str, dict] = {}
    product_links = [a for a in doc.select('a[href*="/products/"]') if a.get_text(strip=True)]
    for a in product_links:
        title = a.get_text(strip=True)
        url = normalize_url(a.get("href",""))
        node = a; container=None
        for _ in range(8):
            node = node.parent
            if not hasattr(node, "get_text"): break
            if money_rx.search(node.get_text(" ", strip=True)):
                container = node; break
        if not container: continue
        text = container.get_text(" ", strip=True)
        prices = money_rx.findall(text)
        msrp = prices[0] if prices else None
        sale = prices[1] if len(prices)>1 else None
        discm = pct_rx.search(text)
        discount = discm.group(0) if discm else None
        if url not in items:
            items[url] = {"title": title, "url": url, "msrp": msrp, "sale": sale, "discount": discount}
    return list(items.values())

def scrape_all_pages() -> List[dict]:
    all_items, seen_urls = [], set()
    for pg in range(1, MAX_PAGES+1):
        url = BASE_URL if pg==1 else f"{BASE_URL}?pg={pg}"
        doc = fetch_doc(url)
        if not doc: break
        items = parse_page(doc)
        if not items: break
        for it in items:
            if it["url"] in seen_urls: continue
            seen_urls.add(it["url"]); all_items.append(it)
        time.sleep(PER_PAGE_DELAY)
    all_items.sort(key=lambda x: x["title"].lower())
    return all_items

def load_seen() -> Dict[str, dict]:
    if not SEEN_JSON.exists(): return {}
    try: return json.loads(SEEN_JSON.read_text(encoding="utf-8"))
    except Exception: return {}

def save_seen(seen: Dict[str, dict]) -> None:
    SEEN_JSON.write_text(json.dumps(seen, ensure_ascii=False, indent=2), encoding="utf-8")

def get_updates() -> Tuple[List[dict], List[dict], int, int]:
    all_items = scrape_all_pages()
    total_scraped = len(all_items)
    current_urls = {it["url"] for it in all_items}
    seen = load_seen()

    stale = set(seen.keys()) - current_urls
    for u in stale: seen.pop(u, None)
    pruned_count = len(stale)

    new_items, changed_items = [], []
    for it in all_items:
        url = it["url"]
        sale_c = price_to_cents(it.get("sale"))
        msrp_c = price_to_cents(it.get("msrp"))
        if url not in seen:
            new_items.append(it)
            seen[url] = {"sale_cents": sale_c, "msrp_cents": msrp_c,
                         "title": it.get("title",""), "discount": it.get("discount")}
        else:
            prev = seen[url]
            sale_changed = (sale_c is not None and sale_c != prev.get("sale_cents"))
            msrp_changed = (msrp_c is not None and msrp_c != prev.get("msrp_cents"))
            if sale_changed or msrp_changed:
                it["_old_sale_str"] = cents_to_str(prev.get("sale_cents"))
                it["_old_msrp_str"] = cents_to_str(prev.get("msrp_cents"))
                changed_items.append(it)
                prev["sale_cents"] = sale_c if sale_c is not None else prev.get("sale_cents")
                prev["msrp_cents"] = msrp_c if msrp_c is not None else prev.get("msrp_cents")
                prev["title"] = it.get("title", prev.get("title",""))
                prev["discount"] = it.get("discount", prev.get("discount"))
    save_seen(seen)
    return new_items, changed_items, pruned_count, total_scraped

def render_item(it: dict) -> str:
    change_parts=[]
    if it.get("_old_sale_str") is not None or it.get("_old_msrp_str") is not None:
        if it.get("sale"): change_parts.append(f"Sale: {it.get('_old_sale_str','?')} → {it['sale']}")
        if it.get("msrp"): change_parts.append(f"MSRP: {it.get('_old_msrp_str','?')} → {it['msrp']}")
    lines=[f"**{it['title']}**", it["url"]]
    price_bits=[]
    if it.get("msrp"): price_bits.append(f"MSRP: {it['msrp']}")
    if it.get("sale"): price_bits.append(f"Sale: {it['sale']}")
    if price_bits: lines.append(" • ".join(price_bits))
    if it.get("discount"): lines.append(it["discount"])
    if change_parts: lines.append("**Price changed** → " + " | ".join(change_parts))
    return "\n".join(lines)
