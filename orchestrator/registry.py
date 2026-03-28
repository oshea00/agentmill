"""ToolDefinition, ToolResult, @tool decorator, ToolRegistry, and tool errors.

ToolRegistry is the single source of truth for tools available to one agent.
It merges native Python tools and MCP-server tools and handles dispatched calls.
Each agent owns exactly one ToolRegistry instance.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Optional


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class ToolNotFoundError(KeyError):
    """Raised when a requested tool name is not registered."""


class ToolTimeoutError(TimeoutError):
    """Raised when a tool call exceeds its timeout budget."""


class BudgetExceededError(RuntimeError):
    """Raised when an agent exhausts its tool-call or iteration budget."""


class MCPServerCrashError(RuntimeError):
    """Raised by MCPServerManager when a server crashes and fails to recover.

    ToolRegistry catches this to de-register all tools from the crashed server.
    """


# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------


@dataclass
class ToolDefinition:
    """Schema + callable for a single tool.

    ``handler`` is *None* for MCP-backed tools; ToolRegistry dispatches those
    through the MCPServerManager instead of calling the handler directly.
    """

    name: str
    description: str
    input_schema: dict
    handler: Optional[Callable[..., Any]] = field(default=None, repr=False)


@dataclass
class ToolResult:
    tool_name: str
    input: dict
    output: Any
    error: Optional[str] = None
    duration_ms: float = 0.0
    is_error: bool = False


# ---------------------------------------------------------------------------
# @tool decorator
# ---------------------------------------------------------------------------


def tool(name: str, description: str, schema: dict) -> Callable:
    """Decorate an async function to expose it as a native tool.

    Usage::

        @tool(
            name="read_file",
            description="Read file contents.",
            schema={"type": "object", "properties": {"path": {"type": "string"}},
                    "required": ["path"]},
        )
        async def read_file(input: dict) -> str:
            ...

    The decorated function gains a ``_tool_meta`` attribute holding the
    :class:`ToolDefinition`.  Pass that to
    :meth:`ToolRegistry.register_native`.
    """

    def decorator(fn: Callable) -> Callable:
        setattr(
            fn,
            "_tool_meta",
            ToolDefinition(name=name, description=description, input_schema=schema, handler=fn),
        )
        return fn

    return decorator


# ---------------------------------------------------------------------------
# ToolRegistry
# ---------------------------------------------------------------------------


class ToolRegistry:
    """Per-agent tool registry.  Merges native and MCP tools; dispatches calls.

    Parameters
    ----------
    agent_id:
        UUID of the owning agent (used for logging and telemetry).
    tool_timeout_seconds:
        Per-tool call timeout enforced via :func:`asyncio.wait_for`.
    """

    def __init__(
        self,
        agent_id: str,
        tool_timeout_seconds: float = 30.0,
    ) -> None:
        self._agent_id = agent_id
        self._timeout = tool_timeout_seconds
        # name → ToolDefinition (native tools only)
        self._native: dict[str, ToolDefinition] = {}
        # name → (server_name, manager) (MCP tools only)
        self._mcp: dict[str, tuple[str, Any]] = {}
        # name → Anthropic API dict (all tools)
        self._definitions: dict[str, dict] = {}

    # ------------------------------------------------------------------
    # Registration
    # ------------------------------------------------------------------

    def register_native(self, tool_def: ToolDefinition) -> None:
        """Register a native Python tool."""
        self._native[tool_def.name] = tool_def
        self._definitions[tool_def.name] = {
            "name": tool_def.name,
            "description": tool_def.description,
            "input_schema": tool_def.input_schema,
        }

    async def register_mcp(self, server_name: str, manager: Any) -> None:
        """Query *manager* for its tool list and register all of them.

        *manager* must implement ``list_tools(server_name) -> list[ToolDefinition]``
        (satisfied by :class:`~orchestrator.mcp.MCPServerManager`).
        """
        tools: list[ToolDefinition] = await manager.list_tools(server_name)
        for t in tools:
            self._mcp[t.name] = (server_name, manager)
            self._definitions[t.name] = {
                "name": t.name,
                "description": t.description,
                "input_schema": t.input_schema,
            }

    # ------------------------------------------------------------------
    # Query
    # ------------------------------------------------------------------

    def has_tool(self, tool_name: str) -> bool:
        return tool_name in self._definitions

    def list_definitions(self) -> list[dict]:
        """Return tool definitions in the format expected by the Anthropic API."""
        return list(self._definitions.values())

    # ------------------------------------------------------------------
    # Dispatch
    # ------------------------------------------------------------------

    async def call(self, tool_name: str, input: dict) -> ToolResult:  # noqa: A002
        """Invoke *tool_name* with *input* and return a :class:`ToolResult`.

        Rules
        -----
        * :exc:`ToolNotFoundError` is raised immediately (never captured as a
          result) — callers must not pass unknown tool names.
        * All other errors are captured in ``ToolResult.is_error = True`` so
          Claude can decide whether to retry.
        * :exc:`MCPServerCrashError` additionally de-registers all tools from
          the crashed server before returning the error result.
        """
        if not self.has_tool(tool_name):
            raise ToolNotFoundError(f"Tool '{tool_name}' not registered in agent {self._agent_id}")

        start = time.monotonic()
        output: Any = None
        error: Optional[str] = None
        is_error = False

        try:
            coro = self._dispatch(tool_name, input)
            output = await asyncio.wait_for(coro, timeout=self._timeout)
        except asyncio.TimeoutError:
            is_error = True
            error = f"Tool '{tool_name}' timed out after {self._timeout}s"
        except MCPServerCrashError as exc:
            is_error = True
            error = str(exc)
            if tool_name in self._mcp:
                crashed_server, _ = self._mcp[tool_name]
                self._deregister_mcp_server(crashed_server)
        except Exception as exc:  # noqa: BLE001
            is_error = True
            error = str(exc)

        duration_ms = (time.monotonic() - start) * 1_000
        return ToolResult(
            tool_name=tool_name,
            input=input,
            output=output,
            error=error,
            duration_ms=duration_ms,
            is_error=is_error,
        )

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _dispatch(self, tool_name: str, input: dict) -> Any:  # noqa: A002
        if tool_name in self._native:
            handler = self._native[tool_name].handler
            if handler is None:
                raise AssertionError(f"Native tool '{tool_name}' has no handler")
            return await handler(input)
        server_name, manager = self._mcp[tool_name]
        return await manager.call_tool(server_name, tool_name, input)

    def _deregister_mcp_server(self, server_name: str) -> None:
        """Remove all tools belonging to *server_name* from the registry."""
        stale = [name for name, (sn, _) in self._mcp.items() if sn == server_name]
        for name in stale:
            del self._mcp[name]
            self._definitions.pop(name, None)
