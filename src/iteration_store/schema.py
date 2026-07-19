"""Schema and migrations.

Migrations exist from the first version even though there is only one. The schema
will move, and retrofitting versioning onto stores already in the field is far
worse than carrying a trivial migration list now.
"""

from __future__ import annotations

import sqlite3

MIGRATIONS: tuple[str, ...] = (
    # v1 — memories, their dependencies, and the full-text index.
    """
    CREATE TABLE memory (
        id                      INTEGER PRIMARY KEY,
        content                 TEXT    NOT NULL,
        kind                    TEXT,
        created_at              TEXT    NOT NULL,
        last_confirmed_at       TEXT    NOT NULL,
        write_commit            TEXT,
        validation_strategy     TEXT    NOT NULL,
        review_interval_seconds INTEGER,
        next_review_at          TEXT,
        last_recalled_at        TEXT,
        recall_count            INTEGER NOT NULL DEFAULT 0
    );

    CREATE TABLE memory_dep (
        id        INTEGER PRIMARY KEY,
        memory_id INTEGER NOT NULL REFERENCES memory(id) ON DELETE CASCADE,
        path      TEXT    NOT NULL,
        anchor    TEXT,
        blob_sha  TEXT,
        span_sha  TEXT
    );

    CREATE INDEX memory_dep_memory_id ON memory_dep(memory_id);
    CREATE INDEX memory_dep_path      ON memory_dep(path);
    CREATE INDEX memory_next_review   ON memory(next_review_at);

    CREATE VIRTUAL TABLE memory_fts USING fts5(
        content,
        kind,
        content='memory',
        content_rowid='id',
        tokenize='porter unicode61'
    );

    CREATE TRIGGER memory_fts_insert AFTER INSERT ON memory BEGIN
        INSERT INTO memory_fts(rowid, content, kind)
             VALUES (new.id, new.content, new.kind);
    END;

    CREATE TRIGGER memory_fts_delete AFTER DELETE ON memory BEGIN
        INSERT INTO memory_fts(memory_fts, rowid, content, kind)
             VALUES ('delete', old.id, old.content, old.kind);
    END;

    -- Scoped to the indexed columns so recall bookkeeping (recall_count,
    -- last_recalled_at) does not churn the index on every read.
    CREATE TRIGGER memory_fts_update AFTER UPDATE OF content, kind ON memory BEGIN
        INSERT INTO memory_fts(memory_fts, rowid, content, kind)
             VALUES ('delete', old.id, old.content, old.kind);
        INSERT INTO memory_fts(rowid, content, kind)
             VALUES (new.id, new.content, new.kind);
    END;
    """,
)


def connect(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    apply_migrations(conn)
    return conn


def apply_migrations(conn: sqlite3.Connection) -> int:
    """Bring a store up to the current schema. Returns the resulting version."""
    version = conn.execute("PRAGMA user_version").fetchone()[0]

    for index in range(version, len(MIGRATIONS)):
        conn.executescript(MIGRATIONS[index])
        # PRAGMA does not accept bound parameters.
        conn.execute(f"PRAGMA user_version = {index + 1}")
        conn.commit()

    return len(MIGRATIONS)
