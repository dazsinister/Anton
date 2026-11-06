"""SQLite database: every scraped listing, price history, watchlists, birthdays,
polls, settings and AI usage live in one file (data/anton.db).

Each call opens its own short-lived connection, so it is safe to use from the
scraper threads and from the bot's event loop at the same time.
"""
from __future__ import annotations

import difflib
import json
import re
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Iterable, Iterator, Optional

from core import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS items (
    feed         TEXT NOT NULL,
    key          TEXT NOT NULL,
    title        TEXT NOT NULL,
    url          TEXT,
    price_cents  INTEGER,
    msrp_cents   INTEGER,
    discount     TEXT,
    available    INTEGER,
    image        TEXT,
    extra        TEXT,
    active       INTEGER NOT NULL DEFAULT 1,
    first_seen   TEXT NOT NULL,
    last_seen    TEXT NOT NULL,
    last_changed TEXT,
    PRIMARY KEY (feed, key)
);
CREATE INDEX IF NOT EXISTS idx_items_title ON items(title COLLATE NOCASE);

CREATE TABLE IF NOT EXISTS price_history (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    feed        TEXT NOT NULL,
    key         TEXT NOT NULL,
    price_cents INTEGER,
    msrp_cents  INTEGER,
    recorded_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_history_item ON price_history(feed, key);

CREATE TABLE IF NOT EXISTS feed_runs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    feed        TEXT NOT NULL,
    finished_at TEXT NOT NULL,
    duration_s  REAL,
    scraped     INTEGER,
    new         INTEGER,
    changed     INTEGER,
    removed     INTEGER,
    note        TEXT,
    error       TEXT
);
CREATE INDEX IF NOT EXISTS idx_runs_feed ON feed_runs(feed, id);

CREATE TABLE IF NOT EXISTS watches (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id         INTEGER NOT NULL,
    guild_id        INTEGER,
    channel_id      INTEGER,
    keyword         TEXT NOT NULL,
    max_price_cents INTEGER,
    created_at      TEXT NOT NULL,
    UNIQUE (user_id, keyword)
);

CREATE TABLE IF NOT EXISTS birthdays (
    user_id INTEGER PRIMARY KEY,
    month   INTEGER NOT NULL,
    day     INTEGER NOT NULL,
    year    INTEGER
);

CREATE TABLE IF NOT EXISTS polls (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id   INTEGER,
    channel_id INTEGER,
    message_id INTEGER,
    owner_id   INTEGER NOT NULL,
    question   TEXT NOT NULL,
    options    TEXT NOT NULL,
    ends_at    TEXT,
    closed     INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS poll_votes (
    poll_id    INTEGER NOT NULL,
    user_id    INTEGER NOT NULL,
    option_idx INTEGER NOT NULL,
    PRIMARY KEY (poll_id, user_id)
);

CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT
);

CREATE TABLE IF NOT EXISTS ai_usage (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    used_at TEXT NOT NULL
);
"""


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@contextmanager
def connect() -> Iterator[sqlite3.Connection]:
    con = sqlite3.connect(config.DB_PATH, timeout=30)
    con.row_factory = sqlite3.Row
    try:
        yield con
        con.commit()
    finally:
        con.close()


def init() -> None:
    config.DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with connect() as con:
        con.execute("PRAGMA journal_mode=WAL")
        con.executescript(SCHEMA)


# ── Settings ────────────────────────────────────────────────

def get_setting(key: str, default: Optional[str] = None) -> Optional[str]:
    with connect() as con:
        row = con.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return row["value"] if row else default


def set_setting(key: str, value: Optional[str]) -> None:
    with connect() as con:
        if value is None:
            con.execute("DELETE FROM settings WHERE key=?", (key,))
        else:
            con.execute(
                "INSERT INTO settings(key, value) VALUES(?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, value),
            )


# ── Prices ──────────────────────────────────────────────────

_money_rx = re.compile(r"\$?\s?(\d{1,3}(?:,\d{3})*|\d+)(?:\.(\d{1,2}))?")


def price_to_cents(s: Any) -> Optional[int]:
    if s is None:
        return None
    if isinstance(s, (int, float)):
        return int(round(float(s) * 100))
    m = _money_rx.search(str(s))
    if not m:
        return None
    dollars = int(m.group(1).replace(",", ""))
    cents = int((m.group(2) or "0").ljust(2, "0"))
    return dollars * 100 + cents


def cents_to_str(c: Optional[int]) -> Optional[str]:
    return None if c is None else f"${c / 100:,.2f}"


# ── Scrape diffing ──────────────────────────────────────────

@dataclass
class Event:
    kind: str                     # new, back, price_drop, price_up, msrp_change, restock, oos, removed, updated
    item: dict
    old_price: Optional[int] = None
    old_msrp: Optional[int] = None
    old_extra: dict = field(default_factory=dict)


@dataclass
class ScrapeResult:
    feed: str
    scraped: int = 0
    events: list[Event] = field(default_factory=list)
    seeded_now: bool = False      # first run: stored quietly, nothing posted
    removal_skipped: bool = False  # scrape looked broken, so nothing was marked removed

    def count(self, *kinds: str) -> int:
        return sum(1 for e in self.events if e.kind in kinds)


def _row_to_item(row: sqlite3.Row) -> dict:
    d = dict(row)
    d["extra"] = json.loads(d["extra"]) if d.get("extra") else {}
    if d.get("available") is not None:
        d["available"] = bool(d["available"])
    return d


def is_seeded(feed: str) -> bool:
    return get_setting(f"seeded:{feed}") == "1"


def apply_scrape(feed: str, items: Iterable[dict], track_removals: bool = True) -> ScrapeResult:
    """Compare a fresh scrape against the database, store it, and return what changed.

    Each item: key, title, url, price_cents, msrp_cents, discount, available, image, extra.
    """
    items = list(items)
    res = ScrapeResult(feed=feed, scraped=len(items))
    ts = now_iso()
    seeded = is_seeded(feed)

    with connect() as con:
        existing = {
            r["key"]: _row_to_item(r)
            for r in con.execute("SELECT * FROM items WHERE feed=?", (feed,))
        }
        prev_active = sum(1 for it in existing.values() if it["active"])
        seen_keys: set[str] = set()

        for it in items:
            key = it["key"]
            if key in seen_keys:
                continue
            seen_keys.add(key)
            price, msrp = it.get("price_cents"), it.get("msrp_cents")
            avail = it.get("available")
            extra = it.get("extra") or {}
            prev = existing.get(key)

            if prev is None:
                con.execute(
                    "INSERT INTO items(feed,key,title,url,price_cents,msrp_cents,discount,available,image,extra,"
                    "active,first_seen,last_seen,last_changed) VALUES(?,?,?,?,?,?,?,?,?,?,1,?,?,?)",
                    (feed, key, it["title"], it.get("url"), price, msrp, it.get("discount"),
                     None if avail is None else int(avail), it.get("image"), json.dumps(extra), ts, ts, ts),
                )
                if price is not None or msrp is not None:
                    con.execute(
                        "INSERT INTO price_history(feed,key,price_cents,msrp_cents,recorded_at) VALUES(?,?,?,?,?)",
                        (feed, key, price, msrp, ts),
                    )
                if seeded:
                    res.events.append(Event("new", it))
                continue

            changed = False
            if not prev["active"]:
                if seeded:
                    res.events.append(Event("back", it, old_price=prev["price_cents"]))
                changed = True
            else:
                if price is not None and prev["price_cents"] is not None and price != prev["price_cents"]:
                    kind = "price_drop" if price < prev["price_cents"] else "price_up"
                    res.events.append(Event(kind, it, old_price=prev["price_cents"], old_msrp=prev["msrp_cents"]))
                    changed = True
                elif msrp is not None and prev["msrp_cents"] is not None and msrp != prev["msrp_cents"]:
                    res.events.append(Event("msrp_change", it, old_price=prev["price_cents"], old_msrp=prev["msrp_cents"]))
                    changed = True
                if avail is not None and prev["available"] is not None and avail != prev["available"]:
                    res.events.append(Event("restock" if avail else "oos", it))
                    changed = True
                watched = {k: extra.get(k) for k in ("end_date",)}
                old_watched = {k: prev["extra"].get(k) for k in ("end_date",)}
                if any(watched[k] and watched[k] != old_watched[k] for k in watched) and not changed:
                    res.events.append(Event("updated", it, old_extra=old_watched))
                    changed = True

            if price is not None and (price != prev["price_cents"] or (msrp is not None and msrp != prev["msrp_cents"])):
                con.execute(
                    "INSERT INTO price_history(feed,key,price_cents,msrp_cents,recorded_at) VALUES(?,?,?,?,?)",
                    (feed, key, price, msrp if msrp is not None else prev["msrp_cents"], ts),
                )
            con.execute(
                "UPDATE items SET title=?, url=?, price_cents=COALESCE(?, price_cents), msrp_cents=COALESCE(?, msrp_cents), "
                "discount=?, available=COALESCE(?, available), image=COALESCE(?, image), extra=?, active=1, "
                "last_seen=?, last_changed=CASE WHEN ? THEN ? ELSE last_changed END WHERE feed=? AND key=?",
                (it["title"], it.get("url"), price, msrp, it.get("discount"),
                 None if avail is None else int(avail), it.get("image"), json.dumps(extra),
                 ts, int(changed), ts, feed, key),
            )

        # Listings that disappeared. If the scrape came back empty or tiny, the site is
        # probably down or blocking us, so don't wipe everything (that would cause a
        # flood of "back" posts next run).
        gone = [k for k, it in existing.items() if it["active"] and k not in seen_keys]
        looks_broken = len(seen_keys) == 0 or (prev_active >= 20 and len(seen_keys) < prev_active * 0.5)
        if gone and looks_broken:
            res.removal_skipped = True
        elif gone:
            for k in gone:
                con.execute("UPDATE items SET active=0, last_changed=? WHERE feed=? AND key=?", (ts, feed, k))
                if seeded and track_removals:
                    res.events.append(Event("removed", existing[k]))

    if not seeded and items:
        set_setting(f"seeded:{feed}", "1")
        res.seeded_now = True
    return res


def record_run(feed: str, res: Optional[ScrapeResult], duration_s: float, error: Optional[str] = None) -> None:
    note = None
    if res and res.seeded_now:
        note = "first run: stored quietly"
    elif res and res.removal_skipped:
        note = "scrape looked incomplete: removals skipped"
    with connect() as con:
        con.execute(
            "INSERT INTO feed_runs(feed,finished_at,duration_s,scraped,new,changed,removed,note,error) "
            "VALUES(?,?,?,?,?,?,?,?,?)",
            (feed, now_iso(), round(duration_s, 1),
             res.scraped if res else 0,
             res.count("new", "back") if res else 0,
             res.count("price_drop", "price_up", "msrp_change", "restock", "oos", "updated") if res else 0,
             res.count("removed") if res else 0,
             note, error),
        )
        con.execute(
            "DELETE FROM feed_runs WHERE feed=? AND id NOT IN "
            "(SELECT id FROM feed_runs WHERE feed=? ORDER BY id DESC LIMIT 50)",
            (feed, feed),
        )


def last_run(feed: str) -> Optional[dict]:
    with connect() as con:
        row = con.execute("SELECT * FROM feed_runs WHERE feed=? ORDER BY id DESC LIMIT 1", (feed,)).fetchone()
    return dict(row) if row else None


def feed_item_counts() -> dict[str, tuple[int, int]]:
    """feed -> (active listings, all-time listings)"""
    with connect() as con:
        rows = con.execute(
            "SELECT feed, SUM(active) AS active, COUNT(*) AS total FROM items GROUP BY feed"
        ).fetchall()
    return {r["feed"]: (r["active"] or 0, r["total"] or 0) for r in rows}


# ── Search ──────────────────────────────────────────────────

_word_rx = re.compile(r"[a-z0-9]+")


def _words(s: str) -> list[str]:
    return _word_rx.findall(s.lower())


_NUM_WORDS = {"vol": "volume", "v": "volume", "pt": "part", "omni": "omnibus", "tp": "tpb"}


def _norm_query_words(query: str) -> list[str]:
    out = []
    for w in _words(query):
        if w in {"by", "the", "a", "an", "of", "and"}:
            continue
        out.append(_NUM_WORDS.get(w, w))
    return out


def search_items(query: str, limit: int = 10, active_only: bool = False) -> list[dict]:
    """Find listings by title. Every word must appear; falls back to fuzzy matching."""
    words = _norm_query_words(query)
    with connect() as con:
        rows: list[sqlite3.Row] = []
        if words:
            # "volume" and "vol" are both common on listings, so match either
            clauses, params = [], []
            for w in words:
                variants = [w] + [k for k, v in _NUM_WORDS.items() if v == w]
                clauses.append("(" + " OR ".join("lower(title) LIKE ?" for _ in variants) + ")")
                params.extend(f"%{v}%" for v in variants)
            sql = "SELECT * FROM items WHERE " + " AND ".join(clauses)
            if active_only:
                sql += " AND active=1"
            sql += " ORDER BY active DESC, length(title) ASC LIMIT ?"
            rows = con.execute(sql, (*params, limit * 3)).fetchall()

        if not rows:
            sql = "SELECT * FROM items" + (" WHERE active=1" if active_only else "")
            all_rows = con.execute(sql).fetchall()
            q = " ".join(words) or query.lower()
            scored = []
            for r in all_rows:
                score = difflib.SequenceMatcher(None, q, r["title"].lower()).ratio()
                if score >= 0.45:
                    scored.append((score, r))
            scored.sort(key=lambda x: (-x[0], -x[1]["active"]))
            rows = [r for _, r in scored[: limit * 3]]

    items = [_row_to_item(r) for r in rows]
    # Rank: exact-ish matches first, then active, then cheaper
    qn = " ".join(words)

    def rank(it: dict):
        t = " ".join(_norm_query_words(it["title"]))
        return (0 if t == qn else 1, 0 if it["active"] else 1, len(t))

    items.sort(key=rank)
    return items[:limit]


def get_item(feed: str, key: str) -> Optional[dict]:
    with connect() as con:
        row = con.execute("SELECT * FROM items WHERE feed=? AND key=?", (feed, key)).fetchone()
    return _row_to_item(row) if row else None


def price_history(feed: str, key: str) -> list[dict]:
    with connect() as con:
        rows = con.execute(
            "SELECT price_cents, msrp_cents, recorded_at FROM price_history WHERE feed=? AND key=? ORDER BY id",
            (feed, key),
        ).fetchall()
    return [dict(r) for r in rows]


# ── Watches ─────────────────────────────────────────────────

def add_watch(user_id: int, guild_id: Optional[int], channel_id: Optional[int],
              keyword: str, max_price_cents: Optional[int]) -> bool:
    """Returns False if the user already watches this keyword (it gets updated)."""
    keyword = " ".join(keyword.split())
    with connect() as con:
        cur = con.execute(
            "SELECT id FROM watches WHERE user_id=? AND lower(keyword)=lower(?)", (user_id, keyword)
        ).fetchone()
        if cur:
            con.execute("UPDATE watches SET max_price_cents=?, channel_id=? WHERE id=?",
                        (max_price_cents, channel_id, cur["id"]))
            return False
        con.execute(
            "INSERT INTO watches(user_id,guild_id,channel_id,keyword,max_price_cents,created_at) VALUES(?,?,?,?,?,?)",
            (user_id, guild_id, channel_id, keyword, max_price_cents, now_iso()),
        )
    return True


def remove_watch(user_id: int, keyword: str) -> bool:
    with connect() as con:
        cur = con.execute("DELETE FROM watches WHERE user_id=? AND lower(keyword)=lower(?)",
                          (user_id, " ".join(keyword.split())))
        return cur.rowcount > 0


def list_watches(user_id: Optional[int] = None) -> list[dict]:
    with connect() as con:
        if user_id is None:
            rows = con.execute("SELECT * FROM watches").fetchall()
        else:
            rows = con.execute("SELECT * FROM watches WHERE user_id=? ORDER BY keyword", (user_id,)).fetchall()
    return [dict(r) for r in rows]


def watch_matches(keyword: str, title: str) -> bool:
    """All words of the keyword appear in the title (case-insensitive)."""
    kw = _norm_query_words(keyword)
    tw = set(_norm_query_words(title))
    return bool(kw) and all(any(t == w or t.startswith(w) for t in tw) for w in kw)


# ── Birthdays ───────────────────────────────────────────────

def set_birthday(user_id: int, month: int, day: int, year: Optional[int]) -> None:
    with connect() as con:
        con.execute(
            "INSERT INTO birthdays(user_id,month,day,year) VALUES(?,?,?,?) "
            "ON CONFLICT(user_id) DO UPDATE SET month=excluded.month, day=excluded.day, year=excluded.year",
            (user_id, month, day, year),
        )


def remove_birthday(user_id: int) -> bool:
    with connect() as con:
        return con.execute("DELETE FROM birthdays WHERE user_id=?", (user_id,)).rowcount > 0


def all_birthdays() -> list[dict]:
    with connect() as con:
        return [dict(r) for r in con.execute("SELECT * FROM birthdays")]


def migrate_birthdays_json() -> int:
    """One-time import of the old data/birthdays.json, if present."""
    path = config.DATA_DIR / "birthdays.json"
    if get_setting("migrated:birthdays_json") == "1" or not path.exists():
        return 0
    try:
        raw = json.loads(path.read_text(encoding="utf-8") or "{}")
    except Exception:
        raw = {}
    n = 0
    with connect() as con:
        for uid, v in raw.items():
            try:
                con.execute(
                    "INSERT OR IGNORE INTO birthdays(user_id,month,day,year) VALUES(?,?,?,?)",
                    (int(uid), int(v["month"]), int(v["day"]), int(v["year"]) if v.get("year") else None),
                )
                n += 1
            except Exception:
                continue
    set_setting("migrated:birthdays_json", "1")
    return n


# ── Polls ───────────────────────────────────────────────────

def create_poll(guild_id: Optional[int], channel_id: int, owner_id: int, question: str,
                options: list[str], ends_at: Optional[str]) -> int:
    with connect() as con:
        cur = con.execute(
            "INSERT INTO polls(guild_id,channel_id,owner_id,question,options,ends_at) VALUES(?,?,?,?,?,?)",
            (guild_id, channel_id, owner_id, question, json.dumps(options), ends_at),
        )
        return int(cur.lastrowid)


def set_poll_message(poll_id: int, message_id: int) -> None:
    with connect() as con:
        con.execute("UPDATE polls SET message_id=? WHERE id=?", (message_id, poll_id))


def delete_poll(poll_id: int) -> None:
    with connect() as con:
        con.execute("DELETE FROM poll_votes WHERE poll_id=?", (poll_id,))
        con.execute("DELETE FROM polls WHERE id=?", (poll_id,))


def get_poll(poll_id: int) -> Optional[dict]:
    with connect() as con:
        row = con.execute("SELECT * FROM polls WHERE id=?", (poll_id,)).fetchone()
        if not row:
            return None
        poll = dict(row)
        poll["options"] = json.loads(poll["options"])
        counts = [0] * len(poll["options"])
        for v in con.execute("SELECT option_idx, COUNT(*) AS n FROM poll_votes WHERE poll_id=? GROUP BY option_idx",
                             (poll_id,)):
            if 0 <= v["option_idx"] < len(counts):
                counts[v["option_idx"]] = v["n"]
        poll["counts"] = counts
    return poll


def get_poll_by_message(message_id: int) -> Optional[dict]:
    with connect() as con:
        row = con.execute("SELECT id FROM polls WHERE message_id=?", (message_id,)).fetchone()
    return get_poll(row["id"]) if row else None


def cast_vote(poll_id: int, user_id: int, option_idx: int) -> str:
    """Returns 'added', 'changed', or 'removed' (clicking your own vote again removes it)."""
    with connect() as con:
        prev = con.execute("SELECT option_idx FROM poll_votes WHERE poll_id=? AND user_id=?",
                           (poll_id, user_id)).fetchone()
        if prev and prev["option_idx"] == option_idx:
            con.execute("DELETE FROM poll_votes WHERE poll_id=? AND user_id=?", (poll_id, user_id))
            return "removed"
        con.execute(
            "INSERT INTO poll_votes(poll_id,user_id,option_idx) VALUES(?,?,?) "
            "ON CONFLICT(poll_id,user_id) DO UPDATE SET option_idx=excluded.option_idx",
            (poll_id, user_id, option_idx),
        )
        return "changed" if prev else "added"


def close_poll(poll_id: int) -> None:
    with connect() as con:
        con.execute("UPDATE polls SET closed=1 WHERE id=?", (poll_id,))


def due_polls(now: str) -> list[int]:
    with connect() as con:
        rows = con.execute(
            "SELECT id FROM polls WHERE closed=0 AND ends_at IS NOT NULL AND ends_at<=?", (now,)
        ).fetchall()
    return [r["id"] for r in rows]


# ── AI usage ────────────────────────────────────────────────

def log_ai_use(user_id: int) -> None:
    with connect() as con:
        con.execute("INSERT INTO ai_usage(user_id, used_at) VALUES(?, ?)", (user_id, now_iso()))
        con.execute("DELETE FROM ai_usage WHERE used_at < datetime('now', '-3 days')")


def ai_usage_since(since_iso: str, user_id: Optional[int] = None) -> int:
    with connect() as con:
        if user_id is None:
            row = con.execute("SELECT COUNT(*) AS n FROM ai_usage WHERE used_at>=?", (since_iso,)).fetchone()
        else:
            row = con.execute("SELECT COUNT(*) AS n FROM ai_usage WHERE used_at>=? AND user_id=?",
                              (since_iso, user_id)).fetchone()
    return int(row["n"])


def last_ai_use(user_id: int) -> Optional[str]:
    with connect() as con:
        row = con.execute("SELECT MAX(used_at) AS t FROM ai_usage WHERE user_id=?", (user_id,)).fetchone()
    return row["t"] if row else None
