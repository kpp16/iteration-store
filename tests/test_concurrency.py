"""Concurrent access from multiple agents.

The real scenario is multiple processes: each agent session runs its own MCP
server, and they share one database file per project. These use actual
subprocesses and threads rather than simulating contention.
"""

from __future__ import annotations

import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from iteration_store.schema import BUSY_TIMEOUT_MS
from iteration_store.store import Store

WRITER = """
import sys
from iteration_store.store import Store

project, count, tag = sys.argv[1], int(sys.argv[2]), sys.argv[3]
with Store.open(project) as store:
    for index in range(count):
        store.note(f"{tag}-{index}")
"""

READER = """
import sys
from iteration_store.store import Store

project, rounds = sys.argv[1], int(sys.argv[2])
with Store.open(project) as store:
    for _ in range(rounds):
        store.recall("anything")
        store.note_count()
print("ok")
"""


def spawn(script: str, *args: str, home: Path) -> subprocess.Popen:
    return subprocess.Popen(
        [sys.executable, "-c", script, *args],
        env={"PATH": "/usr/bin:/bin", "ITERATION_STORE_HOME": str(home),
             "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src")},
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )


def drain(process: subprocess.Popen) -> tuple[int, str]:
    stdout, stderr = process.communicate(timeout=120)
    return process.returncode, stderr


class TestPragmas:
    def test_wal_is_enabled(self, store: Store):
        mode = store._conn.execute("PRAGMA journal_mode").fetchone()[0]
        assert mode.lower() == "wal"

    def test_busy_timeout_is_set(self, store: Store):
        # Without this, a competing writer fails instantly instead of waiting.
        assert store._conn.execute("PRAGMA busy_timeout").fetchone()[0] == BUSY_TIMEOUT_MS

    def test_synchronous_is_normal(self, store: Store):
        # 1 == NORMAL. Safe under WAL, and avoids an fsync on every write.
        assert store._conn.execute("PRAGMA synchronous").fetchone()[0] == 1

    def test_wal_persists_across_connections(self, git_repo: Path):
        with Store.open(git_repo) as first:
            first.note("first")
        with Store.open(git_repo) as second:
            assert second._conn.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"


class TestMultipleProcesses:
    def test_concurrent_writers_all_succeed(self, git_repo: Path, isolated_home: Path):
        # Four agents writing at once. Every write must land: a dropped write or a
        # "database is locked" here is the failure this configuration prevents.
        writers = [
            spawn(WRITER, str(git_repo), "25", f"agent{index}", home=isolated_home)
            for index in range(4)
        ]
        failures = [stderr for code, stderr in map(drain, writers) if code != 0]
        assert not failures, failures[0]

        with Store.open(git_repo) as store:
            assert store.note_count() == 100

    def test_readers_proceed_while_writers_work(self, git_repo: Path, isolated_home: Path):
        # WAL's main benefit: a reader is not blocked by an active writer.
        writers = [
            spawn(WRITER, str(git_repo), "40", f"w{index}", home=isolated_home)
            for index in range(2)
        ]
        readers = [spawn(READER, str(git_repo), "40", home=isolated_home) for _ in range(2)]

        results = [drain(process) for process in writers + readers]
        failures = [stderr for code, stderr in results if code != 0]
        assert not failures, failures[0]

        with Store.open(git_repo) as store:
            assert store.note_count() == 80

    def test_writes_from_another_process_are_visible(self, git_repo: Path,
                                                     isolated_home: Path):
        with Store.open(git_repo) as store:
            store.note("local")
            code, stderr = drain(spawn(WRITER, str(git_repo), "3", "remote",
                                       home=isolated_home))
            assert code == 0, stderr
            # A fresh read must see the other process's committed rows.
            assert store.note_count() == 4

    def test_concurrent_memory_writes_all_succeed(self, git_repo: Path,
                                                  isolated_home: Path):
        script = """
import sys
from iteration_store.store import Store
project, tag = sys.argv[1], sys.argv[2]
with Store.open(project) as store:
    for index in range(15):
        store.remember(f"{tag} fact {index}", deps=["services/auth.py::rotate_token"])
"""
        writers = [
            spawn(script, str(git_repo), f"agent{index}", home=isolated_home)
            for index in range(3)
        ]
        failures = [stderr for code, stderr in map(drain, writers) if code != 0]
        assert not failures, failures[0]

        with Store.open(git_repo) as store:
            assert store.count() == 45

    def test_simultaneous_first_open_does_not_race(self, git_repo: Path,
                                                   isolated_home: Path):
        # Migrations run on first open; several agents starting together must not
        # collide while creating the schema.
        starters = [
            spawn(WRITER, str(git_repo), "1", f"s{index}", home=isolated_home)
            for index in range(5)
        ]
        failures = [stderr for code, stderr in map(drain, starters) if code != 0]
        assert not failures, failures[0]

        with Store.open(git_repo) as store:
            assert store.note_count() == 5


class TestThreads:
    def test_one_store_shared_across_threads(self, store: Store):
        # Cross-process safety comes from WAL; this is the other axis — a single
        # connection used from several threads, which SQLite does not serialize.
        with ThreadPoolExecutor(max_workers=8) as pool:
            list(pool.map(lambda index: store.note(f"threaded {index}"), range(80)))
        assert store.note_count() == 80

    def test_mixed_reads_and_writes_across_threads(self, store: Store):
        store.remember("A fact about billing.")

        def work(index: int) -> None:
            store.note(f"note {index}")
            store.recall("billing")

        with ThreadPoolExecutor(max_workers=8) as pool:
            list(pool.map(work, range(40)))

        assert store.note_count() == 40
        assert store.count() == 1
