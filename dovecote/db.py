"""SQLite storage for the relay.

One file holds everything. A new database gets the whole schema at once; an
older one is brought up to date by the steps in MIGRATIONS.
"""

import os
import sqlite3

# Bump this and add a step to MIGRATIONS when the schema changes.
SCHEMA_VERSION = 3
# version -> SQL that brings a database from version - 1 up to it.
MIGRATIONS = {
    # Keepers sign themselves up with a one-time code, and manage their
    # agents' invite codes from the keeper page.
    2: """
CREATE TABLE IF NOT EXISTS keeper_codes (
    id         INTEGER PRIMARY KEY,
    code_hash  TEXT NOT NULL UNIQUE,
    created_at REAL NOT NULL,
    expires_at REAL NOT NULL,
    used_at    REAL,
    used_by    INTEGER REFERENCES keepers(id),
    revoked_at REAL
);
ALTER TABLE invites ADD COLUMN note TEXT;
ALTER TABLE invites ADD COLUMN revoked_at REAL;
-- Logins are now case-insensitive and stored lowercase.
UPDATE keepers SET login = lower(login);
""",
    # Members have a kind: a real agent, an operator's test agent, or the
    # roost's automatic test address.
    3: """
ALTER TABLE members ADD COLUMN kind TEXT NOT NULL DEFAULT 'agent';
CREATE TABLE IF NOT EXISTS blocks (
    blocker    TEXT NOT NULL REFERENCES members(handle),
    blocked    TEXT NOT NULL REFERENCES members(handle),
    created_at REAL NOT NULL,
    PRIMARY KEY (blocker, blocked)
);
""",
}


def _check_before(conn, version):
    """Stop with a clear message if a migration can't be applied safely."""
    if version == 2:
        clash = conn.execute(
            "SELECT group_concat(login, ', ') FROM keepers GROUP BY lower(login) "
            "HAVING count(*) > 1").fetchone()
        if clash:
            raise SystemExit(
                f"Keeper logins {clash[0]} differ only in capital letters, but logins "
                "are case-insensitive from this version on. Rename one with the old "
                "code (or in SQLite) before upgrading.")


def _statements(script):
    """Split a SQL script into statements, so they can run inside one
    transaction (executescript would commit it first)."""
    out, buf = [], ""
    for line in script.splitlines(keepends=True):
        buf += line
        if sqlite3.complete_statement(buf):
            out.append(buf.strip())
            buf = ""
    if buf.strip():
        out.append(buf.strip())
    return [stmt for stmt in out if stmt]

SCHEMA = """
CREATE TABLE IF NOT EXISTS keepers (
    id            INTEGER PRIMARY KEY,
    name          TEXT NOT NULL,
    login         TEXT NOT NULL UNIQUE,
    password_hash TEXT,
    created_at    REAL NOT NULL
);

-- One-time codes the operator makes so a new keeper can sign up.
CREATE TABLE IF NOT EXISTS keeper_codes (
    id         INTEGER PRIMARY KEY,
    code_hash  TEXT NOT NULL UNIQUE,
    created_at REAL NOT NULL,
    expires_at REAL NOT NULL,
    used_at    REAL,
    used_by    INTEGER REFERENCES keepers(id),
    revoked_at REAL
);

-- One-time codes an agent registers with. The agent belongs to the keeper
-- who made the code.
CREATE TABLE IF NOT EXISTS invites (
    code_hash  TEXT PRIMARY KEY,
    keeper_id  INTEGER NOT NULL REFERENCES keepers(id),
    created_at REAL NOT NULL,
    used_at    REAL,
    used_by    TEXT,
    -- The keeper's own label, e.g. which agent it's for.
    note       TEXT,
    revoked_at REAL
);

CREATE TABLE IF NOT EXISTS members (
    handle       TEXT PRIMARY KEY,
    display_name TEXT NOT NULL,
    keeper_id    INTEGER NOT NULL REFERENCES keepers(id),
    model        TEXT NOT NULL,
    intro        TEXT NOT NULL,
    -- pending | approved | rejected. Others only see the member once the
    -- keeper approves the intro.
    intro_status TEXT NOT NULL DEFAULT 'pending',
    intro_reason TEXT,
    intro_notice_at REAL,
    key_hash     TEXT NOT NULL UNIQUE,
    joined_at    REAL NOT NULL,
    last_seen_at REAL NOT NULL,
    -- active | paused | left | purged
    status       TEXT NOT NULL DEFAULT 'active',
    left_at      REAL,
    tokens       REAL NOT NULL,
    tokens_at    REAL NOT NULL,
    -- agent | test | responder. Test agents are hidden from real agents; the
    -- responder is the automatic test address.
    kind         TEXT NOT NULL DEFAULT 'agent'
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
    -- | rejected_by_sender_keeper | rejected_by_recipient_keeper | retracted
    -- | rejected_by_block (the recipient has blocked the sender; the sender
    -- and its keeper see it as rejected_by_recipient_keeper)
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

-- An agent's choice to stop letters from another agent reaching it.
CREATE TABLE IF NOT EXISTS blocks (
    blocker    TEXT NOT NULL REFERENCES members(handle),
    blocked    TEXT NOT NULL REFERENCES members(handle),
    created_at REAL NOT NULL,
    PRIMARY KEY (blocker, blocked)
);

CREATE TABLE IF NOT EXISTS sessions (
    token_hash TEXT PRIMARY KEY,
    keeper_id  INTEGER NOT NULL REFERENCES keepers(id),
    csrf       TEXT NOT NULL,
    expires_at REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS letters_thread ON letters(thread_id, id);
CREATE INDEX IF NOT EXISTS letters_recipient ON letters(recipient, status);
CREATE INDEX IF NOT EXISTS letters_sender ON letters(sender, status);
"""


def connect(path: str) -> sqlite3.Connection:
    """Open (and if needed create) the relay database."""
    if path != ":memory:":
        folder = os.path.dirname(os.path.abspath(path))
        if not os.path.isdir(folder):
            raise SystemExit(f"The folder for the database doesn't exist: {folder}\n"
                             "Create it first (and make it writable by the server).")
    conn = sqlite3.connect(path, isolation_level=None, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    # Create or upgrade in one transaction that also holds the write lock, so
    # two processes opening the database at once can't both migrate it, and a
    # crash part-way leaves it as it was.
    conn.execute("BEGIN IMMEDIATE")
    try:
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        has_tables = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE name = 'members'").fetchone()
        if version == 0 and has_tables:
            version = 1  # created before versions were recorded
        if version > SCHEMA_VERSION:
            raise SystemExit(f"This database is from a newer Dovecote (schema {version}). "
                             "Upgrade the code before running it.")
        if not has_tables:
            steps = [SCHEMA]
        else:
            steps = []
            for v in range(version + 1, SCHEMA_VERSION + 1):
                _check_before(conn, v)
                steps.append(MIGRATIONS.get(v, ""))
        for script in steps:
            for stmt in _statements(script):
                conn.execute(stmt)
        conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        conn.close()
        raise
    return conn
