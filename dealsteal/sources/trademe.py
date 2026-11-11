"""Trade Me general search client.

Endpoint: GET https://api.trademe.co.nz/v1/Search/General.json
Auth: OAuth 1.0a, four credentials, required (search is a protected endpoint).
Notes verified against the API reference:
  - there is no price filter, so price caps are applied client-side
  - `user_region` takes a numeric region ID, so region matching uses the
    `Region` field on each result instead
  - `sort_order=ExpiryDesc` is the "newest listings" sort (there is no "Latest")
"""

from __future__ import annotations

import logging
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

import requests
from requests_oauthlib import OAuth1

from ..config import Secrets, Settings, WishItem
from ..models import Listing, as_price

log = logging.getLogger(__name__)

BASE_URL = "https://api.trademe.co.nz/v1"
SORT_NEWEST = "ExpiryDesc"

_DOTNET_DATE = re.compile(r"/Date\((-?\d+)([+-]\d{4})?\)/")


class TradeMeError(RuntimeError):
    """A search failed in a way the caller should report, not retry forever."""


def parse_datetime(value: Any) -> datetime | None:
    """Trade Me mixes .NET `/Date(ms)/` strings with ISO 8601."""
    if not value or not isinstance(value, str):
        return None
    match = _DOTNET_DATE.fullmatch(value.strip())
    if match:
        return datetime.fromtimestamp(int(match.group(1)) / 1000, tz=timezone.utc)
    text = value.strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def map_listing(raw: dict[str, Any], settings: Settings) -> Listing | None:
    """Map one Trade Me search result to a Listing, or None if unusable."""
    listing_id = raw.get("ListingId")
    title = (raw.get("Title") or "").strip()
    if listing_id in (None, "", 0) or not title:
        return None

    buy_now = as_price(raw.get("BuyNowPrice"))
    start = as_price(raw.get("StartPrice"))
    pickup = raw.get("PickupLocality") or {}
    photos = raw.get("PhotoUrls") or []

    return Listing(
        source="trademe",
        listing_id=str(listing_id),
        title=title,
        url=settings.url_template.format(listing_id=listing_id),
        price=buy_now or start,
        buy_now_price=buy_now,
        start_price=start,
        price_display=raw.get("PriceDisplay"),
        region=raw.get("Region") or pickup.get("Region"),
        suburb=raw.get("Suburb") or pickup.get("Suburb"),
        category_path=raw.get("CategoryPath"),
        listed_at=parse_datetime(raw.get("StartDate")),
        ends_at=parse_datetime(raw.get("EndDate")),
        bid_count=int(raw.get("BidCount") or 0),
        photo_count=len(photos) if isinstance(photos, list) else 0,
        is_classified=bool(raw.get("IsClassified")),
        is_buy_now_only=bool(raw.get("IsBuyNowOnly")),
        raw=raw,
    )


class TradeMeClient:
    def __init__(
        self,
        secrets: Secrets,
        settings: Settings,
        session: requests.Session | None = None,
        timeout: float = 30.0,
        max_attempts: int = 3,
    ) -> None:
        self._settings = settings
        self._timeout = timeout
        self._max_attempts = max_attempts
        self._session = session or requests.Session()
        self._session.auth = OAuth1(
            secrets.trademe_consumer_key,
            secrets.trademe_consumer_secret,
            secrets.trademe_token,
            secrets.trademe_token_secret,
        )
        self._session.headers.update(
            {"Accept": "application/json", "User-Agent": "dealsteal/0.1"}
        )

    def build_params(self, item: WishItem) -> dict[str, Any]:
        params: dict[str, Any] = {
            "search_string": item.query,
            "sort_order": SORT_NEWEST,
            "rows": self._settings.rows_per_search,
            "page": 1,
            "return_24_hrs_aggregation": "true",
        }
        if item.condition and item.condition.lower() in {"new", "used"}:
            params["condition"] = item.condition.capitalize()
        if item.listed_as and item.listed_as.lower() in {"auctions", "classifieds"}:
            params["listed_as"] = item.listed_as.capitalize()
        return params

    def search(self, item: WishItem) -> list[Listing]:
        params = self.build_params(item)
        payload = self._get("/Search/General.json", params)
        results = payload.get("List") or []
        if payload.get("TotalCountTruncated"):
            log.warning(
                "%s: Trade Me truncated results beyond %s",
                item.name,
                payload.get("TotalCount"),
            )
        listings = [map_listing(raw, self._settings) for raw in results]
        mapped = [listing for listing in listings if listing is not None]
        log.info(
            "%s: %s results (total %s)",
            item.name,
            len(mapped),
            payload.get("TotalCount"),
        )
        return mapped

    def _get(self, path: str, params: dict[str, Any]) -> dict[str, Any]:
        url = f"{BASE_URL}{path}"
        last_error = "unknown error"
        for attempt in range(1, self._max_attempts + 1):
            try:
                response = self._session.get(url, params=params, timeout=self._timeout)
            except requests.RequestException as exc:
                last_error = f"network error: {exc.__class__.__name__}"
            else:
                if response.status_code == 200:
                    try:
                        return response.json()
                    except ValueError as exc:
                        raise TradeMeError(f"invalid JSON from {url}: {exc}") from exc
                if response.status_code in (401, 403):
                    raise TradeMeError(
                        f"auth rejected ({response.status_code}) - check the four "
                        "TRADEME_* credentials and that the app is still authorised"
                    )
                if response.status_code == 429:
                    last_error = "rate limited (429)"
                elif response.status_code >= 500:
                    last_error = f"server error ({response.status_code})"
                else:
                    raise TradeMeError(
                        f"unexpected status {response.status_code} from {url}"
                    )
            if attempt < self._max_attempts:
                time.sleep(2**attempt)
        raise TradeMeError(f"gave up after {self._max_attempts} attempts: {last_error}")


def listings_from_fixture(path: Path, settings: Settings) -> list[Listing]:
    """Offline mode: read a recorded Search/General response."""
    import json

    payload = json.loads(path.read_text(encoding="utf-8"))
    results: Iterable[dict[str, Any]] = payload.get("List") or []
    return [
        listing
        for listing in (map_listing(raw, settings) for raw in results)
        if listing is not None
    ]
