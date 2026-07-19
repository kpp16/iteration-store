"""Shared fixtures.

Real SQLite and real git repositories throughout — no mocks. SQLite is a
sub-millisecond fixture, and the git behavior (blob hashing, diffs, worktrees) is
precisely what needs verifying, so mocking it would only assert that the mock
returns what it was told to.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from iteration_store.projects import HOME_ENV_VAR
from iteration_store.store import Store


@pytest.fixture(autouse=True)
def isolated_home(tmp_path, monkeypatch):
    """Keep every test's stores inside tmp_path rather than the real $HOME."""
    home = tmp_path / "store-home"
    monkeypatch.setenv(HOME_ENV_VAR, str(home))
    return home


def git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=repo,
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout.strip()


def commit_all(repo: Path, message: str = "change") -> str:
    git(repo, "add", "-A")
    git(repo, "commit", "-m", message, "--allow-empty")
    return git(repo, "rev-parse", "HEAD")


@pytest.fixture
def git_repo(tmp_path) -> Path:
    """A real repository with one commit and a small source tree."""
    repo = tmp_path / "project"
    (repo / "services").mkdir(parents=True)

    git(repo.parent, "init", repo.name)
    git(repo, "config", "user.email", "test@example.com")
    git(repo, "config", "user.name", "Test")
    git(repo, "config", "commit.gpgsign", "false")

    (repo / "services" / "auth.py").write_text(AUTH_SOURCE)
    (repo / "services" / "billing.py").write_text("def charge(amount):\n    return amount\n")
    commit_all(repo, "initial")

    return repo


@pytest.fixture
def store(git_repo) -> Store:
    with Store.open(git_repo) as opened:
        yield opened


AUTH_SOURCE = '''\
"""Auth service."""

TOKEN_TTL = 3600


def rotate_token(token):
    """Rotate a token."""
    validate(token)
    return token[::-1]


def validate(token):
    if not token:
        raise ValueError("empty")
    return True
'''
