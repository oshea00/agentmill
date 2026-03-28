"""Async, SQLite-backed message bus for inter-agent communication."""

import asyncio
import json
import sqlite3
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Optional

from opentelemetry import trace

tracer = trace.get_tracer("agent_orchestrator")

# Backpressure watermarks (unacknowledged direct messages per recipient)
_BP_HIGH = 50   # block send when pending count reaches this
_BP_LOW = 25    # unblock send once pending count falls below this

# How long to sleep between DB polls when waiting for a message or backpressure
# to drain.  Must be ≤ 100ms per spec.
_POLL_INTERVAL = 0.05  # 50ms


class MessageType(str, Enum):
    TASK_ASSIGN = "TASK_ASSIGN"
    TASK_RESULT = "TASK_RESULT"
    TASK_ERROR = "TASK_ERROR"
    STATUS_REQUEST = "STATUS_REQUEST"
    STATUS_RESPONSE = "STATUS_RESPONSE"
    PEER_SYNC = "PEER_SYNC"
    CONTROL = "CONTROL"
    HEARTBEAT = "HEARTBEAT"


@dataclass
class Message:
    type: MessageType
    sender_id: str
    cluster_id: str
    payload: dict[str, Any]
    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    recipient_id: Optional[str] = None  # None = broadcast
    status: str = "PENDING"
    created_at: float = field(default_factory=time.time)
    delivered_at: Optional[float] = None


_MSG_COLS = (
    "id, type, sender_id, recipient_id, cluster_id, "
    "payload, status, created_at, delivered_at"
)


def _row_to_message(row: sqlite3.Row) -> Message:
    return Message(
        id=row["id"],
        type=MessageType(row["type"]),
        sender_id=row["sender_id"],
        recipient_id=row["recipient_id"],
        cluster_id=row["cluster_id"],
        payload=json.loads(row["payload"]),
        status=row["status"],
        created_at=row["created_at"],
        delivered_at=row["delivered_at"],
    )


def _insert_message(conn: sqlite3.Connection, msg: Message) -> None:
    conn.execute(
        f"INSERT INTO messages ({_MSG_COLS}) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            msg.id,
            msg.type.value,
            msg.sender_id,
            msg.recipient_id,
            msg.cluster_id,
            json.dumps(msg.payload),
            msg.status,
            msg.created_at,
            msg.delivered_at,
        ),
    )


class MessageBus:
    """Async message bus backed by SQLite.

    All agents in the same process share a single bus instance; across processes
    they rely on polling (see receive()).

    Broadcast semantics
    -------------------
    When a broadcast message (recipient_id = None) is first seen by a given
    agent in receive(), the bus atomically:
      1. Inserts a broadcast_receipt row to prevent re-delivery to that agent.
      2. Clones the message as a direct (recipient_id = agent_id) copy.
      3. Returns the clone — the caller then calls acknowledge(clone_id) normally.

    The original broadcast row stays PENDING indefinitely (TTL cleanup is a
    separate concern not in scope for this module).
    """

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn
        # per-agent asyncio Event for low-latency same-process wake-up
        self._agent_events: dict[str, asyncio.Event] = {}
        # cluster_id → set of agent_ids that have called receive() at least once
        self._cluster_members: dict[str, set[str]] = {}

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _event(self, agent_id: str) -> asyncio.Event:
        if agent_id not in self._agent_events:
            self._agent_events[agent_id] = asyncio.Event()
        return self._agent_events[agent_id]

    def _get_cluster_id(self, agent_id: str) -> Optional[str]:
        row = self._conn.execute(
            "SELECT cluster_id FROM agents WHERE id = ?", (agent_id,)
        ).fetchone()
        return row["cluster_id"] if row else None

    def _poll_db(self, agent_id: str, cluster_id: str) -> Optional[Message]:
        """Return the oldest PENDING message for agent_id, or None."""
        row = self._conn.execute(
            f"""
            SELECT {_MSG_COLS} FROM messages
            WHERE status = 'PENDING'
              AND (
                    recipient_id = ?
                    OR (
                        recipient_id IS NULL
                        AND cluster_id = ?
                        AND NOT EXISTS (
                            SELECT 1 FROM broadcast_receipts
                            WHERE message_id = messages.id AND agent_id = ?
                        )
                    )
              )
            ORDER BY created_at ASC
            LIMIT 1
            """,
            (agent_id, cluster_id, agent_id),
        ).fetchone()
        return _row_to_message(row) if row else None

    def _materialise_broadcast(self, msg: Message, agent_id: str) -> Message:
        """Convert a broadcast hit into a direct clone the caller can acknowledge.

        Atomically records a broadcast_receipt (preventing re-delivery to this
        agent) and inserts the clone as a new PENDING direct message.
        """
        clone = Message(
            type=msg.type,
            sender_id=msg.sender_id,
            cluster_id=msg.cluster_id,
            payload=msg.payload,
            recipient_id=agent_id,
        )
        with self._conn:
            self._conn.execute(
                """
                INSERT OR IGNORE INTO broadcast_receipts (message_id, agent_id, created_at)
                VALUES (?, ?, ?)
                """,
                (msg.id, agent_id, time.time()),
            )
            _insert_message(self._conn, clone)
        return clone

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def send(self, message: Message) -> None:
        """Persist *message* as PENDING and notify the recipient.

        For direct messages, applies backpressure: if the recipient already has
        ≥ _BP_HIGH unacknowledged messages, blocks until the count falls below
        _BP_LOW.
        """
        if message.recipient_id is not None:
            if await self.pending_count(message.recipient_id) >= _BP_HIGH:
                while await self.pending_count(message.recipient_id) >= _BP_LOW:
                    await asyncio.sleep(_POLL_INTERVAL)

        with tracer.start_as_current_span(
            "message.send",
            kind=trace.SpanKind.PRODUCER,
            attributes={
                "message.id": message.id,
                "message.type": message.type.value,
                "message.sender_id": message.sender_id,
                "message.recipient_id": message.recipient_id or "broadcast",
            },
        ):
            with self._conn:
                _insert_message(self._conn, message)

        # Wake up recipient(s) for in-process low-latency delivery
        if message.recipient_id is not None:
            self._event(message.recipient_id).set()
        else:
            for member in self._cluster_members.get(message.cluster_id, set()):
                self._event(member).set()

    async def receive(self, agent_id: str, timeout: float = 5.0) -> Optional[Message]:
        """Return the oldest PENDING message for *agent_id*, or None on timeout.

        Does not auto-acknowledge.  Caller must call acknowledge() after
        processing to mark the message DELIVERED.
        """
        cluster_id = self._get_cluster_id(agent_id)
        if cluster_id is None:
            return None

        # Register so broadcast sends can wake this agent
        self._cluster_members.setdefault(cluster_id, set()).add(agent_id)
        event = self._event(agent_id)

        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout

        while True:
            msg = self._poll_db(agent_id, cluster_id)
            if msg is not None:
                if msg.recipient_id is None:
                    msg = self._materialise_broadcast(msg, agent_id)
                with tracer.start_as_current_span(
                    "message.receive",
                    kind=trace.SpanKind.CONSUMER,
                    attributes={
                        "message.id": msg.id,
                        "message.type": msg.type.value,
                        "message.sender_id": msg.sender_id,
                        "message.recipient_id": agent_id,
                    },
                ):
                    pass
                return msg

            remaining = deadline - loop.time()
            if remaining <= 0:
                return None

            # Clear before waiting; re-poll at top of loop catches any message
            # that arrives between clear and wait (worst-case: _POLL_INTERVAL late).
            event.clear()
            try:
                await asyncio.wait_for(
                    event.wait(), timeout=min(_POLL_INTERVAL, remaining)
                )
            except asyncio.TimeoutError:
                pass

    async def broadcast(self, cluster_id: str, message: Message) -> None:
        """Send *message* to all agents in *cluster_id*.

        Sets recipient_id = None so every agent in the cluster receives it via
        their next receive() call.
        """
        message.recipient_id = None
        message.cluster_id = cluster_id
        await self.send(message)

    async def acknowledge(self, message_id: str) -> None:
        """Mark *message_id* as DELIVERED."""
        with self._conn:
            self._conn.execute(
                "UPDATE messages SET status = 'DELIVERED', delivered_at = ? WHERE id = ?",
                (time.time(), message_id),
            )

    async def pending_count(self, agent_id: str) -> int:
        """Return the number of PENDING messages directly addressed to *agent_id*."""
        row = self._conn.execute(
            "SELECT COUNT(*) FROM messages WHERE recipient_id = ? AND status = 'PENDING'",
            (agent_id,),
        ).fetchone()
        return row[0]
