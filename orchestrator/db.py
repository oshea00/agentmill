"""SQLite connection management and schema initialisation."""

import os
import sqlite3
from typing import Optional

_connections: dict[str, sqlite3.Connection] = {}

_CREATE_AGENTS = """
CREATE TABLE IF NOT EXISTS agents (
    id          TEXT PRIMARY KEY,
    role        TEXT NOT NULL,
    state       TEXT NOT NULL,
    parent_id   TEXT REFERENCES agents(id),
    cluster_id  TEXT NOT NULL,
    name        TEXT,
    created_at  REAL NOT NULL,
    updated_at  REAL NOT NULL,
    context     TEXT,
    error       TEXT
);
"""

_CREATE_AGENT_STATE_HISTORY = """
CREATE TABLE IF NOT EXISTS agent_state_history (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    agent_id    TEXT NOT NULL REFERENCES agents(id),
    from_state  TEXT,
    to_state    TEXT NOT NULL,
    reason      TEXT,
    timestamp   REAL NOT NULL
);
"""

_CREATE_MESSAGES = """
CREATE TABLE IF NOT EXISTS messages (
    id            TEXT PRIMARY KEY,
    type          TEXT NOT NULL,
    sender_id     TEXT NOT NULL,
    recipient_id  TEXT,
    cluster_id    TEXT NOT NULL,
    payload       TEXT NOT NULL,
    status        TEXT NOT NULL DEFAULT 'PENDING',
    created_at    REAL NOT NULL,
    delivered_at  REAL
);
"""

_CREATE_MESSAGES_INDEXES = """
CREATE INDEX IF NOT EXISTS idx_messages_recipient
    ON messages(recipient_id, status, created_at);
CREATE INDEX IF NOT EXISTS idx_messages_cluster
    ON messages(cluster_id, status, created_at);
"""

# Tracks per-agent delivery of broadcast (NULL-recipient) messages so that each
# agent in a cluster receives exactly one copy without interfering with others.
_CREATE_BROADCAST_RECEIPTS = """
CREATE TABLE IF NOT EXISTS broadcast_receipts (
    message_id  TEXT NOT NULL REFERENCES messages(id),
    agent_id    TEXT NOT NULL,
    created_at  REAL NOT NULL,
    PRIMARY KEY (message_id, agent_id)
);
"""


_CREATE_INTERACTION_REQUESTS = """
CREATE TABLE IF NOT EXISTS interaction_requests (
    id               TEXT PRIMARY KEY,
    short_id         TEXT NOT NULL,
    agent_id         TEXT NOT NULL REFERENCES agents(id),
    interaction_type TEXT NOT NULL,
    prompt           TEXT NOT NULL,
    status           TEXT NOT NULL DEFAULT 'PENDING',
    response_type    TEXT,
    response_nick    TEXT,
    response_payload TEXT,
    created_at       REAL NOT NULL,
    responded_at     REAL
);
"""

_CREATE_INTERACTION_REQUESTS_INDEXES = """
CREATE INDEX IF NOT EXISTS idx_ir_short_id ON interaction_requests(short_id, status);
CREATE INDEX IF NOT EXISTS idx_ir_agent    ON interaction_requests(agent_id, status);
"""


def get_connection(path: Optional[str] = None) -> sqlite3.Connection:
    """Return a shared connection for *path*, creating it on first call.

    Uses WAL journal mode and enforces foreign-key constraints.  Connections
    are keyed by resolved path so callers sharing the same path always get the
    same object — no per-operation connection churn.
    """
    if path is None:
        path = os.environ.get("DB_PATH", "./agent_orchestrator.db")

    if path not in _connections:
        conn = sqlite3.connect(path, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.commit()
        _connections[path] = conn

    return _connections[path]


def init_schema(conn: sqlite3.Connection) -> None:
    """Create tables and indexes if they do not already exist."""
    conn.execute(_CREATE_AGENTS)
    conn.execute(_CREATE_AGENT_STATE_HISTORY)
    conn.execute(_CREATE_MESSAGES)
    conn.executescript(_CREATE_MESSAGES_INDEXES)
    conn.execute(_CREATE_BROADCAST_RECEIPTS)
    conn.execute(_CREATE_INTERACTION_REQUESTS)
    conn.executescript(_CREATE_INTERACTION_REQUESTS_INDEXES)
    conn.commit()
