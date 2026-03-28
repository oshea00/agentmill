# Spec: Agent Lifecycle & State Management

## Overview

Every agent in the system is a finite-state machine. State transitions are the
only way an agent's status changes. All state is persisted to SQLite so agents
can survive process restarts and be resumed or inspected post-mortem.

---

## Agent Roles

The system has two distinct agent roles, determined at instantiation:

| Role | Description |
|---|---|
| `orchestrator` | Owns a task graph; spawns, delegates to, and terminates sub-agents. Exactly one per cluster. |
| `worker` | Executes a single delegated task; may form peer relationships with other workers in the same cluster. |

An orchestrator is itself a worker from the perspective of a parent orchestrator
(hybrid topology: orchestrators can be nested).

---

## Lifecycle States

```
CREATED → INITIALIZING → IDLE → RUNNING → SUSPENDED → IDLE
                                         ↓
                                      FAILED
                                         ↓
                                    TERMINATED
                              IDLE → TERMINATED
```

| State | Meaning |
|---|---|
| `CREATED` | Row inserted into DB; agent not yet started. |
| `INITIALIZING` | Loading context, tools, and MCP servers. |
| `IDLE` | Ready and waiting for a task assignment or message. |
| `RUNNING` | Actively executing a task (Claude API call in flight or tool use). |
| `SUSPENDED` | Paused mid-task (e.g. waiting on a peer, awaiting human input). |
| `FAILED` | Unrecoverable error; stores error payload. |
| `TERMINATED` | Cleanly shut down; final state. |

**Allowed transitions only.** Any attempt to move to an invalid next state raises
`InvalidStateTransitionError` and the transition is not persisted.

---

## Data Model

### `agents` table (SQLite)

```sql
CREATE TABLE agents (
    id          TEXT PRIMARY KEY,          -- UUID v4
    role        TEXT NOT NULL,             -- 'orchestrator' | 'worker'
    state       TEXT NOT NULL,             -- enum value above
    parent_id   TEXT REFERENCES agents(id), -- NULL for root orchestrator
    cluster_id  TEXT NOT NULL,             -- groups peers together
    name        TEXT,                      -- human-readable label
    created_at  REAL NOT NULL,             -- Unix timestamp
    updated_at  REAL NOT NULL,
    context     TEXT,                      -- JSON blob: task description, tools, etc.
    error       TEXT                       -- JSON blob: only set on FAILED
);
```

### `agent_state_history` table

```sql
CREATE TABLE agent_state_history (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    agent_id    TEXT NOT NULL REFERENCES agents(id),
    from_state  TEXT,
    to_state    TEXT NOT NULL,
    reason      TEXT,
    timestamp   REAL NOT NULL
);
```

---

## Python Interface

```python
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional
import uuid, time

class AgentRole(str, Enum):
    ORCHESTRATOR = "orchestrator"
    WORKER = "worker"

class AgentState(str, Enum):
    CREATED       = "CREATED"
    INITIALIZING  = "INITIALIZING"
    IDLE          = "IDLE"
    RUNNING       = "RUNNING"
    SUSPENDED     = "SUSPENDED"
    FAILED        = "FAILED"
    TERMINATED    = "TERMINATED"

VALID_TRANSITIONS: dict[AgentState, set[AgentState]] = {
    AgentState.CREATED:      {AgentState.INITIALIZING},
    AgentState.INITIALIZING: {AgentState.IDLE, AgentState.FAILED},
    AgentState.IDLE:         {AgentState.RUNNING, AgentState.TERMINATED},
    AgentState.RUNNING:      {AgentState.IDLE, AgentState.SUSPENDED, AgentState.FAILED},
    AgentState.SUSPENDED:    {AgentState.IDLE, AgentState.FAILED, AgentState.TERMINATED},
    AgentState.FAILED:       {AgentState.TERMINATED},
    AgentState.TERMINATED:   set(),
}

@dataclass
class Agent:
    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    role: AgentRole = AgentRole.WORKER
    state: AgentState = AgentState.CREATED
    parent_id: Optional[str] = None
    cluster_id: str = ""
    name: Optional[str] = None
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    context: dict = field(default_factory=dict)
    error: Optional[dict] = None
```

### `AgentRepository` (SQLite persistence)

```python
class AgentRepository:
    def create(self, agent: Agent) -> Agent: ...
    def get(self, agent_id: str) -> Optional[Agent]: ...
    def transition(self, agent_id: str, to_state: AgentState, reason: str = "") -> Agent: ...
    def list_by_cluster(self, cluster_id: str) -> list[Agent]: ...
    def list_by_state(self, state: AgentState) -> list[Agent]: ...
```

`transition()` must:
1. Load current state from DB inside a transaction.
2. Validate against `VALID_TRANSITIONS`.
3. Write new state to `agents` and append a row to `agent_state_history`.
4. Emit an OTel span event (see observability spec).

---

## Agent Context

The `context` JSON blob is the only agent-level storage for task-specific data.
It is **not** a message queue (use the messaging layer for that). Typical fields:

```json
{
  "task_description": "Summarise Q3 earnings reports",
  "tools_enabled": ["file_read", "web_search"],
  "mcp_servers": ["filesystem", "brave-search"],
  "max_iterations": 20,
  "conversation_history": []
}
```

`conversation_history` is updated in place after each Claude turn and persisted
back to SQLite so a suspended agent can be resumed with full context intact.

---

## Spawn & Termination

### Spawning a sub-agent (orchestrator responsibility)

```python
async def spawn_worker(
    orchestrator: Agent,
    task: str,
    tools: list[str],
    mcp_servers: list[str],
) -> Agent:
    worker = Agent(
        role=AgentRole.WORKER,
        parent_id=orchestrator.id,
        cluster_id=orchestrator.cluster_id,
        context={"task_description": task, "tools_enabled": tools, "mcp_servers": mcp_servers},
    )
    repo.create(worker)
    repo.transition(worker.id, AgentState.INITIALIZING)
    return worker
```

### Termination

- Orchestrator sends a `TERMINATE` control message to the worker (see messaging spec).
- Worker transitions `RUNNING → IDLE → TERMINATED` or `SUSPENDED → TERMINATED`.
- Orchestrator transitions itself to `TERMINATED` only after all workers in its
  cluster are `TERMINATED` or `FAILED`.

---

## Error Handling

- Any unhandled exception during `RUNNING` must call `transition(FAILED)` with the
  error stored as JSON in `agent.error`.
- The orchestrator is responsible for deciding whether to retry (re-spawn) or
  propagate failure upward.
- Never leave an agent stuck in `RUNNING` — use try/finally to guarantee a
  transition on exception.

---

## Design Decisions

| Decision | Choice | Rationale |
|---|---|---|
| State machine strictness | Hard-fail on invalid transitions | Prevents silent corruption; easier to debug |
| Persistence granularity | Every transition written to DB | Enables post-mortem audit without extra tooling |
| Context as JSON blob | Single column, not normalised | Avoids schema migrations as context evolves |
| Async execution model | `asyncio` throughout | Matches Claude Code SDK's async-first design |
