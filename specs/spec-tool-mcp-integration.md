# Spec: Tool Use & MCP Integration

## Overview

Agents have access to tools in two ways: native Python tools registered
directly with the framework, and MCP (Model Context Protocol) servers that
expose tools over a standard protocol. Both are surfaced to Claude identically
as tool definitions. The agent's `context.tools_enabled` and
`context.mcp_servers` lists control which tools are available per agent.

---

## Tool Categories

| Category | Source | Examples |
|---|---|---|
| Native tools | Python functions decorated with `@tool` | `read_file`, `write_file`, `run_shell` |
| MCP tools | External MCP server process | `brave-search`, `filesystem`, `postgres`, `github` |

An orchestrator decides which tools to grant each worker at spawn time.
Workers cannot acquire new tools at runtime.

---

## Native Tool Definition

```python
from typing import Any
from dataclasses import dataclass

@dataclass
class ToolDefinition:
    name: str
    description: str
    input_schema: dict          # JSON Schema object
    handler: callable           # async (input: dict) -> Any

# Decorator shorthand
def tool(name: str, description: str, schema: dict):
    def decorator(fn):
        fn._tool_meta = ToolDefinition(
            name=name,
            description=description,
            input_schema=schema,
            handler=fn,
        )
        return fn
    return decorator
```

### Example native tool

```python
@tool(
    name="read_file",
    description="Read the contents of a file at the given path.",
    schema={
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "Absolute or relative file path"}
        },
        "required": ["path"],
    },
)
async def read_file(input: dict) -> str:
    path = input["path"]
    with open(path) as f:
        return f.read()
```

---

## MCP Server Integration

### Lifecycle

MCP servers are managed as subprocesses (stdio transport). Each server is
started once per agent session and torn down when the agent terminates.

```
Agent INITIALIZING
    │
    ├─ Start MCP server process (stdio)
    ├─ Run `tools/list` → receive ToolDefinition list
    ├─ Merge native + MCP tool definitions
    └─ Agent transitions to IDLE
```

### `MCPServerManager`

```python
class MCPServerManager:
    async def start(self, server_name: str, command: list[str], env: dict = {}) -> None: ...
    async def list_tools(self, server_name: str) -> list[ToolDefinition]: ...
    async def call_tool(self, server_name: str, tool_name: str, input: dict) -> Any: ...
    async def stop(self, server_name: str) -> None: ...
    async def stop_all(self) -> None: ...
```

### MCP server registry (`mcp_servers.json`)

Place this file at the project root. Each entry defines how to launch a server:

```json
{
  "filesystem": {
    "command": ["npx", "-y", "@modelcontextprotocol/server-filesystem"],
    "args": ["/allowed/path"],
    "env": {}
  },
  "brave-search": {
    "command": ["npx", "-y", "@modelcontextprotocol/server-brave-search"],
    "args": [],
    "env": { "BRAVE_API_KEY": "${BRAVE_API_KEY}" }
  },
  "postgres": {
    "command": ["uvx", "mcp-server-postgres"],
    "args": [],
    "env": { "DATABASE_URL": "${DATABASE_URL}" }
  }
}
```

Environment variable substitution (`${VAR}`) is resolved at startup from the
process environment. Missing required env vars cause an `MCPStartupError` that
transitions the agent to `FAILED`.

---

## `ToolRegistry`

The registry is the single source of truth for tools available to an agent. It
merges native and MCP tools and handles dispatch.

```python
class ToolRegistry:
    def __init__(self, agent_id: str): ...

    def register_native(self, tool_def: ToolDefinition) -> None: ...
    async def register_mcp(self, server_name: str, manager: MCPServerManager) -> None: ...

    def list_definitions(self) -> list[dict]:
        """Return tool defs in the format expected by the Anthropic API."""
        ...

    async def call(self, tool_name: str, input: dict) -> ToolResult: ...
    def has_tool(self, tool_name: str) -> bool: ...
```

### `ToolResult`

```python
@dataclass
class ToolResult:
    tool_name: str
    input: dict
    output: Any
    error: Optional[str] = None     # set if the tool raised an exception
    duration_ms: float = 0.0
    is_error: bool = False
```

---

## Tool Call Execution Loop

The execution loop integrates with the Claude API directly. Tool use is handled
inside the agent's `run()` method:

```python
async def run_turn(agent: Agent, registry: ToolRegistry, messages: list) -> str:
    response = await anthropic_client.messages.create(
        model="claude-opus-4-5",
        tools=registry.list_definitions(),
        messages=messages,
    )

    while response.stop_reason == "tool_use":
        tool_results = []
        for block in response.content:
            if block.type == "tool_use":
                result = await registry.call(block.name, block.input)
                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": str(result.output) if not result.is_error else result.error,
                    "is_error": result.is_error,
                })

        messages.append({"role": "assistant", "content": response.content})
        messages.append({"role": "user", "content": tool_results})

        response = await anthropic_client.messages.create(
            model="claude-opus-4-5",
            tools=registry.list_definitions(),
            messages=messages,
        )

    return response.content[0].text
```

---

## Tool Call Limits

To prevent runaway tool use, each agent enforces:

| Limit | Default | Configurable via |
|---|---|---|
| Max tool calls per task | 50 | `context.max_tool_calls` |
| Max iterations (turns) | 20 | `context.max_iterations` |
| Tool call timeout | 30s | `context.tool_timeout_seconds` |

Exceeding `max_tool_calls` or `max_iterations` transitions the agent to `FAILED`
with a `BudgetExceededError`.

---

## Error Handling

| Error | Behaviour |
|---|---|
| Tool raises exception | Captured in `ToolResult.error`; passed back to Claude as `is_error: true`; Claude decides whether to retry or give up |
| MCP server crashes | `MCPServerManager` attempts one restart; on second failure, all tools from that server are removed from `registry` and a warning is logged |
| Tool not found | Raise `ToolNotFoundError` immediately; do not pass to Claude |
| Timeout | `asyncio.wait_for` wrapper; raises `ToolTimeoutError`, treated as tool error |

---

## Security Constraints

- Native tools that write to the filesystem **must** validate paths against an
  allowlist defined in `context.allowed_paths`.
- Shell execution tools (`run_shell`) are disabled by default and must be
  explicitly enabled via `context.tools_enabled: ["run_shell"]`.
- MCP servers run as subprocesses with no elevated privileges. Do not pass
  credentials in `args`; use `env` only.
- Tool inputs are passed through as-is — Claude is trusted but inputs touching
  the filesystem or network should be logged at DEBUG level.

---

## Design Decisions

| Decision | Choice | Rationale |
|---|---|---|
| MCP transport | stdio subprocess | Matches MCP spec default; no extra network layer |
| Tool dispatch | Single `ToolRegistry` per agent | Avoids inter-agent tool sharing bugs |
| Error passing | Return to Claude as `is_error` | Lets Claude self-correct before hard failure |
| Server registry | JSON file at project root | Convention matches Claude Code's `mcp_servers.json` pattern |
| Tool schema format | JSON Schema (Anthropic API format) | No conversion layer needed |
