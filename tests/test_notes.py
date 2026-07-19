"""Notes — the low-bar table.

Write-only by design, so these cover the write path and, above all, that
provenance actually lands. Provenance is the one thing that cannot be
reconstructed once it is lost.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from iteration_store.store import Store


class TestWriting:
    def test_records_a_note(self, store: Store):
        note = store.note("Tried the regex approach for anchors; brace nesting broke it.")
        assert note.id
        assert store.note_count() == 1

    def test_rejects_empty_bodies(self, store: Store):
        with pytest.raises(ValueError):
            store.note("   ")

    def test_accepts_anything_otherwise(self, store: Store):
        # No curation bar — that is the entire point of the table.
        store.note("hmm")
        store.note("?" * 5000)
        assert store.note_count() == 2


class TestProvenance:
    def test_captures_author_and_session(self, store: Store):
        note = store.note("An observation.", author="Explore", session_id="abc123")
        assert note.author == "Explore"
        assert note.session_id == "abc123"

    def test_captures_the_commit(self, store: Store):
        assert store.note("An observation.").cwd_commit is not None

    def test_captures_the_time(self, store: Store):
        assert store.note("An observation.").created_at.tzinfo is not None

    def test_records_paths(self, store: Store):
        note = store.note("Auth is confusing.", paths=["services/auth.py"])
        assert note.paths == ("services/auth.py",)

    def test_paths_are_advisory_not_validated(self, store: Store):
        # Unlike a memory's dependencies, a note's paths are context, not a claim
        # about the world — a path that does not exist is recorded as written.
        note = store.note("Thinking about a file that isn't there yet.",
                          paths=["services/not_yet_written.py"])
        assert note.paths == ("services/not_yet_written.py",)

    def test_survives_reopening(self, git_repo: Path):
        with Store.open(git_repo) as first:
            first.note("Durable across sessions.", author="claude-code")
        with Store.open(git_repo) as second:
            assert second.note_count() == 1


class TestIsolationFromMemories:
    def test_notes_do_not_appear_in_recall(self, store: Store):
        # recall() is memory-only until notes retrieval is designed.
        store.note("A note that mentions billing.")
        assert store.recall("billing") == []

    def test_notes_do_not_count_as_memories(self, store: Store):
        store.note("A note.")
        assert store.count() == 0

    def test_indexed_for_later_search(self, store: Store):
        # Nothing reads this yet; the index exists so the corpus is never
        # unqueryable while the querying question stays open.
        store.note("The N+1 query lives in the session lookup.")
        rows = store._conn.execute(
            "SELECT rowid FROM note_fts WHERE note_fts MATCH ?", ('"session"',)
        ).fetchall()
        assert len(rows) == 1


class TestNonGitProjects:
    def test_commit_is_none_without_a_repository(self, tmp_path: Path):
        loose = tmp_path / "loose"
        loose.mkdir()
        with Store.open(loose) as store:
            assert store.note("No repo here.").cwd_commit is None
