from __future__ import annotations

from datetime import datetime, timedelta, timezone

from dealsteal.models import Listing, RunStats, Verdict
from dealsteal.notify import (
    MAX_BODY_CHARS,
    build_failure_notification,
    build_notification,
)

NOW = datetime(2026, 11, 11, 12, 0, tzinfo=timezone.utc)


def verdict(
    listing_id: str = "1",
    item: str = "standing desk",
    score: int | None = 80,
    deal: bool = True,
    price: float | None = 200.0,
    reason: str = "Cheap versus the recent median",
    age_minutes: int = 25,
    **overrides,
) -> Verdict:
    listing = Listing(
        source="trademe",
        listing_id=listing_id,
        title=f"Electric standing desk {listing_id}",
        url=f"https://example.test/{listing_id}",
        price=price,
        region="Auckland",
        listed_at=NOW - timedelta(minutes=age_minutes),
        ends_at=NOW + timedelta(days=2),
    )
    return Verdict(
        listing=listing,
        item=item,
        score=score,
        deal=deal,
        reason=reason,
        **overrides,
    )


def test_no_deals_means_no_notification():
    assert build_notification([], RunStats(), NOW) is None


def test_title_names_the_item_for_a_single_deal():
    notification = build_notification([verdict()], RunStats(unseen=1), NOW)
    assert notification is not None
    assert notification.title == "dealsteal: 1 deal for standing desk"


def test_title_counts_multiple_items():
    notification = build_notification(
        [verdict(item="standing desk"), verdict(listing_id="2", item="road bike")],
        RunStats(),
        NOW,
    )
    assert notification is not None
    assert notification.title == "dealsteal: 2 deals"


def test_body_carries_the_details_needed_to_act():
    notification = build_notification([verdict()], RunStats(unseen=3, fetched=40), NOW)
    assert notification is not None
    body = notification.body
    assert "$200" in body
    assert "Electric standing desk 1" in body
    assert "https://example.test/1" in body
    assert "score 80" in body
    assert "25m old" in body
    assert "3 new" in body
    assert notification.click == "https://example.test/1"


def test_high_score_raises_priority():
    notification = build_notification([verdict(score=92)], RunStats(), NOW)
    assert notification is not None
    assert notification.priority == "high"
    assert notification.tags == "fire"


def test_ordinary_score_is_normal_priority():
    notification = build_notification([verdict(score=70)], RunStats(), NOW)
    assert notification is not None
    assert notification.priority == "default"


def test_deals_are_sorted_by_score():
    notification = build_notification(
        [verdict(listing_id="low", score=61), verdict(listing_id="high", score=95)],
        RunStats(),
        NOW,
    )
    assert notification is not None
    assert notification.body.index("desk high") < notification.body.index("desk low")


def test_red_flags_and_max_offer_are_shown():
    notification = build_notification(
        [verdict(red_flags=["single photo"], max_offer=150.0)], RunStats(), NOW
    )
    assert notification is not None
    assert "single photo" in notification.body
    assert "max offer: $150" in notification.body


def test_unknown_price_is_described_not_guessed():
    notification = build_notification([verdict(price=None)], RunStats(), NOW)
    assert notification is not None
    assert "price unknown" in notification.body


def test_held_back_listings_are_reported():
    notification = build_notification([verdict()], RunStats(), NOW, held_back=4)
    assert notification is not None
    assert "4 held back" in notification.body


def test_body_is_truncated_to_ntfy_limit():
    deals = [
        verdict(listing_id=str(i), item=f"item {i}", reason="x" * 300)
        for i in range(40)
    ]
    notification = build_notification(deals, RunStats(), NOW)
    assert notification is not None
    assert len(notification.body) <= MAX_BODY_CHARS
    assert notification.body.rstrip().endswith("(truncated)")


def test_only_first_five_per_item_are_listed():
    deals = [verdict(listing_id=str(i)) for i in range(8)]
    notification = build_notification(deals, RunStats(), NOW)
    assert notification is not None
    assert "plus 3 more below the cut" in notification.body


def test_failure_notification_is_low_priority():
    notification = build_failure_notification(
        RunStats(unseen=7), "the judge errored (timeout)", [verdict()]
    )
    assert notification.title == "dealsteal: judge unavailable"
    assert notification.priority == "low"
    assert "7 new listings were not screened" in notification.body
    assert "https://example.test/1" in notification.body
