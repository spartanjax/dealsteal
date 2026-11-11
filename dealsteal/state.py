"""Persist the SQLite database on an orphan `state` branch.

GitHub Actions runners are ephemeral, so "have I seen this listing?" has to
live somewhere durable. An orphan branch holding a single binary blob is the
least moving parts: no external service, no cache eviction, and the history
is auditable if you ever care to look.

Each persist is an orphan commit, force-pushed, so the branch never grows
beyond one commit and no worktree is disturbed.
"""

from __future__ import annotations

import logging
import os
import subprocess
from pathlib import Path

log = logging.getLogger(__name__)

STATE_BRANCH = "state"
DB_RELATIVE_PATH = "data/dealsteal.db"


class StateError(RuntimeError):
    pass


def _git(
    args: list[str],
    cwd: Path,
    check: bool = True,
    input_text: str | None = None,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=cwd,
        input=input_text,
        capture_output=True,
        text=True,
        check=check,
        env=env,
    )


def _git_bytes(args: list[str], cwd: Path) -> subprocess.CompletedProcess[bytes] | None:
    completed = subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, check=False
    )
    return completed if completed.returncode == 0 else None


def _identity_env() -> dict[str, str]:
    env = dict(os.environ)
    env.setdefault("GIT_AUTHOR_NAME", env.get("GITHUB_ACTOR") or "dealsteal-bot")
    env.setdefault("GIT_AUTHOR_EMAIL", "dealsteal@users.noreply.github.com")
    env.setdefault("GIT_COMMITTER_NAME", env["GIT_AUTHOR_NAME"])
    env.setdefault("GIT_COMMITTER_EMAIL", env["GIT_AUTHOR_EMAIL"])
    return env


def restore(
    db_path: Path,
    repo_root: Path,
    branch: str = STATE_BRANCH,
    remote: str = "origin",
) -> bool:
    """Fetch the state branch and drop its database into place."""
    db_path.parent.mkdir(parents=True, exist_ok=True)
    fetched = _git(
        ["fetch", "--depth=1", remote, branch], repo_root, check=False
    )
    if fetched.returncode != 0:
        log.info("no %s/%s branch yet, starting with an empty database", remote, branch)
        return False

    blob = _git_bytes(["show", f"FETCH_HEAD:{DB_RELATIVE_PATH}"], repo_root)
    if blob is None:
        log.info("state branch has no database yet")
        return False

    db_path.write_bytes(blob.stdout)
    log.info("restored %s bytes from %s/%s", len(blob.stdout), remote, branch)
    return True


def _build_tree(repo_root: Path, relative_path: str, blob_sha: str) -> str:
    """Build the tree objects for a nested path, innermost first.

    `git mktree` takes one directory level at a time, so data/dealsteal.db
    needs a tree for `data` and then a root tree pointing at it.
    """
    parts = relative_path.split("/")
    entry = f"100644 blob {blob_sha}\t{parts[-1]}\n"
    sha = _git(["mktree"], repo_root, input_text=entry).stdout.strip()
    for directory in reversed(parts[:-1]):
        entry = f"040000 tree {sha}\t{directory}\n"
        sha = _git(["mktree"], repo_root, input_text=entry).stdout.strip()
    return sha


def persist(
    db_path: Path,
    repo_root: Path,
    branch: str = STATE_BRANCH,
    remote: str = "origin",
    message: str = "state: dealsteal run",
) -> bool:
    """Commit the database and force-push it as a single-commit orphan branch."""
    if not db_path.is_file():
        log.debug("no database to persist")
        return False

    blob_sha = _git(
        ["hash-object", "-w", str(db_path)], repo_root
    ).stdout.strip()

    for ref in ("FETCH_HEAD", f"{remote}/{branch}"):
        current = _git(
            ["rev-parse", f"{ref}:{DB_RELATIVE_PATH}"], repo_root, check=False
        )
        if current.returncode == 0 and current.stdout.strip() == blob_sha:
            log.debug("database unchanged, skipping state push")
            return False

    tree = _build_tree(repo_root, DB_RELATIVE_PATH, blob_sha)

    commit = _git(
        ["commit-tree", tree, "-m", message], repo_root, env=_identity_env()
    ).stdout.strip()

    pushed = _git(
        ["push", "--force", remote, f"{commit}:refs/heads/{branch}"],
        repo_root,
        check=False,
    )
    if pushed.returncode != 0:
        raise StateError(f"could not push state branch: {pushed.stderr.strip()}")

    log.info("persisted state to %s/%s (%s)", remote, branch, commit[:8])
    return True
