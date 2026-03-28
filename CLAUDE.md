# CLAUDE.md — Agent Orchestration Framework

This file is the authoritative project memory for Claude Code. Read it fully
before writing any code. When in doubt about a design decision, the specs
referenced here take precedence over general best practices.

---

## What This Project Is

A Python agent orchestration framework that coordinates multiple Claude-powered
agents in a **hybrid topology** (one orchestrator per cluster, with peer worker
clusters beneath it). Orchestrators can be nested — an orchestrator is itself a
worker from a parent orchestrator's perspective.

All state is persisted to **SQLite**. All agent communication goes through a
**SQLite-backed message bus**. There are no external services required to run
the framework.

---

## Spec Documents

All architectural decisions live in the `specs/` directory. Read the relevant
spec before implementing any feature in its domain.

| File | Domain | Key abstractions |
|---|---|---|
| `specs/spec-agent-lifecycle.md` | Agent FSM, state persistence | `Agent`, `AgentState`, `AgentRepository` |
| `specs/spec-inter-agent-communication.md` | Message bus, delivery guarantees | `Message`, `MessageType`, `MessageBus` |
| `specs/spec-tool-mcp-integration.md` | Native tools, MCP servers, tool loop | `ToolDefinition`, `ToolRegistry`, `MCPServerManager` |
| `specs/spec-observability.md` | OTel tracing, console logging | `tracer`, `ConsoleFormatter`, span conventions |
| `specs/spec-claude-code-sdk.md` | Claude Code sessions, coding workers | `ClaudeCodeRunner`, `ClaudeCodeTask`, `ClaudeCodeResult` |

---

## Project Layout

```
.
├── CLAUDE.md                    ← you are here
├── mcp_servers.json             ← MCP server registry (shared by framework + Claude Code)
├── specs/
│   ├── spec-agent-lifecycle.md
│   ├── spec-inter-agent-communication.md
│   ├── spec-tool-mcp-integration.md
│   ├── spec-observability.md
│   └── spec-claude-code-sdk.md
├── orchestrator/
│   ├── __init__.py
│   ├── agent.py                 ← Agent dataclass, AgentRole, AgentState, VALID_TRANSITIONS
│   ├── repository.py            ← AgentRepository (SQLite)
│   ├── bus.py                   ← MessageBus, Message, MessageType
│   ├── registry.py              ← ToolRegistry, ToolDefinition, ToolResult
│   ├── mcp.py                   ← MCPServerManager
│   ├── runner.py                ← ClaudeCodeRunner, ClaudeCodeTask, ClaudeCodeResult
│   ├── telemetry.py             ← init_tracing(), get_logger(), ConsoleFormatter
│   └── db.py                    ← SQLite connection, schema migrations
├── tools/
│   └── builtin.py               ← Native tools decorated with @tool
├── traces/                      ← Local OTel JSONL output (git-ignored)
└── tests/
```

---

## Core Principles

These are non-negotiable constraints. Do not work around them.

### 1. State transitions are the only way agent status changes
Never set `agent.state` directly. Always call `AgentRepository.transition()`.
It validates against `VALID_TRANSITIONS`, writes to `agent_state_history`, and
emits an OTel span event. Any code that bypasses this is a bug.

### 2. Agents never share memory
All coordination between agents happens via `MessageBus`. There is no shared
in-memory queue, no global dict, no class-level state shared between agents.

### 3. Tool errors go back to Claude, not up as exceptions
When a tool call fails, capture it in `ToolResult.is_error = True` and return
it to Claude via the messages API `tool_result` block. Claude decides whether
to retry or give up. Only raise a hard exception if the tool is not found or
the agent's tool budget is exceeded.

### 4. OTel context travels with messages
When sending a message, inject the current OTel trace context into
`message.payload._otel_context`. When receiving, extract it and use it as the
parent span context. This is what stitches per-agent traces into a single
unified trace per orchestrated task.

### 5. Claude Code workers get a CLAUDE.md
Before spawning a worker with `execution_mode: "claude_code"`, the orchestrator
writes a `CLAUDE.md` into the working directory with task description,
constraints, and relevant file paths. This is the only way to give Claude Code
session-level context.

### 6. asyncio throughout
The entire framework is async. Use `asyncio.wait_for` for all timeout
enforcement (tool calls, Claude Code sessions, message receive). Never use
blocking I/O on the event loop.

---

## Database

SQLite file: `./agent_orchestrator.db`

Tables (defined in `orchestrator/db.py`):

| Table | Purpose |
|---|---|
| `agents` | Agent identity, role, state, context (JSON blob) |
| `agent_state_history` | Immutable audit log of every state transition |
| `messages` | Message bus: all sent messages with delivery status |

Schema is created via `db.init_schema()` at startup. No ORM — use raw SQL with
parameterised queries. Use WAL mode for SQLite (`PRAGMA journal_mode=WAL`).

---

## Telemetry

Two outputs, always active:

- **OTel traces** → `./traces/YYYY-MM-DD.jsonl` (local) and optionally an OTLP
  HTTP endpoint set via `OTLP_ENDPOINT` env var.
- **Console logs** → stdout, single-line `[HH:MM:SS] LEVEL  agent/<short-id>  MESSAGE  {key=value}` format.

Span names follow the pattern `<domain>.<operation>` — e.g. `agent.run`,
`llm.call`, `tool.call`, `message.send`. All spans carry `agent.id`,
`agent.role`, `agent.cluster_id` attributes. See `spec-observability.md` for
the full attribute reference.

Do not put tool inputs/outputs in span attributes. Log them at `DEBUG` level
via the console logger only.

---

## MCP Servers

All MCP server definitions live in `mcp_servers.json` at the project root.
This file is used by both `MCPServerManager` (framework tools) and
`ClaudeCodeRunner` (Claude Code sessions). Credentials are passed via
environment variables only — never hardcoded in the JSON.

To add a new MCP server: add an entry to `mcp_servers.json`, then reference
its name in `agent.context.mcp_servers`.

---

## Agent Execution Modes

A worker's `context.execution_mode` determines which execution path it takes:

| Mode | Class used | When to use |
|---|---|---|
| `"messages_api"` | Anthropic `messages.create` loop | Reasoning, planning, summarisation |
| `"claude_code"` | `ClaudeCodeRunner` | Code generation, file editing, shell tasks |

The orchestrator sets this at spawn time. Workers do not change their execution
mode at runtime.

---

## Environment Variables

| Variable | Required | Purpose |
|---|---|---|
| `ANTHROPIC_API_KEY` | Yes | Anthropic API access |
| `OTLP_ENDPOINT` | No | Remote OTel collector (e.g. `http://localhost:4318`) |
| `DB_PATH` | No | SQLite file path (default: `./agent_orchestrator.db`) |
| `LOG_LEVEL` | No | Console log level (default: `INFO`) |
| Any MCP server credential | Conditional | Referenced as `${VAR}` in `mcp_servers.json` |

---

## What To Avoid

- **Do not** use `threading` — `asyncio` only.
- **Do not** store sensitive data (API keys, file contents, tool outputs) in
  OTel span attributes.
- **Do not** give a worker more tools than it needs for its task — follow least
  privilege.
- **Do not** let an orchestrator terminate before all workers in its cluster
  are `TERMINATED` or `FAILED`.
- **Do not** create a new SQLite connection per operation — use a shared
  connection pool managed by `db.py`.
- **Do not** use `SELECT *` — always name columns explicitly in queries.
