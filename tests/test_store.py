"""End-to-end store behavior against real repositories."""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import pytest

from iteration_store.models import Reason, Strategy
from iteration_store.store import Store

from .conftest import commit_all

REWRITTEN_AUTH = '''\
"""Auth service."""

TOKEN_TTL = 7200


def rotate_token(token):
    """Rotate a token."""
    validate(token)
    return token[::-1]


def validate(token):
    if not token:
        raise ValueError("empty")
    return True
'''

CHANGED_ROTATE = '''\
"""Auth service."""

TOKEN_TTL = 3600


def rotate_token(token):
    """Rotate a token, twice over."""
    validate(token)
    return token[::-1] + "!"


def validate(token):
    if not token:
        raise ValueError("empty")
    return True
'''


class TestRemember:
    def test_round_trips(self, store: Store):
        stored = store.remember("Tokens rotate on every request.", kind="convention")
        assert store.get(stored.id).content == "Tokens rotate on every request."
        assert stored.kind == "convention"

    def test_records_dependencies_with_hashes(self, store: Store):
        memory = store.remember("Rotation reverses the token.",
                                deps=["services/auth.py::rotate_token"])
        (dep,) = memory.deps
        assert dep.path == "services/auth.py"
        assert dep.anchor == "rotate_token"
        assert dep.blob_sha and dep.span_sha

    def test_captures_the_write_commit(self, store: Store, git_repo: Path):
        memory = store.remember("x", deps=["services/auth.py"])
        assert memory.write_commit is not None

    def test_infers_strategy_from_what_was_supplied(self, store: Store):
        assert store.remember("a", deps=["services/auth.py"]).validation_strategy is Strategy.DEPS
        assert store.remember("b", review_interval=timedelta(days=30)).validation_strategy is Strategy.DECAY
        assert store.remember("c").validation_strategy is Strategy.MANUAL

    def test_rejects_empty_content(self, store: Store):
        with pytest.raises(ValueError):
            store.remember("   ")

    def test_rejects_a_missing_file(self, store: Store):
        with pytest.raises(ValueError, match="does not exist"):
            store.remember("x", deps=["services/nope.py"])

    def test_rejects_an_unresolvable_anchor(self, store: Store):
        # Better to fail at write time than to store something suspect from birth.
        with pytest.raises(ValueError, match="anchor not found"):
            store.remember("x", deps=["services/auth.py::no_such_function"])


class TestRecall:
    def test_finds_by_keyword(self, store: Store):
        store.remember("Billing charges in cents, never dollars.")
        store.remember("Auth tokens rotate hourly.")

        results = store.recall("billing cents")
        assert len(results) == 1
        assert "cents" in results[0].memory.content

    def test_returns_recent_memories_without_a_query(self, store: Store):
        store.remember("first")
        store.remember("second")
        assert len(store.recall()) == 2

    def test_tolerates_punctuation_in_queries(self, store: Store):
        store.remember("Rate limits apply per-tenant.")
        assert store.recall('what about "rate limits"? (per-tenant)')

    def test_respects_the_limit(self, store: Store):
        for index in range(5):
            store.remember(f"fact number {index}")
        assert len(store.recall(limit=2)) == 2

    def test_records_usage(self, store: Store):
        stored = store.remember("tracked fact")
        store.recall("tracked")
        store.recall("tracked")

        refreshed = store.get(stored.id)
        assert refreshed.recall_count == 2
        assert refreshed.last_recalled_at is not None


class TestValidation:
    def test_clean_when_nothing_changed(self, store: Store):
        store.remember("Rotation reverses the token.",
                       deps=["services/auth.py::rotate_token"])
        (result,) = store.recall("rotation")
        assert not result.is_suspect

    def test_edit_elsewhere_in_the_file_does_not_flag(self, store: Store, git_repo: Path):
        # The false-invalidation case the anchor design exists to prevent. A
        # store that flags this looks like it works while being useless.
        store.remember("Rotation reverses the token.",
                       deps=["services/auth.py::rotate_token"])
        (git_repo / "services" / "auth.py").write_text(REWRITTEN_AUTH)
        commit_all(git_repo, "change the ttl constant")

        (result,) = store.recall("rotation")
        assert not result.is_suspect

    def test_edit_inside_the_anchor_flags(self, store: Store, git_repo: Path):
        store.remember("Rotation reverses the token.",
                       deps=["services/auth.py::rotate_token"])
        (git_repo / "services" / "auth.py").write_text(CHANGED_ROTATE)
        commit_all(git_repo, "change rotation")

        (result,) = store.recall("rotation")
        assert result.reasons == (Reason.DEPS_CHANGED,)

    def test_flags_include_a_diff_as_evidence(self, store: Store, git_repo: Path):
        store.remember("Rotation reverses the token.",
                       deps=["services/auth.py::rotate_token"])
        (git_repo / "services" / "auth.py").write_text(CHANGED_ROTATE)
        commit_all(git_repo, "change rotation")

        (result,) = store.recall("rotation")
        assert "twice over" in result.suspicions[0].evidence

    def test_unanchored_dependency_flags_on_any_change(self, store: Store, git_repo: Path):
        store.remember("Auth lives here.", deps=["services/auth.py"])
        (git_repo / "services" / "auth.py").write_text(REWRITTEN_AUTH)
        commit_all(git_repo, "unrelated edit")

        (result,) = store.recall("auth")
        assert result.is_suspect

    def test_deleted_dependency_flags(self, store: Store, git_repo: Path):
        store.remember("Billing charges once.", deps=["services/billing.py"])
        (git_repo / "services" / "billing.py").unlink()
        commit_all(git_repo, "remove billing")

        (result,) = store.recall("billing")
        assert "no longer exists" in result.suspicions[0].evidence

    def test_removed_anchor_flags(self, store: Store, git_repo: Path):
        store.remember("Rotation reverses the token.",
                       deps=["services/auth.py::rotate_token"])
        (git_repo / "services" / "auth.py").write_text("def validate(token):\n    return True\n")
        commit_all(git_repo, "drop rotate_token")

        (result,) = store.recall("rotation")
        assert "renamed, moved, or removed" in result.suspicions[0].evidence


class TestConfirmAndRevise:
    def test_confirm_clears_suspicion(self, store: Store, git_repo: Path):
        memory = store.remember("Rotation reverses the token.",
                                deps=["services/auth.py::rotate_token"])
        (git_repo / "services" / "auth.py").write_text(CHANGED_ROTATE)
        commit_all(git_repo, "change rotation")
        assert store.recall("rotation")[0].is_suspect

        store.confirm(memory.id)
        assert not store.recall("rotation")[0].is_suspect

    def test_confirm_rebaselines_the_write_commit(self, store: Store, git_repo: Path):
        memory = store.remember("x", deps=["services/auth.py"])
        (git_repo / "services" / "auth.py").write_text(REWRITTEN_AUTH)
        head = commit_all(git_repo, "edit")

        assert store.confirm(memory.id).write_commit == head

    def test_revise_updates_content_and_clears_suspicion(self, store: Store, git_repo: Path):
        memory = store.remember("Rotation reverses the token.",
                                deps=["services/auth.py::rotate_token"])
        (git_repo / "services" / "auth.py").write_text(CHANGED_ROTATE)
        commit_all(git_repo, "change rotation")

        revised = store.revise(memory.id, content="Rotation reverses and appends.")
        assert revised.content == "Rotation reverses and appends."
        assert not store.recall("rotation")[0].is_suspect

    def test_revise_can_repoint_dependencies(self, store: Store):
        memory = store.remember("Charging is simple.", deps=["services/auth.py"])
        revised = store.revise(memory.id, deps=["services/billing.py::charge"])
        assert [dep.path for dep in revised.deps] == ["services/billing.py"]

    def test_revised_content_is_searchable(self, store: Store):
        memory = store.remember("original wording")
        store.revise(memory.id, content="replacement wording about quotas")

        assert store.recall("quotas")
        assert not store.recall("original")


class TestIsolation:
    def test_projects_cannot_see_each_other(self, git_repo: Path, tmp_path: Path):
        other = tmp_path / "other"
        other.mkdir()

        with Store.open(git_repo) as first, Store.open(other) as second:
            first.remember("only in the first project")
            assert second.count() == 0
            assert second.recall("first") == []

    def test_reopening_sees_previous_writes(self, git_repo: Path):
        with Store.open(git_repo) as first:
            first.remember("durable across sessions")
        with Store.open(git_repo) as second:
            assert second.count() == 1


class TestNonGitProjects:
    def test_works_without_a_repository(self, tmp_path: Path):
        loose = tmp_path / "loose"
        loose.mkdir()
        (loose / "notes.txt").write_text("hello\n")

        with Store.open(loose) as store:
            memory = store.remember("Notes are plain text.", deps=["notes.txt"])
            assert memory.write_commit is None
            assert not store.recall("notes")[0].is_suspect

            (loose / "notes.txt").write_text("goodbye\n")
            assert store.recall("notes")[0].is_suspect
