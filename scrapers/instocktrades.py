"""InStockTrades listing scraper, shared by Red Tag, Damages and the publisher catalogs.

All of these pages use the same product-card layout, so one parser handles them:
find each product link, climb up to its card, and read the title, prices,
discount, stock status and cover image from that card.
"""
from __future__ import annotations

import re
import time
from typing import Optional

from bs4 import BeautifulSoup, Tag

from core import config
from core.db import price_to_cents
from scrapers.common import ScrapeError, abs_url, clean_url, get, make_session

money_rx = re.compile(r"\$\s?\d{1,3}(?:,\d{3})*(?:\.\d{2})?|\$\s?\d+(?:\.\d{2})?")
pct_rx = re.compile(r"\b\d{1,3}%\s*off\b!?", re.I)


def _href_key(a: Tag) -> str:
    return re.split(r"[?#]", a.get("href", ""))[0].rstrip("/")


def _product_links(node: Tag) -> set[str]:
    return {_href_key(a) for a in node.select('a[href*="/products/"]')}


def _find_card(a: Tag) -> Optional[Tag]:
    """Climb from the product link to the smallest ancestor that holds a price or a
    cart button, without spilling into a neighbouring product's card."""
    own = _href_key(a)
    node: Tag = a
    for _ in range(8):
        parent = node.parent
        if not isinstance(parent, Tag):
            break
        if _product_links(parent) - {own}:
            break  # parent contains another product, so stay at this level
        node = parent
        txt = node.get_text(" ", strip=True)
        if money_rx.search(txt) or re.search(r"add\s*to\s*cart|pre-?order", txt, re.I):
            return node
    return node if node is not a else None


def _availability(text: str) -> Optional[bool]:
    t = text.lower()
    if re.search(r"add\s*to\s*cart|pre-?order", t):
        return True
    if re.search(r"out\s*of\s*stock|sold\s*out|unavailable|notify\s*me", t):
        return False
    return None  # can't tell, so no stock events for this item


def _image(card: Tag, base: str) -> Optional[str]:
    img = card.find("img")
    if not img:
        return None
    for attr in ("data-src", "data-original", "data-lazy", "src"):
        url = abs_url(base, img.get(attr))
        if url:
            return url
    return None


def parse_page(html: str, base: str) -> list[dict]:
    doc = BeautifulSoup(html, "html.parser")
    items: dict[str, dict] = {}
    for a in doc.select('a[href*="/products/"]'):
        title = a.get_text(" ", strip=True)
        if not title:
            continue  # image-only link; the text link in the same card gives the title
        url = clean_url(base, a.get("href", ""))
        if url in items:
            continue
        card = _find_card(a)
        if card is None:
            continue
        text = card.get_text(" ", strip=True)
        prices = [price_to_cents(p) for p in money_rx.findall(text)]
        prices = [p for p in prices if p is not None]
        if len(prices) >= 2:
            msrp, price = prices[0], prices[1]
            if price > msrp:  # page listed "our price" first
                msrp, price = price, msrp
        elif prices:
            msrp, price = None, prices[0]
        else:
            msrp = price = None
        disc = pct_rx.search(text)
        items[url] = {
            "key": url.split("instocktrades.com")[-1] or url,
            "title": " ".join(title.split()),
            "url": url,
            "price_cents": price,
            "msrp_cents": msrp,
            "discount": disc.group(0).rstrip("!") if disc else None,
            "available": _availability(text),
            "image": _image(card, base),
            "extra": {},
        }
    return list(items.values())


def scrape(base_url: str) -> list[dict]:
    """Walk ?pg=1,2,3… until a page adds nothing new."""
    start = time.monotonic()
    session = make_session(
        referer="https://www.instocktrades.com/",
        cookie=config.env_str("IST_COOKIE"),
        browser=config.env_bool("IST_BROWSER_HEADERS", False),
        user_agent=config.env_str("IST_USER_AGENT"),
    )
    found: dict[str, dict] = {}
    with session:
        for pg in range(1, config.SCRAPE_MAX_PAGES + 1):
            if time.monotonic() - start > config.SCRAPE_MAX_RUN_SECONDS:
                print(f"[IST] {base_url}: stopped at page {pg}, time budget used up")
                break
            url = base_url if pg == 1 else f"{base_url}?pg={pg}"
            try:
                html = get(session, url).text
            except ScrapeError:
                if pg == 1:
                    raise  # couldn't load the listing at all
                break
            page_items = parse_page(html, base_url)
            fresh = [it for it in page_items if it["url"] not in found]
            if not fresh:
                break  # empty page, or the site ignored ?pg= and repeated page 1
            for it in fresh:
                found[it["url"]] = it
            time.sleep(config.SCRAPE_PAGE_DELAY)
    return list(found.values())
