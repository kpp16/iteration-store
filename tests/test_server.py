"""MCP layer.

The server adds no storage logic, so these tests cover only what it does own:
tool registration, argument coercion, and the formatting an agent actually reads.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from iteration_store import server
from iteration_store.server import PROJECT_ENV_VAR

from .conftest import commit_all

CHANGED_ROTATE = '''\
"""Auth service."""

TOKEN_TTL = 3600


def rotate_token(token):
    """Rotate a token, twice over."""
    return token[::-1] + "!"
'''


@pytest.fixture(autouse=True)
def project(git_repo: Path, monkeypatch):
    """Point the server at the fixture repo and reset its cached store."""
    monkeypatch.setenv(PROJECT_ENV_VAR, str(git_repo))
    monkeypatch.setattr(server, "_store", None)
    yield git_repo
    if server._store is not None:
        server._store.close()


def tools() -> dict:
    return {tool.name: tool for tool in asyncio.run(server.mcp.list_tools())}


class TestRegistration:
    def test_exposes_the_expected_operations(self):
        assert set(tools()) == {"remember", "recall", "confirm", "revise", "note"}

    def test_notes_have_no_read_tool(self):
        # Notes are write-only until retrieval is designed; a read tool appearing
        # here would mean that decision got reversed without the design changing.
        assert not any(name.startswith("recall_note") for name in tools())

    def test_descriptions_are_substantial(self):
        # These descriptions are the only governance on what gets stored, so a
        # stub or an accidentally-empty docstring is a real defect.
        for name, tool in tools().items():
            assert tool.description and len(tool.description) > 200, name

    def test_remember_schema_matches_its_signature(self):
        schema = tools()["remember"].inputSchema
        assert set(schema["required"]) == {"content"}
        assert set(schema["properties"]) == {"content", "kind", "deps", "review_days"}

    def test_recall_takes_an_optional_query(self):
        schema = tools()["recall"].inputSchema
        assert "query" not in schema.get("required", [])


class TestRoundTrip:
    def test_remember_then_recall(self):
        server.remember("Billing charges in cents, never dollars.", kind="convention")
        output = server.recall("billing cents")
        assert "cents" in output
        assert "[convention]" in output

    def test_reports_the_new_id(self):
        assert server.remember("a fact").startswith("Stored as #")

    def test_empty_result_is_explicit(self):
        assert "No memories" in server.recall("nothing matches this")

    def test_review_days_becomes_an_interval(self):
        server.remember("We chose SQLite over Postgres.", review_days=30)
        (result,) = server.store().recall("sqlite")
        assert result.memory.review_interval.days == 30
        assert result.memory.next_review_at is not None


class TestSuspicionIsVisible:
    def test_clean_results_carry_no_marker(self):
        server.remember("Rotation reverses the token.",
                        deps=["services/auth.py::rotate_token"])
        assert "[!]" not in server.recall("rotation")

    def test_flagged_results_show_reason_and_evidence(self, project: Path):
        server.remember("Rotation reverses the token.",
                        deps=["services/auth.py::rotate_token"])
        (project / "services" / "auth.py").write_text(CHANGED_ROTATE)
        commit_all(project, "change rotation")

        output = server.recall("rotation")
        assert "[!] deps_changed" in output
        assert "twice over" in output

    def test_dependencies_are_listed_with_anchors(self):
        server.remember("Rotation reverses the token.",
                        deps=["services/auth.py::rotate_token"])
        assert "depends on: services/auth.py::rotate_token" in server.recall("rotation")

    def test_evidence_is_clipped(self, project: Path):
        server.remember("Auth exists.", deps=["services/auth.py"])
        (project / "services" / "auth.py").write_text(
            "\n".join(f"# line {i}" for i in range(400))
        )
        commit_all(project, "rewrite")

        output = server.recall("auth")
        assert "more lines of evidence" in output
        # A single suspect memory must not flood the context this exists to protect.
        assert len(output.splitlines()) < 30


class TestRepair:
    def test_confirm_clears_the_marker(self, project: Path):
        stored = server.remember("Rotation reverses the token.",
                                 deps=["services/auth.py::rotate_token"])
        memory_id = int(stored.split("#")[1].rstrip("."))

        (project / "services" / "auth.py").write_text(CHANGED_ROTATE)
        commit_all(project, "change rotation")
        assert "[!]" in server.recall("rotation")

        assert server.confirm(memory_id) == f"Confirmed #{memory_id}."
        assert "[!]" not in server.recall("rotation")

    def test_revise_replaces_the_content(self):
        stored = server.remember("original wording")
        memory_id = int(stored.split("#")[1].rstrip("."))

        server.revise(memory_id, content="replacement wording about quotas")
        assert "quotas" in server.recall("quotas")

    def test_revise_clears_the_cadence_with_zero_review_days(self):
        # JSON has no sentinel, so the tool encodes "clear" as 0 and "leave
        # alone" as an omitted argument.
        stored = server.remember("a decaying fact", review_days=30)
        memory_id = int(stored.split("#")[1].rstrip("."))

        server.revise(memory_id, content="reworded")
        assert server.store().get(memory_id).review_interval is not None

        server.revise(memory_id, review_days=0)
        assert server.store().get(memory_id).review_interval is None

    def test_revise_keeps_the_same_id(self):
        stored = server.remember("a fact")
        memory_id = int(stored.split("#")[1].rstrip("."))
        assert server.revise(memory_id, content="a revised fact") == f"Revised #{memory_id}."
