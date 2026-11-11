"""Cheap, deterministic rules. Everything here runs before the LLM.

Keeping this stage rule-based means a bad prompt can never let an obviously
wrong listing through, and "why was this skipped?" is always answerable.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone

from .config import WishItem
from .models import Listing

WORD_BOUNDARY = r"(?<![a-z0-9])"


@dataclass(frozen=True)
class Drop:
    listing: Listing
    reason: str


def _contains_any(haystack: str, needles: tuple[str, ...]) -> str | None:
    for needle in needles:
        if not needle:
            continue
        pattern = WORD_BOUNDARY + re.escape(needle.lower())
        if re.search(pattern, haystack):
            return needle
    return None


def filter_listings(
    listings: list[Listing],
    item: WishItem,
    now: datetime | None = None,
) -> tuple[list[Listing], list[Drop]]:
    """Apply the wishlist item's hard constraints."""
    now = now or datetime.now(timezone.utc)
    kept: list[Listing] = []
    dropped: list[Drop] = []

    for listing in listings:
        haystack = listing.title.lower()

        if len(haystack.strip()) < 4:
            dropped.append(Drop(listing, "title too short"))
            continue

        if listing.ends_at is not None and listing.ends_at <= now:
            dropped.append(Drop(listing, "already closed"))
            continue

        excluded = _contains_any(haystack, item.exclude_terms)
        if excluded:
            dropped.append(Drop(listing, f"excluded term: {excluded}"))
            continue

        if item.must_match_any:
            if not _contains_any(haystack, item.must_match_any):
                dropped.append(Drop(listing, "missing required keyword"))
                continue

        if item.max_price is not None and listing.price is not None:
            if listing.price > item.max_price:
                dropped.append(
                    Drop(listing, f"over cap ({listing.price:g} > {item.max_price:g})")
                )
                continue

        if item.min_price is not None and listing.price is not None:
            if listing.price < item.min_price:
                dropped.append(
                    Drop(listing, f"under floor ({listing.price:g} < {item.min_price:g})")
                )
                continue

        if not item.matches_region(listing.region, listing.suburb):
            dropped.append(Drop(listing, f"wrong region: {listing.region or 'unknown'}"))
            continue

        kept.append(listing)

    return kept, dropped


def rank(listings: list[Listing], now: datetime | None = None) -> list[Listing]:
    """Newest first, then cheapest. Newest matters because deals get taken."""
    now = now or datetime.now(timezone.utc)

    def sort_key(listing: Listing) -> tuple[int, float]:
        age = listing.age_minutes(now)
        return (age if age is not None else 10**6, listing.price or float("inf"))

    return sorted(listings, key=sort_key)


def cap(
    listings: list[Listing], limit: int, now: datetime | None = None
) -> tuple[list[Listing], int]:
    """Trim to the LLM budget, reporting how many were held back."""
    ordered = rank(listings, now)
    if limit <= 0 or len(ordered) <= limit:
        return ordered, 0
    return ordered[:limit], len(ordered) - limit
