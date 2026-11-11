"""Normalised types shared by every source and by the judge."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any


def as_price(value: Any) -> float | None:
    """Coerce a Trade Me price field to a float, treating 0 as "no price".

    Auctions with no bids report a start price of 0, and classifieds with
    "price by negotiation" have nothing useful at all. Both become None so
    the filters can tell "cheap" apart from "unknown".
    """
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


@dataclass(frozen=True)
class Listing:
    """One marketplace listing, already normalised across sources."""

    source: str
    listing_id: str
    title: str
    url: str
    price: float | None = None
    buy_now_price: float | None = None
    start_price: float | None = None
    price_display: str | None = None
    region: str | None = None
    suburb: str | None = None
    category: str | None = None
    category_path: str | None = None
    listed_at: datetime | None = None
    ends_at: datetime | None = None
    bid_count: int = 0
    photo_count: int = 0
    is_classified: bool = False
    is_buy_now_only: bool = False
    raw: dict[str, Any] = field(default_factory=dict, compare=False, repr=False)

    @property
    def key(self) -> tuple[str, str]:
        return (self.source, self.listing_id)

    def age_minutes(self, now: datetime | None = None) -> int | None:
        if self.listed_at is None:
            return None
        now = now or datetime.now(timezone.utc)
        return max(0, int((now - self.listed_at).total_seconds() // 60))

    def expires_in_hours(self, now: datetime | None = None) -> int | None:
        if self.ends_at is None:
            return None
        now = now or datetime.now(timezone.utc)
        return int((self.ends_at - now).total_seconds() // 3600)

    def to_prompt_dict(self, now: datetime | None = None) -> dict[str, Any]:
        """Compact representation handed to the LLM. Keeps tokens down."""
        data: dict[str, Any] = {
            "listing_id": self.listing_id,
            "title": self.title,
            "price": self.price,
            "price_display": self.price_display,
            "region": self.region,
            "category": self.category_path or self.category,
            "age_minutes": self.age_minutes(now),
            "expires_in_hours": self.expires_in_hours(now),
            "bids": self.bid_count,
            "photos": self.photo_count,
            "classified": self.is_classified,
        }
        if self.suburb:
            data["suburb"] = self.suburb
        if self.buy_now_price is not None and self.buy_now_price != self.price:
            data["buy_now"] = self.buy_now_price
        return data


@dataclass
class Verdict:
    """The judge's opinion of one listing for one wishlist item."""

    listing: Listing
    item: str
    score: int | None = None
    deal: bool = False
    reason: str = ""
    red_flags: list[str] = field(default_factory=list)
    max_offer: float | None = None
    error: str | None = None

    def is_worth_notifying(self, min_score: int) -> bool:
        if self.error:
            return False
        if not self.deal:
            return False
        return (self.score or 0) >= min_score


@dataclass
class RunStats:
    """Counters carried through one invocation and written to the runs table."""

    items: int = 0
    fetched: int = 0
    filtered: int = 0
    unseen: int = 0
    judged: int = 0
    notified: int = 0
    source_errors: int = 0
