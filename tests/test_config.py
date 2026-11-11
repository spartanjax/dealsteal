from __future__ import annotations

import os
from pathlib import Path

import pytest

from dealsteal.config import (
    SECRET_NAMES,
    ConfigError,
    MissingSecrets,
    Secrets,
    load_config,
    load_dotenv,
    load_secrets,
)

WISHLIST = """
settings:
  min_score: 70
  max_candidates_per_run: 10

items:
  - name: standing desk
    query: standing desk
    max_price: 400
    min_price: 50
    regions: [Auckland, Wellington]
    exclude_terms: [wanted, "parts only"]
    must_match_any: [desk]
    condition: used
    notes: Electric preferred.
"""

CRITERIA = "Be picky."


def clear_secrets(monkeypatch) -> None:
    for name in SECRET_NAMES:
        monkeypatch.delenv(name, raising=False)


def test_missing_secrets_lists_every_absent_name(monkeypatch):
    clear_secrets(monkeypatch)
    monkeypatch.setenv("TRADEME_CONSUMER_KEY", "present")
    with pytest.raises(MissingSecrets) as excinfo:
        load_secrets()
    expected = [name for name in SECRET_NAMES if name != "TRADEME_CONSUMER_KEY"]
    assert excinfo.value.missing == expected
    for name in expected:
        assert name in str(excinfo.value)


def test_load_secrets_returns_values(monkeypatch):
    clear_secrets(monkeypatch)
    for index, name in enumerate(SECRET_NAMES):
        monkeypatch.setenv(name, f"value-{index}")
    secrets = load_secrets()
    assert secrets.trademe_consumer_key == "value-0"
    assert secrets.ntfy_topic == "value-5"


def test_secrets_never_render_their_values(monkeypatch):
    clear_secrets(monkeypatch)
    for name in SECRET_NAMES:
        monkeypatch.setenv(name, "super-secret-token")
    secrets = load_secrets()
    assert "super-secret-token" not in repr(secrets)
    assert "super-secret-token" not in str(secrets)
    assert "redacted" in repr(secrets)


def test_subset_of_required_secrets(monkeypatch):
    clear_secrets(monkeypatch)
    monkeypatch.setenv("DEEPSEEK_API_KEY", "llm-only")
    secrets = load_secrets(("DEEPSEEK_API_KEY",))
    assert secrets.deepseek_api_key == "llm-only"
    assert secrets.trademe_token == ""


def test_dotenv_loads_values(tmp_path, monkeypatch):
    monkeypatch.delenv("DEALSTEAL_TEST_KEY", raising=False)
    env_file = tmp_path / ".env"
    env_file.write_text(
        "# comment\n\nDEALSTEAL_TEST_KEY=abc123\nQUOTED='hello world'\n",
        encoding="utf-8",
    )
    load_dotenv(env_file)
    assert os.environ["DEALSTEAL_TEST_KEY"] == "abc123"
    assert os.environ["QUOTED"] == "hello world"


def test_dotenv_does_not_override_real_environment(tmp_path, monkeypatch):
    monkeypatch.setenv("DEALSTEAL_TEST_KEY", "from-env")
    env_file = tmp_path / ".env"
    env_file.write_text("DEALSTEAL_TEST_KEY=from-file\n", encoding="utf-8")
    load_dotenv(env_file)
    assert os.environ["DEALSTEAL_TEST_KEY"] == "from-env"


def test_missing_dotenv_is_ignored(tmp_path):
    load_dotenv(tmp_path / "nope.env")


def write_config(tmp_path: Path, wishlist: str = WISHLIST, criteria: str = CRITERIA):
    (tmp_path / "wishlist.yaml").write_text(wishlist, encoding="utf-8")
    (tmp_path / "criteria.md").write_text(criteria, encoding="utf-8")
    return tmp_path


def test_load_config(tmp_path):
    config = load_config(write_config(tmp_path))
    assert config.settings.min_score == 70
    assert config.settings.max_candidates_per_run == 10
    assert config.criteria == "Be picky."
    item = config.item("standing desk")
    assert item is not None
    assert item.max_price == 400
    assert item.regions == ("Auckland", "Wellington")
    assert item.exclude_terms == ("wanted", "parts only")
    assert item.notes == "Electric preferred."


def test_item_lookup_is_case_insensitive(tmp_path):
    config = load_config(write_config(tmp_path))
    assert config.item("STANDING DESK") is not None
    assert config.item("road bike") is None


def test_unknown_setting_is_a_config_error(tmp_path):
    bad = WISHLIST.replace("min_score: 70", "min_scor: 70")
    with pytest.raises(ConfigError, match="unknown settings"):
        load_config(write_config(tmp_path, wishlist=bad))


def test_item_without_query_is_a_config_error(tmp_path):
    bad = "items:\n  - name: desk\n"
    with pytest.raises(ConfigError, match="query"):
        load_config(write_config(tmp_path, wishlist=bad))


def test_empty_items_is_a_config_error(tmp_path):
    with pytest.raises(ConfigError, match="no items"):
        load_config(write_config(tmp_path, wishlist="items: []\n"))


def test_missing_files_are_config_errors(tmp_path):
    with pytest.raises(ConfigError, match="missing"):
        load_config(tmp_path)


def test_region_matching(tmp_path):
    item = load_config(write_config(tmp_path)).item("standing desk")
    assert item is not None
    assert item.matches_region("Auckland")
    assert item.matches_region(None, "wellington")  # suburb fallback
    assert not item.matches_region("Canterbury")


def test_seller_status_defaults_to_casual(tmp_path):
    config = load_config(write_config(tmp_path))
    assert config.settings.seller_status == "casual"


def test_item_can_override_seller_status(tmp_path):
    wishlist = WISHLIST.replace(
        "    notes: Electric preferred.\n", "    seller_status: in_trade\n"
    )
    config = load_config(write_config(tmp_path, wishlist=wishlist))
    item = config.item("standing desk")
    assert item is not None
    assert item.seller_status == "in_trade"


def test_bad_seller_status_in_settings_is_a_config_error(tmp_path):
    bad = WISHLIST.replace("settings:\n", "settings:\n  seller_status: maybe\n")
    with pytest.raises(ConfigError, match="seller_status"):
        load_config(write_config(tmp_path, wishlist=bad))


def test_bad_seller_status_on_an_item_is_a_config_error(tmp_path):
    bad = WISHLIST.replace(
        "    notes: Electric preferred.\n", "    seller_status: shop\n"
    )
    with pytest.raises(ConfigError, match="seller_status"):
        load_config(write_config(tmp_path, wishlist=bad))


def test_secrets_dataclass_is_frozen():
    secrets = Secrets("a", "b", "c", "d", "e", "f")
    with pytest.raises(Exception):
        secrets.ntfy_topic = "hijacked"  # type: ignore[misc]
