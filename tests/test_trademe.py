from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from dealsteal.config import Secrets, Settings, WishItem
from dealsteal.sources.trademe import (
    TradeMeClient,
    listings_from_fixture,
    map_listing,
    parse_datetime,
)

SETTINGS = Settings()
FIXTURE = Path(__file__).parent / "fixtures" / "trademe_search.json"

SECRETS = Secrets(
    trademe_consumer_key="key",
    trademe_consumer_secret="secret",
    trademe_token="token",
    trademe_token_secret="token-secret",
    deepseek_api_key="llm",
    ntfy_topic="topic",
)


def test_parse_iso_datetime():
    assert parse_datetime("2026-11-11T01:15:00Z") == datetime(
        2026, 11, 11, 1, 15, tzinfo=timezone.utc
    )


def test_parse_dotnet_datetime():
    expected = datetime.fromtimestamp(1762826400, tz=timezone.utc)
    assert parse_datetime("/Date(1762826400000)/") == expected


def test_parse_datetime_handles_garbage():
    assert parse_datetime("not a date") is None
    assert parse_datetime(None) is None
    assert parse_datetime(12345) is None


def test_prefers_buy_now_price():
    listing = map_listing(
        {
            "ListingId": 9,
            "Title": "Desk",
            "StartPrice": 300,
            "BuyNowPrice": 250,
            "Region": "Auckland",
        },
        SETTINGS,
    )
    assert listing is not None
    assert listing.price == 250
    assert listing.start_price == 300
    assert listing.buy_now_price == 250


def test_falls_back_to_start_price_when_buy_now_is_zero():
    listing = map_listing(
        {"ListingId": 10, "Title": "Desk", "StartPrice": 120, "BuyNowPrice": 0},
        SETTINGS,
    )
    assert listing is not None
    assert listing.price == 120
    assert listing.buy_now_price is None


def test_unknown_price_becomes_none():
    listing = map_listing(
        {"ListingId": 11, "Title": "Desk", "StartPrice": 0, "BuyNowPrice": 0},
        SETTINGS,
    )
    assert listing is not None
    assert listing.price is None


def test_url_comes_from_template():
    listing = map_listing({"ListingId": 42, "Title": "Desk"}, SETTINGS)
    assert listing is not None
    assert listing.url == "https://www.trademe.co.nz/a.aspx?id=42"


def test_custom_url_template():
    settings = Settings(url_template="https://example.test/{listing_id}")
    listing = map_listing({"ListingId": 7, "Title": "Desk"}, settings)
    assert listing is not None
    assert listing.url == "https://example.test/7"


def test_missing_id_or_title_is_dropped():
    assert map_listing({"Title": "Desk"}, SETTINGS) is None
    assert map_listing({"ListingId": 1, "Title": "  "}, SETTINGS) is None
    assert map_listing({"ListingId": 0, "Title": "Desk"}, SETTINGS) is None


def test_pickup_locality_used_when_region_missing():
    listing = map_listing(
        {
            "ListingId": 5,
            "Title": "Desk",
            "PickupLocality": {"Region": "Canterbury", "Suburb": "Riccarton"},
        },
        SETTINGS,
    )
    assert listing is not None
    assert listing.region == "Canterbury"
    assert listing.suburb == "Riccarton"


def test_fixture_maps_expected_listings():
    listings = listings_from_fixture(FIXTURE, SETTINGS)
    assert [listing.listing_id for listing in listings] == [
        "5123456789",
        "5123456790",
        "5123456791",
    ]
    first = listings[0]
    assert first.price == 180.0
    assert first.region == "Auckland"
    assert first.photo_count == 2
    assert first.is_classified is True


def test_search_params_use_verified_field_names():
    client = TradeMeClient(SECRETS, SETTINGS)
    item = WishItem(
        name="desk", query="standing desk", regions=("Auckland",), condition="used"
    )
    params = client.build_params(item)
    assert params["search_string"] == "standing desk"
    assert params["sort_order"] == "ExpiryDesc"  # there is no "Latest" sort
    assert params["condition"] == "Used"
    assert params["rows"] == SETTINGS.rows_per_search
    assert "price_max" not in params  # Trade Me has no price filter


def test_search_params_respect_listed_as():
    client = TradeMeClient(SECRETS, SETTINGS)
    item = WishItem(name="desk", query="desk", listed_as="classifieds")
    assert client.build_params(item)["listed_as"] == "Classifieds"


def test_search_params_exclude_businesses_by_default():
    client = TradeMeClient(SECRETS, SETTINGS)
    params = client.build_params(WishItem(name="desk", query="desk"))
    assert params["seller_status"] == "Casual"


def test_item_can_opt_into_business_sellers():
    client = TradeMeClient(SECRETS, SETTINGS)
    item = WishItem(name="desk", query="desk", seller_status="in_trade")
    assert client.build_params(item)["seller_status"] == "InTrade"


def test_item_can_disable_the_seller_filter():
    client = TradeMeClient(SECRETS, SETTINGS)
    item = WishItem(name="desk", query="desk", seller_status="all")
    assert client.build_params(item)["seller_status"] == "All"


DEAL_FIXTURE = Path(__file__).parent / "fixtures" / "trademe_deal.json"


def test_deal_fixture_has_one_obvious_bargain():
    listings = listings_from_fixture(DEAL_FIXTURE, SETTINGS)
    assert len(listings) == 4
    bargain = listings[0]
    assert bargain.price == 150.0
    assert bargain.region == "Auckland"
    assert bargain.photo_count == 6
    assert bargain.age_minutes() is not None
