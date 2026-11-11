"""The LLM stage: decide which survivors are actually worth a notification.

DeepSeek is OpenAI-compatible, so the official SDK does the work with a
different base_url. Output is forced to JSON and parsed defensively: a model
that returns prose or skips a listing must not crash the run.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import Any, Iterable

from .config import Settings, WishItem
from .models import Listing, Verdict

log = logging.getLogger(__name__)

OUTPUT_CONTRACT = """
Return ONLY a JSON object, no prose and no markdown fences, with this shape:

{
  "verdicts": [
    {
      "listing_id": "<the listing_id you were given, as a string>",
      "score": 0-100,
      "deal": true or false,
      "reason": "one or two sentences, specific to this listing",
      "red_flags": ["short phrase", "..."],
      "max_offer": number or null
    }
  ]
}

Rules:
- Return exactly one verdict per listing supplied. Never invent listings.
- "deal" is true only if you would genuinely act on it today.
- score is your confidence that this is a good buy for the stated wishlist item.
- max_offer is what you would pay, in NZD. Use null when it is not a deal.
""".strip()


class JudgeError(RuntimeError):
    """The judge stage could not produce usable output."""


def _strip_fences(text: str) -> str:
    text = text.strip()
    if not text.startswith("```"):
        return text
    lines = text.splitlines()
    if lines:
        lines = lines[1:]
    while lines and lines[-1].strip().startswith("```"):
        lines = lines[:-1]
    return "\n".join(lines).strip()


def parse_payload(text: str) -> dict[str, Any]:
    """Tolerate fenced JSON, leading prose, and trailing commentary."""
    cleaned = _strip_fences(text)
    try:
        parsed = json.loads(cleaned)
    except json.JSONDecodeError:
        start = cleaned.find("{")
        end = cleaned.rfind("}")
        if start == -1 or end <= start:
            raise JudgeError("judge did not return JSON")
        try:
            parsed = json.loads(cleaned[start : end + 1])
        except json.JSONDecodeError as exc:
            raise JudgeError(f"judge returned unparseable JSON: {exc}") from exc
    if not isinstance(parsed, dict):
        raise JudgeError("judge returned JSON that is not an object")
    return parsed


def _score(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        number = int(round(float(value)))
    except (TypeError, ValueError):
        return None
    return max(0, min(100, number))


def _money(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


def parse_verdicts(
    payload: dict[str, Any],
    item: str,
    listings: Iterable[Listing],
) -> list[Verdict]:
    """Join the model's verdicts back onto the listings we sent."""
    by_id = {listing.listing_id: listing for listing in listings}
    seen: set[str] = set()
    verdicts: list[Verdict] = []

    for row in payload.get("verdicts") or []:
        if not isinstance(row, dict):
            continue
        listing_id = str(row.get("listing_id") or row.get("id") or "").strip()
        listing = by_id.get(listing_id)
        if listing is None or listing_id in seen:
            continue
        seen.add(listing_id)
        flags = row.get("red_flags") or []
        if isinstance(flags, str):
            flags = [flags]
        verdicts.append(
            Verdict(
                listing=listing,
                item=item,
                score=_score(row.get("score")),
                deal=bool(row.get("deal")),
                reason=str(row.get("reason") or "").strip()[:400],
                red_flags=[str(flag)[:120] for flag in flags if flag][:6],
                max_offer=_money(row.get("max_offer")),
            )
        )

    for listing_id, listing in by_id.items():
        if listing_id in seen:
            continue
        verdicts.append(
            Verdict(
                listing=listing,
                item=item,
                score=None,
                deal=False,
                reason="judge returned no verdict for this listing",
                error="missing_verdict",
            )
        )
    return verdicts


def failed_verdicts(item: str, listings: Iterable[Listing], error: str) -> list[Verdict]:
    return [
        Verdict(
            listing=listing,
            item=item,
            score=None,
            deal=False,
            reason="",
            error=error,
        )
        for listing in listings
    ]


def build_messages(
    criteria: str,
    item: WishItem,
    listings: list[Listing],
    median_price: float | None = None,
    now: datetime | None = None,
) -> list[dict[str, str]]:
    context: dict[str, Any] = {
        "wishlist_item": item.name,
        "max_price": item.max_price,
        "regions": list(item.regions),
        "recent_median_price": median_price,
        "criteria": criteria.strip(),
        "listings": [listing.to_prompt_dict(now) for listing in listings],
    }
    if item.notes:
        context["notes"] = item.notes

    return [
        {
            "role": "system",
            "content": (
                "You screen New Zealand second-hand marketplace listings for a "
                "single buyer. You are decisive, sceptical of scams, and you never "
                "invent details that are not in the listing data.\n\n"
                + OUTPUT_CONTRACT
            ),
        },
        {
            "role": "user",
            "content": json.dumps(context, ensure_ascii=False, separators=(",", ":")),
        },
    ]


class Judge:
    """Thin wrapper over the DeepSeek chat completions endpoint."""

    def __init__(
        self,
        api_key: str,
        settings: Settings,
        client: Any | None = None,
    ) -> None:
        self._settings = settings
        if client is None:
            from openai import OpenAI

            client = OpenAI(
                api_key=api_key,
                base_url=settings.deepseek_base_url,
                timeout=90.0,
                max_retries=2,
            )
        self._client = client

    def judge(
        self,
        criteria: str,
        item: WishItem,
        listings: list[Listing],
        median_price: float | None = None,
        now: datetime | None = None,
    ) -> list[Verdict]:
        if not listings:
            return []

        messages = build_messages(criteria, item, listings, median_price, now)
        try:
            response = self._client.chat.completions.create(
                model=self._settings.deepseek_model,
                messages=messages,
                temperature=0,
                max_tokens=2000,
                response_format={"type": "json_object"},
            )
        except Exception as exc:  # network, auth, quota - all the same to us
            raise JudgeError(f"{exc.__class__.__name__}: {exc}") from exc

        try:
            content = response.choices[0].message.content or ""
        except (AttributeError, IndexError) as exc:
            raise JudgeError("judge response had no choices") from exc

        payload = parse_payload(content)
        verdicts = parse_verdicts(payload, item.name, listings)
        log.info(
            "%s: judged %s listings, %s marked as deals",
            item.name,
            len(verdicts),
            sum(1 for v in verdicts if v.deal),
        )
        return verdicts
