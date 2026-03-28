# Spec: Claude Code SDK Integration

## Overview

The orchestration framework integrates with the Claude Code Python SDK
(`anthropic[claude-code]`) to give agents the ability to spawn Claude Code
sub-processes for coding tasks. This is distinct from the Anthropic messages
API used for conversational turns — Claude Code is invoked as a long-running
agentic coding session with its own tool loop and file system access.

---

## When to Use Claude Code vs the Messages API

| Use Case | Interface |
|---|---|
| Conversational reasoning, summarisation, planning | Anthropic Messages API (`anthropic.messages.create`) |
| Code generation, file editing, test running, shell commands | Claude Code SDK (`claude_code.run`) |
| Delegating a full coding task to a sub-agent | Claude Code SDK, spawned as a worker |

Orchestrators decide which interface to use when assigning a task. This is
encoded in the worker's `context.execution_mode`:

```json
{
  "execution_mode": "claude_code",   // "messages_api" | "claude_code"
  "task_description": "...",
  "allowed_paths": ["/workspace/project"],
  "mcp_servers": ["filesystem", "github"]
}
```

---

## SDK Installation & Import

```bash
pip install anthropic[claude-code]
```

```python
import anthropic
from anthropic.claude_code import ClaudeCodeSession
```

> **Note:** Verify the exact import path against the installed SDK version.
> The package is under active development. If the import fails, fall back to
> the subprocess approach (see below).

---

## `ClaudeCodeRunner`

The framework wraps the Claude Code SDK in a `ClaudeCodeRunner` that handles
session lifecycle, progress streaming, and OTel instrumentation.

```python
from dataclasses import dataclass
from typing import AsyncIterator, Optional

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

class ClaudeCodeRunner:
    def __init__(self, agent_id: str, tracer, logger): ...

    async def run(self, task: ClaudeCodeTask) -> ClaudeCodeResult: ...
    async def stream(self, task: ClaudeCodeTask) -> AsyncIterator[ClaudeCodeEvent]: ...
```

---

## Session Execution

### Using the SDK directly

```python
async def run(self, task: ClaudeCodeTask) -> ClaudeCodeResult:
    with tracer.start_as_current_span("claude_code.run") as span:
        span.set_attribute("agent.id", self.agent_id)
        span.set_attribute("cc.working_directory", task.working_directory)
        span.set_attribute("cc.max_turns", task.max_turns)

        try:
            session = ClaudeCodeSession(
                working_directory=task.working_directory,
                allowed_paths=task.allowed_paths,
                mcp_servers=self._resolve_mcp_servers(task.mcp_servers),
                max_turns=task.max_turns,
            )

            result = await asyncio.wait_for(
                session.run(task.prompt),
                timeout=task.timeout_seconds,
            )

            span.set_attribute("cc.turns_used", result.turns_used)
            span.set_attribute("cc.files_modified", len(result.files_modified))
            return ClaudeCodeResult(
                success=True,
                final_response=result.final_response,
                files_modified=result.files_modified,
                turns_used=result.turns_used,
            )

        except asyncio.TimeoutError:
            span.set_status(trace.StatusCode.ERROR, "timeout")
            return ClaudeCodeResult(success=False, final_response="", files_modified=[], turns_used=0, error="timeout")

        except Exception as e:
            span.record_exception(e)
            span.set_status(trace.StatusCode.ERROR, str(e))
            return ClaudeCodeResult(success=False, final_response="", files_modified=[], turns_used=0, error=str(e))
```

### Subprocess fallback

If the SDK is unavailable, fall back to spawning the `claude` CLI as a
subprocess. This is the resilience path — not the default.

```python
async def _run_subprocess_fallback(self, task: ClaudeCodeTask) -> ClaudeCodeResult:
    cmd = [
        "claude",
        "--print",                          # non-interactive, print result
        "--max-turns", str(task.max_turns),
        task.prompt,
    ]
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
        final_response=stdout.decode(),
        files_modified=[],          # subprocess mode cannot introspect this
        turns_used=0,
        error=stderr.decode() if not success else None,
    )
```

---

## MCP Server Resolution for Claude Code

Claude Code sessions need MCP servers configured in its own format. The
`_resolve_mcp_servers()` method reads `mcp_servers.json` and maps the server
names in `task.mcp_servers` to the format expected by the SDK:

```python
def _resolve_mcp_servers(self, server_names: list[str]) -> list[dict]:
    registry = load_mcp_registry("mcp_servers.json")
    return [
        {
            "name": name,
            "command": registry[name]["command"],
            "args": registry[name].get("args", []),
            "env": resolve_env_vars(registry[name].get("env", {})),
        }
        for name in server_names
        if name in registry
    ]
```

---

## Streaming Progress Events

For long-running coding tasks, stream events back to the orchestrator via the
message bus so the orchestrator can surface progress to the user:

```python
@dataclass
class ClaudeCodeEvent:
    type: str          # "tool_use" | "turn_complete" | "file_modified" | "final"
    data: dict

async def stream(self, task: ClaudeCodeTask) -> AsyncIterator[ClaudeCodeEvent]:
    async for event in session.stream(task.prompt):
        if event.type == "tool_use":
            yield ClaudeCodeEvent(type="tool_use", data={"tool": event.tool_name})
        elif event.type == "turn":
            yield ClaudeCodeEvent(type="turn_complete", data={"turn": event.turn_index})
        elif event.type == "result":
            yield ClaudeCodeEvent(type="final", data={"response": event.final_response})
```

The worker agent sends a `PEER_SYNC` message with `sync_type: "partial_result"`
for each streamed event so the orchestrator can log progress without waiting
for task completion.

---

## Worker Integration

A worker agent with `execution_mode: "claude_code"` uses `ClaudeCodeRunner`
instead of the standard messages API loop:

```python
async def run_worker(agent: Agent, bus: MessageBus, runner: ClaudeCodeRunner):
    repo.transition(agent.id, AgentState.RUNNING)

    task_msg = await bus.receive(agent.id)
    await bus.acknowledge(task_msg.id)

    cc_task = ClaudeCodeTask(
        prompt=task_msg.payload["description"],
        working_directory=agent.context.get("working_directory", "."),
        allowed_paths=agent.context.get("allowed_paths", []),
        mcp_servers=agent.context.get("mcp_servers", []),
        max_turns=agent.context.get("max_iterations", 10),
    )

    result = await runner.run(cc_task)

    if result.success:
        await bus.send(Message(
            type=MessageType.TASK_RESULT,
            sender_id=agent.id,
            recipient_id=agent.parent_id,
            cluster_id=agent.cluster_id,
            payload={
                "task_id": task_msg.payload["task_id"],
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
            payload={"task_id": task_msg.payload["task_id"], "error_type": "ClaudeCodeFailed", "message": result.error, "retryable": True},
        ))
        repo.transition(agent.id, AgentState.FAILED, reason=result.error)
```

---

## CLAUDE.md Integration

Each Claude Code session should be started in a working directory that contains
a `CLAUDE.md` file. This file provides Claude Code with project-specific
context, conventions, and constraints.

The orchestrator is responsible for writing or updating `CLAUDE.md` before
spawning a coding worker. At minimum it should contain:

```markdown
# Project Context (auto-generated by orchestrator)

## Task
{task_description}

## Constraints
- Only modify files under: {allowed_paths}
- Do not install new packages without confirming
- Write tests for all new functions

## Relevant Files
{file_list}
```

---

## Design Decisions

| Decision | Choice | Rationale |
|---|---|---|
| SDK vs subprocess | SDK primary, subprocess fallback | SDK gives structured output; subprocess is resilience path |
| Streaming | Yes, via `AsyncIterator` | Allows orchestrator to show live progress |
| MCP for Claude Code | Shared `mcp_servers.json` | Single registry for both framework and Claude Code sessions |
| `CLAUDE.md` generation | Orchestrator responsibility | Keeps context injection centralised |
| Timeout enforcement | `asyncio.wait_for` wrapper | Prevents runaway sessions from blocking workers |
| `files_modified` tracking | SDK only (not subprocess) | Subprocess mode trades introspection for simplicity |
