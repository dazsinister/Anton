"""Half Price Books omnibus listing (restored from the old hpb.py and moved onto the database)."""
from __future__ import annotations

import re
import time
from typing import Optional
from urllib.parse import urlencode

from bs4 import BeautifulSoup, Tag

from core import config
from core.db import price_to_cents
from scrapers.common import ScrapeError, abs_url, clean_url, get, make_session

PAGE_SIZE = 80  # HPB serves up to 80 per page


def _txt(node: Optional[Tag]) -> Optional[str]:
    return node.get_text(" ", strip=True) if node else None


def _page_url(base: str, page_index: int) -> str:
    params = {"sz": PAGE_SIZE, "srule": "most-popular", "start": page_index * PAGE_SIZE}
    return f"{base}{'&' if '?' in base else '?'}{urlencode(params)}"


def parse_page(html: str, base: str) -> list[dict]:
    doc = BeautifulSoup(html, "html.parser")
    tiles = doc.select("div.product") or doc.select("div.grid-tile") or doc.select("li.product") or doc.select("li.grid-tile")
    items: dict[str, dict] = {}
    for tile in tiles:
        a_title = tile.select_one("a.link") or tile.select_one("a.name-link") or tile.select_one("a.product-name")
        title = _txt(a_title)
        if not title:
            continue
        a_href = tile.select_one("div.image-container a") or tile.select_one("a.thumb-link") or a_title
        href = a_href.get("href") if a_href else None
        if not href:
            continue
        url = clean_url(base, href)

        price_div = tile.select_one("div.price") or tile.select_one("div.product-pricing") or tile.select_one("div.pricing")
        spans = price_div.select("span.sales") if price_div else []
        prices = [price_to_cents(_txt(s)) for s in spans]
        prices = [p for p in prices if p is not None]

        img = tile.select_one("img.tile-image") or tile.select_one("img.product-image") or tile.select_one("img")
        src = (img.get("data-src") or img.get("src")) if img else None
        isbn = ""
        if src:
            m = re.search(r"/normal/(\w+)/", src)
            isbn = m.group(1) if m else ""

        items[url] = {
            "key": url.split("hpb.com")[-1] or url,
            "title": " ".join(title.split()),
            "url": url,
            # HPB lists used copies at several prices; track the cheapest
            "price_cents": min(prices) if prices else None,
            "msrp_cents": None,
            "discount": None,
            "available": None,
            "image": abs_url(base, src),
            "extra": {"isbn": isbn, "max_price_cents": max(prices) if prices else None},
        }
    return list(items.values())


def _result_total(html: str) -> Optional[int]:
    m = re.search(r"\d+\s*-\s*\d+\s*of\s*([\d,]+)\s*results", html)
    return int(m.group(1).replace(",", "")) if m else None


def scrape(base_url: str) -> list[dict]:
    start = time.monotonic()
    session = make_session(referer="https://www.hpb.com/", cookie=config.env_str("HPB_COOKIE"))
    found: dict[str, dict] = {}
    with session:
        for pg in range(config.SCRAPE_MAX_PAGES):
            if time.monotonic() - start > config.SCRAPE_MAX_RUN_SECONDS:
                break
            try:
                html = get(session, _page_url(base_url, pg)).text
            except ScrapeError:
                if pg == 0:
                    raise
                break
            fresh = [it for it in parse_page(html, base_url) if it["url"] not in found]
            if not fresh:
                break
            for it in fresh:
                found[it["url"]] = it
            total = _result_total(html)
            if (total is not None and len(found) >= total) or len(fresh) < PAGE_SIZE // 2:
                break
            time.sleep(config.SCRAPE_PAGE_DELAY)
    return list(found.values())
