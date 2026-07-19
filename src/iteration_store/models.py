"""Data shapes shared across the store."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum


class Reason(str, Enum):
    """Why a memory is suspect. A memory may carry more than one."""

    DEPS_CHANGED = "deps_changed"
    EXPIRED = "expired"


class Strategy(str, Enum):
    DEPS = "deps"
    DECAY = "decay"
    MANUAL = "manual"


@dataclass(frozen=True)
class Dep:
    """A file (optionally a span within it) a memory was written against."""

    path: str
    anchor: str | None = None
    blob_sha: str | None = None
    span_sha: str | None = None


@dataclass(frozen=True)
class Suspicion:
    reason: Reason
    evidence: str


@dataclass(frozen=True)
class Memory:
    id: int
    content: str
    kind: str | None
    created_at: datetime
    last_confirmed_at: datetime
    write_commit: str | None
    validation_strategy: Strategy
    review_interval: timedelta | None
    next_review_at: datetime | None
    last_recalled_at: datetime | None
    recall_count: int
    deps: tuple[Dep, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class Note:
    """A free-form observation. No curation bar, and no read path yet.

    ``paths`` is advisory context, never validated against disk — notes do not
    participate in the staleness machinery that governs memories.
    """

    id: int
    body: str
    author: str | None
    session_id: str | None
    created_at: datetime
    cwd_commit: str | None
    paths: tuple[str, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class RecallResult:
    memory: Memory
    score: float
    suspicions: tuple[Suspicion, ...] = field(default_factory=tuple)

    @property
    def is_suspect(self) -> bool:
        return bool(self.suspicions)

    @property
    def reasons(self) -> tuple[Reason, ...]:
        return tuple(s.reason for s in self.suspicions)
