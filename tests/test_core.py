"""Offline tests for the database and scrapers (no Discord or internet needed).

Run from the project folder:  python -m unittest discover tests
"""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
_tmp = tempfile.mkdtemp()
os.environ["ANTON_DB_PATH"] = os.path.join(_tmp, "test.db")

from core import config, db  # noqa: E402
from scrapers import freegames, hpb, instocktrades  # noqa: E402


def item(key, price, avail=True, title=None, msrp=2499):
    return {"key": key, "title": title or f"Batman Vol {key}", "url": f"https://x/{key}",
            "price_cents": price, "msrp_cents": msrp, "available": avail, "extra": {}}


def kinds(res):
    return sorted((e.kind, e.item["key"]) for e in res.events)


class DiffTests(unittest.TestCase):
    def setUp(self):
        if config.DB_PATH.exists():
            config.DB_PATH.unlink()
        db.init()

    def test_first_run_is_quiet(self):
        res = db.apply_scrape("f", [item("1", 1000), item("2", 1500)])
        self.assertTrue(res.seeded_now)
        self.assertEqual(res.events, [])

    def test_changes(self):
        db.apply_scrape("f", [item("1", 1000), item("2", 1500)])
        res = db.apply_scrape("f", [item("1", 900), item("2", 1500, avail=False), item("3", 500)])
        self.assertEqual(kinds(res), [("new", "3"), ("oos", "2"), ("price_drop", "1")])
        res = db.apply_scrape("f", [item("1", 950), item("2", 1500, avail=True), item("3", 500)])
        self.assertEqual(kinds(res), [("price_up", "1"), ("restock", "2")])

    def test_removed_and_back(self):
        db.apply_scrape("f", [item("1", 1000), item("2", 1500)])
        self.assertEqual(kinds(db.apply_scrape("f", [item("1", 1000)])), [("removed", "2")])
        self.assertEqual(kinds(db.apply_scrape("f", [item("1", 1000), item("2", 1500)])), [("back", "2")])

    def test_broken_scrape_does_not_wipe_listing(self):
        db.apply_scrape("f", [item(str(i), 1000) for i in range(40)])
        res = db.apply_scrape("f", [item("1", 1000)])  # site half-loaded
        self.assertTrue(res.removal_skipped)
        self.assertEqual(res.events, [])
        self.assertTrue(db.apply_scrape("f", []).removal_skipped)

    def test_history_and_search(self):
        db.apply_scrape("f", [item("2", 1500, title="Batman by James Tynion IV Vol. 2")])
        db.apply_scrape("f", [item("2", 1200, title="Batman by James Tynion IV Vol. 2")])
        hits = db.search_items("Batman by James Tynion volume 2")
        self.assertEqual(hits[0]["key"], "2")
        self.assertEqual(hits[0]["price_cents"], 1200)
        self.assertEqual([h["price_cents"] for h in db.price_history("f", "2")], [1500, 1200])
        self.assertEqual(db.search_items("batmn tynion")[0]["key"], "2")  # typo falls back to fuzzy

    def test_watches(self):
        self.assertTrue(db.watch_matches("saga compendium", "Saga Compendium Vol 1 TP"))
        self.assertTrue(db.watch_matches("batman vol 2", "Batman Volume 2"))
        self.assertFalse(db.watch_matches("saga", "Batman Vol 2"))
        self.assertTrue(db.add_watch(1, 2, 3, "Saga", 2500))
        self.assertFalse(db.add_watch(1, 2, 3, "saga", None))  # same keyword updates
        self.assertEqual(len(db.list_watches(1)), 1)
        self.assertTrue(db.remove_watch(1, "SAGA"))

    def test_polls(self):
        pid = db.create_poll(1, 2, 3, "Best?", ["A", "B"], None)
        self.assertEqual(db.cast_vote(pid, 10, 0), "added")
        self.assertEqual(db.cast_vote(pid, 10, 1), "changed")
        self.assertEqual(db.cast_vote(pid, 11, 1), "added")
        self.assertEqual(db.get_poll(pid)["counts"], [0, 2])
        self.assertEqual(db.cast_vote(pid, 11, 1), "removed")
        self.assertEqual(db.get_poll(pid)["counts"], [0, 1])

    def test_birthday_migration(self):
        real_data_dir = config.DATA_DIR
        config.DATA_DIR = Path(_tmp)
        try:
            (config.DATA_DIR / "birthdays.json").write_text(json.dumps({"42": {"month": 3, "day": 9, "year": None}}))
            self.assertEqual(db.migrate_birthdays_json(), 1)
            self.assertEqual(db.migrate_birthdays_json(), 0)  # only once
            self.assertEqual(db.all_birthdays()[0]["month"], 3)
        finally:
            config.DATA_DIR = real_data_dir

    def test_prices(self):
        self.assertEqual(db.price_to_cents("$1,234.56"), 123456)
        self.assertEqual(db.price_to_cents("$12"), 1200)
        self.assertIsNone(db.price_to_cents("free"))


IST_HTML = """<div class=grid>
<div class=item><a href="/products/AUG220001/batman-vol-2?ref=1"><img data-src="/img/b2.jpg"></a>
 <a href="/products/AUG220001/batman-vol-2">Batman by James Tynion Vol 2</a>
 <div>MSRP: $24.99</div><div>$12.49</div><div>50% Off!</div><button>Add to Cart</button></div>
<div class=item><a href="/products/X1/saga">Saga Compendium</a><div>$59.99</div><span>Out of Stock</span></div>
</div>"""


class ParserTests(unittest.TestCase):
    def test_instocktrades(self):
        items = instocktrades.parse_page(IST_HTML, "https://www.instocktrades.com/damages")
        self.assertEqual(len(items), 2)
        b, s = items
        self.assertEqual((b["price_cents"], b["msrp_cents"], b["available"]), (1249, 2499, True))
        self.assertEqual(b["image"], "https://www.instocktrades.com/img/b2.jpg")
        self.assertEqual(b["key"], "/products/AUG220001/batman-vol-2")
        self.assertEqual((s["price_cents"], s["msrp_cents"], s["available"]), (5999, None, False))

    def test_hpb(self):
        html = """<div class="product" data-pid="1"><div class="image-container"><a href="/omni-1?x=1">
        <img class="tile-image" src="https://img/normal/9781302900000/x.jpg"></a></div>
        <a class="link">X-Men Omnibus</a><div class="price"><span class="sales">$40.99</span>
        <span class="sales">$75.00</span></div></div>"""
        it = hpb.parse_page(html, "https://www.hpb.com/books/trending-topics/omnibuses")[0]
        self.assertEqual((it["title"], it["price_cents"]), ("X-Men Omnibus", 4099))
        self.assertEqual(it["extra"]["isbn"], "9781302900000")

    def test_gamerpower(self):
        class Resp:
            def __init__(self, data): self.data = data
            def raise_for_status(self): pass
            def json(self): return self.data

        class Session:
            def get(self, url, timeout=None):
                return Resp([{"id": 7, "title": "Cool Game (Steam) Giveaway", "worth": "$19.99",
                              "open_giveaway_url": "https://gp/7", "platforms": "PC, Steam",
                              "end_date": "2026-11-13 23:59:00", "image": "https://img/7.jpg"}])

        g = freegames.fetch_gamerpower(Session())[0]
        self.assertEqual((g["key"], g["price_cents"], g["msrp_cents"]), ("gamerpower:7", 0, 1999))
        self.assertEqual(g["extra"]["end_date"], "2026-11-13T23:59:00+00:00")


if __name__ == "__main__":
    unittest.main()
