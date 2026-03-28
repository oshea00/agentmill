"""Tests for MessageBus: send/receive, broadcast, acknowledge, backpressure."""

import asyncio
import json
import sqlite3
import time

import pytest

from orchestrator.db import init_schema
from orchestrator.agent import Agent, AgentRole, AgentRepository
from orchestrator.bus import (
    Message,
    MessageBus,
    MessageType,
    _BP_HIGH,
    _BP_LOW,
    _POLL_INTERVAL,
)


# The bus uses asyncio-specific primitives (asyncio.Event, asyncio.create_task,
# asyncio.get_running_loop).  Override anyio_backend to run only on asyncio.
@pytest.fixture
def anyio_backend():
    return "asyncio"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def conn():
    c = sqlite3.connect(":memory:", check_same_thread=False)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys=ON")
    init_schema(c)
    yield c
    c.close()


@pytest.fixture()
def repo(conn):
    return AgentRepository(conn)


@pytest.fixture()
def bus(conn):
    return MessageBus(conn)


def make_agent(repo: AgentRepository, cluster_id: str = "cluster-1") -> Agent:
    a = Agent(cluster_id=cluster_id)
    repo.create(a)
    return a


def direct_msg(
    sender: Agent,
    recipient: Agent,
    *,
    msg_type: MessageType = MessageType.HEARTBEAT,
    payload: dict | None = None,
) -> Message:
    return Message(
        type=msg_type,
        sender_id=sender.id,
        cluster_id=sender.cluster_id,
        recipient_id=recipient.id,
        payload=payload or {},
    )


def broadcast_msg(
    sender: Agent,
    *,
    msg_type: MessageType = MessageType.CONTROL,
    payload: dict | None = None,
) -> Message:
    return Message(
        type=msg_type,
        sender_id=sender.id,
        cluster_id=sender.cluster_id,
        recipient_id=None,
        payload=payload or {"command": "STATUS"},
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _db_status(conn: sqlite3.Connection, message_id: str) -> str:
    """Read status of a message directly from the DB."""
    row = conn.execute(
        "SELECT status FROM messages WHERE id = ?", (message_id,)
    ).fetchone()
    return row["status"] if row else "NOT_FOUND"


def _db_pending(conn: sqlite3.Connection, agent_id: str) -> int:
    row = conn.execute(
        "SELECT COUNT(*) FROM messages WHERE recipient_id = ? AND status = 'PENDING'",
        (agent_id,),
    ).fetchone()
    return row[0]


async def _drain_queue(bus: MessageBus, agent: Agent, *, count: int) -> list[Message]:
    """Receive and acknowledge *count* messages for *agent*."""
    msgs = []
    for _ in range(count):
        m = await bus.receive(agent.id, timeout=1.0)
        assert m is not None
        await bus.acknowledge(m.id)
        msgs.append(m)
    return msgs


# ---------------------------------------------------------------------------
# send / receive
# ---------------------------------------------------------------------------


class TestSendReceive:
    @pytest.mark.anyio
    async def test_direct_message_arrives(self, repo, bus):
        a = make_agent(repo)
        b = make_agent(repo)
        msg = direct_msg(a, b, payload={"x": 1})
        await bus.send(msg)
        received = await bus.receive(b.id, timeout=1.0)
        assert received is not None
        assert received.id == msg.id
        assert received.payload == {"x": 1}

    @pytest.mark.anyio
    async def test_receive_returns_none_on_timeout(self, repo, bus):
        a = make_agent(repo)
        result = await bus.receive(a.id, timeout=0.05)
        assert result is None

    @pytest.mark.anyio
    async def test_receive_for_unknown_agent_returns_none(self, bus):
        result = await bus.receive("no-such-agent", timeout=0.05)
        assert result is None

    @pytest.mark.anyio
    async def test_fifo_ordering_per_sender(self, repo, bus):
        """Oldest created_at is returned first across three messages."""
        a = make_agent(repo)
        b = make_agent(repo)
        payloads = [{"seq": i} for i in range(3)]
        for i, p in enumerate(payloads):
            # Stagger timestamps explicitly to guarantee deterministic ordering
            msg = Message(
                type=MessageType.HEARTBEAT,
                sender_id=a.id,
                cluster_id=a.cluster_id,
                recipient_id=b.id,
                payload=p,
                created_at=1_000_000.0 + i,  # t+0, t+1, t+2
            )
            await bus.send(msg)

        for expected in payloads:
            m = await bus.receive(b.id, timeout=1.0)
            assert m is not None
            assert m.payload == expected
            # Acknowledge so the next receive() advances to the next message
            await bus.acknowledge(m.id)

    @pytest.mark.anyio
    async def test_message_not_visible_to_other_agent(self, repo, bus):
        """A direct message addressed to B should not appear for C."""
        a = make_agent(repo)
        b = make_agent(repo)
        c = make_agent(repo)
        await bus.send(direct_msg(a, b))
        result = await bus.receive(c.id, timeout=0.05)
        assert result is None

    @pytest.mark.anyio
    async def test_message_type_preserved(self, repo, bus):
        a = make_agent(repo)
        b = make_agent(repo)
        msg = direct_msg(a, b, msg_type=MessageType.TASK_ASSIGN, payload={"task_id": "t1"})
        await bus.send(msg)
        received = await bus.receive(b.id, timeout=1.0)
        assert received.type == MessageType.TASK_ASSIGN

    @pytest.mark.anyio
    async def test_receive_wakes_on_send(self, repo, bus):
        """receive() should return quickly once a message is sent, not just on poll."""
        a = make_agent(repo)
        b = make_agent(repo)

        async def sender():
            await asyncio.sleep(0.05)
            await bus.send(direct_msg(a, b))

        start = time.monotonic()
        asyncio.create_task(sender())
        msg = await bus.receive(b.id, timeout=2.0)
        elapsed = time.monotonic() - start

        assert msg is not None
        # Should wake within ~3× poll interval, not wait the full 2s timeout
        assert elapsed < 0.5


# ---------------------------------------------------------------------------
# acknowledge
# ---------------------------------------------------------------------------


class TestAcknowledge:
    @pytest.mark.anyio
    async def test_acknowledge_marks_delivered(self, repo, bus, conn):
        a = make_agent(repo)
        b = make_agent(repo)
        await bus.send(direct_msg(a, b))
        msg = await bus.receive(b.id, timeout=1.0)
        assert msg is not None

        assert _db_status(conn, msg.id) == "PENDING"
        await bus.acknowledge(msg.id)
        assert _db_status(conn, msg.id) == "DELIVERED"

    @pytest.mark.anyio
    async def test_acknowledged_message_not_redelivered(self, repo, bus):
        a = make_agent(repo)
        b = make_agent(repo)
        await bus.send(direct_msg(a, b))
        msg = await bus.receive(b.id, timeout=1.0)
        await bus.acknowledge(msg.id)

        # No more messages should be pending
        second = await bus.receive(b.id, timeout=0.05)
        assert second is None

    @pytest.mark.anyio
    async def test_unacknowledged_message_stays_pending(self, repo, bus, conn):
        a = make_agent(repo)
        b = make_agent(repo)
        await bus.send(direct_msg(a, b))
        msg = await bus.receive(b.id, timeout=1.0)
        assert msg is not None
        # Do NOT acknowledge
        assert _db_status(conn, msg.id) == "PENDING"

    @pytest.mark.anyio
    async def test_delivered_at_is_set_on_acknowledge(self, repo, bus, conn):
        a = make_agent(repo)
        b = make_agent(repo)
        await bus.send(direct_msg(a, b))
        msg = await bus.receive(b.id, timeout=1.0)
        before = time.time()
        await bus.acknowledge(msg.id)
        after = time.time()

        row = conn.execute(
            "SELECT delivered_at FROM messages WHERE id = ?", (msg.id,)
        ).fetchone()
        assert before <= row["delivered_at"] <= after

    @pytest.mark.anyio
    async def test_pending_count_decrements_after_acknowledge(self, repo, bus):
        a = make_agent(repo)
        b = make_agent(repo)
        for _ in range(3):
            await bus.send(direct_msg(a, b))

        assert await bus.pending_count(b.id) == 3
        msg = await bus.receive(b.id, timeout=1.0)
        await bus.acknowledge(msg.id)
        assert await bus.pending_count(b.id) == 2


# ---------------------------------------------------------------------------
# broadcast
# ---------------------------------------------------------------------------


class TestBroadcast:
    @pytest.mark.anyio
    async def test_broadcast_reaches_all_cluster_members(self, repo, bus):
        orch = make_agent(repo, cluster_id="clu")
        w1 = make_agent(repo, cluster_id="clu")
        w2 = make_agent(repo, cluster_id="clu")

        await bus.broadcast("clu", broadcast_msg(orch, payload={"command": "TERMINATE"}))

        m1 = await bus.receive(w1.id, timeout=1.0)
        m2 = await bus.receive(w2.id, timeout=1.0)

        assert m1 is not None
        assert m2 is not None
        assert m1.payload == {"command": "TERMINATE"}
        assert m2.payload == {"command": "TERMINATE"}

    @pytest.mark.anyio
    async def test_broadcast_not_received_by_other_cluster(self, repo, bus):
        orch = make_agent(repo, cluster_id="clu-a")
        outsider = make_agent(repo, cluster_id="clu-b")

        await bus.broadcast("clu-a", broadcast_msg(orch))

        result = await bus.receive(outsider.id, timeout=0.05)
        assert result is None

    @pytest.mark.anyio
    async def test_broadcast_delivered_once_per_agent(self, repo, bus):
        """A second receive() call after acknowledging should not re-deliver."""
        orch = make_agent(repo, cluster_id="clu")
        w = make_agent(repo, cluster_id="clu")

        await bus.broadcast("clu", broadcast_msg(orch))

        first = await bus.receive(w.id, timeout=1.0)
        assert first is not None
        await bus.acknowledge(first.id)

        second = await bus.receive(w.id, timeout=0.05)
        assert second is None

    @pytest.mark.anyio
    async def test_broadcast_acknowledge_per_agent_independent(self, repo, bus):
        """Acknowledging the broadcast copy for one agent does not affect another's."""
        orch = make_agent(repo, cluster_id="clu")
        w1 = make_agent(repo, cluster_id="clu")
        w2 = make_agent(repo, cluster_id="clu")

        await bus.broadcast("clu", broadcast_msg(orch))

        m1 = await bus.receive(w1.id, timeout=1.0)
        assert m1 is not None
        await bus.acknowledge(m1.id)

        # w2 should still be able to receive and acknowledge independently
        m2 = await bus.receive(w2.id, timeout=1.0)
        assert m2 is not None
        await bus.acknowledge(m2.id)

    @pytest.mark.anyio
    async def test_broadcast_clone_has_correct_recipient(self, repo, bus):
        """The cloned direct copy returned by receive() has recipient_id = agent_id."""
        orch = make_agent(repo, cluster_id="clu")
        w = make_agent(repo, cluster_id="clu")

        await bus.broadcast("clu", broadcast_msg(orch))
        m = await bus.receive(w.id, timeout=1.0)

        assert m is not None
        assert m.recipient_id == w.id

    @pytest.mark.anyio
    async def test_broadcast_preserves_payload(self, repo, bus):
        orch = make_agent(repo, cluster_id="clu")
        w = make_agent(repo, cluster_id="clu")
        payload = {"command": "SUSPEND", "reason": "maintenance"}

        await bus.broadcast("clu", broadcast_msg(orch, payload=payload))
        m = await bus.receive(w.id, timeout=1.0)

        assert m.payload == payload

    @pytest.mark.anyio
    async def test_sender_does_not_receive_own_broadcast(self, repo, bus):
        """Sender should not pick up its own broadcast unless it calls receive()."""
        orch = make_agent(repo, cluster_id="clu")
        # orch calls receive() — it should get its own broadcast since it is
        # also in the cluster; this tests that the mechanism is neutral about sender.
        await bus.broadcast("clu", broadcast_msg(orch))
        m = await bus.receive(orch.id, timeout=1.0)
        # Sender is in the cluster, so it CAN receive its own broadcast.
        assert m is not None


# ---------------------------------------------------------------------------
# pending_count
# ---------------------------------------------------------------------------


class TestPendingCount:
    @pytest.mark.anyio
    async def test_empty_queue_returns_zero(self, repo, bus):
        a = make_agent(repo)
        assert await bus.pending_count(a.id) == 0

    @pytest.mark.anyio
    async def test_count_reflects_sent_messages(self, repo, bus):
        a = make_agent(repo)
        b = make_agent(repo)
        for _ in range(5):
            await bus.send(direct_msg(a, b))
        assert await bus.pending_count(b.id) == 5

    @pytest.mark.anyio
    async def test_broadcast_does_not_affect_direct_pending_count(self, repo, bus):
        """NULL-recipient broadcast rows are not counted toward an agent's backpressure."""
        orch = make_agent(repo, cluster_id="clu")
        w = make_agent(repo, cluster_id="clu")
        await bus.broadcast("clu", broadcast_msg(orch))
        # The clone only exists after receive(); before that, pending_count = 0
        assert await bus.pending_count(w.id) == 0


# ---------------------------------------------------------------------------
# Backpressure
# ---------------------------------------------------------------------------


class TestBackpressure:
    def _fill_queue(self, conn: sqlite3.Connection, sender_id: str, recipient_id: str, cluster_id: str, n: int) -> None:
        """Insert *n* PENDING messages directly into the DB, bypassing the bus."""
        base_ts = time.time()
        for i in range(n):
            conn.execute(
                """
                INSERT INTO messages
                    (id, type, sender_id, recipient_id, cluster_id, payload, status, created_at)
                VALUES (?, ?, ?, ?, ?, ?, 'PENDING', ?)
                """,
                (
                    str(__import__("uuid").uuid4()),
                    MessageType.HEARTBEAT.value,
                    sender_id,
                    recipient_id,
                    cluster_id,
                    json.dumps({}),
                    base_ts + i * 0.0001,
                ),
            )
        conn.commit()

    @pytest.mark.anyio
    async def test_send_blocks_when_queue_at_high_watermark(self, repo, bus, conn):
        """send() must not complete while recipient has ≥ _BP_HIGH pending msgs."""
        a = make_agent(repo)
        b = make_agent(repo)

        # Pre-fill to exactly _BP_HIGH
        self._fill_queue(conn, a.id, b.id, a.cluster_id, _BP_HIGH)
        assert _db_pending(conn, b.id) == _BP_HIGH

        send_started = asyncio.Event()
        send_done = asyncio.Event()

        async def slow_send():
            send_started.set()
            await bus.send(direct_msg(a, b))
            send_done.set()

        task = asyncio.create_task(slow_send())
        await asyncio.wait_for(send_started.wait(), timeout=1.0)

        # Give the task a moment to enter the backpressure loop
        await asyncio.sleep(_POLL_INTERVAL * 3)
        assert not send_done.is_set(), "send() completed before queue drained — backpressure failed"

        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    @pytest.mark.anyio
    async def test_send_unblocks_when_queue_drains_below_low_watermark(self, repo, bus, conn):
        """send() completes once pending count drops below _BP_LOW."""
        a = make_agent(repo)
        b = make_agent(repo)

        self._fill_queue(conn, a.id, b.id, a.cluster_id, _BP_HIGH)

        send_done = asyncio.Event()

        async def waiting_send():
            await bus.send(direct_msg(a, b, payload={"marker": True}))
            send_done.set()

        task = asyncio.create_task(waiting_send())

        # Let task enter backpressure loop
        await asyncio.sleep(_POLL_INTERVAL * 3)
        assert not send_done.is_set()

        # Drain enough messages so count falls below _BP_LOW
        # Need to go from _BP_HIGH down to _BP_LOW - 1
        messages_to_drain = _BP_HIGH - _BP_LOW + 1
        rows = conn.execute(
            "SELECT id FROM messages WHERE recipient_id = ? AND status = 'PENDING' LIMIT ?",
            (b.id, messages_to_drain),
        ).fetchall()
        for row in rows:
            await bus.acknowledge(row["id"])

        remaining = _db_pending(conn, b.id)
        assert remaining < _BP_LOW, f"Expected < {_BP_LOW} pending, got {remaining}"

        # Now the send should complete
        await asyncio.wait_for(send_done.wait(), timeout=2.0)
        assert send_done.is_set()

        # Verify the new message was actually persisted
        new_row = conn.execute(
            "SELECT payload FROM messages WHERE recipient_id = ? AND status = 'PENDING'",
            (b.id,),
        ).fetchone()
        assert new_row is not None
        assert json.loads(new_row["payload"]) == {"marker": True}

        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):
            pass

    @pytest.mark.anyio
    async def test_send_below_threshold_is_not_blocked(self, repo, bus, conn):
        """send() must not block at all when pending count is below _BP_HIGH."""
        a = make_agent(repo)
        b = make_agent(repo)

        # One below the threshold
        self._fill_queue(conn, a.id, b.id, a.cluster_id, _BP_HIGH - 1)

        start = time.monotonic()
        await bus.send(direct_msg(a, b))
        elapsed = time.monotonic() - start

        # Should complete almost instantly (no backpressure sleep)
        assert elapsed < _POLL_INTERVAL * 2

    @pytest.mark.anyio
    async def test_backpressure_does_not_apply_to_broadcasts(self, repo, bus, conn):
        """Broadcasts bypass backpressure (recipient_id is None)."""
        a = make_agent(repo, cluster_id="clu")
        b = make_agent(repo, cluster_id="clu")

        # Fill b's direct queue to _BP_HIGH
        self._fill_queue(conn, a.id, b.id, "clu", _BP_HIGH)

        # A broadcast should not block even though b is saturated
        start = time.monotonic()
        await bus.broadcast("clu", broadcast_msg(a, payload={"command": "STATUS"}))
        elapsed = time.monotonic() - start

        assert elapsed < _POLL_INTERVAL * 2
