"""Agent dataclass, state machine, and SQLite-backed repository."""

import json
import sqlite3
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional

from opentelemetry import trace


class AgentRole(str, Enum):
    ORCHESTRATOR = "orchestrator"
    WORKER = "worker"


class AgentState(str, Enum):
    CREATED = "CREATED"
    INITIALIZING = "INITIALIZING"
    IDLE = "IDLE"
    RUNNING = "RUNNING"
    SUSPENDED = "SUSPENDED"
    FAILED = "FAILED"
    TERMINATED = "TERMINATED"


VALID_TRANSITIONS: dict[AgentState, set[AgentState]] = {
    AgentState.CREATED: {AgentState.INITIALIZING},
    AgentState.INITIALIZING: {AgentState.IDLE, AgentState.FAILED},
    AgentState.IDLE: {AgentState.RUNNING, AgentState.TERMINATED},
    AgentState.RUNNING: {AgentState.IDLE, AgentState.SUSPENDED, AgentState.FAILED},
    AgentState.SUSPENDED: {AgentState.IDLE, AgentState.FAILED, AgentState.TERMINATED},
    AgentState.FAILED: {AgentState.TERMINATED},
    AgentState.TERMINATED: set(),
}


class InvalidStateTransitionError(Exception):
    """Raised when a requested state transition is not in VALID_TRANSITIONS."""


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


# Column list shared by SELECT statements — never use SELECT *
_AGENT_COLS = (
    "id, role, state, parent_id, cluster_id, name, created_at, updated_at, context, error"
)


def _row_to_agent(row: sqlite3.Row) -> Agent:
    return Agent(
        id=row["id"],
        role=AgentRole(row["role"]),
        state=AgentState(row["state"]),
        parent_id=row["parent_id"],
        cluster_id=row["cluster_id"],
        name=row["name"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        context=json.loads(row["context"]) if row["context"] else {},
        error=json.loads(row["error"]) if row["error"] else None,
    )


class AgentRepository:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def create(self, agent: Agent) -> Agent:
        """Persist a new agent and record its initial state in history."""
        self._conn.execute(
            f"""
            INSERT INTO agents ({_AGENT_COLS})
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                agent.id,
                agent.role.value,
                agent.state.value,
                agent.parent_id,
                agent.cluster_id,
                agent.name,
                agent.created_at,
                agent.updated_at,
                json.dumps(agent.context),
                json.dumps(agent.error) if agent.error is not None else None,
            ),
        )
        self._conn.execute(
            """
            INSERT INTO agent_state_history (agent_id, from_state, to_state, reason, timestamp)
            VALUES (?, ?, ?, ?, ?)
            """,
            (agent.id, None, agent.state.value, "created", agent.created_at),
        )
        self._conn.commit()
        return agent

    def get(self, agent_id: str) -> Optional[Agent]:
        """Return the agent with *agent_id*, or None if not found."""
        row = self._conn.execute(
            f"SELECT {_AGENT_COLS} FROM agents WHERE id = ?",
            (agent_id,),
        ).fetchone()
        return _row_to_agent(row) if row is not None else None

    def transition(
        self,
        agent_id: str,
        to_state: AgentState,
        reason: str = "",
        error: Optional[dict] = None,
    ) -> Agent:
        """Atomically validate and apply a state transition.

        1. Loads current state inside a transaction.
        2. Validates against VALID_TRANSITIONS.
        3. Writes new state to *agents* and appends a row to *agent_state_history*.
        4. Emits an OTel span event on the current span (no-op when none is active).

        Raises InvalidStateTransitionError if the transition is not allowed.
        Raises ValueError if *agent_id* does not exist.
        """
        now = time.time()

        with self._conn:
            row = self._conn.execute(
                f"SELECT {_AGENT_COLS} FROM agents WHERE id = ?",
                (agent_id,),
            ).fetchone()
            if row is None:
                raise ValueError(f"Agent {agent_id!r} not found")

            agent = _row_to_agent(row)
            from_state = agent.state

            if to_state not in VALID_TRANSITIONS[from_state]:
                raise InvalidStateTransitionError(
                    f"Cannot transition {agent_id!r} from {from_state.value!r} "
                    f"to {to_state.value!r}"
                )

            error_json = json.dumps(error) if error is not None else None
            self._conn.execute(
                "UPDATE agents SET state = ?, updated_at = ?, error = ? WHERE id = ?",
                (to_state.value, now, error_json, agent_id),
            )
            self._conn.execute(
                """
                INSERT INTO agent_state_history
                    (agent_id, from_state, to_state, reason, timestamp)
                VALUES (?, ?, ?, ?, ?)
                """,
                (agent_id, from_state.value, to_state.value, reason, now),
            )

        # Emit OTel span event — no-op when no span is active (e.g. in tests)
        trace.get_current_span().add_event(
            "state_transition",
            attributes={
                "agent.id": agent_id,
                "agent.role": agent.role.value,
                "agent.cluster_id": agent.cluster_id,
                "agent.state.from": from_state.value,
                "agent.state.to": to_state.value,
                "agent.transition.reason": reason,
            },
        )

        agent.state = to_state
        agent.updated_at = now
        if error is not None:
            agent.error = error
        return agent

    def list_by_cluster(self, cluster_id: str) -> list[Agent]:
        """Return all agents belonging to *cluster_id*."""
        rows = self._conn.execute(
            f"SELECT {_AGENT_COLS} FROM agents WHERE cluster_id = ?",
            (cluster_id,),
        ).fetchall()
        return [_row_to_agent(r) for r in rows]

    def list_by_state(self, state: AgentState) -> list[Agent]:
        """Return all agents currently in *state*."""
        rows = self._conn.execute(
            f"SELECT {_AGENT_COLS} FROM agents WHERE state = ?",
            (state.value,),
        ).fetchall()
        return [_row_to_agent(r) for r in rows]
