"""Deciding whether a memory is still trustworthy.

Pure: takes gathered facts about the world and returns a verdict. Reading files,
shelling out to git, and querying SQLite all happen in the caller.

``now`` is a parameter for the same reason ``dep_states`` is — both are external
facts about the world, gathered at the boundary. It also guarantees that every
memory in one recall is judged against a single instant.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from .models import Reason, Suspicion


@dataclass(frozen=True)
class DepState:
    """A dependency's stored hashes alongside what is on disk right now."""

    path: str
    anchor: str | None
    stored_blob_sha: str | None
    current_blob_sha: str | None
    stored_span_sha: str | None = None
    current_span_sha: str | None = None
    file_exists: bool = True
    anchor_found: bool = True
    diff: str | None = None


def determine_suspicion(
    dep_states: list[DepState] | tuple[DepState, ...],
    next_review_at: datetime | None,
    now: datetime,
) -> tuple[Suspicion, ...]:
    """Return every reason this memory should be treated as suspect."""
    suspicions: list[Suspicion] = []

    changed = [state for state in dep_states if _has_changed(state)]
    if changed:
        suspicions.append(
            Suspicion(
                reason=Reason.DEPS_CHANGED,
                evidence="\n\n".join(_describe(state) for state in changed),
            )
        )

    if next_review_at is not None and next_review_at <= now:
        suspicions.append(
            Suspicion(
                reason=Reason.EXPIRED,
                evidence=f"review due {_ago(next_review_at, now)} ago",
            )
        )

    return tuple(suspicions)


def _has_changed(state: DepState) -> bool:
    if not state.file_exists:
        return True

    # Fast path: the whole file is byte-identical, so nothing inside it moved.
    if state.stored_blob_sha is not None and state.stored_blob_sha == state.current_blob_sha:
        return False

    # No anchor means the memory depends on the file as a whole.
    if state.anchor is None:
        return True

    # The anchor is gone: renamed, moved, or deleted. Worth a look either way.
    if not state.anchor_found:
        return True

    # The file changed but the anchored span did not. This is the case the whole
    # anchor design exists for — do not flag it.
    return state.stored_span_sha != state.current_span_sha


def _describe(state: DepState) -> str:
    if not state.file_exists:
        return f"{state.path}: file no longer exists"

    target = f"{state.path}::{state.anchor}" if state.anchor else state.path

    if state.anchor is not None and not state.anchor_found:
        return f"{target}: anchor not found — renamed, moved, or removed"

    detail = f"{target}: changed since this was written"
    if state.diff:
        detail = f"{detail}\n{state.diff}"
    return detail


def _ago(then: datetime, now: datetime) -> str:
    seconds = int((now - then).total_seconds())
    if seconds < 3600:
        return f"{max(seconds // 60, 0)}m"
    if seconds < 86400:
        return f"{seconds // 3600}h"
    return f"{seconds // 86400}d"
