from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from dealsteal.judge import (
    Judge,
    JudgeError,
    build_messages,
    failed_verdicts,
    parse_payload,
    parse_verdicts,
)
from dealsteal.config import Settings, WishItem
from dealsteal.models import Listing

NOW = datetime(2026, 11, 11, 12, 0, tzinfo=timezone.utc)
ITEM = WishItem(name="standing desk", query="standing desk", max_price=400)


def make(listing_id: str = "1") -> Listing:
    return Listing(
        source="trademe",
        listing_id=listing_id,
        title=f"Standing desk {listing_id}",
        url=f"https://example.test/{listing_id}",
        price=200.0,
        region="Auckland",
        listed_at=NOW - timedelta(minutes=15),
        ends_at=NOW + timedelta(days=3),
    )


def test_parse_payload_plain_json():
    payload = parse_payload('{"verdicts": []}')
    assert payload == {"verdicts": []}


def test_parse_payload_strips_code_fences():
    text = '```json\n{"verdicts": [{"listing_id": "1"}]}\n```'
    assert parse_payload(text)["verdicts"][0]["listing_id"] == "1"


def test_parse_payload_recovers_from_leading_prose():
    text = 'Here you go:\n{"verdicts": []}\nHope that helps.'
    assert parse_payload(text) == {"verdicts": []}


def test_parse_payload_rejects_non_json():
    with pytest.raises(JudgeError):
        parse_payload("no json at all")


def test_parse_verdicts_joins_on_listing_id():
    listings = [make("1"), make("2")]
    payload = {
        "verdicts": [
            {
                "listing_id": "1",
                "score": 91,
                "deal": True,
                "reason": " Cheap vs median ",
                "red_flags": ["single photo"],
                "max_offer": 350,
            }
        ]
    }
    verdicts = parse_verdicts(payload, "standing desk", listings)
    by_id = {v.listing.listing_id: v for v in verdicts}
    assert by_id["1"].score == 91
    assert by_id["1"].deal is True
    assert by_id["1"].reason == "Cheap vs median"
    assert by_id["1"].max_offer == 350.0
    assert by_id["2"].error == "missing_verdict"


def test_parse_verdicts_clamps_and_coerces():
    payload = {"verdicts": [{"listing_id": "1", "score": "140", "deal": "yes"}]}
    verdict = parse_verdicts(payload, "desk", [make("1")])[0]
    assert verdict.score == 100
    assert verdict.deal is True


def test_parse_verdicts_handles_bad_score():
    payload = {"verdicts": [{"listing_id": "1", "score": "excellent", "deal": False}]}
    verdict = parse_verdicts(payload, "desk", [make("1")])[0]
    assert verdict.score is None


def test_parse_verdicts_ignores_unknown_listing_ids():
    payload = {"verdicts": [{"listing_id": "does-not-exist", "deal": True}]}
    verdicts = parse_verdicts(payload, "desk", [make("1")])
    assert [v.listing.listing_id for v in verdicts] == ["1"]
    assert verdicts[0].error == "missing_verdict"


def test_parse_verdicts_ignores_duplicates():
    payload = {
        "verdicts": [
            {"listing_id": "1", "score": 80, "deal": True},
            {"listing_id": "1", "score": 10, "deal": False},
        ]
    }
    verdicts = parse_verdicts(payload, "desk", [make("1")])
    assert len(verdicts) == 1
    assert verdicts[0].score == 80


def test_parse_verdicts_accepts_string_red_flags():
    payload = {"verdicts": [{"listing_id": "1", "deal": True, "red_flags": "blurry"}]}
    assert parse_verdicts(payload, "desk", [make("1")])[0].red_flags == ["blurry"]


def test_failed_verdicts_mark_every_listing():
    verdicts = failed_verdicts("desk", [make("1"), make("2")], "boom")
    assert len(verdicts) == 2
    assert all(v.error == "boom" for v in verdicts)
    assert not any(v.is_worth_notifying(0) for v in verdicts)


def test_build_messages_includes_criteria_and_listings():
    messages = build_messages("Be picky.", ITEM, [make("1")], 310.0, NOW)
    assert messages[0]["role"] == "system"
    assert "JSON" in messages[0]["content"]
    payload = json.loads(messages[1]["content"])
    assert payload["criteria"] == "Be picky."
    assert payload["recent_median_price"] == 310.0
    assert payload["listings"][0]["listing_id"] == "1"
    assert payload["listings"][0]["age_minutes"] == 15


def test_judge_returns_empty_without_listings():
    judge = Judge("key", Settings(), client=object())
    assert judge.judge("criteria", ITEM, []) == []


class _StubChoice:
    def __init__(self, content: str) -> None:
        self.message = type("M", (), {"content": content})


class _StubCompletions:
    def __init__(self, content: str) -> None:
        self._content = content

    def create(self, **kwargs):
        assert kwargs["response_format"] == {"type": "json_object"}
        return type("R", (), {"choices": [_StubChoice(self._content)]})


class _StubClient:
    def __init__(self, content: str) -> None:
        self.chat = type("C", (), {"completions": _StubCompletions(content)})


def test_judge_parses_model_output():
    content = '{"verdicts":[{"listing_id":"1","score":88,"deal":true,"reason":"good"}]}'
    judge = Judge("key", Settings(), client=_StubClient(content))
    verdicts = judge.judge("criteria", ITEM, [make("1")])
    assert verdicts[0].score == 88
    assert verdicts[0].deal is True


def test_judge_wraps_transport_errors():
    class Exploding:
        def __init__(self):
            self.chat = type("C", (), {"completions": self})

        def create(self, **kwargs):
            raise RuntimeError("network down")

    judge = Judge("key", Settings(), client=Exploding())
    with pytest.raises(JudgeError):
        judge.judge("criteria", ITEM, [make("1")])
