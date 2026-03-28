"""Agent execution runners and the single-task worker dispatch function.

Three public components:

``ClaudeCodeRunner``
    Wraps the Claude Code SDK when available; falls back to ``claude --print``
    subprocess.  Use for workers with ``execution_mode: "claude_code"``.

``MessagesAPIRunner``
    Anthropic messages API with an integrated tool-use loop and budget
    enforcement.  Use for workers with ``execution_mode: "messages_api"``.

``run_worker``
    Handles one task cycle for an agent: IDLE → receive → RUNNING →
    send result → IDLE | FAILED | TERMINATED.  Callers run this in a loop
    for agents that process multiple sequential tasks.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Optional

from opentelemetry import trace as otel_trace
from opentelemetry.trace import StatusCode

import anthropic as _anthropic

from orchestrator.agent import Agent, AgentRepository, AgentState
from orchestrator.bus import Message, MessageBus, MessageType
from orchestrator.mcp import load_mcp_registry
from orchestrator.registry import BudgetExceededError, ToolRegistry

# ---------------------------------------------------------------------------
# Claude Code SDK availability
# ---------------------------------------------------------------------------

# Try the SDK; fall back to subprocess.  The import path is still in flux
# across SDK releases, so we probe both known locations.
_SDK_AVAILABLE = False
_ClaudeCodeSession: Any = None

try:
    from claude_code_sdk import ClaudeCodeSession as _ClaudeCodeSession  # type: ignore[no-redef]
    _SDK_AVAILABLE = True
except ImportError:
    try:
        from anthropic.claude_code import ClaudeCodeSession as _ClaudeCodeSession  # type: ignore[no-redef]
        _SDK_AVAILABLE = True
    except ImportError:
        pass

# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------


@dataclass
class ClaudeCodeTask:
    prompt: str
    working_directory: str
    allowed_paths: list[str]
    mcp_servers: list[str]           # server names from mcp_servers.json
    max_turns: int = 10
    timeout_seconds: float = 300.0


@dataclass
class ClaudeCodeResult:
    success: bool
    final_response: str
    files_modified: list[str]
    turns_used: int
    error: Optional[str] = None


@dataclass
class ClaudeCodeEvent:
    type: str   # "tool_use" | "turn_complete" | "file_modified" | "final"
    data: dict = field(default_factory=dict)


# ---------------------------------------------------------------------------
# ClaudeCodeRunner
# ---------------------------------------------------------------------------


class ClaudeCodeRunner:
    """Wraps Claude Code SDK (when available) with a subprocess fallback.

    The subprocess fallback invokes ``claude --print --max-turns N <prompt>``
    in the task's working directory.  It cannot introspect ``files_modified``
    or ``turns_used`` — those fields are always ``[]`` and ``0`` in that path.
    """

    def __init__(
        self,
        agent_id: str,
        tracer: otel_trace.Tracer,
        logger,
        *,
        mcp_registry_path: str = "mcp_servers.json",
    ) -> None:
        self._agent_id = agent_id
        self._tracer = tracer
        self._logger = logger
        self._mcp_registry_path = mcp_registry_path

    async def run(self, task: ClaudeCodeTask) -> ClaudeCodeResult:
        with self._tracer.start_as_current_span("claude_code.run") as span:
            span.set_attribute("agent.id", self._agent_id)
            span.set_attribute("cc.working_directory", task.working_directory)
            span.set_attribute("cc.max_turns", task.max_turns)

            if _SDK_AVAILABLE:
                result = await self._run_sdk(task)
            else:
                result = await self._run_subprocess_fallback(task)

            span.set_attribute("cc.success", result.success)
            span.set_attribute("cc.turns_used", result.turns_used)
            if not result.success:
                span.set_status(StatusCode.ERROR, result.error or "unknown")
            return result

    async def stream(self, task: ClaudeCodeTask) -> AsyncIterator[ClaudeCodeEvent]:
        """Stream ClaudeCodeEvents from the session.

        In subprocess mode, streaming is not supported — a single ``final``
        event is yielded after the process completes.
        """
        if not _SDK_AVAILABLE:
            result = await self._run_subprocess_fallback(task)
            yield ClaudeCodeEvent(
                type="final",
                data={"response": result.final_response, "success": result.success},
            )
            return

        try:
            session = _ClaudeCodeSession(
                working_directory=task.working_directory,
                allowed_paths=task.allowed_paths,
                mcp_servers=self._resolve_mcp_servers(task.mcp_servers),
                max_turns=task.max_turns,
            )
            async for event in session.stream(task.prompt):
                if event.type == "tool_use":
                    yield ClaudeCodeEvent(
                        type="tool_use", data={"tool": event.tool_name}
                    )
                elif event.type == "turn":
                    yield ClaudeCodeEvent(
                        type="turn_complete", data={"turn": event.turn_index}
                    )
                elif event.type == "result":
                    yield ClaudeCodeEvent(
                        type="final", data={"response": event.final_response}
                    )
        except Exception as exc:  # noqa: BLE001
            yield ClaudeCodeEvent(
                type="final",
                data={"response": "", "success": False, "error": str(exc)},
            )

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    async def _run_sdk(self, task: ClaudeCodeTask) -> ClaudeCodeResult:
        try:
            session = _ClaudeCodeSession(
                working_directory=task.working_directory,
                allowed_paths=task.allowed_paths,
                mcp_servers=self._resolve_mcp_servers(task.mcp_servers),
                max_turns=task.max_turns,
            )
            result = await asyncio.wait_for(
                session.run(task.prompt),
                timeout=task.timeout_seconds,
            )
            return ClaudeCodeResult(
                success=True,
                final_response=result.final_response,
                files_modified=result.files_modified,
                turns_used=result.turns_used,
            )
        except asyncio.TimeoutError:
            return ClaudeCodeResult(
                success=False, final_response="", files_modified=[], turns_used=0,
                error="timeout",
            )
        except Exception as exc:  # noqa: BLE001
            return ClaudeCodeResult(
                success=False, final_response="", files_modified=[], turns_used=0,
                error=str(exc),
            )

    async def _run_subprocess_fallback(self, task: ClaudeCodeTask) -> ClaudeCodeResult:
        cmd = [
            "claude",
            "--print",
            "--max-turns", str(task.max_turns),
            task.prompt,
        ]
        self._logger.debug(
            "cc.subprocess_start",
            extra={"cwd": task.working_directory, "max_turns": task.max_turns},
        )
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                cwd=task.working_directory,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(),
                timeout=task.timeout_seconds,
            )
            success = proc.returncode == 0
            return ClaudeCodeResult(
                success=success,
                final_response=stdout.decode(errors="replace"),
                files_modified=[],
                turns_used=0,
                error=stderr.decode(errors="replace") if not success else None,
            )
        except asyncio.TimeoutError:
            return ClaudeCodeResult(
                success=False, final_response="", files_modified=[], turns_used=0,
                error="timeout",
            )
        except Exception as exc:  # noqa: BLE001
            return ClaudeCodeResult(
                success=False, final_response="", files_modified=[], turns_used=0,
                error=str(exc),
            )

    def _resolve_mcp_servers(self, server_names: list[str]) -> list[dict]:
        """Map server names to the format expected by the Claude Code SDK."""
        if not server_names:
            return []
        try:
            registry = load_mcp_registry(self._mcp_registry_path)
        except (OSError, Exception):  # noqa: BLE001
            return []
        return [
            {
                "name": name,
                "command": registry[name]["command"],
                "args": [],
                "env": registry[name].get("env", {}),
            }
            for name in server_names
            if name in registry
        ]


# ---------------------------------------------------------------------------
# MessagesAPIRunner
# ---------------------------------------------------------------------------

_DEFAULT_MODEL = "claude-opus-4-6"

_DEFAULT_MAX_TOOL_CALLS = 50
_DEFAULT_MAX_ITERATIONS = 20


class MessagesAPIRunner:
    """Anthropic messages API with integrated tool-use loop.

    Runs ``messages.create`` in a loop, dispatching tool calls via the
    ToolRegistry until Claude returns a non-tool_use ``stop_reason`` or a
    budget limit is reached.
    """

    def __init__(
        self,
        agent_id: str,
        client: _anthropic.AsyncAnthropic,
        registry: ToolRegistry,
        tracer: otel_trace.Tracer,
        logger,
        *,
        model: str = _DEFAULT_MODEL,
    ) -> None:
        self._agent_id = agent_id
        self._client = client
        self._registry = registry
        self._tracer = tracer
        self._logger = logger
        self._model = model

    async def run_turn(
        self,
        messages: list[dict],
        *,
        system: str = "",
        max_tool_calls: int = _DEFAULT_MAX_TOOL_CALLS,
        max_iterations: int = _DEFAULT_MAX_ITERATIONS,
    ) -> tuple[str, list[dict], dict[str, int]]:
        """Run the tool loop until a text response or budget exhaustion.

        Returns ``(final_text, updated_messages, stats)`` where *stats* has
        keys ``iterations_used`` and ``tool_calls_made``.

        Raises :exc:`~orchestrator.registry.BudgetExceededError` when either
        budget is hit.
        """
        iterations = 0
        tool_calls_made = 0

        create_kwargs: dict[str, Any] = {
            "model": self._model,
            "max_tokens": 8192,
            "messages": messages,
        }
        if system:
            create_kwargs["system"] = system
        tools = self._registry.list_definitions()
        if tools:
            create_kwargs["tools"] = tools

        while True:
            if iterations >= max_iterations:
                raise BudgetExceededError(
                    f"Agent {self._agent_id} exceeded max_iterations={max_iterations}"
                )
            iterations += 1

            with self._tracer.start_as_current_span("llm.call") as span:
                span.set_attribute("agent.id", self._agent_id)
                span.set_attribute("llm.model", self._model)
                span.set_attribute("llm.iteration", iterations)

                response = await self._client.messages.create(**create_kwargs)
                span.set_attribute("llm.stop_reason", response.stop_reason or "")

            if response.stop_reason != "tool_use":
                text = ""
                for block in response.content:
                    if hasattr(block, "text"):
                        text = block.text
                        break
                stats = {"iterations_used": iterations, "tool_calls_made": tool_calls_made}
                return text, messages, stats

            # Tool use — dispatch all tool calls in this turn
            tool_results = []
            for block in response.content:
                if not hasattr(block, "type") or block.type != "tool_use":
                    continue

                if tool_calls_made >= max_tool_calls:
                    raise BudgetExceededError(
                        f"Agent {self._agent_id} exceeded max_tool_calls={max_tool_calls}"
                    )
                tool_calls_made += 1

                with self._tracer.start_as_current_span("tool.call") as span:
                    span.set_attribute("agent.id", self._agent_id)
                    span.set_attribute("tool.name", block.name)

                    result = await self._registry.call(block.name, block.input)
                    span.set_attribute("tool.is_error", result.is_error)
                    span.set_attribute("tool.duration_ms", result.duration_ms)

                self._logger.debug(
                    "tool.called",
                    extra={
                        "tool": block.name,
                        "is_error": result.is_error,
                        "duration_ms": round(result.duration_ms),
                    },
                )

                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": (
                        str(result.output)
                        if not result.is_error
                        else (result.error or "tool error")
                    ),
                    "is_error": result.is_error,
                })

            messages = [
                *messages,
                {"role": "assistant", "content": response.content},
                {"role": "user", "content": tool_results},
            ]
            create_kwargs["messages"] = messages


# ---------------------------------------------------------------------------
# run_worker
# ---------------------------------------------------------------------------


async def run_worker(
    agent: Agent,
    bus: MessageBus,
    repo: AgentRepository,
    tracer: otel_trace.Tracer,
    logger,
    *,
    cc_runner: Optional[ClaudeCodeRunner] = None,
    api_runner: Optional[MessagesAPIRunner] = None,
    receive_timeout: float = 30.0,
) -> None:
    """Handle one task cycle: IDLE → receive → RUNNING → result → IDLE | FAILED | TERMINATED.

    The agent must be in ``IDLE`` state when called.  After the cycle it
    will be in ``IDLE`` (success), ``FAILED`` (task error or unhandled
    exception), or ``TERMINATED`` (received ``CONTROL:TERMINATE``).

    Callers run this in a loop to process multiple sequential tasks.
    """
    with tracer.start_as_current_span("agent.run") as span:
        span.set_attribute("agent.id", agent.id)
        span.set_attribute("agent.role", agent.role.value)
        span.set_attribute("agent.cluster_id", agent.cluster_id)

        try:
            repo.transition(agent.id, AgentState.RUNNING)

            msg = await bus.receive(agent.id, timeout=receive_timeout)

            if msg is None:
                repo.transition(agent.id, AgentState.IDLE, reason="no_task")
                return

            await bus.acknowledge(msg.id)

            # CONTROL messages — lifecycle commands take priority.
            # Agent is in RUNNING state here; RUNNING → TERMINATED is not a
            # valid direct transition (spec: RUNNING → IDLE → TERMINATED).
            if msg.type == MessageType.CONTROL:
                command = msg.payload.get("command", "")
                reason = msg.payload.get("reason", "")
                if command == "TERMINATE":
                    repo.transition(agent.id, AgentState.IDLE, reason="pre-terminate")
                    repo.transition(
                        agent.id, AgentState.TERMINATED,
                        reason=reason or "control:terminate",
                    )
                elif command == "SUSPEND":
                    repo.transition(
                        agent.id, AgentState.SUSPENDED,
                        reason=reason or "control:suspend",
                    )
                else:
                    repo.transition(
                        agent.id, AgentState.IDLE,
                        reason=f"control:{command.lower()}",
                    )
                return

            if msg.type != MessageType.TASK_ASSIGN:
                logger.warning(
                    "agent.unexpected_message_type",
                    extra={"agent_id": agent.id, "type": msg.type.value},
                )
                repo.transition(agent.id, AgentState.IDLE, reason="unexpected_message")
                return

            task_id = msg.payload.get("task_id", msg.id)
            execution_mode = agent.context.get("execution_mode", "messages_api")

            span.set_attribute("agent.execution_mode", execution_mode)
            span.set_attribute("agent.task_id", task_id)

            if execution_mode == "claude_code":
                if cc_runner is None:
                    raise RuntimeError(
                        "execution_mode='claude_code' requires cc_runner"
                    )
                await _run_claude_code_task(
                    agent, msg, task_id, cc_runner, bus, repo, logger
                )
            else:
                if api_runner is None:
                    raise RuntimeError(
                        "execution_mode='messages_api' requires api_runner"
                    )
                await _run_messages_api_task(
                    agent, msg, task_id, api_runner, bus, repo, logger
                )

        except Exception as exc:  # noqa: BLE001
            span.record_exception(exc)
            span.set_status(StatusCode.ERROR, str(exc))
            logger.error(
                "agent.run_error",
                extra={"agent_id": agent.id, "error": str(exc)},
            )
            try:
                repo.transition(
                    agent.id,
                    AgentState.FAILED,
                    reason=str(exc),
                    error={"type": type(exc).__name__, "message": str(exc)},
                )
            except Exception:  # noqa: BLE001
                pass
            raise


# ---------------------------------------------------------------------------
# Private task helpers
# ---------------------------------------------------------------------------


async def _run_claude_code_task(
    agent: Agent,
    msg: Message,
    task_id: str,
    runner: ClaudeCodeRunner,
    bus: MessageBus,
    repo: AgentRepository,
    logger,
) -> None:
    cc_task = ClaudeCodeTask(
        prompt=msg.payload["description"],
        working_directory=agent.context.get("working_directory", "."),
        allowed_paths=agent.context.get("allowed_paths", []),
        mcp_servers=agent.context.get("mcp_servers", []),
        max_turns=agent.context.get("max_iterations", 10),
        timeout_seconds=agent.context.get("task_timeout_seconds", 300.0),
    )

    result = await runner.run(cc_task)

    if result.success:
        await bus.send(Message(
            type=MessageType.TASK_RESULT,
            sender_id=agent.id,
            recipient_id=agent.parent_id,
            cluster_id=agent.cluster_id,
            payload={
                "task_id": task_id,
                "output": result.final_response,
                "files_modified": result.files_modified,
                "turns_used": result.turns_used,
            },
        ))
        repo.transition(agent.id, AgentState.IDLE)
    else:
        await bus.send(Message(
            type=MessageType.TASK_ERROR,
            sender_id=agent.id,
            recipient_id=agent.parent_id,
            cluster_id=agent.cluster_id,
            payload={
                "task_id": task_id,
                "error_type": "ClaudeCodeFailed",
                "message": result.error,
                "retryable": True,
            },
        ))
        repo.transition(
            agent.id, AgentState.FAILED,
            reason=result.error or "claude_code_failed",
            error={"type": "ClaudeCodeFailed", "message": result.error},
        )


async def _run_messages_api_task(
    agent: Agent,
    msg: Message,
    task_id: str,
    runner: MessagesAPIRunner,
    bus: MessageBus,
    repo: AgentRepository,
    logger,
) -> None:
    system = agent.context.get("system_prompt", "")
    history: list[dict] = list(agent.context.get("conversation_history", []))
    description = msg.payload.get("description", "")
    if description:
        history = [*history, {"role": "user", "content": description}]

    try:
        final_text, _, stats = await runner.run_turn(
            history,
            system=system,
            max_tool_calls=agent.context.get("max_tool_calls", _DEFAULT_MAX_TOOL_CALLS),
            max_iterations=agent.context.get("max_iterations", _DEFAULT_MAX_ITERATIONS),
        )
        await bus.send(Message(
            type=MessageType.TASK_RESULT,
            sender_id=agent.id,
            recipient_id=agent.parent_id,
            cluster_id=agent.cluster_id,
            payload={
                "task_id": task_id,
                "output": final_text,
                "iterations_used": stats["iterations_used"],
                "tool_calls_made": stats["tool_calls_made"],
            },
        ))
        repo.transition(agent.id, AgentState.IDLE)

    except BudgetExceededError as exc:
        await bus.send(Message(
            type=MessageType.TASK_ERROR,
            sender_id=agent.id,
            recipient_id=agent.parent_id,
            cluster_id=agent.cluster_id,
            payload={
                "task_id": task_id,
                "error_type": "BudgetExceeded",
                "message": str(exc),
                "retryable": False,
            },
        ))
        repo.transition(
            agent.id, AgentState.FAILED,
            reason=str(exc),
            error={"type": "BudgetExceeded", "message": str(exc)},
        )
