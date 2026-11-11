"""The state branch is the only durable thing in the whole design, so test it."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from dealsteal import state

pytestmark = pytest.mark.skipif(
    shutil.which("git") is None, reason="git is not installed"
)


def git(*args: str, cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True
    )


@pytest.fixture()
def repo(tmp_path: Path) -> tuple[Path, Path]:
    remote = tmp_path / "remote.git"
    subprocess.run(
        ["git", "init", "--bare", "-q", str(remote)], check=True, capture_output=True
    )
    work = tmp_path / "work"
    work.mkdir()
    git("init", "-q", cwd=work)
    git("remote", "add", "origin", str(remote), cwd=work)
    return work, remote


def test_restore_without_state_branch_returns_false(repo):
    work, _ = repo
    assert state.restore(work / "data" / "dealsteal.db", work) is False


def test_persist_then_restore_round_trip(repo, tmp_path):
    work, remote = repo
    db = work / "data" / "dealsteal.db"
    db.parent.mkdir(parents=True)
    db.write_bytes(b"\x00sqlite-ish bytes\xff")

    assert state.persist(db, work) is True

    fresh = tmp_path / "fresh"
    fresh.mkdir()
    git("init", "-q", cwd=fresh)
    git("remote", "add", "origin", str(remote), cwd=fresh)

    restored = fresh / "data" / "dealsteal.db"
    assert state.restore(restored, fresh) is True
    assert restored.read_bytes() == db.read_bytes()


def test_persist_skips_when_unchanged(repo):
    work, _ = repo
    db = work / "data" / "dealsteal.db"
    db.parent.mkdir(parents=True)
    db.write_bytes(b"first")
    assert state.persist(db, work) is True
    assert state.persist(db, work) is False


def test_persist_pushes_when_content_changes(repo):
    work, _ = repo
    db = work / "data" / "dealsteal.db"
    db.parent.mkdir(parents=True)
    db.write_bytes(b"first")
    assert state.persist(db, work) is True
    db.write_bytes(b"second")
    assert state.persist(db, work) is True


def test_state_branch_stays_a_single_commit(repo):
    work, remote = repo
    db = work / "data" / "dealsteal.db"
    db.parent.mkdir(parents=True)
    for index in range(3):
        db.write_bytes(f"version-{index}".encode())
        state.persist(db, work)

    count = subprocess.run(
        ["git", "--git-dir", str(remote), "rev-list", "--count", "state"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    assert count == "1"


def test_persist_without_database_is_a_no_op(repo):
    work, _ = repo
    assert state.persist(work / "data" / "missing.db", work) is False
