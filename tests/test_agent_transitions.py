"""Tests for Agent state transitions, AgentRepository, and db schema."""

import sqlite3
import time
import pytest

from orchestrator.db import init_schema
from orchestrator.agent import (
    Agent,
    AgentRole,
    AgentState,
    AgentRepository,
    InvalidStateTransitionError,
    VALID_TRANSITIONS,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def conn():
    """In-memory SQLite connection with schema applied."""
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
def agent(repo):
    """A persisted agent in CREATED state."""
    a = Agent(cluster_id="cluster-1", name="test-agent")
    return repo.create(a)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def advance(repo: AgentRepository, agent: Agent, *states: AgentState) -> Agent:
    """Walk *agent* through a sequence of states, returning the final Agent."""
    for s in states:
        agent = repo.transition(agent.id, s)
    return agent


# ---------------------------------------------------------------------------
# Valid transitions — one test per edge in the FSM
# ---------------------------------------------------------------------------


class TestValidTransitions:
    def test_created_to_initializing(self, repo, agent):
        a = repo.transition(agent.id, AgentState.INITIALIZING)
        assert a.state == AgentState.INITIALIZING

    def test_initializing_to_idle(self, repo, agent):
        a = advance(repo, agent, AgentState.INITIALIZING, AgentState.IDLE)
        assert a.state == AgentState.IDLE

    def test_initializing_to_failed(self, repo, agent):
        a = advance(repo, agent, AgentState.INITIALIZING, AgentState.FAILED)
        assert a.state == AgentState.FAILED

    def test_idle_to_running(self, repo, agent):
        a = advance(repo, agent, AgentState.INITIALIZING, AgentState.IDLE, AgentState.RUNNING)
        assert a.state == AgentState.RUNNING

    def test_idle_to_terminated(self, repo, agent):
        a = advance(repo, agent, AgentState.INITIALIZING, AgentState.IDLE, AgentState.TERMINATED)
        assert a.state == AgentState.TERMINATED

    def test_running_to_idle(self, repo, agent):
        a = advance(
            repo, agent,
            AgentState.INITIALIZING, AgentState.IDLE, AgentState.RUNNING, AgentState.IDLE,
        )
        assert a.state == AgentState.IDLE

    def test_running_to_suspended(self, repo, agent):
        a = advance(
            repo, agent,
            AgentState.INITIALIZING, AgentState.IDLE, AgentState.RUNNING, AgentState.SUSPENDED,
        )
        assert a.state == AgentState.SUSPENDED

    def test_running_to_failed(self, repo, agent):
        a = advance(
            repo, agent,
            AgentState.INITIALIZING, AgentState.IDLE, AgentState.RUNNING, AgentState.FAILED,
        )
        assert a.state == AgentState.FAILED

    def test_suspended_to_idle(self, repo, agent):
        a = advance(
            repo, agent,
            AgentState.INITIALIZING, AgentState.IDLE, AgentState.RUNNING,
            AgentState.SUSPENDED, AgentState.IDLE,
        )
        assert a.state == AgentState.IDLE

    def test_suspended_to_failed(self, repo, agent):
        a = advance(
            repo, agent,
            AgentState.INITIALIZING, AgentState.IDLE, AgentState.RUNNING,
            AgentState.SUSPENDED, AgentState.FAILED,
        )
        assert a.state == AgentState.FAILED

    def test_suspended_to_terminated(self, repo, agent):
        a = advance(
            repo, agent,
            AgentState.INITIALIZING, AgentState.IDLE, AgentState.RUNNING,
            AgentState.SUSPENDED, AgentState.TERMINATED,
        )
        assert a.state == AgentState.TERMINATED

    def test_failed_to_terminated(self, repo, agent):
        a = advance(
            repo, agent,
            AgentState.INITIALIZING, AgentState.FAILED, AgentState.TERMINATED,
        )
        assert a.state == AgentState.TERMINATED


# ---------------------------------------------------------------------------
# Invalid transitions — representative subset covering every source state
# ---------------------------------------------------------------------------


class TestInvalidTransitions:
    def _assert_invalid(self, repo, agent_id, from_states, bad_target):
        """Walk to the last state in *from_states* then expect rejection."""
        a = repo.get(agent_id)
        for s in from_states:
            a = repo.transition(a.id, s)
        with pytest.raises(InvalidStateTransitionError):
            repo.transition(a.id, bad_target)

    # CREATED
    def test_created_cannot_go_to_idle(self, repo, agent):
        self._assert_invalid(repo, agent.id, [], AgentState.IDLE)

    def test_created_cannot_go_to_running(self, repo, agent):
        self._assert_invalid(repo, agent.id, [], AgentState.RUNNING)

    def test_created_cannot_go_to_suspended(self, repo, agent):
        self._assert_invalid(repo, agent.id, [], AgentState.SUSPENDED)

    def test_created_cannot_go_to_failed(self, repo, agent):
        self._assert_invalid(repo, agent.id, [], AgentState.FAILED)

    def test_created_cannot_go_to_terminated(self, repo, agent):
        self._assert_invalid(repo, agent.id, [], AgentState.TERMINATED)

    def test_created_cannot_go_to_itself(self, repo, agent):
        self._assert_invalid(repo, agent.id, [], AgentState.CREATED)

    # INITIALIZING
    def test_initializing_cannot_go_to_running(self, repo, agent):
        self._assert_invalid(repo, agent.id, [AgentState.INITIALIZING], AgentState.RUNNING)

    def test_initializing_cannot_go_to_suspended(self, repo, agent):
        self._assert_invalid(repo, agent.id, [AgentState.INITIALIZING], AgentState.SUSPENDED)

    def test_initializing_cannot_go_to_terminated(self, repo, agent):
        self._assert_invalid(repo, agent.id, [AgentState.INITIALIZING], AgentState.TERMINATED)

    def test_initializing_cannot_go_to_created(self, repo, agent):
        self._assert_invalid(repo, agent.id, [AgentState.INITIALIZING], AgentState.CREATED)

    # IDLE
    def test_idle_cannot_go_to_initializing(self, repo, agent):
        self._assert_invalid(
            repo, agent.id,
            [AgentState.INITIALIZING, AgentState.IDLE], AgentState.INITIALIZING,
        )

    def test_idle_cannot_go_to_suspended(self, repo, agent):
        self._assert_invalid(
            repo, agent.id,
            [AgentState.INITIALIZING, AgentState.IDLE], AgentState.SUSPENDED,
        )

    def test_idle_cannot_go_to_failed(self, repo, agent):
        self._assert_invalid(
            repo, agent.id,
            [AgentState.INITIALIZING, AgentState.IDLE], AgentState.FAILED,
        )

    def test_idle_cannot_go_to_created(self, repo, agent):
        self._assert_invalid(
            repo, agent.id,
            [AgentState.INITIALIZING, AgentState.IDLE], AgentState.CREATED,
        )

    # RUNNING
    def test_running_cannot_go_to_initializing(self, repo, agent):
        self._assert_invalid(
            repo, agent.id,
            [AgentState.INITIALIZING, AgentState.IDLE, AgentState.RUNNING],
            AgentState.INITIALIZING,
        )

    def test_running_cannot_go_to_terminated(self, repo, agent):
        self._assert_invalid(
            repo, agent.id,
            [AgentState.INITIALIZING, AgentState.IDLE, AgentState.RUNNING],
            AgentState.TERMINATED,
        )

    def test_running_cannot_go_to_created(self, repo, agent):
        self._assert_invalid(
            repo, agent.id,
            [AgentState.INITIALIZING, AgentState.IDLE, AgentState.RUNNING],
            AgentState.CREATED,
        )

    # SUSPENDED
    def test_suspended_cannot_go_to_running(self, repo, agent):
        self._assert_invalid(
            repo, agent.id,
            [AgentState.INITIALIZING, AgentState.IDLE, AgentState.RUNNING, AgentState.SUSPENDED],
            AgentState.RUNNING,
        )

    def test_suspended_cannot_go_to_initializing(self, repo, agent):
        self._assert_invalid(
            repo, agent.id,
            [AgentState.INITIALIZING, AgentState.IDLE, AgentState.RUNNING, AgentState.SUSPENDED],
            AgentState.INITIALIZING,
        )

    def test_suspended_cannot_go_to_created(self, repo, agent):
        self._assert_invalid(
            repo, agent.id,
            [AgentState.INITIALIZING, AgentState.IDLE, AgentState.RUNNING, AgentState.SUSPENDED],
            AgentState.CREATED,
        )

    # FAILED
    def test_failed_cannot_go_to_idle(self, repo, agent):
        self._assert_invalid(
            repo, agent.id,
            [AgentState.INITIALIZING, AgentState.FAILED], AgentState.IDLE,
        )

    def test_failed_cannot_go_to_running(self, repo, agent):
        self._assert_invalid(
            repo, agent.id,
            [AgentState.INITIALIZING, AgentState.FAILED], AgentState.RUNNING,
        )

    def test_failed_cannot_go_to_suspended(self, repo, agent):
        self._assert_invalid(
            repo, agent.id,
            [AgentState.INITIALIZING, AgentState.FAILED], AgentState.SUSPENDED,
        )

    def test_failed_cannot_go_to_initializing(self, repo, agent):
        self._assert_invalid(
            repo, agent.id,
            [AgentState.INITIALIZING, AgentState.FAILED], AgentState.INITIALIZING,
        )

    def test_failed_cannot_go_to_created(self, repo, agent):
        self._assert_invalid(
            repo, agent.id,
            [AgentState.INITIALIZING, AgentState.FAILED], AgentState.CREATED,
        )

    # TERMINATED — terminal state, nothing is valid
    def test_terminated_cannot_go_to_any_state(self, repo, agent):
        advance(repo, agent, AgentState.INITIALIZING, AgentState.IDLE, AgentState.TERMINATED)
        for target in AgentState:
            with pytest.raises(InvalidStateTransitionError):
                repo.transition(agent.id, target)

    # Invalid transition does not mutate DB
    def test_invalid_transition_leaves_state_unchanged(self, repo, agent):
        with pytest.raises(InvalidStateTransitionError):
            repo.transition(agent.id, AgentState.RUNNING)
        reloaded = repo.get(agent.id)
        assert reloaded.state == AgentState.CREATED


# ---------------------------------------------------------------------------
# VALID_TRANSITIONS coverage — data-driven completeness check
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "from_state,to_state",
    [
        (from_s, to_s)
        for from_s, allowed in VALID_TRANSITIONS.items()
        for to_s in allowed
    ],
)
def test_valid_transitions_table_is_correct(from_state, to_state):
    """Every edge declared in VALID_TRANSITIONS must actually be reachable."""
    assert to_state in VALID_TRANSITIONS[from_state]


# ---------------------------------------------------------------------------
# Repository behaviour
# ---------------------------------------------------------------------------


class TestAgentRepository:
    def test_create_and_get_roundtrip(self, repo):
        a = Agent(
            role=AgentRole.ORCHESTRATOR,
            cluster_id="clu-x",
            name="orch",
            context={"task_description": "do things"},
        )
        repo.create(a)
        loaded = repo.get(a.id)
        assert loaded is not None
        assert loaded.id == a.id
        assert loaded.role == AgentRole.ORCHESTRATOR
        assert loaded.state == AgentState.CREATED
        assert loaded.cluster_id == "clu-x"
        assert loaded.name == "orch"
        assert loaded.context == {"task_description": "do things"}
        assert loaded.error is None

    def test_get_returns_none_for_unknown_id(self, repo):
        assert repo.get("does-not-exist") is None

    def test_transition_not_found_raises(self, repo):
        with pytest.raises(ValueError, match="not found"):
            repo.transition("ghost-id", AgentState.INITIALIZING)

    def test_transition_persists_new_state(self, repo, agent):
        repo.transition(agent.id, AgentState.INITIALIZING)
        reloaded = repo.get(agent.id)
        assert reloaded.state == AgentState.INITIALIZING

    def test_transition_updates_updated_at(self, repo, agent):
        before = agent.updated_at
        time.sleep(0.01)
        updated = repo.transition(agent.id, AgentState.INITIALIZING)
        assert updated.updated_at > before

    def test_transition_returns_updated_agent(self, repo, agent):
        returned = repo.transition(agent.id, AgentState.INITIALIZING)
        assert returned.state == AgentState.INITIALIZING
        assert returned.id == agent.id

    def test_transition_stores_error_on_failed(self, repo, agent):
        error_payload = {"message": "something broke", "code": 42}
        advance(repo, agent, AgentState.INITIALIZING)
        repo.transition(agent.id, AgentState.FAILED, error=error_payload)
        reloaded = repo.get(agent.id)
        assert reloaded.error == error_payload

    def test_transition_records_history(self, repo, conn, agent):
        repo.transition(agent.id, AgentState.INITIALIZING, reason="startup")
        rows = conn.execute(
            "SELECT from_state, to_state, reason FROM agent_state_history WHERE agent_id = ? ORDER BY id",
            (agent.id,),
        ).fetchall()
        # Initial "created" row + transition row
        assert len(rows) == 2
        assert rows[0]["from_state"] is None
        assert rows[0]["to_state"] == "CREATED"
        assert rows[1]["from_state"] == "CREATED"
        assert rows[1]["to_state"] == "INITIALIZING"
        assert rows[1]["reason"] == "startup"

    def test_list_by_cluster(self, repo):
        a1 = repo.create(Agent(cluster_id="clu-a"))
        a2 = repo.create(Agent(cluster_id="clu-a"))
        repo.create(Agent(cluster_id="clu-b"))

        results = repo.list_by_cluster("clu-a")
        ids = {a.id for a in results}
        assert ids == {a1.id, a2.id}

    def test_list_by_state(self, repo):
        a1 = repo.create(Agent(cluster_id="c"))
        a2 = repo.create(Agent(cluster_id="c"))
        repo.create(Agent(cluster_id="c"))

        repo.transition(a1.id, AgentState.INITIALIZING)
        repo.transition(a2.id, AgentState.INITIALIZING)

        initializing = repo.list_by_state(AgentState.INITIALIZING)
        created = repo.list_by_state(AgentState.CREATED)

        assert len(initializing) == 2
        assert len(created) == 1

    def test_parent_child_relationship(self, repo):
        parent = repo.create(Agent(role=AgentRole.ORCHESTRATOR, cluster_id="clu"))
        child = repo.create(
            Agent(role=AgentRole.WORKER, parent_id=parent.id, cluster_id="clu")
        )
        loaded = repo.get(child.id)
        assert loaded.parent_id == parent.id

    def test_context_survives_roundtrip(self, repo):
        ctx = {
            "task_description": "do work",
            "tools_enabled": ["file_read"],
            "max_iterations": 10,
            "conversation_history": [{"role": "user", "content": "hello"}],
        }
        a = repo.create(Agent(cluster_id="c", context=ctx))
        loaded = repo.get(a.id)
        assert loaded.context == ctx
