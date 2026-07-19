"""Migrations."""

from __future__ import annotations

import sqlite3

from iteration_store.schema import MIGRATIONS, apply_migrations, connect


def version(conn: sqlite3.Connection) -> int:
    return conn.execute("PRAGMA user_version").fetchone()[0]


def test_fresh_store_is_at_the_current_version(tmp_path):
    with connect(str(tmp_path / "store.db")) as conn:
        assert version(conn) == len(MIGRATIONS)


def test_applying_twice_is_a_no_op(tmp_path):
    path = str(tmp_path / "store.db")
    with connect(path) as conn:
        pass
    # Reopening an existing store must not attempt to recreate its tables.
    with connect(path) as conn:
        assert apply_migrations(conn) == len(MIGRATIONS)


def test_upgrades_a_store_left_at_an_older_version(tmp_path):
    conn = sqlite3.connect(str(tmp_path / "store.db"))
    assert version(conn) == 0

    apply_migrations(conn)
    assert version(conn) == len(MIGRATIONS)
    assert conn.execute("SELECT COUNT(*) FROM memory").fetchone()[0] == 0


def test_foreign_keys_cascade_dependency_rows(tmp_path):
    with connect(str(tmp_path / "store.db")) as conn:
        conn.execute(
            """
            INSERT INTO memory (id, content, created_at, last_confirmed_at, validation_strategy)
                 VALUES (1, 'x', '2026-01-01', '2026-01-01', 'deps')
            """
        )
        conn.execute("INSERT INTO memory_dep (memory_id, path) VALUES (1, 'a.py')")
        conn.execute("DELETE FROM memory WHERE id = 1")
        assert conn.execute("SELECT COUNT(*) FROM memory_dep").fetchone()[0] == 0
