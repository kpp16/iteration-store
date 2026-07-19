"""MCP server exposing the store to coding agents.

A thin wrapper. All storage logic lives in the library — this module adds no
behavior of its own beyond formatting and argument coercion.

The tool descriptions below are the real design surface. `remember` applies no
programmatic gatekeeping (see DESIGN.md → Write path), so what ends up in the
store is decided entirely by how these descriptions read to an agent.
"""

from __future__ import annotations

import atexit
import os
import signal
import uuid
from datetime import timedelta
from pathlib import Path

from mcp.server.fastmcp import FastMCP

from .models import RecallResult
from .store import UNSET, Store

PROJECT_ENV_VAR = "ITERATION_STORE_PROJECT"
SESSION_ENV_VAR = "ITERATION_STORE_SESSION"

# One server process per agent session, so a per-process id is a faithful stand-in
# for "the session that wrote this" — the host can override if it knows better.
SESSION_ID = os.environ.get(SESSION_ENV_VAR) or uuid.uuid4().hex[:12]

# Diff evidence is already capped in gitutil; cap again per result so a recall
# returning several suspect memories cannot crowd out the memories themselves.
MAX_EVIDENCE_LINES = 12

mcp = FastMCP(
    "iteration-store",
    instructions=(
        "Project-scoped long-term memory. Recall before re-deriving something that "
        "was likely worked out in an earlier session; remember durable facts that "
        "were expensive to establish."
    ),
)

_store: Store | None = None


def store() -> Store:
    """Open the project's store once, on first use.

    The connection is held for the life of the process rather than reopened per
    call: this server is long-lived, and WAL means holding it does not block
    other agents. `close_store` is registered at exit so the WAL is checkpointed
    back into the database file on the way out.
    """
    global _store
    if _store is None:
        override = os.environ.get(PROJECT_ENV_VAR)
        _store = Store.open(Path(override) if override else None)
        atexit.register(close_store)
    return _store


def close_store() -> None:
    """Close the cached store, if one was ever opened. Idempotent."""
    global _store
    if _store is not None:
        _store.close()
        _store = None


@mcp.tool()
def remember(
    content: str,
    kind: str | None = None,
    deps: list[str] | None = None,
    review_days: float | None = None,
) -> str:
    """Store a durable fact about this project so it survives into future sessions.

    Store something when re-deriving it later would be expensive — it took reading
    several files, tracing a call chain, or a round of trial and error to establish.

    Do NOT store:
      - Anything obtainable by reading one file. The cost of rediscovery is what
        makes a fact worth keeping, and a single read is cheap.
      - Restatements of what the code plainly says. "`parse_config` reads a TOML
        file" is not worth a memory; "config must be parsed before logging is
        configured, or the log level is silently ignored" is.
      - Transient state: what you are working on right now, or what you just changed.

    Good candidates: non-obvious constraints and orderings, why an approach was
    chosen over an alternative, behavior that surprised you, decisions and
    conventions that no single file records.

    Args:
        content: The fact, stated so it is useful to someone without this
            conversation's context. Self-contained sentences, not notes to self.
        kind: Optional free-text label, e.g. "constraint", "decision", "convention",
            "gotcha". Useful for filtering later.
        deps: Files this fact depends on, so it can be flagged when they change.
            Strongly prefer anchoring to a symbol — "services/auth.py::rotate_token"
            rather than "services/auth.py" — because an unanchored dependency makes
            the fact suspect on ANY edit to that file, and in an active repository
            that means constant false alarms. A line span ("file.py::L40-L88") also
            works. Paths are relative to the project root. Both the file and the
            anchor must exist now, or the call fails.
        review_days: For facts no file can validate — decisions, preferences,
            conventions — how many days until this should be reviewed again. Leave
            unset for facts that are genuinely durable; a short interval on a stable
            fact just produces noise.

    Returns:
        The stored memory's id.
    """
    memory = store().remember(
        content,
        kind=kind,
        deps=deps or (),
        review_interval=timedelta(days=review_days) if review_days else None,
    )
    return f"Stored as #{memory.id}."


@mcp.tool()
def recall(query: str | None = None, limit: int = 10) -> str:
    """Search this project's stored facts before re-deriving something.

    Worth calling when you are about to investigate how part of this project works,
    especially if it looks like something a previous session would have hit.

    Results may carry a suspicion marker, which does NOT mean the fact is wrong —
    most edits do not falsify a fact about the code:
      - deps_changed: code the fact was written against has changed. The diff is
        included; judge whether it actually affects the fact.
      - expired: the fact's review deadline passed. Nothing detected a change, it
        simply has not been checked in a while.

    On encountering a suspect result, check it against the current code and then
    call `confirm` (still true) or `revise` (no longer true). Repairing as you go
    is what keeps the store trustworthy; ignoring the markers is what makes it
    gradually useless.

    Args:
        query: Free text. Matched against fact contents. Omit to list the most
            recently confirmed facts.
        limit: Maximum results.

    Returns:
        Matching facts, each with its id and any reason to distrust it.
    """
    results = store().recall(query, limit=limit)
    if not results:
        return "No memories stored for this project yet." if query is None else (
            f"No memories matched {query!r}."
        )
    return "\n".join(_format(result) for result in results)


@mcp.tool()
def confirm(memory_id: int) -> str:
    """Mark a suspect fact as still true after checking it against current code.

    Re-baselines the fact: its dependency hashes are refreshed to what is on disk
    now, and any review deadline is pushed out. Use this when a flagged fact turned
    out to be unaffected by whatever changed — the common case.

    Args:
        memory_id: The id shown by `recall`.
    """
    memory = store().confirm(memory_id)
    return f"Confirmed #{memory.id}."


@mcp.tool()
def revise(
    memory_id: int,
    content: str | None = None,
    deps: list[str] | None = None,
    review_days: float | None = None,
) -> str:
    """Correct a fact that is no longer true, keeping its id.

    Prefer this over storing a fresh memory when the underlying fact changed — two
    memories saying different things about the same subject is worse than one
    correct memory, because a future recall will surface both.

    Args:
        memory_id: The id shown by `recall`.
        content: Replacement text. Omit to keep the existing wording.
        deps: Replacement dependency list — a full replacement, not an addition.
            Omit to keep the existing dependencies and simply re-baseline them.
        review_days: Replacement review interval in days. Omit to keep the
            current cadence. Pass 0 to clear it, so a fact that turned out to be
            durable stops being flagged for review.
    """
    memory = store().revise(
        memory_id,
        content=UNSET if content is None else content,
        deps=UNSET if deps is None else deps,
        review_interval=(
            UNSET
            if review_days is None
            else timedelta(days=review_days) if review_days > 0 else None
        ),
    )
    return f"Revised #{memory.id}."


@mcp.tool()
def note(body: str, author: str | None = None, paths: list[str] | None = None) -> str:
    """Jot down an observation, dead end, or piece of reasoning. No bar to clear.

    This is the low-bar counterpart to `remember`. Where a memory must be a durable
    fact that was expensive to establish, a note can be anything you might want a
    future session to have seen. Write freely — that is what it is for.

    Especially worth noting: approaches that did NOT work and why, surprises,
    half-formed observations, reasoning behind a choice you are about to make,
    context that would be tedious to reconstruct. A dead end recorded here can save
    a future session an hour, and it fits nowhere else — it states no durable fact,
    so it is not a memory.

    Notes are not searchable yet. Write them anyway: they are being collected now so
    that retrieval can be designed against real notes rather than a guess about
    them. Provenance (who, which session, which commit) is recorded automatically.

    Args:
        body: The observation, in whatever form is useful.
        author: Who is writing — your agent or subagent name, if you have one.
        paths: Files in play right now, if any. Recorded as context only; unlike a
            memory's dependencies these are never validated, so they cost nothing
            to include and may help find this note later.
    """
    stored = store().note(body, author=author, session_id=SESSION_ID, paths=paths or ())
    return f"Noted (#{stored.id})."


def _format(result: RecallResult) -> str:
    memory = result.memory
    label = f" [{memory.kind}]" if memory.kind else ""
    lines = [f"#{memory.id}{label} {memory.content}"]

    if memory.deps:
        anchors = ", ".join(
            f"{dep.path}::{dep.anchor}" if dep.anchor else dep.path for dep in memory.deps
        )
        lines.append(f"    depends on: {anchors}")

    for suspicion in result.suspicions:
        lines.append(f"    [!] {suspicion.reason.value}")
        for line in _clip(suspicion.evidence):
            lines.append(f"        {line}")

    return "\n".join(lines)


def _clip(evidence: str) -> list[str]:
    """Trim evidence to a readable size without losing the informative half.

    Diff preamble (`diff --git`, `index`, `---`, `+++`) is dropped first. It is
    four lines of pure noise, and spending the budget on it truncates the hunk
    partway — which reliably shows the removed lines and cuts off the added ones,
    leaving a reader who can see what went away but not what replaced it.
    """
    lines = [line for line in evidence.splitlines() if not _is_diff_preamble(line)]
    if len(lines) <= MAX_EVIDENCE_LINES:
        return lines

    # Keep both ends of what remains: a hunk's meaning usually lives in the
    # removals and the additions together, and those sit at opposite ends.
    head = MAX_EVIDENCE_LINES // 2
    tail = MAX_EVIDENCE_LINES - head
    return [
        *lines[:head],
        f"… {len(lines) - MAX_EVIDENCE_LINES} more lines of evidence …",
        *lines[-tail:],
    ]


def _is_diff_preamble(line: str) -> bool:
    return line.startswith(("diff --git ", "index ", "--- ", "+++ ", "new file mode "))


def install_shutdown_handler() -> None:
    """Make SIGTERM exit normally, so the store is closed on the way out.

    `atexit` already covers an ordinary exit, including stdin closing under the
    stdio transport. A host that stops the server with SIGTERM would otherwise
    bypass it and leave the WAL unmerged, so translate that signal into a normal
    exit. SIGKILL cannot be caught; the data is still durable in the WAL, it just
    is not folded back into the database file.
    """

    def terminate(*_: object) -> None:
        raise SystemExit(0)

    signal.signal(signal.SIGTERM, terminate)


def main() -> None:
    """Entry point. Speaks MCP over stdio."""
    install_shutdown_handler()
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
