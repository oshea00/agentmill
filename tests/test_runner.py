"""Tests for ClaudeCodeRunner, MessagesAPIRunner, and run_worker.

All async tests use anyio pinned to the asyncio backend, consistent with
the rest of the test suite.
"""

from __future__ import annotations

import asyncio
import sqlite3
from typing import Any
from unittest.mock import AsyncMock, MagicMock, Mock, patch

import pytest

from orchestrator.agent import Agent, AgentRepository, AgentRole, AgentState
from orchestrator.bus import Message, MessageBus, MessageType
from orchestrator.db import init_schema
from orchestrator.registry import BudgetExceededError, ToolDefinition, ToolRegistry
from orchestrator.runner import (
    ClaudeCodeResult,
    ClaudeCodeRunner,
    ClaudeCodeTask,
    MessagesAPIRunner,
    _DEFAULT_MAX_ITERATIONS,
    _DEFAULT_MAX_TOOL_CALLS,
    run_worker,
)


@pytest.fixture
def anyio_backend():
    return "asyncio"


# ---------------------------------------------------------------------------
# Shared fixtures
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


@pytest.fixture()
def tracer():
    from opentelemetry import trace
    from orchestrator.telemetry import init_tracing
    return trace.get_tracer("test")


@pytest.fixture()
def logger():
    from orchestrator.telemetry import get_logger
    return get_logger("test/runner")


def make_idle_agent(
    repo: AgentRepository,
    *,
    cluster_id: str = "cluster-1",
    parent_id: str | None = None,
    execution_mode: str = "messages_api",
    extra_context: dict | None = None,
) -> Agent:
    """Create an agent in IDLE state ready to receive tasks."""
    context: dict = {"execution_mode": execution_mode}
    if extra_context:
        context.update(extra_context)
    a = Agent(
        role=AgentRole.WORKER,
        cluster_id=cluster_id,
        parent_id=parent_id,
        context=context,
    )
    repo.create(a)
    repo.transition(a.id, AgentState.INITIALIZING)
    repo.transition(a.id, AgentState.IDLE)
    a = repo.get(a.id)
    assert a is not None
    return a


def make_orchestrator(repo: AgentRepository, cluster_id: str = "cluster-1") -> Agent:
    o = Agent(role=AgentRole.ORCHESTRATOR, cluster_id=cluster_id)
    repo.create(o)
    repo.transition(o.id, AgentState.INITIALIZING)
    repo.transition(o.id, AgentState.IDLE)
    o = repo.get(o.id)
    assert o is not None
    return o


def task_assign_msg(
    sender: Agent,
    recipient: Agent,
    *,
    description: str = "do the thing",
    task_id: str = "task-001",
) -> Message:
    return Message(
        type=MessageType.TASK_ASSIGN,
        sender_id=sender.id,
        recipient_id=recipient.id,
        cluster_id=sender.cluster_id,
        payload={
            "task_id": task_id,
            "description": description,
        },
    )


def control_msg(
    sender: Agent,
    recipient: Agent,
    *,
    command: str,
    reason: str = "",
) -> Message:
    return Message(
        type=MessageType.CONTROL,
        sender_id=sender.id,
        recipient_id=recipient.id,
        cluster_id=sender.cluster_id,
        payload={"command": command, "reason": reason},
    )


# ---------------------------------------------------------------------------
# Helpers: mock subprocess process
# ---------------------------------------------------------------------------


def _make_proc(*, returncode: int, stdout: bytes = b"", stderr: bytes = b"") -> Mock:
    """Return a mock asyncio subprocess with pre-set communicate() output."""
    proc = Mock()
    proc.returncode = returncode
    proc.communicate = AsyncMock(return_value=(stdout, stderr))
    proc.kill = Mock()
    proc.wait = AsyncMock(return_value=returncode)
    return proc


# ---------------------------------------------------------------------------
# Helpers: mock Anthropic response
# ---------------------------------------------------------------------------


def _text_block(text: str) -> Mock:
    b = Mock()
    b.type = "text"
    b.text = text
    return b


def _tool_use_block(
    name: str, input: dict, *, tool_id: str = "tool_abc123"
) -> Mock:
    b = Mock()
    b.type = "tool_use"
    b.id = tool_id
    b.name = name
    b.input = input
    return b


def _api_response(stop_reason: str, *content_blocks) -> Mock:
    resp = Mock()
    resp.stop_reason = stop_reason
    resp.content = list(content_blocks)
    return resp


# ---------------------------------------------------------------------------
# ClaudeCodeRunner — subprocess fallback
# ---------------------------------------------------------------------------


class TestClaudeCodeRunnerSubprocess:
    """Tests for _run_subprocess_fallback (the primary path without SDK)."""

    def _runner(self, tracer, logger) -> ClaudeCodeRunner:
        return ClaudeCodeRunner("agent-1", tracer, logger)

    def _task(self, **kwargs) -> ClaudeCodeTask:
        defaults = dict(
            prompt="write a hello world",
            working_directory="/tmp",
            allowed_paths=["/tmp"],
            mcp_servers=[],
            max_turns=5,
            timeout_seconds=10.0,
        )
        defaults.update(kwargs)
        return ClaudeCodeTask(**defaults)

    @pytest.mark.anyio
    async def test_subprocess_success(self, tracer, logger):
        proc = _make_proc(returncode=0, stdout=b"Hello, world!\n")
        runner = self._runner(tracer, logger)

        with patch("asyncio.create_subprocess_exec", AsyncMock(return_value=proc)):
            result = await runner._run_subprocess_fallback(self._task())

        assert result.success is True
        assert result.final_response == "Hello, world!\n"
        assert result.error is None
        assert result.files_modified == []
        assert result.turns_used == 0

    @pytest.mark.anyio
    async def test_subprocess_failure(self, tracer, logger):
        proc = _make_proc(returncode=1, stderr=b"Something went wrong\n")
        runner = self._runner(tracer, logger)

        with patch("asyncio.create_subprocess_exec", AsyncMock(return_value=proc)):
            result = await runner._run_subprocess_fallback(self._task())

        assert result.success is False
        assert "Something went wrong" in result.error
        assert result.final_response == ""

    @pytest.mark.anyio
    async def test_subprocess_timeout(self, tracer, logger):
        proc = Mock()
        proc.communicate = AsyncMock(side_effect=asyncio.TimeoutError())
        runner = self._runner(tracer, logger)

        with patch("asyncio.create_subprocess_exec", AsyncMock(return_value=proc)):
            result = await runner._run_subprocess_fallback(
                self._task(timeout_seconds=0.001)
            )

        assert result.success is False
        assert result.error == "timeout"

    @pytest.mark.anyio
    async def test_subprocess_oserror(self, tracer, logger):
        runner = self._runner(tracer, logger)

        with patch(
            "asyncio.create_subprocess_exec",
            AsyncMock(side_effect=OSError("claude not found")),
        ):
            result = await runner._run_subprocess_fallback(self._task())

        assert result.success is False
        assert "claude not found" in result.error

    @pytest.mark.anyio
    async def test_run_dispatches_to_subprocess_when_no_sdk(self, tracer, logger):
        """run() uses subprocess when _SDK_AVAILABLE is False (the default here)."""
        proc = _make_proc(returncode=0, stdout=b"done")
        runner = self._runner(tracer, logger)

        with patch("orchestrator.runner._SDK_AVAILABLE", False), \
             patch("asyncio.create_subprocess_exec", AsyncMock(return_value=proc)):
            result = await runner.run(self._task())

        assert result.success is True
        assert result.final_response == "done"

    @pytest.mark.anyio
    async def test_resolve_mcp_servers_empty_list(self, tracer, logger):
        runner = self._runner(tracer, logger)
        assert runner._resolve_mcp_servers([]) == []

    @pytest.mark.anyio
    async def test_resolve_mcp_servers_filters_unknown(self, tracer, logger, tmp_path):
        registry_json = tmp_path / "mcp_servers.json"
        registry_json.write_text(
            '{"myserver": {"command": ["npx", "myserver"], "args": [], "env": {}}}'
        )
        runner = ClaudeCodeRunner(
            "agent-1", tracer, logger, mcp_registry_path=str(registry_json)
        )
        result = runner._resolve_mcp_servers(["myserver", "nonexistent"])
        assert len(result) == 1
        assert result[0]["name"] == "myserver"

    @pytest.mark.anyio
    async def test_resolve_mcp_servers_missing_json(self, tracer, logger):
        runner = ClaudeCodeRunner(
            "agent-1", tracer, logger, mcp_registry_path="/nonexistent/path.json"
        )
        assert runner._resolve_mcp_servers(["any"]) == []

    @pytest.mark.anyio
    async def test_stream_subprocess_yields_final_event(self, tracer, logger):
        proc = _make_proc(returncode=0, stdout=b"streamed result")
        runner = self._runner(tracer, logger)
        events = []

        with patch("orchestrator.runner._SDK_AVAILABLE", False), \
             patch("asyncio.create_subprocess_exec", AsyncMock(return_value=proc)):
            async for event in runner.stream(self._task()):
                events.append(event)

        assert len(events) == 1
        assert events[0].type == "final"
        assert events[0].data["response"] == "streamed result"


# ---------------------------------------------------------------------------
# MessagesAPIRunner
# ---------------------------------------------------------------------------


class TestMessagesAPIRunner:

    def _make_registry(self) -> ToolRegistry:
        return ToolRegistry("agent-test")

    def _make_registry_with_tool(
        self, tool_name: str, return_value: Any = "tool output"
    ) -> ToolRegistry:
        registry = ToolRegistry("agent-test")
        handler = AsyncMock(return_value=return_value)
        registry.register_native(
            ToolDefinition(
                name=tool_name,
                description=f"Test tool: {tool_name}",
                input_schema={"type": "object", "properties": {}},
                handler=handler,
            )
        )
        return registry

    def _runner(self, tracer, logger, registry=None) -> MessagesAPIRunner:
        client = AsyncMock()
        if registry is None:
            registry = self._make_registry()
        return MessagesAPIRunner("agent-1", client, registry, tracer, logger)

    @pytest.mark.anyio
    async def test_simple_text_response_no_tools(self, tracer, logger):
        runner = self._runner(tracer, logger)
        runner._client.messages.create = AsyncMock(
            return_value=_api_response("end_turn", _text_block("final answer"))
        )

        text, _, stats = await runner.run_turn(
            [{"role": "user", "content": "hello"}]
        )

        assert text == "final answer"
        assert stats["iterations_used"] == 1
        assert stats["tool_calls_made"] == 0

    @pytest.mark.anyio
    async def test_tool_use_single_round(self, tracer, logger):
        """One tool call followed by a text response."""
        registry = self._make_registry_with_tool("my_tool", return_value="42")
        runner = self._runner(tracer, logger, registry)

        runner._client.messages.create = AsyncMock(
            side_effect=[
                _api_response(
                    "tool_use",
                    _tool_use_block("my_tool", {"x": 1}, tool_id="tid1"),
                ),
                _api_response("end_turn", _text_block("the answer is 42")),
            ]
        )

        text, messages, stats = await runner.run_turn(
            [{"role": "user", "content": "what is the answer?"}]
        )

        assert text == "the answer is 42"
        assert stats["iterations_used"] == 2
        assert stats["tool_calls_made"] == 1
        # Messages should include the tool result turn
        role_sequence = [m["role"] for m in messages if isinstance(m, dict)]
        assert "user" in role_sequence

    @pytest.mark.anyio
    async def test_tool_error_returned_to_claude(self, tracer, logger):
        """A tool that raises an exception returns is_error=True to Claude."""
        registry = ToolRegistry("agent-test")
        handler = AsyncMock(side_effect=ValueError("file not found"))
        registry.register_native(
            ToolDefinition(
                name="bad_tool",
                description="always fails",
                input_schema={"type": "object", "properties": {}},
                handler=handler,
            )
        )
        runner = self._runner(tracer, logger, registry)

        captured_call_kwargs: list[dict] = []

        async def _create(**kwargs):
            captured_call_kwargs.append(kwargs)
            call_count = len(captured_call_kwargs)
            if call_count == 1:
                return _api_response(
                    "tool_use",
                    _tool_use_block("bad_tool", {}, tool_id="tid1"),
                )
            # Second call: Claude receives the error result
            tool_results = kwargs["messages"][-1]["content"]
            assert tool_results[0]["is_error"] is True
            assert "file not found" in tool_results[0]["content"]
            return _api_response("end_turn", _text_block("I got an error"))

        runner._client.messages.create = _create

        text, _, stats = await runner.run_turn(
            [{"role": "user", "content": "try it"}]
        )
        assert text == "I got an error"
        assert stats["tool_calls_made"] == 1

    @pytest.mark.anyio
    async def test_max_iterations_raises_budget_exceeded(self, tracer, logger):
        # Register a real tool so ToolNotFoundError is not raised; the loop
        # should hit max_iterations before max_tool_calls.
        registry = self._make_registry_with_tool("echo", return_value="ok")
        runner = self._runner(tracer, logger, registry)
        runner._client.messages.create = AsyncMock(
            return_value=_api_response(
                "tool_use",
                _tool_use_block("echo", {"msg": "hi"}, tool_id="t1"),
            )
        )

        with pytest.raises(BudgetExceededError, match="max_iterations"):
            await runner.run_turn(
                [{"role": "user", "content": "loop forever"}],
                max_iterations=2,
                max_tool_calls=999,
            )

    @pytest.mark.anyio
    async def test_max_tool_calls_raises_budget_exceeded(self, tracer, logger):
        registry = self._make_registry_with_tool("cheap_tool")
        runner = self._runner(tracer, logger, registry)

        # Returns tool_use on first call, end_turn on second
        runner._client.messages.create = AsyncMock(
            side_effect=[
                _api_response(
                    "tool_use",
                    _tool_use_block("cheap_tool", {}, tool_id="t1"),
                    _tool_use_block("cheap_tool", {}, tool_id="t2"),
                ),
                _api_response("end_turn", _text_block("done")),
            ]
        )

        with pytest.raises(BudgetExceededError, match="max_tool_calls"):
            await runner.run_turn(
                [{"role": "user", "content": "go"}],
                max_tool_calls=1,
                max_iterations=10,
            )

    @pytest.mark.anyio
    async def test_system_prompt_included_when_set(self, tracer, logger):
        runner = self._runner(tracer, logger)
        create_mock = AsyncMock(
            return_value=_api_response("end_turn", _text_block("ok"))
        )
        runner._client.messages.create = create_mock

        await runner.run_turn(
            [{"role": "user", "content": "hi"}],
            system="You are a helpful assistant.",
        )

        call_kwargs = create_mock.call_args.kwargs
        assert call_kwargs["system"] == "You are a helpful assistant."

    @pytest.mark.anyio
    async def test_no_tools_omits_tools_param(self, tracer, logger):
        """When registry is empty, 'tools' should not be sent to the API."""
        runner = self._runner(tracer, logger)
        create_mock = AsyncMock(
            return_value=_api_response("end_turn", _text_block("ok"))
        )
        runner._client.messages.create = create_mock

        await runner.run_turn([{"role": "user", "content": "hi"}])

        call_kwargs = create_mock.call_args.kwargs
        assert "tools" not in call_kwargs


# ---------------------------------------------------------------------------
# run_worker
# ---------------------------------------------------------------------------


class TestRunWorker:

    @pytest.mark.anyio
    async def test_receive_timeout_returns_to_idle(
        self, repo, bus, tracer, logger
    ):
        orch = make_orchestrator(repo)
        worker = make_idle_agent(repo, parent_id=orch.id)

        await run_worker(
            worker, bus, repo, tracer, logger,
            api_runner=_noop_api_runner(tracer, logger),
            receive_timeout=0.05,
        )

        refreshed = repo.get(worker.id)
        assert refreshed.state == AgentState.IDLE

    @pytest.mark.anyio
    async def test_control_terminate(self, repo, bus, tracer, logger):
        orch = make_orchestrator(repo)
        worker = make_idle_agent(repo, parent_id=orch.id)

        await bus.send(control_msg(orch, worker, command="TERMINATE", reason="done"))

        await run_worker(
            worker, bus, repo, tracer, logger,
            receive_timeout=1.0,
        )

        refreshed = repo.get(worker.id)
        assert refreshed.state == AgentState.TERMINATED

    @pytest.mark.anyio
    async def test_control_suspend(self, repo, bus, tracer, logger):
        orch = make_orchestrator(repo)
        worker = make_idle_agent(repo, parent_id=orch.id)

        await bus.send(control_msg(orch, worker, command="SUSPEND"))

        await run_worker(
            worker, bus, repo, tracer, logger,
            receive_timeout=1.0,
        )

        refreshed = repo.get(worker.id)
        assert refreshed.state == AgentState.SUSPENDED

    @pytest.mark.anyio
    async def test_unexpected_message_type_returns_to_idle(
        self, repo, bus, tracer, logger
    ):
        orch = make_orchestrator(repo)
        worker = make_idle_agent(repo, parent_id=orch.id)

        await bus.send(Message(
            type=MessageType.HEARTBEAT,
            sender_id=orch.id,
            recipient_id=worker.id,
            cluster_id=orch.cluster_id,
            payload={},
        ))

        await run_worker(
            worker, bus, repo, tracer, logger,
            api_runner=_noop_api_runner(tracer, logger),
            receive_timeout=1.0,
        )

        refreshed = repo.get(worker.id)
        assert refreshed.state == AgentState.IDLE

    @pytest.mark.anyio
    async def test_cc_task_success_sends_result_and_idles(
        self, repo, bus, tracer, logger
    ):
        orch = make_orchestrator(repo)
        worker = make_idle_agent(
            repo,
            parent_id=orch.id,
            execution_mode="claude_code",
            extra_context={"working_directory": "/tmp"},
        )

        await bus.send(task_assign_msg(orch, worker, description="write tests"))

        cc_runner = _mock_cc_runner(
            tracer, logger,
            result=ClaudeCodeResult(
                success=True,
                final_response="tests written",
                files_modified=["test_foo.py"],
                turns_used=3,
            ),
        )

        await run_worker(
            worker, bus, repo, tracer, logger,
            cc_runner=cc_runner,
            receive_timeout=1.0,
        )

        refreshed = repo.get(worker.id)
        assert refreshed.state == AgentState.IDLE

        # Orchestrator should have received a TASK_RESULT
        result_msg = await bus.receive(orch.id, timeout=0.5)
        assert result_msg is not None
        assert result_msg.type == MessageType.TASK_RESULT
        assert result_msg.payload["output"] == "tests written"
        assert result_msg.payload["files_modified"] == ["test_foo.py"]
        assert result_msg.payload["turns_used"] == 3

    @pytest.mark.anyio
    async def test_cc_task_failure_sends_error_and_fails(
        self, repo, bus, tracer, logger
    ):
        orch = make_orchestrator(repo)
        worker = make_idle_agent(
            repo, parent_id=orch.id, execution_mode="claude_code"
        )

        await bus.send(task_assign_msg(orch, worker, description="do something"))

        cc_runner = _mock_cc_runner(
            tracer, logger,
            result=ClaudeCodeResult(
                success=False,
                final_response="",
                files_modified=[],
                turns_used=0,
                error="compilation failed",
            ),
        )

        await run_worker(
            worker, bus, repo, tracer, logger,
            cc_runner=cc_runner,
            receive_timeout=1.0,
        )

        refreshed = repo.get(worker.id)
        assert refreshed.state == AgentState.FAILED

        error_msg = await bus.receive(orch.id, timeout=0.5)
        assert error_msg is not None
        assert error_msg.type == MessageType.TASK_ERROR
        assert error_msg.payload["error_type"] == "ClaudeCodeFailed"
        assert "compilation failed" in error_msg.payload["message"]

    @pytest.mark.anyio
    async def test_api_task_success_sends_result_and_idles(
        self, repo, bus, tracer, logger
    ):
        orch = make_orchestrator(repo)
        worker = make_idle_agent(
            repo, parent_id=orch.id, execution_mode="messages_api"
        )

        await bus.send(task_assign_msg(
            orch, worker,
            description="summarise the report",
            task_id="task-42",
        ))

        api_runner = _mock_api_runner(
            tracer, logger,
            final_text="The report says Q3 was good.",
            stats={"iterations_used": 2, "tool_calls_made": 1},
        )

        await run_worker(
            worker, bus, repo, tracer, logger,
            api_runner=api_runner,
            receive_timeout=1.0,
        )

        refreshed = repo.get(worker.id)
        assert refreshed.state == AgentState.IDLE

        result_msg = await bus.receive(orch.id, timeout=0.5)
        assert result_msg is not None
        assert result_msg.type == MessageType.TASK_RESULT
        assert result_msg.payload["task_id"] == "task-42"
        assert result_msg.payload["output"] == "The report says Q3 was good."
        assert result_msg.payload["iterations_used"] == 2

    @pytest.mark.anyio
    async def test_api_task_budget_exceeded_sends_error_and_fails(
        self, repo, bus, tracer, logger
    ):
        orch = make_orchestrator(repo)
        worker = make_idle_agent(
            repo, parent_id=orch.id, execution_mode="messages_api"
        )

        await bus.send(task_assign_msg(orch, worker, description="loop forever"))

        api_runner = _mock_api_runner_raises(
            tracer, logger,
            exc=BudgetExceededError("max_tool_calls=0 exceeded"),
        )

        await run_worker(
            worker, bus, repo, tracer, logger,
            api_runner=api_runner,
            receive_timeout=1.0,
        )

        refreshed = repo.get(worker.id)
        assert refreshed.state == AgentState.FAILED

        error_msg = await bus.receive(orch.id, timeout=0.5)
        assert error_msg is not None
        assert error_msg.type == MessageType.TASK_ERROR
        assert error_msg.payload["error_type"] == "BudgetExceeded"
        assert error_msg.payload["retryable"] is False

    @pytest.mark.anyio
    async def test_unhandled_exception_transitions_to_failed(
        self, repo, bus, tracer, logger
    ):
        orch = make_orchestrator(repo)
        worker = make_idle_agent(
            repo, parent_id=orch.id, execution_mode="messages_api"
        )

        await bus.send(task_assign_msg(orch, worker, description="crash"))

        api_runner = _mock_api_runner_raises(
            tracer, logger, exc=RuntimeError("unexpected crash")
        )

        with pytest.raises(RuntimeError, match="unexpected crash"):
            await run_worker(
                worker, bus, repo, tracer, logger,
                api_runner=api_runner,
                receive_timeout=1.0,
            )

        refreshed = repo.get(worker.id)
        assert refreshed.state == AgentState.FAILED

    @pytest.mark.anyio
    async def test_missing_runner_raises_and_fails_agent(
        self, repo, bus, tracer, logger
    ):
        orch = make_orchestrator(repo)
        worker = make_idle_agent(
            repo, parent_id=orch.id, execution_mode="claude_code"
        )

        await bus.send(task_assign_msg(orch, worker, description="do it"))

        with pytest.raises(RuntimeError, match="cc_runner"):
            await run_worker(
                worker, bus, repo, tracer, logger,
                # cc_runner intentionally omitted
                receive_timeout=1.0,
            )

        refreshed = repo.get(worker.id)
        assert refreshed.state == AgentState.FAILED

    @pytest.mark.anyio
    async def test_task_id_propagated_from_payload(self, repo, bus, tracer, logger):
        orch = make_orchestrator(repo)
        worker = make_idle_agent(
            repo, parent_id=orch.id, execution_mode="messages_api"
        )

        await bus.send(task_assign_msg(orch, worker, task_id="my-special-task"))

        api_runner = _mock_api_runner(tracer, logger, final_text="done")

        await run_worker(
            worker, bus, repo, tracer, logger,
            api_runner=api_runner,
            receive_timeout=1.0,
        )

        result_msg = await bus.receive(orch.id, timeout=0.5)
        assert result_msg.payload["task_id"] == "my-special-task"


# ---------------------------------------------------------------------------
# Mock runner factories
# ---------------------------------------------------------------------------


def _noop_api_runner(tracer, logger) -> MessagesAPIRunner:
    """API runner that immediately returns empty text (never actually called)."""
    return _mock_api_runner(tracer, logger, final_text="")


def _mock_cc_runner(tracer, logger, *, result: ClaudeCodeResult) -> ClaudeCodeRunner:
    runner = ClaudeCodeRunner("agent-mock", tracer, logger)
    runner.run = AsyncMock(return_value=result)
    return runner


def _mock_api_runner(
    tracer,
    logger,
    *,
    final_text: str = "",
    stats: dict | None = None,
) -> MessagesAPIRunner:
    if stats is None:
        stats = {"iterations_used": 1, "tool_calls_made": 0}
    client = AsyncMock()
    registry = ToolRegistry("agent-mock")
    runner = MessagesAPIRunner("agent-mock", client, registry, tracer, logger)
    runner.run_turn = AsyncMock(return_value=(final_text, [], stats))
    return runner


def _mock_api_runner_raises(tracer, logger, *, exc: Exception) -> MessagesAPIRunner:
    client = AsyncMock()
    registry = ToolRegistry("agent-mock")
    runner = MessagesAPIRunner("agent-mock", client, registry, tracer, logger)
    runner.run_turn = AsyncMock(side_effect=exc)
    return runner
