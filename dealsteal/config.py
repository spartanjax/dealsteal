"""Configuration and secret loading. Nothing secret is ever hardcoded."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
CONFIG_DIR = REPO_ROOT / "config"
DATA_DIR = REPO_ROOT / "data"
DB_PATH = DATA_DIR / "dealsteal.db"

SECRET_NAMES = (
    "TRADEME_CONSUMER_KEY",
    "TRADEME_CONSUMER_SECRET",
    "TRADEME_TOKEN",
    "TRADEME_TOKEN_SECRET",
    "DEEPSEEK_API_KEY",
    "NTFY_TOPIC",
)

TRADEME_SECRET_NAMES = SECRET_NAMES[:4]


class ConfigError(RuntimeError):
    """Raised when config or credentials are unusable."""


class MissingSecrets(ConfigError):
    def __init__(self, missing: list[str]) -> None:
        self.missing = missing
        super().__init__(
            "missing environment variables: " + ", ".join(missing)
        )


@dataclass(frozen=True)
class Secrets:
    trademe_consumer_key: str
    trademe_consumer_secret: str
    trademe_token: str
    trademe_token_secret: str
    deepseek_api_key: str
    ntfy_topic: str

    def __repr__(self) -> str:  # never leak values into logs or tracebacks
        return "Secrets(<redacted>)"

    __str__ = __repr__


def load_dotenv(path: Path) -> None:
    """Load KEY=VALUE lines into os.environ without overwriting real env vars."""
    if not path.is_file():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and value:
            os.environ.setdefault(key, value)


def load_secrets(require: tuple[str, ...] = SECRET_NAMES) -> Secrets:
    missing = [name for name in require if not os.environ.get(name)]
    if missing:
        raise MissingSecrets(missing)
    return Secrets(
        trademe_consumer_key=os.environ.get("TRADEME_CONSUMER_KEY", ""),
        trademe_consumer_secret=os.environ.get("TRADEME_CONSUMER_SECRET", ""),
        trademe_token=os.environ.get("TRADEME_TOKEN", ""),
        trademe_token_secret=os.environ.get("TRADEME_TOKEN_SECRET", ""),
        deepseek_api_key=os.environ.get("DEEPSEEK_API_KEY", ""),
        ntfy_topic=os.environ.get("NTFY_TOPIC", ""),
    )


@dataclass(frozen=True)
class Settings:
    max_candidates_per_run: int = 25
    rows_per_search: int = 100
    min_score: int = 60
    url_template: str = "https://www.trademe.co.nz/a.aspx?id={listing_id}"
    ntfy_base_url: str = "https://ntfy.sh"
    deepseek_model: str = "deepseek-chat"
    deepseek_base_url: str = "https://api.deepseek.com"


@dataclass(frozen=True)
class WishItem:
    name: str
    query: str
    max_price: float | None = None
    min_price: float | None = None
    regions: tuple[str, ...] = ()
    exclude_terms: tuple[str, ...] = ()
    must_match_any: tuple[str, ...] = ()
    condition: str | None = None
    listed_as: str | None = None
    notes: str | None = None

    def matches_region(self, *candidates: str | None) -> bool:
        if not self.regions:
            return True
        wanted = {r.lower() for r in self.regions}
        for candidate in candidates:
            if candidate and candidate.lower() in wanted:
                return True
        return False


@dataclass(frozen=True)
class Config:
    settings: Settings
    items: tuple[WishItem, ...]
    criteria: str

    def item(self, name: str) -> WishItem | None:
        for candidate in self.items:
            if candidate.name.lower() == name.lower():
                return candidate
        return None


def _str_tuple(value: Any) -> tuple[str, ...]:
    if not value:
        return ()
    if isinstance(value, str):
        return (value,)
    return tuple(str(v) for v in value)


def parse_item(raw: dict[str, Any]) -> WishItem:
    if not raw.get("name"):
        raise ConfigError("every wishlist item needs a 'name'")
    if not raw.get("query"):
        raise ConfigError(f"wishlist item {raw['name']!r} needs a 'query'")
    return WishItem(
        name=str(raw["name"]).strip(),
        query=str(raw["query"]).strip(),
        max_price=float(raw["max_price"]) if raw.get("max_price") else None,
        min_price=float(raw["min_price"]) if raw.get("min_price") else None,
        regions=_str_tuple(raw.get("regions")),
        exclude_terms=_str_tuple(raw.get("exclude_terms")),
        must_match_any=_str_tuple(raw.get("must_match_any")),
        condition=raw.get("condition"),
        listed_as=raw.get("listed_as"),
        notes=raw.get("notes"),
    )


def load_config(config_dir: Path | None = None) -> Config:
    config_dir = config_dir or CONFIG_DIR
    wishlist_path = config_dir / "wishlist.yaml"
    criteria_path = config_dir / "criteria.md"

    if not wishlist_path.is_file():
        raise ConfigError(f"missing {wishlist_path}")
    if not criteria_path.is_file():
        raise ConfigError(f"missing {criteria_path}")

    raw = yaml.safe_load(wishlist_path.read_text(encoding="utf-8")) or {}
    items = tuple(parse_item(item) for item in (raw.get("items") or []))
    if not items:
        raise ConfigError(f"{wishlist_path} defines no items")

    settings_raw = raw.get("settings") or {}
    known = {f.name for f in Settings.__dataclass_fields__.values()}
    unknown = set(settings_raw) - known
    if unknown:
        raise ConfigError(f"unknown settings keys: {', '.join(sorted(unknown))}")

    return Config(
        settings=Settings(**settings_raw),
        items=items,
        criteria=criteria_path.read_text(encoding="utf-8"),
    )
