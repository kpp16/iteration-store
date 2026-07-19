"""Schema and migrations.

Migrations exist from the first version even though there was only one. The schema
will move, and retrofitting versioning onto stores already in the field is far
worse than carrying a trivial migration list now.

Each migration is a *tuple of statements* rather than one script. That is not
stylistic: `executescript` issues an implicit COMMIT before it runs, which would
break the exclusive transaction that makes migrating safe when several agents
open a fresh store at the same moment. Splitting on ``;`` is not an option either
— the trigger bodies below contain their own statements.
"""

from __future__ import annotations

import sqlite3

# How long a writer waits for a competing writer before giving up. Multiple
# agents on one project means multiple processes against one file; without this
# SQLite raises "database is locked" immediately rather than waiting out a write
# that typically takes microseconds.
BUSY_TIMEOUT_MS = 5000

V1_MEMORIES: tuple[str, ...] = (
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
    )
    """,
    """
    CREATE TABLE memory_dep (
        id        INTEGER PRIMARY KEY,
        memory_id INTEGER NOT NULL REFERENCES memory(id) ON DELETE CASCADE,
        path      TEXT    NOT NULL,
        anchor    TEXT,
        blob_sha  TEXT,
        span_sha  TEXT
    )
    """,
    "CREATE INDEX memory_dep_memory_id ON memory_dep(memory_id)",
    "CREATE INDEX memory_dep_path      ON memory_dep(path)",
    "CREATE INDEX memory_next_review   ON memory(next_review_at)",
    """
    CREATE VIRTUAL TABLE memory_fts USING fts5(
        content,
        kind,
        content='memory',
        content_rowid='id',
        tokenize='porter unicode61'
    )
    """,
    """
    CREATE TRIGGER memory_fts_insert AFTER INSERT ON memory BEGIN
        INSERT INTO memory_fts(rowid, content, kind)
             VALUES (new.id, new.content, new.kind);
    END
    """,
    """
    CREATE TRIGGER memory_fts_delete AFTER DELETE ON memory BEGIN
        INSERT INTO memory_fts(memory_fts, rowid, content, kind)
             VALUES ('delete', old.id, old.content, old.kind);
    END
    """,
    # Scoped to the indexed columns so recall bookkeeping (recall_count,
    # last_recalled_at) does not churn the index on every read.
    """
    CREATE TRIGGER memory_fts_update AFTER UPDATE OF content, kind ON memory BEGIN
        INSERT INTO memory_fts(memory_fts, rowid, content, kind)
             VALUES ('delete', old.id, old.content, old.kind);
        INSERT INTO memory_fts(rowid, content, kind)
             VALUES (new.id, new.content, new.kind);
    END
    """,
)

V2_NOTES: tuple[str, ...] = (
    """
    CREATE TABLE note (
        id         INTEGER PRIMARY KEY,
        body       TEXT NOT NULL,
        author     TEXT,
        session_id TEXT,
        created_at TEXT NOT NULL,
        cwd_commit TEXT
    )
    """,
    # Paths are advisory: recorded as context, never hash-validated. A child
    # table rather than a blob so the eventual path-overlap ranking (the
    # strongest signal in the deferred retrieval design) is a plain indexed
    # join instead of a migration.
    """
    CREATE TABLE note_path (
        id      INTEGER PRIMARY KEY,
        note_id INTEGER NOT NULL REFERENCES note(id) ON DELETE CASCADE,
        path    TEXT NOT NULL
    )
    """,
    "CREATE INDEX note_path_note_id ON note_path(note_id)",
    "CREATE INDEX note_path_path    ON note_path(path)",
    "CREATE INDEX note_session      ON note(session_id)",
    "CREATE INDEX note_created      ON note(created_at)",
    """
    CREATE VIRTUAL TABLE note_fts USING fts5(
        body,
        content='note',
        content_rowid='id',
        tokenize='porter unicode61'
    )
    """,
    """
    CREATE TRIGGER note_fts_insert AFTER INSERT ON note BEGIN
        INSERT INTO note_fts(rowid, body) VALUES (new.id, new.body);
    END
    """,
    """
    CREATE TRIGGER note_fts_delete AFTER DELETE ON note BEGIN
        INSERT INTO note_fts(note_fts, rowid, body) VALUES ('delete', old.id, old.body);
    END
    """,
    """
    CREATE TRIGGER note_fts_update AFTER UPDATE OF body ON note BEGIN
        INSERT INTO note_fts(note_fts, rowid, body) VALUES ('delete', old.id, old.body);
        INSERT INTO note_fts(rowid, body) VALUES (new.id, new.body);
    END
    """,
)

MIGRATIONS: tuple[tuple[str, ...], ...] = (V1_MEMORIES, V2_NOTES)


def connect(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(path, check_same_thread=False)
    conn.row_factory = sqlite3.Row

    # Set before anything that can contend, so even migration waits rather than
    # failing when two agents open a fresh store at the same moment.
    conn.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MS}")

    # WAL lets readers proceed while a writer holds the lock — the common case
    # here, where one agent is recalling while another writes a note. It is
    # persisted in the database header, so this is a no-op after the first open.
    # Must run outside a transaction, hence before migrating.
    conn.execute("PRAGMA journal_mode = WAL")

    # Safe to relax under WAL: a crash cannot corrupt the database, it can only
    # lose the last transaction. Worth it — every write fsyncs otherwise.
    conn.execute("PRAGMA synchronous = NORMAL")

    conn.execute("PRAGMA foreign_keys = ON")
    apply_migrations(conn)
    return conn


def apply_migrations(conn: sqlite3.Connection) -> int:
    """Bring a store up to the current schema. Returns the resulting version.

    Safe to run concurrently. Several agents starting at once will all reach
    this; BEGIN EXCLUSIVE serializes them, and the version is re-read *inside*
    the transaction so the losers see the winner's work and do nothing. Checking
    the version outside the lock would let two processes both decide to create
    the schema, and the second would fail with "table memory already exists".
    """
    if _version(conn) == len(MIGRATIONS):
        return len(MIGRATIONS)  # Fast path: already current, no lock needed.

    conn.execute("BEGIN EXCLUSIVE")
    try:
        version = _version(conn)
        for index in range(version, len(MIGRATIONS)):
            for statement in MIGRATIONS[index]:
                conn.execute(statement)
            # PRAGMA does not accept bound parameters. user_version lives in the
            # database header and is transactional, so it commits with the DDL.
            conn.execute(f"PRAGMA user_version = {index + 1}")
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise

    return len(MIGRATIONS)


def _version(conn: sqlite3.Connection) -> int:
    return int(conn.execute("PRAGMA user_version").fetchone()[0])
