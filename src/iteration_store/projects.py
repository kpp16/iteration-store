"""Project identity and where each project's store lives.

One database file per project. Isolation is structural rather than a WHERE clause
that has to be correct on every query path — with an MCP server in the loop, a
missed filter would leak one project's memory into another's context.
"""

from __future__ import annotations

import hashlib
import os
import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from .gitutil import resolve_repo

HOME_ENV_VAR = "ITERATION_STORE_HOME"
DEFAULT_HOME = Path.home() / ".iteration-store"

_SLUG_RE = re.compile(r"[^a-zA-Z0-9._-]+")


@dataclass(frozen=True)
class Project:
    key: str
    root: Path
    is_git: bool


def store_home() -> Path:
    """Root of all stores. Overridable so tests and sandboxes stay out of $HOME."""
    override = os.environ.get(HOME_ENV_VAR)
    return Path(override).expanduser() if override else DEFAULT_HOME


def resolve_project(cwd: Path | str | None = None) -> Project:
    """Identify the project containing ``cwd``.

    Git repository root when there is one — worktrees resolving to the main
    repository — and the resolved working directory otherwise.
    """
    start = Path(cwd).resolve() if cwd else Path.cwd().resolve()

    repo = resolve_repo(start)
    if repo:
        return Project(key=project_key(repo.main_root), root=repo.main_root, is_git=True)

    return Project(key=project_key(start), root=start, is_git=False)


def project_key(root: Path) -> str:
    """A stable, filesystem-safe, human-recognizable key for a project root.

    The hash suffix disambiguates same-named directories in different places;
    the readable prefix means ``ls ~/.iteration-store`` is legible.
    """
    digest = hashlib.sha256(str(root).encode("utf-8")).hexdigest()[:12]
    slug = _SLUG_RE.sub("-", root.name).strip("-").lower() or "project"
    return f"{slug}-{digest}"


def store_path(project: Project) -> Path:
    directory = store_home() / project.key
    directory.mkdir(parents=True, exist_ok=True)
    return directory / "store.db"


def registry_path() -> Path:
    home = store_home()
    home.mkdir(parents=True, exist_ok=True)
    return home / "registry.db"


def register(project: Project) -> None:
    """Record the project so the future cross-project tool can enumerate stores.

    Nothing reads this yet. It exists now because it cannot be backfilled — a
    store whose project was never registered is invisible to any later sweep.
    """
    now = datetime.now(timezone.utc).isoformat()
    with sqlite3.connect(registry_path()) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS project (
                key         TEXT PRIMARY KEY,
                root        TEXT NOT NULL,
                is_git      INTEGER NOT NULL,
                created_at  TEXT NOT NULL,
                last_seen_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            INSERT INTO project (key, root, is_git, created_at, last_seen_at)
                 VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(key) DO UPDATE SET last_seen_at = excluded.last_seen_at
            """,
            (project.key, str(project.root), int(project.is_git), now, now),
        )


def list_projects() -> list[Project]:
    """Every project registered on this machine. For the future global tool."""
    path = registry_path()
    if not path.exists():
        return []

    with sqlite3.connect(path) as conn:
        conn.row_factory = sqlite3.Row
        try:
            rows = conn.execute("SELECT * FROM project ORDER BY last_seen_at DESC").fetchall()
        except sqlite3.OperationalError:
            return []

    return [
        Project(key=row["key"], root=Path(row["root"]), is_git=bool(row["is_git"]))
        for row in rows
    ]
