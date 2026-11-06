"""Shared HTTP helpers for the scrapers (these run in a worker thread, not the bot loop)."""
from __future__ import annotations

import time
from random import random
from typing import Optional
from urllib.parse import urljoin, urlparse

import requests

from core import config

BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Cache-Control": "no-cache",
}


class ScrapeError(Exception):
    """The site couldn't be reached at all (as opposed to an empty listing)."""


def make_session(referer: str = "", cookie: str = "") -> requests.Session:
    s = requests.Session()
    s.headers.update(BROWSER_HEADERS)
    if referer:
        s.headers["Referer"] = referer
    if cookie:
        s.headers["Cookie"] = cookie
    return s


def get(session: requests.Session, url: str, tries: int = 3) -> requests.Response:
    last: Optional[Exception] = None
    for i in range(tries):
        try:
            r = session.get(url, timeout=config.SCRAPE_TIMEOUT)
            r.raise_for_status()
            return r
        except Exception as e:  # network error, 4xx/5xx
            last = e
            time.sleep(0.75 + i * 1.5 + random() * 0.5)
    raise ScrapeError(f"{url}: {last}")


def clean_url(base: str, href: str) -> str:
    """Absolute URL without query string or fragment, so the same product always has the same key."""
    u = urlparse(urljoin(base, href))
    return f"{u.scheme}://{u.netloc}{u.path}"


def abs_url(base: str, href: Optional[str]) -> Optional[str]:
    if not href or href.startswith("data:"):
        return None
    return urljoin(base, href.strip())
