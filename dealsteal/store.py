"""SQLite persistence: what we have seen, what it cost, how it was judged."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from .models import Listing, RunStats, Verdict

SCHEMA = """
CREATE TABLE IF NOT EXISTS listings (
    source        TEXT NOT NULL,
    listing_id    TEXT NOT NULL,
    item          TEXT NOT NULL,
    title         TEXT NOT NULL,
    url           TEXT NOT NULL,
    price         REAL,
    region        TEXT,
    listed_at     TEXT,
    ends_at       TEXT,
    first_seen_at TEXT NOT NULL,
    last_seen_at  TEXT NOT NULL,
    PRIMARY KEY (source, listing_id, item)
);
CREATE INDEX IF NOT EXISTS idx_listings_first_seen ON listings (first_seen_at);

CREATE TABLE IF NOT EXISTS price_history (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    source    TEXT NOT NULL,
    listing_id TEXT NOT NULL,
    item      TEXT NOT NULL,
    price     REAL NOT NULL,
    seen_at   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_price_item ON price_history (item, seen_at);

CREATE TABLE IF NOT EXISTS verdicts (
    source     TEXT NOT NULL,
    listing_id TEXT NOT NULL,
    item       TEXT NOT NULL,
    score      INTEGER,
    deal       INTEGER NOT NULL,
    reason     TEXT,
    red_flags  TEXT,
    max_offer  REAL,
    judged_at  TEXT NOT NULL,
    PRIMARY KEY (source, listing_id, item)
);

CREATE TABLE IF NOT EXISTS runs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at  TEXT NOT NULL,
    finished_at TEXT,
    status      TEXT NOT NULL,
    items       INTEGER NOT NULL DEFAULT 0,
    fetched     INTEGER NOT NULL DEFAULT 0,
    filtered    INTEGER NOT NULL DEFAULT 0,
    unseen      INTEGER NOT NULL DEFAULT 0,
    judged      INTEGER NOT NULL DEFAULT 0,
    notified    INTEGER NOT NULL DEFAULT 0,
    detail      TEXT
);
"""


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def iso(moment: datetime | None) -> str | None:
    return moment.isoformat() if moment else None


def connect(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    return conn


def known_keys(conn: sqlite3.Connection, item: str) -> set[tuple[str, str]]:
    rows = conn.execute(
        "SELECT source, listing_id FROM listings WHERE item = ?", (item,)
    ).fetchall()
    return {(row["source"], row["listing_id"]) for row in rows}


def record_seen(
    conn: sqlite3.Connection,
    item: str,
    listings: list[Listing],
    seen_at: datetime | None = None,
) -> None:
    seen_at = seen_at or utcnow()
    stamp = iso(seen_at)
    conn.executemany(
        """
        INSERT INTO listings (
            source, listing_id, item, title, url, price, region,
            listed_at, ends_at, first_seen_at, last_seen_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT (source, listing_id, item) DO UPDATE SET
            last_seen_at = excluded.last_seen_at,
            price = excluded.price
        """,
        [
            (
                listing.source,
                listing.listing_id,
                item,
                listing.title,
                listing.url,
                listing.price,
                listing.region,
                iso(listing.listed_at),
                iso(listing.ends_at),
                stamp,
                stamp,
            )
            for listing in listings
        ],
    )
    conn.commit()


def record_prices(
    conn: sqlite3.Connection,
    item: str,
    listings: list[Listing],
    seen_at: datetime | None = None,
) -> None:
    """Append a price point, but only when it actually changed."""
    seen_at = seen_at or utcnow()
    for listing in listings:
        if listing.price is None:
            continue
        last = conn.execute(
            """
            SELECT price FROM price_history
            WHERE source = ? AND listing_id = ? AND item = ?
            ORDER BY id DESC LIMIT 1
            """,
            (listing.source, listing.listing_id, item),
        ).fetchone()
        if last is not None and abs(last["price"] - listing.price) < 0.01:
            continue
        conn.execute(
            """
            INSERT INTO price_history (source, listing_id, item, price, seen_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (listing.source, listing.listing_id, item, listing.price, iso(seen_at)),
        )
    conn.commit()


def median_price(
    conn: sqlite3.Connection,
    item: str,
    exclude: set[tuple[str, str]] | None = None,
    limit: int = 200,
) -> float | None:
    """Median of recently observed prices for this item, ignoring today's batch.

    Used to tell the judge whether something is actually cheap rather than
    making it guess from vibes.
    """
    rows = conn.execute(
        """
        SELECT source, listing_id, price FROM price_history
        WHERE item = ?
        ORDER BY id DESC LIMIT ?
        """,
        (item, limit * 4),
    ).fetchall()
    exclude = exclude or set()
    prices: list[float] = []
    seen: set[tuple[str, str]] = set()
    for row in rows:
        key = (row["source"], row["listing_id"])
        if key in exclude or key in seen:
            continue
        seen.add(key)
        prices.append(row["price"])
        if len(prices) >= limit:
            break
    if not prices:
        return None
    prices.sort()
    middle = len(prices) // 2
    if len(prices) % 2:
        return prices[middle]
    return (prices[middle - 1] + prices[middle]) / 2


def save_verdicts(
    conn: sqlite3.Connection,
    verdicts: list[Verdict],
    judged_at: datetime | None = None,
) -> None:
    judged_at = judged_at or utcnow()
    conn.executemany(
        """
        INSERT INTO verdicts (
            source, listing_id, item, score, deal, reason, red_flags,
            max_offer, judged_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT (source, listing_id, item) DO UPDATE SET
            score = excluded.score,
            deal = excluded.deal,
            reason = excluded.reason,
            red_flags = excluded.red_flags,
            max_offer = excluded.max_offer,
            judged_at = excluded.judged_at
        """,
        [
            (
                v.listing.source,
                v.listing.listing_id,
                v.item,
                v.score,
                1 if v.deal else 0,
                v.reason,
                "\n".join(v.red_flags),
                v.max_offer,
                iso(judged_at),
            )
            for v in verdicts
        ],
    )
    conn.commit()


def start_run(conn: sqlite3.Connection, started_at: datetime | None = None) -> int:
    cur = conn.execute(
        "INSERT INTO runs (started_at, status) VALUES (?, 'running')",
        (iso(started_at or utcnow()),),
    )
    conn.commit()
    return int(cur.lastrowid)


def finish_run(
    conn: sqlite3.Connection,
    run_id: int,
    stats: RunStats,
    status: str,
    detail: str | None = None,
    finished_at: datetime | None = None,
) -> None:
    conn.execute(
        """
        UPDATE runs SET finished_at = ?, status = ?, items = ?, fetched = ?,
            filtered = ?, unseen = ?, judged = ?, notified = ?, detail = ?
        WHERE id = ?
        """,
        (
            iso(finished_at or utcnow()),
            status,
            stats.items,
            stats.fetched,
            stats.filtered,
            stats.unseen,
            stats.judged,
            stats.notified,
            detail,
            run_id,
        ),
    )
    conn.commit()


def pending_runs(conn: sqlite3.Connection) -> int:
    return int(
        conn.execute(
            "SELECT COUNT(*) AS n FROM runs WHERE status = 'running'"
        ).fetchone()["n"]
    )
