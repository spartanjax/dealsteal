from __future__ import annotations

from datetime import datetime, timedelta, timezone

from dealsteal.config import WishItem
from dealsteal.filters import cap, filter_listings, rank
from dealsteal.models import Listing

NOW = datetime(2026, 11, 11, 12, 0, tzinfo=timezone.utc)

ITEM = WishItem(
    name="standing desk",
    query="standing desk",
    max_price=400,
    min_price=50,
    regions=("Auckland",),
    exclude_terms=("wanted", "parts only", "faulty"),
    must_match_any=("desk",),
)


def make(**overrides) -> Listing:
    base = dict(
        source="trademe",
        listing_id="1",
        title="Electric standing desk 1200mm",
        url="https://example.test/1",
        price=200.0,
        region="Auckland",
        listed_at=NOW - timedelta(minutes=10),
        ends_at=NOW + timedelta(days=3),
    )
    base.update(overrides)
    return Listing(**base)


def reasons(dropped) -> list[str]:
    return [drop.reason for drop in dropped]


def test_keeps_a_normal_listing():
    kept, dropped = filter_listings([make()], ITEM, NOW)
    assert len(kept) == 1
    assert dropped == []


def test_drops_over_price_cap():
    kept, dropped = filter_listings([make(price=750.0)], ITEM, NOW)
    assert kept == []
    assert "over cap" in reasons(dropped)[0]


def test_keeps_unknown_price_when_no_floor():
    item = WishItem(name="desk", query="desk", max_price=400, regions=("Auckland",))
    kept, _ = filter_listings([make(price=None)], item, NOW)
    assert len(kept) == 1


def test_drops_below_floor():
    kept, dropped = filter_listings([make(price=20.0)], ITEM, NOW)
    assert kept == []
    assert "under floor" in reasons(dropped)[0]


def test_drops_excluded_term_on_word_boundary():
    kept, dropped = filter_listings([make(title="Wanted: standing desk")], ITEM, NOW)
    assert kept == []
    assert "excluded term" in reasons(dropped)[0]


def test_does_not_match_substring_of_excluded_term():
    kept, _ = filter_listings([make(title="Unwanted gift standing desk")], ITEM, NOW)
    assert len(kept) == 1


def test_drops_multiword_excluded_term():
    kept, dropped = filter_listings(
        [make(title="Standing desk for parts only")], ITEM, NOW
    )
    assert kept == []
    assert "excluded term" in reasons(dropped)[0]


def test_drops_missing_required_keyword():
    kept, dropped = filter_listings([make(title="Standing lamp with shade")], ITEM, NOW)
    assert kept == []
    assert "missing required keyword" in reasons(dropped)[0]


def test_drops_wrong_region():
    kept, dropped = filter_listings([make(region="Wellington")], ITEM, NOW)
    assert kept == []
    assert "wrong region" in reasons(dropped)[0]


def test_region_match_is_case_insensitive():
    item = WishItem(name="desk", query="desk", regions=("auckland",))
    kept, _ = filter_listings([make(region="Auckland")], item, NOW)
    assert len(kept) == 1


def test_drops_closed_listing():
    kept, dropped = filter_listings(
        [make(ends_at=NOW - timedelta(minutes=1))], ITEM, NOW
    )
    assert kept == []
    assert reasons(dropped)[0] == "already closed"


def test_drops_blank_title():
    kept, _ = filter_listings([make(title="   ")], ITEM, NOW)
    assert kept == []


def test_rank_puts_newest_first_then_cheapest():
    old_cheap = make(listing_id="old", listed_at=NOW - timedelta(hours=5), price=100.0)
    new_cheap = make(listing_id="new", listed_at=NOW - timedelta(minutes=5), price=300.0)
    new_other = make(listing_id="new2", listed_at=NOW - timedelta(minutes=5), price=200.0)
    assert [l.listing_id for l in rank([old_cheap, new_cheap, new_other], NOW)] == [
        "new2",
        "new",
        "old",
    ]


def test_cap_reports_how_many_were_held_back():
    listings = [
        make(listing_id=str(i), listed_at=NOW - timedelta(minutes=i)) for i in range(5)
    ]
    kept, held_back = cap(listings, 2, NOW)
    assert len(kept) == 2
    assert held_back == 3
    assert kept[0].listing_id == "0"


def test_cap_with_no_limit_keeps_everything():
    listings = [make(listing_id=str(i)) for i in range(3)]
    kept, held_back = cap(listings, 0, NOW)
    assert len(kept) == 3
    assert held_back == 0
