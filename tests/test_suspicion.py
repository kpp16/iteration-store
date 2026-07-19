"""Suspicion determination — pure, so every combination is cheap to cover.

The false-invalidation cases matter most: a store that flags everything fails just
as completely as one that flags nothing, and it fails while still looking like it
works.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from iteration_store.models import Reason
from iteration_store.suspicion import DepState, determine_suspicion

NOW = datetime(2026, 7, 18, 12, 0, tzinfo=timezone.utc)


def dep(**overrides) -> DepState:
    base = dict(
        path="services/auth.py",
        anchor="rotate_token",
        stored_blob_sha="blob-a",
        current_blob_sha="blob-a",
        stored_span_sha="span-a",
        current_span_sha="span-a",
    )
    return DepState(**{**base, **overrides})


class TestDependencies:
    def test_no_dependencies_is_never_suspect(self):
        assert determine_suspicion([], None, NOW) == ()

    def test_untouched_file_is_not_suspect(self):
        assert determine_suspicion([dep()], None, NOW) == ()

    def test_file_changed_outside_the_anchor_is_not_suspect(self):
        # The whole point of anchoring: an edit elsewhere in the file leaves the
        # anchored span byte-identical, so the fact survives.
        state = dep(current_blob_sha="blob-b", current_span_sha="span-a")
        assert determine_suspicion([state], None, NOW) == ()

    def test_anchored_span_changed_is_suspect(self):
        state = dep(current_blob_sha="blob-b", current_span_sha="span-b")
        (suspicion,) = determine_suspicion([state], None, NOW)
        assert suspicion.reason is Reason.DEPS_CHANGED

    def test_unanchored_dependency_flags_on_any_file_change(self):
        state = dep(anchor=None, stored_span_sha=None, current_span_sha=None,
                    current_blob_sha="blob-b")
        (suspicion,) = determine_suspicion([state], None, NOW)
        assert suspicion.reason is Reason.DEPS_CHANGED

    def test_missing_anchor_is_suspect(self):
        state = dep(current_blob_sha="blob-b", anchor_found=False, current_span_sha=None)
        (suspicion,) = determine_suspicion([state], None, NOW)
        assert "renamed, moved, or removed" in suspicion.evidence

    def test_deleted_file_is_suspect(self):
        state = dep(file_exists=False, current_blob_sha=None, current_span_sha=None)
        (suspicion,) = determine_suspicion([state], None, NOW)
        assert "no longer exists" in suspicion.evidence

    def test_one_changed_dependency_among_many_is_enough(self):
        states = [dep(), dep(path="services/billing.py",
                             current_blob_sha="blob-b", current_span_sha="span-b")]
        (suspicion,) = determine_suspicion(states, None, NOW)
        assert "services/billing.py" in suspicion.evidence
        assert "services/auth.py" not in suspicion.evidence

    def test_diff_is_included_as_evidence(self):
        state = dep(current_blob_sha="blob-b", current_span_sha="span-b",
                    diff="@@ -1 +1 @@\n-old\n+new")
        (suspicion,) = determine_suspicion([state], None, NOW)
        assert "+new" in suspicion.evidence


class TestExpiry:
    """Store-level expiry is deferred; the pure verdict is free to cover here."""

    def test_future_deadline_is_not_suspect(self):
        assert determine_suspicion([], NOW + timedelta(days=1), NOW) == ()

    def test_passed_deadline_is_suspect(self):
        (suspicion,) = determine_suspicion([], NOW - timedelta(days=3), NOW)
        assert suspicion.reason is Reason.EXPIRED
        assert "3d" in suspicion.evidence

    def test_no_deadline_never_expires(self):
        assert determine_suspicion([], None, NOW) == ()


class TestCombined:
    def test_both_reasons_can_apply_at_once(self):
        state = dep(current_blob_sha="blob-b", current_span_sha="span-b")
        suspicions = determine_suspicion([state], NOW - timedelta(days=1), NOW)
        assert [s.reason for s in suspicions] == [Reason.DEPS_CHANGED, Reason.EXPIRED]
