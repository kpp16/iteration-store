"""The store: remember, recall, confirm, revise.

A plain library. The MCP server (phase 2) wraps this and adds no storage logic of
its own.
"""

from __future__ import annotations

import functools
import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable, Sequence

from . import anchors, gitutil
from .models import Dep, Memory, Note, RecallResult, Strategy
from .projects import Project, register, resolve_project, store_path
from .schema import connect
from .suspicion import DepState, determine_suspicion

DepSpec = str | tuple[str, str | None]


def _synchronized(method):
    """Serialize an operation against other threads sharing this Store."""

    @functools.wraps(method)
    def wrapper(self, *args, **kwargs):
        with self._lock:
            return method(self, *args, **kwargs)

    return wrapper


class Store:
    """Memory for a single project. One database file, no cross-project reach."""

    def __init__(self, conn: sqlite3.Connection, project: Project) -> None:
        self._conn = conn
        self.project = project

        # Cross-process contention is handled by WAL plus busy_timeout. This
        # guards the other axis: one connection shared across threads, which
        # SQLite does not serialize for us. Held only for the duration of a
        # single operation, so it never blocks another process.
        self._lock = threading.RLock()

    @classmethod
    def open(cls, cwd: Path | str | None = None) -> "Store":
        project = resolve_project(cwd)
        register(project)
        return cls(connect(str(store_path(project))), project)

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> "Store":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    # ------------------------------------------------------------------ write

    @_synchronized
    def remember(
        self,
        content: str,
        *,
        kind: str | None = None,
        deps: Sequence[DepSpec] = (),
        review_interval: timedelta | None = None,
    ) -> Memory:
        """Store a fact.

        No gatekeeping on *what* gets stored — that is governed by the MCP tool
        description, deliberately. The one thing rejected here is a dependency
        that cannot be resolved, because an unresolvable anchor would be silently
        suspect from birth.
        """
        if not content.strip():
            raise ValueError("content must not be empty")

        now = _utcnow()
        parsed = [_parse_dep_spec(spec) for spec in deps]
        records = [self._build_dep(path, anchor) for path, anchor in parsed]

        strategy = (
            Strategy.DEPS
            if records
            else Strategy.DECAY
            if review_interval
            else Strategy.MANUAL
        )

        cursor = self._conn.execute(
            """
            INSERT INTO memory (
                content, kind, created_at, last_confirmed_at, write_commit,
                validation_strategy, review_interval_seconds, next_review_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                content,
                kind,
                now.isoformat(),
                now.isoformat(),
                self._head(),
                strategy.value,
                _interval_seconds(review_interval),
                _next_review(now, review_interval),
            ),
        )
        memory_id = int(cursor.lastrowid)
        self._insert_deps(memory_id, records)
        self._conn.commit()

        return self.get(memory_id)

    @_synchronized
    def confirm(self, memory_id: int) -> Memory:
        """Assert a suspect memory is still true, and re-baseline it.

        Dependency hashes are refreshed to the current file contents and the
        write commit moves to HEAD, so the next diff is measured from the point
        of confirmation rather than from the original write.
        """
        memory = self.get(memory_id)
        now = _utcnow()

        refreshed = [self._build_dep(dep.path, dep.anchor) for dep in memory.deps]
        self._conn.execute("DELETE FROM memory_dep WHERE memory_id = ?", (memory_id,))
        self._insert_deps(memory_id, refreshed)

        self._conn.execute(
            """
            UPDATE memory
               SET last_confirmed_at = ?, write_commit = ?, next_review_at = ?
             WHERE id = ?
            """,
            (
                now.isoformat(),
                self._head(),
                _next_review(now, memory.review_interval),
                memory_id,
            ),
        )
        self._conn.commit()
        return self.get(memory_id)

    @_synchronized
    def revise(
        self,
        memory_id: int,
        *,
        content: str | None = None,
        kind: str | None = None,
        deps: Sequence[DepSpec] | None = None,
        review_interval: timedelta | None = None,
    ) -> Memory:
        """Rewrite a memory that turned out to be wrong, and re-baseline it."""
        memory = self.get(memory_id)
        now = _utcnow()

        interval = review_interval if review_interval is not None else memory.review_interval

        if deps is not None:
            parsed = [_parse_dep_spec(spec) for spec in deps]
            records = [self._build_dep(path, anchor) for path, anchor in parsed]
        else:
            records = [self._build_dep(dep.path, dep.anchor) for dep in memory.deps]

        self._conn.execute("DELETE FROM memory_dep WHERE memory_id = ?", (memory_id,))
        self._insert_deps(memory_id, records)

        strategy = (
            Strategy.DEPS if records else Strategy.DECAY if interval else Strategy.MANUAL
        )

        self._conn.execute(
            """
            UPDATE memory
               SET content = ?, kind = ?, last_confirmed_at = ?, write_commit = ?,
                   validation_strategy = ?, review_interval_seconds = ?, next_review_at = ?
             WHERE id = ?
            """,
            (
                content if content is not None else memory.content,
                kind if kind is not None else memory.kind,
                now.isoformat(),
                self._head(),
                strategy.value,
                _interval_seconds(interval),
                _next_review(now, interval),
                memory_id,
            ),
        )
        self._conn.commit()
        return self.get(memory_id)

    @_synchronized
    def note(
        self,
        body: str,
        *,
        author: str | None = None,
        session_id: str | None = None,
        paths: Sequence[str] = (),
    ) -> Note:
        """Record a free-form observation, dead end, or piece of reasoning.

        No curation bar and no validation — the counterweight to `remember`, which
        rejects anything a single file read would answer. Notes are write-only for
        now; nothing reads them back yet.

        Provenance is captured because it cannot be reconstructed later. Which
        agent wrote this, in what session, against which commit — every plausible
        future retrieval strategy needs some of it, and none of it survives being
        deferred.
        """
        if not body.strip():
            raise ValueError("body must not be empty")

        now = _utcnow()
        cursor = self._conn.execute(
            """
            INSERT INTO note (body, author, session_id, created_at, cwd_commit)
                 VALUES (?, ?, ?, ?, ?)
            """,
            (body, author, session_id, now.isoformat(), self._head()),
        )
        note_id = int(cursor.lastrowid)

        # Advisory only: unlike a memory's dependencies, these are never resolved
        # or hashed, so a path that does not exist is recorded as written.
        self._conn.executemany(
            "INSERT INTO note_path (note_id, path) VALUES (?, ?)",
            [(note_id, _relative(path, self.project.root)) for path in paths],
        )
        self._conn.commit()

        return Note(
            id=note_id,
            body=body,
            author=author,
            session_id=session_id,
            created_at=now,
            cwd_commit=self._head(),
            paths=tuple(_relative(path, self.project.root) for path in paths),
        )

    # ------------------------------------------------------------------- read

    @_synchronized
    def recall(self, query: str | None = None, *, limit: int = 10) -> list[RecallResult]:
        """Retrieve memories, each carrying any reason to distrust it.

        Suspect memories are returned rather than hidden — most edits do not
        falsify a fact, and the caller is better placed to judge than a hash is.
        """
        rows = self._search(query, limit)

        # One clock read for the whole operation, so every memory in a result set
        # is judged against the same instant.
        now = _utcnow()

        results = []
        for row, score in rows:
            memory = self._hydrate(row)
            states = [self._dep_state(dep, memory.write_commit) for dep in memory.deps]
            results.append(
                RecallResult(
                    memory=memory,
                    score=score,
                    suspicions=determine_suspicion(states, memory.next_review_at, now),
                )
            )

        self._mark_recalled([result.memory.id for result in results], now)
        return results

    @_synchronized
    def get(self, memory_id: int) -> Memory:
        row = self._conn.execute("SELECT * FROM memory WHERE id = ?", (memory_id,)).fetchone()
        if row is None:
            raise KeyError(f"no memory with id {memory_id}")
        return self._hydrate(row)

    @_synchronized
    def count(self) -> int:
        return int(self._conn.execute("SELECT COUNT(*) FROM memory").fetchone()[0])

    @_synchronized
    def note_count(self) -> int:
        """Notes have no read path; this exists for tests and diagnostics."""
        return int(self._conn.execute("SELECT COUNT(*) FROM note").fetchone()[0])

    # --------------------------------------------------------------- internals

    def _search(self, query: str | None, limit: int) -> list[tuple[sqlite3.Row, float]]:
        if not query or not _fts_query(query):
            rows = self._conn.execute(
                "SELECT * FROM memory ORDER BY last_confirmed_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
            return [(row, 0.0) for row in rows]

        rows = self._conn.execute(
            """
            SELECT memory.*, bm25(memory_fts) AS score
              FROM memory_fts
              JOIN memory ON memory.id = memory_fts.rowid
             WHERE memory_fts MATCH ?
             ORDER BY score
             LIMIT ?
            """,
            (_fts_query(query), limit),
        ).fetchall()
        return [(row, float(row["score"])) for row in rows]

    def _build_dep(self, path: str, anchor: str | None) -> Dep:
        absolute = (self.project.root / path).resolve()
        if not absolute.is_file():
            raise ValueError(f"dependency does not exist: {path}")

        data = absolute.read_bytes()
        blob_sha = gitutil.git_blob_sha(data) if self.project.is_git else gitutil.sha256_hex(data)

        span_sha = None
        if anchor:
            source = data.decode("utf-8", errors="replace")
            span = anchors.resolve_anchor(source, anchor)
            if span is None:
                raise ValueError(f"anchor not found in {path}: {anchor}")
            span_sha = anchors.hash_text(anchors.extract_span(source, span))

        return Dep(path=_relative(path, self.project.root), anchor=anchor,
                   blob_sha=blob_sha, span_sha=span_sha)

    def _dep_state(self, dep: Dep, write_commit: str | None) -> DepState:
        absolute = (self.project.root / dep.path).resolve()
        if not absolute.is_file():
            return DepState(
                path=dep.path,
                anchor=dep.anchor,
                stored_blob_sha=dep.blob_sha,
                current_blob_sha=None,
                stored_span_sha=dep.span_sha,
                file_exists=False,
            )

        data = absolute.read_bytes()
        current_blob = (
            gitutil.git_blob_sha(data) if self.project.is_git else gitutil.sha256_hex(data)
        )

        if current_blob == dep.blob_sha:
            # Bytes are identical, so nothing inside moved. Skip anchor
            # resolution and the diff entirely — this is the common case.
            return DepState(
                path=dep.path,
                anchor=dep.anchor,
                stored_blob_sha=dep.blob_sha,
                current_blob_sha=current_blob,
                stored_span_sha=dep.span_sha,
                current_span_sha=dep.span_sha,
            )

        return self._changed_state(dep, data, current_blob, write_commit)

    def _changed_state(
        self, dep: Dep, data: bytes, current_blob: str, write_commit: str | None
    ) -> DepState:
        """Only reached when the file's bytes differ — the expensive path."""
        anchor_found = True
        current_span = None

        if dep.anchor:
            source = data.decode("utf-8", errors="replace")
            span = anchors.resolve_anchor(source, dep.anchor)
            if span is None:
                anchor_found = False
            else:
                current_span = anchors.hash_text(anchors.extract_span(source, span))

        # Diff is evidence for a human or agent, so only fetch it if the span
        # actually moved — an unchanged span produces no suspicion to explain.
        diff = None
        span_moved = not dep.anchor or not anchor_found or current_span != dep.span_sha
        if span_moved and write_commit and self.project.is_git:
            diff = gitutil.diff_since(self.project.root, write_commit, dep.path)

        return DepState(
            path=dep.path,
            anchor=dep.anchor,
            stored_blob_sha=dep.blob_sha,
            current_blob_sha=current_blob,
            stored_span_sha=dep.span_sha,
            current_span_sha=current_span,
            anchor_found=anchor_found,
            diff=diff,
        )

    def _insert_deps(self, memory_id: int, deps: Iterable[Dep]) -> None:
        self._conn.executemany(
            """
            INSERT INTO memory_dep (memory_id, path, anchor, blob_sha, span_sha)
                 VALUES (?, ?, ?, ?, ?)
            """,
            [(memory_id, dep.path, dep.anchor, dep.blob_sha, dep.span_sha) for dep in deps],
        )

    def _mark_recalled(self, memory_ids: list[int], now: datetime) -> None:
        """Usage signal. Nothing consumes it yet; it cannot be backfilled later."""
        if not memory_ids:
            return
        self._conn.executemany(
            """
            UPDATE memory
               SET last_recalled_at = ?, recall_count = recall_count + 1
             WHERE id = ?
            """,
            [(now.isoformat(), memory_id) for memory_id in memory_ids],
        )
        self._conn.commit()

    def _hydrate(self, row: sqlite3.Row) -> Memory:
        dep_rows = self._conn.execute(
            "SELECT path, anchor, blob_sha, span_sha FROM memory_dep WHERE memory_id = ?",
            (row["id"],),
        ).fetchall()

        interval_seconds = row["review_interval_seconds"]

        return Memory(
            id=row["id"],
            content=row["content"],
            kind=row["kind"],
            created_at=_parse_time(row["created_at"]),
            last_confirmed_at=_parse_time(row["last_confirmed_at"]),
            write_commit=row["write_commit"],
            validation_strategy=Strategy(row["validation_strategy"]),
            review_interval=timedelta(seconds=interval_seconds) if interval_seconds else None,
            next_review_at=_parse_time(row["next_review_at"]),
            last_recalled_at=_parse_time(row["last_recalled_at"]),
            recall_count=row["recall_count"],
            deps=tuple(
                Dep(
                    path=dep["path"],
                    anchor=dep["anchor"],
                    blob_sha=dep["blob_sha"],
                    span_sha=dep["span_sha"],
                )
                for dep in dep_rows
            ),
        )

    def _head(self) -> str | None:
        return gitutil.head_commit(self.project.root) if self.project.is_git else None


def _parse_dep_spec(spec: DepSpec) -> tuple[str, str | None]:
    """Accept ``"path"``, ``"path::anchor"``, or ``(path, anchor)``."""
    if isinstance(spec, tuple):
        return spec[0], spec[1]
    path, sep, anchor = spec.partition("::")
    return (path, anchor) if sep and anchor else (path, None)


def _relative(path: str, root: Path) -> str:
    """Store paths relative to the project root so the store stays portable."""
    candidate = Path(path)
    if not candidate.is_absolute():
        return candidate.as_posix()
    try:
        return candidate.resolve().relative_to(root).as_posix()
    except ValueError:
        return candidate.as_posix()


def _fts_query(query: str) -> str:
    """Turn free text into a forgiving FTS5 expression.

    Every token is quoted and OR-ed, so punctuation in a natural-language query
    cannot raise a syntax error from the FTS engine.
    """
    tokens = [token for token in "".join(
        char if char.isalnum() or char in "_-" else " " for char in query
    ).split() if token]
    return " OR ".join(f'"{token}"' for token in tokens)


def _interval_seconds(interval: timedelta | None) -> int | None:
    return int(interval.total_seconds()) if interval else None


def _next_review(now: datetime, interval: timedelta | None) -> str | None:
    return (now + interval).isoformat() if interval else None


def _parse_time(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value else None


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)
