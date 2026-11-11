"""ntfy delivery and message formatting."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Iterable

import requests

from .models import RunStats, Verdict

log = logging.getLogger(__name__)

MAX_BODY_CHARS = 3500  # ntfy rejects messages over 4 KB
MAX_LISTINGS_PER_ITEM = 5


class NotifyError(RuntimeError):
    pass


@dataclass(frozen=True)
class Notification:
    title: str
    body: str
    priority: str = "default"
    tags: str = "moneybag"
    click: str | None = None
    deals: list[Verdict] = field(default_factory=list, compare=False)


def _age(listing, now: datetime) -> str:
    minutes = listing.age_minutes(now)
    if minutes is None:
        return "age unknown"
    if minutes < 60:
        return f"{minutes}m old"
    if minutes < 60 * 24:
        return f"{minutes // 60}h old"
    return f"{minutes // (60 * 24)}d old"


def _money(value: float | None) -> str:
    if value is None:
        return "price unknown"
    if float(value).is_integer():
        return f"${int(value)}"
    return f"${value:,.2f}"


def _line(verdict: Verdict, now: datetime) -> str:
    listing = verdict.listing
    bits = [f"score {verdict.score if verdict.score is not None else '?'}"]
    if listing.region:
        bits.append(listing.region)
    bits.append(_age(listing, now))
    head = (
        f"- **{_money(listing.price)}** — {listing.title}\n"
        f"  {verdict.reason or 'no reason given'}\n"
        f"  {' · '.join(bits)} · [view listing]({listing.url})"
    )
    if verdict.red_flags:
        head += f"\n  flags: {', '.join(verdict.red_flags)}"
    if verdict.max_offer:
        head += f"\n  max offer: {_money(verdict.max_offer)}"
    return head


def build_notification(
    deals: Iterable[Verdict],
    stats: RunStats,
    now: datetime | None = None,
    held_back: int = 0,
    failure_note: str | None = None,
) -> Notification | None:
    """One message per run. Returns None when there is nothing to say."""
    now = now or datetime.now(timezone.utc)
    deals = sorted(
        deals,
        key=lambda v: (v.score or 0, -(v.listing.price or 0)),
        reverse=True,
    )
    if not deals:
        return None

    by_item: dict[str, list[Verdict]] = {}
    for verdict in deals:
        by_item.setdefault(verdict.item, []).append(verdict)

    total = len(deals)
    noun = "deal" if total == 1 else "deals"
    title = f"dealsteal: {total} {noun}"
    if len(by_item) == 1:
        title = f"dealsteal: {total} {noun} for {next(iter(by_item))}"

    parts = [
        f"**{total} {noun}** · {stats.unseen} new · {stats.fetched} fetched"
    ]
    if held_back:
        parts.append(f"{held_back} held back (over the LLM cap)")
    body = " · ".join(parts) + "\n"

    for item_name, item_deals in by_item.items():
        body += f"\n### {item_name}\n"
        for verdict in item_deals[:MAX_LISTINGS_PER_ITEM]:
            body += _line(verdict, now) + "\n"
        extra = len(item_deals) - MAX_LISTINGS_PER_ITEM
        if extra > 0:
            body += f"- plus {extra} more below the cut\n"

    if failure_note:
        body += f"\n> {failure_note}\n"

    if len(body) > MAX_BODY_CHARS:
        body = body[: MAX_BODY_CHARS - 20].rstrip() + "\n\n(truncated)"

    top_score = max((v.score or 0) for v in deals)
    return Notification(
        title=title,
        body=body,
        priority="high" if top_score >= 85 else "default",
        tags="fire" if top_score >= 85 else "moneybag",
        click=deals[0].listing.url,
        deals=list(deals),
    )


def build_failure_notification(
    stats: RunStats,
    note: str,
    links: Iterable[Verdict] = (),
) -> Notification:
    """Low-priority ping so a broken judge never fails silently."""
    body = f"{note}\n\n{stats.unseen} new listings were not screened.\n"
    shown = list(links)[:3]
    for verdict in shown:
        listing = verdict.listing
        body += f"- {_money(listing.price)} — {listing.title} · [view]({listing.url})\n"
    return Notification(
        title="dealsteal: judge unavailable",
        body=body,
        priority="low",
        tags="warning",
    )


def send(
    notification: Notification,
    topic: str,
    base_url: str = "https://ntfy.sh",
    timeout: float = 15.0,
) -> None:
    headers = {
        "Title": notification.title.encode("ascii", "replace").decode("ascii"),
        "Priority": notification.priority,
        "Tags": notification.tags,
        "Markdown": "yes",
        "Content-Type": "text/markdown; charset=utf-8",
    }
    if notification.click:
        headers["Click"] = notification.click

    url = f"{base_url.rstrip('/')}/{topic}"
    try:
        response = requests.post(
            url,
            data=notification.body.encode("utf-8"),
            headers=headers,
            timeout=timeout,
        )
    except requests.RequestException as exc:
        raise NotifyError(f"could not reach ntfy: {exc.__class__.__name__}") from exc

    if response.status_code >= 300:
        raise NotifyError(f"ntfy returned {response.status_code}")
    log.info("sent notification to ntfy: %s", notification.title)
