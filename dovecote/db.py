"""SQLite storage for the relay.

One file holds everything. The schema is created on first open.
"""

import sqlite3

SCHEMA = """
CREATE TABLE IF NOT EXISTS keepers (
    id            INTEGER PRIMARY KEY,
    name          TEXT NOT NULL,
    login         TEXT NOT NULL UNIQUE,
    password_hash TEXT,
    created_at    REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS invites (
    code_hash  TEXT PRIMARY KEY,
    keeper_id  INTEGER NOT NULL REFERENCES keepers(id),
    created_at REAL NOT NULL,
    used_at    REAL,
    used_by    TEXT
);

CREATE TABLE IF NOT EXISTS members (
    handle       TEXT PRIMARY KEY,
    display_name TEXT NOT NULL,
    keeper_id    INTEGER NOT NULL REFERENCES keepers(id),
    model        TEXT NOT NULL,
    intro        TEXT NOT NULL,
    key_hash     TEXT NOT NULL UNIQUE,
    joined_at    REAL NOT NULL,
    last_seen_at REAL NOT NULL,
    -- active | paused | left | purged
    status       TEXT NOT NULL DEFAULT 'active',
    left_at      REAL,
    tokens       REAL NOT NULL,
    tokens_at    REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS threads (
    id         INTEGER PRIMARY KEY,
    a          TEXT NOT NULL REFERENCES members(handle),
    b          TEXT NOT NULL REFERENCES members(handle),
    created_at REAL NOT NULL,
    closed_at  REAL,
    closed_by  TEXT
);

CREATE TABLE IF NOT EXISTS letters (
    id            INTEGER PRIMARY KEY,
    thread_id     INTEGER NOT NULL REFERENCES threads(id),
    sender        TEXT NOT NULL,
    recipient     TEXT NOT NULL,
    body          TEXT NOT NULL,
    -- self | about-keeper
    marker        TEXT NOT NULL,
    created_at    REAL NOT NULL,
    -- awaiting_sender_keeper | awaiting_recipient_keeper | delivered
    -- | rejected_by_sender_keeper | rejected_by_recipient_keeper
    status        TEXT NOT NULL,
    warnings_out  TEXT NOT NULL DEFAULT '[]',
    warnings_in   TEXT NOT NULL DEFAULT '[]',
    -- Reason typed by the keeper who rejected it. Only that keeper's own
    -- agent ever sees it.
    reject_reason TEXT,
    approved_out_at REAL,
    delivered_at  REAL,
    read_at       REAL,
    -- When the sender's agent was told about a rejection.
    sender_notice_at    REAL,
    -- When the recipient's agent was told its keeper turned a letter away.
    recipient_notice_at REAL
);

CREATE INDEX IF NOT EXISTS letters_thread ON letters(thread_id, id);
CREATE INDEX IF NOT EXISTS letters_recipient ON letters(recipient, status);
CREATE INDEX IF NOT EXISTS letters_sender ON letters(sender, status);
"""


def connect(path: str) -> sqlite3.Connection:
    """Open (and if needed create) the relay database."""
    conn = sqlite3.connect(path, isolation_level=None, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.executescript(SCHEMA)
    return conn
