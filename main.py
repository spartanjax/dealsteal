#!/usr/bin/env python3
"""dealsteal worker.

Called once an hour by cron-job.org (via a GitHub repository_dispatch).
Fetches each wishlist item from Trade Me, drops everything already seen,
judges the survivors, and sends one ntfy notification when something is worth
looking at.

Exit codes: 0 fine (including "nothing new"), 1 runtime failure, 2 config error.
"""

from __future__ import annotations

import argparse
import logging
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from dealsteal import state, store
from dealsteal.config import (
    CONFIG_DIR,
    DB_PATH,
    REPO_ROOT,
    TRADEME_SECRET_NAMES,
    Config,
    ConfigError,
    MissingSecrets,
    WishItem,
    load_config,
    load_dotenv,
    load_secrets,
)
from dealsteal.filters import Drop, cap, filter_listings
from dealsteal.judge import Judge, JudgeError, failed_verdicts
from dealsteal.models import Listing, RunStats, Verdict
from dealsteal.notify import (
    Notification,
    NotifyError,
    build_failure_notification,
    build_notification,
    send,
)
from dealsteal.sources.trademe import (
    TradeMeClient,
    TradeMeError,
    listings_from_fixture,
)

log = logging.getLogger("dealsteal")


@dataclass
class ItemOutcome:
    item: WishItem
    fetched: list[Listing] = field(default_factory=list)
    kept: list[Listing] = field(default_factory=list)
    dropped: list[Drop] = field(default_factory=list)
    unseen: list[Listing] = field(default_factory=list)
    candidates: list[Listing] = field(default_factory=list)
    held_back: int = 0
    error: str | None = None


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="dealsteal",
        description="Watch Trade Me for deals and push them to ntfy.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="fetch and judge, print the notification, send and save nothing",
    )
    parser.add_argument(
        "--item",
        metavar="NAME",
        help="only process this wishlist item",
    )
    parser.add_argument(
        "--fixture",
        type=Path,
        metavar="PATH",
        help="read a recorded Trade Me response instead of calling the API",
    )
    parser.add_argument(
        "--no-llm",
        action="store_true",
        help="skip the judge and report counts only",
    )
    parser.add_argument(
        "--no-state",
        action="store_true",
        help="do not restore or persist the state branch",
    )
    parser.add_argument(
        "--config-dir",
        type=Path,
        default=CONFIG_DIR,
        help="directory holding wishlist.yaml and criteria.md",
    )
    parser.add_argument("-v", "--verbose", action="store_true")
    return parser.parse_args(argv)


def configure_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    logging.getLogger("urllib3").setLevel(logging.WARNING)


def select_items(config: Config, name: str | None) -> list[WishItem]:
    if not name:
        return list(config.items)
    item = config.item(name)
    if item is None:
        known = ", ".join(i.name for i in config.items)
        raise ConfigError(f"no wishlist item called {name!r} (have: {known})")
    return [item]


def collect(
    config: Config,
    item: WishItem,
    conn,
    client: TradeMeClient | None,
    fixture: Path | None,
    now: datetime,
) -> ItemOutcome:
    outcome = ItemOutcome(item=item)
    try:
        if fixture is not None:
            outcome.fetched = listings_from_fixture(fixture, config.settings)
        else:
            assert client is not None
            outcome.fetched = client.search(item)
    except TradeMeError as exc:
        outcome.error = str(exc)
        log.error("%s: source failed: %s", item.name, exc)
        return outcome

    outcome.kept, outcome.dropped = filter_listings(outcome.fetched, item, now)
    known = store.known_keys(conn, item.name)
    outcome.unseen = [l for l in outcome.kept if l.key not in known]
    outcome.candidates, outcome.held_back = cap(
        outcome.unseen, config.settings.max_candidates_per_run, now
    )

    log.info(
        "%s: %s fetched, %s kept, %s dropped, %s unseen, %s to judge%s",
        item.name,
        len(outcome.fetched),
        len(outcome.kept),
        len(outcome.dropped),
        len(outcome.unseen),
        len(outcome.candidates),
        f" ({outcome.held_back} held back)" if outcome.held_back else "",
    )
    for drop in outcome.dropped[:5]:
        log.debug("  dropped %r: %s", drop.listing.title[:60], drop.reason)
    return outcome


def judge_outcome(
    config: Config,
    outcome: ItemOutcome,
    conn,
    judge: Judge | None,
    now: datetime,
) -> tuple[list[Verdict], str | None]:
    """Returns (verdicts, judge_error_or_None)."""
    if not outcome.candidates:
        return [], None
    if judge is None:
        return [], None

    median = store.median_price(
        conn, outcome.item.name, exclude={l.key for l in outcome.candidates}
    )
    if median is not None:
        log.info("%s: recent median price %s", outcome.item.name, round(median, 2))

    try:
        verdicts = judge.judge(
            config.criteria, outcome.item, outcome.candidates, median, now
        )
    except JudgeError as exc:
        log.error("%s: judge failed: %s", outcome.item.name, exc)
        return (
            failed_verdicts(outcome.item.name, outcome.candidates, str(exc)),
            str(exc),
        )
    return verdicts, None


def run(args: argparse.Namespace) -> int:
    load_dotenv(REPO_ROOT / ".env")
    now = datetime.now(timezone.utc)

    config = load_config(args.config_dir)
    items = select_items(config, args.item)

    required: list[str] = []
    if args.fixture is None:
        required.extend(TRADEME_SECRET_NAMES)
    if not args.no_llm:
        required.append("DEEPSEEK_API_KEY")
    if not args.dry_run:
        required.append("NTFY_TOPIC")
    secrets = load_secrets(tuple(required))

    db_path = DB_PATH
    if not args.no_state:
        state.restore(db_path, REPO_ROOT)

    conn = store.connect(db_path)
    run_id = store.start_run(conn, now)
    stats = RunStats(items=len(items))
    outcomes: list[ItemOutcome] = []
    judged: list[Verdict] = []
    judge_error: str | None = None

    client = None
    if args.fixture is None:
        client = TradeMeClient(secrets, config.settings)

    judge_client = None
    if not args.no_llm:
        judge_client = Judge(secrets.deepseek_api_key, config.settings)

    for item in items:
        outcome = collect(config, item, conn, client, args.fixture, now)
        outcomes.append(outcome)
        stats.fetched += len(outcome.fetched)
        stats.filtered += len(outcome.kept)
        stats.unseen += len(outcome.unseen)
        if outcome.error:
            stats.source_errors += 1

        verdicts, error = judge_outcome(config, outcome, conn, judge_client, now)
        judged.extend(verdicts)
        stats.judged += len(outcome.candidates) if verdicts else 0
        judge_error = judge_error or error

    deals = [v for v in judged if v.is_worth_notifying(config.settings.min_score)]
    held_back = sum(o.held_back for o in outcomes)

    notification: Notification | None
    if judge_error and any(o.candidates for o in outcomes):
        links = [
            Verdict(listing=listing, item=o.item.name)
            for o in outcomes
            for listing in o.candidates[:1]
        ]
        notification = build_failure_notification(
            stats, f"the judge errored ({judge_error})", links
        )
    else:
        notification = build_notification(
            deals, stats, now, held_back=held_back
        )

    if notification is None:
        log.info(
            "nothing to report: %s new, %s judged, no deals",
            stats.unseen,
            stats.judged,
        )
        store.finish_run(conn, run_id, stats, "empty")
        conn.close()
        return 0

    log.info("notification:\n%s\n%s", notification.title, notification.body)
    if args.dry_run:
        log.info("dry run: not sending, not saving state")
        store.finish_run(conn, run_id, stats, "dry-run", notification.title)
        conn.close()
        return 0

    try:
        send(notification, secrets.ntfy_topic, config.settings.ntfy_base_url)
    except NotifyError as exc:
        log.error("notification failed: %s", exc)
        store.finish_run(conn, run_id, stats, "notify-failed", str(exc))
        conn.close()
        return 1

    stats.notified = len(notification.deals)
    for outcome in outcomes:
        store.record_seen(conn, outcome.item.name, outcome.unseen, now)
        store.record_prices(conn, outcome.item.name, outcome.unseen, now)
    store.save_verdicts(conn, judged, now)

    status = "notified" if stats.notified else "notified-unjudged"
    store.finish_run(conn, run_id, stats, status, notification.title)
    conn.close()

    if not args.no_state:
        state.persist(db_path, REPO_ROOT, message=f"state: {notification.title}")

    if stats.source_errors and stats.source_errors == stats.items:
        log.error("every source failed this run")
        return 1
    return 0


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    configure_logging(args.verbose)
    try:
        return run(args)
    except (ConfigError, MissingSecrets) as exc:
        log.error("configuration error: %s", exc)
        return 2
    except KeyboardInterrupt:
        log.warning("interrupted")
        return 1


if __name__ == "__main__":
    sys.exit(main())
