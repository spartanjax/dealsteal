from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from dealsteal import store
from dealsteal.models import Listing, RunStats, Verdict

NOW = datetime(2026, 11, 11, 12, 0, tzinfo=timezone.utc)


def make(listing_id: str = "1", price: float | None = 200.0, **overrides) -> Listing:
    base = dict(
        source="trademe",
        listing_id=listing_id,
        title=f"Standing desk {listing_id}",
        url=f"https://example.test/{listing_id}",
        price=price,
        region="Auckland",
        listed_at=NOW,
        ends_at=NOW + timedelta(days=3),
    )
    base.update(overrides)
    return Listing(**base)


@pytest.fixture()
def conn(tmp_path):
    connection = store.connect(tmp_path / "test.db")
    yield connection
    connection.close()


def test_schema_is_idempotent(tmp_path):
    path = tmp_path / "twice.db"
    store.connect(path).close()
    store.connect(path).close()


def test_unseen_detection_across_runs(conn):
    assert store.known_keys(conn, "standing desk") == set()
    store.record_seen(conn, "standing desk", [make("1"), make("2")], NOW)
    assert store.known_keys(conn, "standing desk") == {
        ("trademe", "1"),
        ("trademe", "2"),
    }


def test_same_listing_is_tracked_separately_per_item(conn):
    store.record_seen(conn, "standing desk", [make("1")], NOW)
    assert store.known_keys(conn, "road bike") == set()


def test_record_seen_updates_price_and_last_seen(conn):
    store.record_seen(conn, "desk", [make("1", price=300.0)], NOW)
    store.record_seen(conn, "desk", [make("1", price=250.0)], NOW)
    row = conn.execute("SELECT price FROM listings WHERE listing_id = '1'").fetchone()
    assert row["price"] == 250.0


def test_price_history_ignores_unchanged_prices(conn):
    store.record_prices(conn, "desk", [make("1", price=300.0)], NOW)
    store.record_prices(conn, "desk", [make("1", price=300.0)], NOW)
    store.record_prices(conn, "desk", [make("1", price=280.0)], NOW)
    rows = conn.execute("SELECT price FROM price_history ORDER BY id").fetchall()
    assert [r["price"] for r in rows] == [300.0, 280.0]


def test_median_price(conn):
    store.record_prices(
        conn, "desk", [make("a", price=100.0), make("b", price=200.0), make("c", price=300.0)]
    )
    assert store.median_price(conn, "desk") == 200.0


def test_median_price_excludes_current_batch(conn):
    store.record_prices(conn, "desk", [make("a", price=100.0), make("b", price=200.0)])
    store.record_prices(conn, "desk", [make("c", price=1000.0)])
    assert store.median_price(conn, "desk", exclude={("trademe", "c")}) == 150.0


def test_median_price_empty(conn):
    assert store.median_price(conn, "nothing") is None


def test_save_and_read_verdicts(conn):
    verdict = Verdict(
        listing=make("1"),
        item="desk",
        score=88,
        deal=True,
        reason="Cheap versus median",
        red_flags=["single photo"],
        max_offer=180.0,
    )
    store.save_verdicts(conn, [verdict], NOW)
    row = conn.execute("SELECT * FROM verdicts WHERE listing_id = '1'").fetchone()
    assert row["score"] == 88
    assert row["deal"] == 1
    assert row["red_flags"] == "single photo"
    assert row["max_offer"] == 180.0


def test_run_lifecycle(conn):
    run_id = store.start_run(conn, NOW)
    assert store.pending_runs(conn) == 1
    stats = RunStats(items=2, fetched=10, filtered=6, unseen=3, judged=3, notified=1)
    store.finish_run(conn, run_id, stats, "notified", "dealsteal: 1 deal")
    assert store.pending_runs(conn) == 0
    row = conn.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
    assert row["status"] == "notified"
    assert row["unseen"] == 3
    assert row["finished_at"] is not None
