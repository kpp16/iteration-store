"""Git plumbing: hashing, repository resolution, and diffs.

Blob hashing is reimplemented rather than shelled out to. It is four lines, it is
exactly what ``git hash-object`` computes, and it turns a subprocess-per-file into
a function call — which matters because recall hashes every dependency of every
candidate.
"""

from __future__ import annotations

import hashlib
import subprocess
from dataclasses import dataclass
from pathlib import Path

# Diffs are evidence for an agent, not a patch to apply. Cap them so a large
# refactor cannot flood the context this system exists to protect.
MAX_DIFF_CHARS = 4000


@dataclass(frozen=True)
class RepoInfo:
    """``worktree_root`` is where you are; ``main_root`` is which store to use."""

    worktree_root: Path
    main_root: Path


def git_blob_sha(data: bytes) -> str:
    """The SHA-1 git would assign this content as a blob."""
    # SHA-1 here is git's object format, not a security property.
    digest = hashlib.sha1(usedforsecurity=False)
    digest.update(b"blob %d\0" % len(data))
    digest.update(data)
    return digest.hexdigest()


def sha256_hex(data: bytes) -> str:
    """Fallback content hash for files outside any git repository."""
    return hashlib.sha256(data).hexdigest()


def resolve_repo(cwd: Path) -> RepoInfo | None:
    """Locate the repository containing ``cwd``, or None if there is not one.

    Worktrees resolve to the main repository, so a worktree shares its parent
    project's store — the same project, mid-iteration.
    """
    top = _run(["rev-parse", "--show-toplevel"], cwd)
    if top is None:
        return None

    worktree_root = Path(top).resolve()
    main_root = worktree_root

    common = _run(["rev-parse", "--git-common-dir"], cwd)
    if common:
        common_dir = Path(common)
        if not common_dir.is_absolute():
            common_dir = (cwd / common_dir).resolve()
        if common_dir.name == ".git":
            main_root = common_dir.parent.resolve()

    return RepoInfo(worktree_root=worktree_root, main_root=main_root)


def head_commit(cwd: Path) -> str | None:
    """Current HEAD, or None in a repository with no commits yet."""
    return _run(["rev-parse", "HEAD"], cwd)


def diff_since(cwd: Path, commit: str, relpath: str) -> str | None:
    """What changed in ``relpath`` between ``commit`` and the working tree."""
    output = _run(["diff", commit, "--", relpath], cwd)
    if not output:
        return None
    if len(output) > MAX_DIFF_CHARS:
        return output[:MAX_DIFF_CHARS] + "\n… diff truncated …"
    return output


def _run(args: list[str], cwd: Path) -> str | None:
    """Run a git command, returning stripped stdout or None on any failure."""
    try:
        result = subprocess.run(
            ["git", *args],
            cwd=cwd,
            capture_output=True,
            text=True,
            check=False,
        )
    except (OSError, ValueError):
        return None

    if result.returncode != 0:
        return None
    return result.stdout.strip() or None
