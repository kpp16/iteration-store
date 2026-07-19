"""Concurrent access from multiple agents.

The real scenario is multiple processes: each agent session runs its own MCP
server, and they share one database file per project. These use actual
subprocesses and threads rather than simulating contention.
"""

from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from iteration_store.projects import store_path
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


class TestCheckpointOnClose:
    """The database file must stand alone once a store is closed.

    SQLite already checkpoints when the *last* connection to a database closes,
    so a single-connection store needs no help. The case that does is two agents
    on one project: while another session holds the store open, closing yours
    leaves the WAL untouched, and ``store.db`` stays a 4KB stub containing no
    tables at all. Anything that copies or opens the ``.db`` alone — a backup,
    an external viewer — then silently sees an empty database.

    Every test here keeps a second connection open, because without one SQLite's
    automatic checkpoint hides whether the explicit one works.
    """

    def test_file_stands_alone_though_another_store_is_open(
        self, git_repo: Path, tmp_path: Path
    ):
        other = Store.open(git_repo)  # Another agent's session, still running.
        try:
            with Store.open(git_repo) as store:
                store.remember("A durable fact about billing.")
                store.note("an observation")
                db_path = Path(store_path(store.project))

            # Copy only the .db, the way a backup or an external viewer sees it.
            detached = tmp_path / "detached.db"
            detached.write_bytes(db_path.read_bytes())

            conn = sqlite3.connect(detached)
            try:
                assert conn.execute("SELECT COUNT(*) FROM memory").fetchone()[0] == 1
                assert conn.execute("SELECT COUNT(*) FROM note").fetchone()[0] == 1
            finally:
                conn.close()
        finally:
            other.close()

    def test_wal_is_emptied_though_another_store_is_open(self, git_repo: Path):
        other = Store.open(git_repo)
        try:
            with Store.open(git_repo) as store:
                for index in range(50):
                    store.note(f"note {index}")
                db_path = Path(store_path(store.project))

            wal = db_path.with_name(db_path.name + "-wal")
            assert not wal.exists() or wal.stat().st_size == 0
        finally:
            other.close()

    def test_the_other_store_still_works_after_a_checkpoint(self, git_repo: Path):
        # Checkpointing must not disturb the connection that stayed open.
        other = Store.open(git_repo)
        try:
            with Store.open(git_repo) as store:
                store.remember("written while another connection is open")

            assert other.count() == 1
            other.note("still writable after the checkpoint")
            assert other.note_count() == 1
        finally:
            other.close()


SERVER_WRITER = """
import os, signal, sys, time
from iteration_store import server

server.install_shutdown_handler()   # main() does this before serving.
server.store().remember("written by the server process")

if sys.argv[1] == "sigterm":
    os.kill(os.getpid(), signal.SIGTERM)
    time.sleep(5)   # Should never be reached; the handler exits.
"""


class TestServerReleasesTheStore:
    """The MCP server holds one connection for its whole life, so it is the only
    thing that ever closes it — nothing else calls `close`.

    Each test keeps a second agent's store open across the subprocess's whole
    life. That is the real deployment (one server per session, several sessions
    per project) and it is also what makes the assertion meaningful: with only
    one connection, SQLite checkpoints automatically when the interpreter
    finalizes it, and the server would look correct however it shut down.
    """

    @pytest.mark.parametrize("how", ["normal", "sigterm"])
    def test_database_stands_alone_after_the_server_exits(
        self, git_repo: Path, tmp_path: Path, how: str
    ):
        env = {
            **os.environ,
            "ITERATION_STORE_PROJECT": str(git_repo),
            "ITERATION_STORE_HOME": os.environ["ITERATION_STORE_HOME"],
        }

        other = Store.open(git_repo)  # Another session, outliving the subprocess.
        try:
            result = subprocess.run(
                [sys.executable, "-c", SERVER_WRITER, how],
                env=env, capture_output=True, text=True, timeout=30,
            )
            assert result.returncode == 0, result.stderr

            source = Path(store_path(other.project))
            db_path = tmp_path / "copied.db"
            db_path.write_bytes(source.read_bytes())
        finally:
            other.close()

        conn = sqlite3.connect(db_path)
        try:
            assert conn.execute("SELECT COUNT(*) FROM memory").fetchone()[0] == 1
        finally:
            conn.close()
