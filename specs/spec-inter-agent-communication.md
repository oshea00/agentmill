# Spec: Inter-Agent Communication & Message Passing

## Overview

Agents communicate via an async message bus backed by SQLite. Messages are
typed, addressed by agent ID, and delivered in-order per sender-receiver pair.
The bus supports both direct (point-to-point) and broadcast (cluster-wide)
delivery. There is no shared memory between agents — all coordination happens
through messages.

---

## Message Types

| Type | Direction | Purpose |
|---|---|---|
| `TASK_ASSIGN` | Orchestrator → Worker | Delegate a task to a worker |
| `TASK_RESULT` | Worker → Orchestrator | Return a completed result |
| `TASK_ERROR` | Worker → Orchestrator | Report an unrecoverable failure |
| `STATUS_REQUEST` | Any → Any | Ask for current state + progress |
| `STATUS_RESPONSE` | Any → Any | Reply to a status request |
| `PEER_SYNC` | Worker ↔ Worker | Share partial results with a peer |
| `CONTROL` | Orchestrator → Worker | Lifecycle control: `SUSPEND`, `RESUME`, `TERMINATE` |
| `HEARTBEAT` | Any → Any | Liveness signal (no payload required) |

---

## Data Model

### `messages` table (SQLite)

```sql
CREATE TABLE messages (
    id            TEXT PRIMARY KEY,          -- UUID v4
    type          TEXT NOT NULL,             -- MessageType enum
    sender_id     TEXT NOT NULL,             -- agent UUID
    recipient_id  TEXT,                      -- NULL = broadcast to cluster
    cluster_id    TEXT NOT NULL,
    payload       TEXT NOT NULL,             -- JSON blob
    status        TEXT NOT NULL DEFAULT 'PENDING',  -- PENDING | DELIVERED | FAILED
    created_at    REAL NOT NULL,
    delivered_at  REAL
);

CREATE INDEX idx_messages_recipient ON messages(recipient_id, status, created_at);
CREATE INDEX idx_messages_cluster   ON messages(cluster_id, status, created_at);
```

### `MessageStatus` values

| Value | Meaning |
|---|---|
| `PENDING` | Written to DB, not yet consumed |
| `DELIVERED` | Consumed and acknowledged by recipient |
| `FAILED` | Could not be delivered (recipient terminated/failed) |

---

## Python Interface

```python
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Optional
import uuid, time

class MessageType(str, Enum):
    TASK_ASSIGN     = "TASK_ASSIGN"
    TASK_RESULT     = "TASK_RESULT"
    TASK_ERROR      = "TASK_ERROR"
    STATUS_REQUEST  = "STATUS_REQUEST"
    STATUS_RESPONSE = "STATUS_RESPONSE"
    PEER_SYNC       = "PEER_SYNC"
    CONTROL         = "CONTROL"
    HEARTBEAT       = "HEARTBEAT"

@dataclass
class Message:
    type: MessageType
    sender_id: str
    cluster_id: str
    payload: dict[str, Any]
    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    recipient_id: Optional[str] = None   # None = broadcast
    status: str = "PENDING"
    created_at: float = field(default_factory=time.time)
    delivered_at: Optional[float] = None
```

### `MessageBus`

```python
class MessageBus:
    async def send(self, message: Message) -> None: ...
    async def receive(self, agent_id: str, timeout: float = 5.0) -> Optional[Message]: ...
    async def broadcast(self, cluster_id: str, message: Message) -> None: ...
    async def acknowledge(self, message_id: str) -> None: ...
    async def pending_count(self, agent_id: str) -> int: ...
```

#### `send()`
- Inserts the message into SQLite with status `PENDING`.
- If `recipient_id` is set, notifies the recipient's asyncio `Event` (in-process)
  or relies on polling (cross-process).
- Emits an OTel span with `message.id`, `message.type`, sender, recipient.

#### `receive()`
- Polls for the oldest `PENDING` message addressed to `agent_id` or broadcast
  to its cluster.
- Returns `None` on timeout (caller should handle gracefully).
- Does **not** auto-acknowledge — caller must call `acknowledge()` after
  processing to mark `DELIVERED`.

#### `broadcast()`
- Sends a message with `recipient_id = NULL`.
- All agents in `cluster_id` will receive it via their next `receive()` call.
- Used for cluster-wide status requests and control signals.

---

## Standard Payloads

### `TASK_ASSIGN`
```json
{
  "task_id": "uuid",
  "description": "Summarise the attached earnings report",
  "input_data": {},
  "deadline_seconds": 120
}
```

### `TASK_RESULT`
```json
{
  "task_id": "uuid",
  "output": {},
  "iterations_used": 7,
  "tool_calls_made": 3
}
```

### `TASK_ERROR`
```json
{
  "task_id": "uuid",
  "error_type": "ToolCallFailed",
  "message": "MCP server 'filesystem' returned 503",
  "retryable": true
}
```

### `CONTROL`
```json
{
  "command": "TERMINATE",     // SUSPEND | RESUME | TERMINATE
  "reason": "Task cancelled by user"
}
```

### `PEER_SYNC`
```json
{
  "sync_type": "partial_result",   // partial_result | context_share | lock_request
  "data": {}
}
```

---

## Message Flow: Task Delegation

```
Orchestrator                        Worker
     │                                 │
     │──── TASK_ASSIGN ───────────────►│
     │                                 │  (Worker transitions IDLE → RUNNING)
     │                                 │  (Worker executes task)
     │◄─── HEARTBEAT (periodic) ───────│
     │◄─── TASK_RESULT ────────────────│
     │                                 │  (Worker transitions RUNNING → IDLE)
```

---

## Message Flow: Peer Sync

```
Worker A                           Worker B
    │                                  │
    │──── PEER_SYNC (partial) ────────►│
    │◄─── PEER_SYNC (partial) ─────────│
    │                                  │
    (Both continue with merged context)
```

Peer sync is optional and initiated by a worker. Workers should only sync when
their tasks have a declared dependency (`context.peer_dependencies`). Workers
**must not** spin-poll for peer sync messages — use `receive()` with timeout
then continue regardless.

---

## Delivery Guarantees

| Guarantee | Level |
|---|---|
| Ordering | FIFO per sender-recipient pair (by `created_at`) |
| Delivery | At-least-once (re-delivered if not acknowledged within TTL) |
| Durability | SQLite write-ahead log; survives process crash |
| Latency | Best-effort; no hard real-time guarantees |

**TTL & retry:** Messages not acknowledged within 30 seconds are re-queued once.
After a second failed delivery the status is set to `FAILED` and the sender is
notified via an internal error event.

---

## Backpressure

If an agent's `pending_count()` exceeds 50 unacknowledged messages, the bus
will block `send()` for that recipient until the queue drains below 25. This
prevents runaway orchestrators from flooding slow workers.

---

## Design Decisions

| Decision | Choice | Rationale |
|---|---|---|
| Transport | SQLite (same DB as state) | Zero external dependencies; good enough for local orchestration |
| Broadcast mechanism | NULL recipient + cluster_id | Simple fan-out without a dedicated pub/sub system |
| Acknowledgement model | Explicit `acknowledge()` | Prevents silent message loss on worker crash |
| In-process notification | asyncio `Event` | Low-latency wake-up for same-process agents |
| Cross-process delivery | Polling on `receive()` | Keeps the bus simple; polling interval ≤ 100ms |
