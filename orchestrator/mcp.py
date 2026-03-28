"""MCPServerManager: lifecycle management for MCP server subprocesses.

Each MCP server runs as a child process over stdio using JSON-RPC 2.0.
MCPServerManager starts, queries, and stops these processes.  One manager
instance is typically shared across all tools for a single agent session.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
from pathlib import Path
from typing import Any, Optional

from orchestrator.registry import MCPServerCrashError, ToolDefinition

_ENV_RE = re.compile(r"\$\{(\w+)\}")

# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class MCPStartupError(RuntimeError):
    """Raised when an MCP server process fails to start or initialise."""


class MCPProtocolError(RuntimeError):
    """Raised on JSON-RPC communication failures (e.g. server stdout closed)."""


class MCPToolError(RuntimeError):
    """Raised when an MCP server returns ``isError: true`` for a tool call."""


# ---------------------------------------------------------------------------
# Internal: one subprocess connection
# ---------------------------------------------------------------------------


class _MCPProcess:
    """Low-level JSON-RPC 2.0 wrapper around a single MCP server subprocess."""

    def __init__(self, proc: asyncio.subprocess.Process) -> None:
        self._proc = proc
        self._next_id = 1

    # ------------------------------------------------------------------
    # Public helpers
    # ------------------------------------------------------------------

    async def initialize(self) -> None:
        """Perform the MCP initialisation handshake."""
        await self.request(
            "initialize",
            {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "agent_orchestrator", "version": "0.1.0"},
            },
        )
        await self.notify("notifications/initialized")

    async def request(self, method: str, params: Optional[dict] = None) -> Any:
        """Send a JSON-RPC request and return the ``result`` field."""
        msg_id = self._next_id
        self._next_id += 1
        msg: dict[str, Any] = {"jsonrpc": "2.0", "id": msg_id, "method": method}
        if params is not None:
            msg["params"] = params
        await self._write(msg)
        return await self._read_until(msg_id)

    async def notify(self, method: str, params: Optional[dict] = None) -> None:
        """Send a JSON-RPC notification (no response expected)."""
        msg: dict[str, Any] = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            msg["params"] = params
        await self._write(msg)

    async def shutdown(self) -> None:
        """Close stdin and wait for the process to exit."""
        try:
            if self._proc.stdin:
                self._proc.stdin.close()
        except Exception:  # noqa: BLE001
            pass
        try:
            await asyncio.wait_for(self._proc.wait(), timeout=5.0)
        except asyncio.TimeoutError:
            self._proc.kill()
            await self._proc.wait()

    # ------------------------------------------------------------------
    # I/O
    # ------------------------------------------------------------------

    async def _write(self, msg: dict) -> None:
        stdin = self._proc.stdin
        if stdin is None:
            raise MCPProtocolError("MCP server stdin is not available")
        line = json.dumps(msg) + "\n"
        stdin.write(line.encode())
        await stdin.drain()

    async def _read_until(self, expected_id: int) -> Any:
        """Read lines until a response with *expected_id* arrives.

        Lines without an ``id`` (notifications) are silently skipped.
        """
        stdout = self._proc.stdout
        if stdout is None:
            raise MCPProtocolError("MCP server stdout is not available")
        while True:
            raw = await stdout.readline()
            if not raw:
                raise MCPProtocolError(
                    "MCP server process died (stdout closed)"
                )
            envelope = json.loads(raw)
            if envelope.get("id") != expected_id:
                continue  # skip notification or unrelated response
            if "error" in envelope:
                raise MCPProtocolError(
                    f"JSON-RPC error from server: {envelope['error']}"
                )
            return envelope.get("result")


# ---------------------------------------------------------------------------
# MCPServerManager
# ---------------------------------------------------------------------------


class MCPServerManager:
    """Manage one or more MCP server subprocesses for a single agent session.

    Usage::

        mgr = MCPServerManager()
        await mgr.start("brave-search", command=["npx", "-y", "..."], env={"KEY": "val"})
        tools = await mgr.list_tools("brave-search")
        result = await mgr.call_tool("brave-search", "brave_web_search", {"query": "..."})
        await mgr.stop_all()
    """

    def __init__(self) -> None:
        # server_name → running _MCPProcess
        self._procs: dict[str, _MCPProcess] = {}
        # server_name → (command, resolved_env) for restart
        self._specs: dict[str, tuple[list[str], dict[str, str]]] = {}

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(
        self,
        server_name: str,
        command: list[str],
        env: Optional[dict[str, str]] = None,
    ) -> None:
        """Start an MCP server and complete the initialisation handshake.

        *env* values are extra environment variables merged on top of the
        current process environment.  ``${VAR}`` references should be resolved
        by the caller (see :func:`load_mcp_registry`) before calling ``start``.

        Raises :exc:`MCPStartupError` on process or handshake failure.
        """
        self._specs[server_name] = (command, dict(env or {}))
        try:
            await self._launch(server_name)
        except Exception as exc:
            raise MCPStartupError(
                f"Failed to start MCP server '{server_name}': {exc}"
            ) from exc

    async def stop(self, server_name: str) -> None:
        """Stop the named MCP server."""
        proc = self._procs.pop(server_name, None)
        if proc:
            await proc.shutdown()

    async def stop_all(self) -> None:
        """Stop all running MCP servers."""
        for name in list(self._procs):
            await self.stop(name)

    # ------------------------------------------------------------------
    # Operations
    # ------------------------------------------------------------------

    async def list_tools(self, server_name: str) -> list[ToolDefinition]:
        """Return the tool list advertised by *server_name*."""
        self._require(server_name)
        result = await self._procs[server_name].request("tools/list")
        return [
            ToolDefinition(
                name=t["name"],
                description=t.get("description", ""),
                input_schema=t.get("inputSchema", {}),
            )
            for t in (result or {}).get("tools", [])
        ]

    async def call_tool(
        self,
        server_name: str,
        tool_name: str,
        input: dict,  # noqa: A002
    ) -> Any:
        """Call *tool_name* on *server_name*.

        On first failure (protocol error / process crash), restarts the server
        and retries once.  On second failure raises :exc:`MCPServerCrashError`.
        :exc:`MCPToolError` (``isError: true``) is re-raised immediately without
        a restart attempt.
        """
        self._require(server_name)
        try:
            return _extract_content(
                await self._procs[server_name].request(
                    "tools/call", {"name": tool_name, "arguments": input}
                )
            )
        except MCPToolError:
            raise
        except Exception:
            pass  # protocol / process error — try restart below

        # One restart attempt
        try:
            await self._restart(server_name)
        except Exception as exc:
            raise MCPServerCrashError(
                f"MCP server '{server_name}' crashed and could not be restarted: {exc}"
            ) from exc

        try:
            return _extract_content(
                await self._procs[server_name].request(
                    "tools/call", {"name": tool_name, "arguments": input}
                )
            )
        except MCPToolError:
            raise
        except Exception as exc:
            raise MCPServerCrashError(
                f"MCP server '{server_name}' crashed again after restart"
            ) from exc

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _require(self, server_name: str) -> None:
        if server_name not in self._procs:
            raise KeyError(
                f"MCP server '{server_name}' is not running; call start() first"
            )

    async def _launch(self, server_name: str) -> None:
        """Start a fresh subprocess for *server_name* and run MCP handshake."""
        command, extra_env = self._specs[server_name]
        full_env = {**os.environ, **extra_env}
        proc = await asyncio.create_subprocess_exec(
            *command,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            env=full_env,
        )
        mcp_proc = _MCPProcess(proc)
        await mcp_proc.initialize()
        self._procs[server_name] = mcp_proc

    async def _restart(self, server_name: str) -> None:
        """Tear down the crashed process and launch a replacement."""
        old = self._procs.pop(server_name, None)
        if old:
            await old.shutdown()
        await self._launch(server_name)


# ---------------------------------------------------------------------------
# Content extraction helper
# ---------------------------------------------------------------------------


def _extract_content(result: Any) -> Any:
    """Pull the return value out of a ``tools/call`` result dict.

    Returns the text string for single-text responses; the full content list
    for multi-item responses; ``None`` for empty results.  Raises
    :exc:`MCPToolError` when ``isError`` is ``true``.
    """
    if not isinstance(result, dict):
        return result
    if result.get("isError"):
        content = result.get("content") or []
        text = content[0].get("text", "MCP tool error") if content else "MCP tool error"
        raise MCPToolError(text)
    content = result.get("content") or []
    if not content:
        return None
    if len(content) == 1 and content[0].get("type") == "text":
        return content[0]["text"]
    return content


# ---------------------------------------------------------------------------
# mcp_servers.json loader
# ---------------------------------------------------------------------------


def _resolve_env(raw: dict[str, str]) -> dict[str, str]:
    """Substitute ``${VAR}`` placeholders from ``os.environ``.

    Raises :exc:`MCPStartupError` for any variable that is not set.
    """
    resolved: dict[str, str] = {}
    for key, value in raw.items():
        def _sub(m: re.Match) -> str:
            var = m.group(1)
            if var not in os.environ:
                raise MCPStartupError(
                    f"Required environment variable ${{{var}}} is not set"
                )
            return os.environ[var]

        resolved[key] = _ENV_RE.sub(_sub, value)
    return resolved


def load_mcp_registry(path: str = "mcp_servers.json") -> dict[str, dict]:
    """Load ``mcp_servers.json`` and return a dict ready for :class:`MCPServerManager`.

    Each entry is normalised to::

        {
            "command": ["npx", "-y", "...", "/allowed/path"],  # command + args merged
            "env": {"KEY": "resolved_value"},                  # ${VAR} substituted
        }

    Raises :exc:`MCPStartupError` when a required env var is absent.
    """
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    out: dict[str, dict] = {}
    for name, spec in raw.items():
        command = list(spec.get("command", [])) + list(spec.get("args", []))
        env = _resolve_env(spec.get("env") or {})
        out[name] = {"command": command, "env": env}
    return out
