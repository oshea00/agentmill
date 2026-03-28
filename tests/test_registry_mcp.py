"""Tests for ToolRegistry (registry.py) and MCPServerManager (mcp.py).

MCPServerManager tests spin up tests/mock_mcp_server.py as a subprocess so no
external services are required.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest

from orchestrator.mcp import (
    MCPProtocolError,
    MCPServerManager,
    MCPStartupError,
    MCPToolError,
    _extract_content,
    load_mcp_registry,
)
from orchestrator.registry import (
    BudgetExceededError,
    MCPServerCrashError,
    ToolDefinition,
    ToolNotFoundError,
    ToolRegistry,
    ToolResult,
    tool,
)


# Run async tests on asyncio (mirrors the pattern in test_bus.py).
@pytest.fixture
def anyio_backend():
    return "asyncio"


# Path to the mock server script.
_MOCK_SERVER = str(Path(__file__).parent / "mock_mcp_server.py")
_SERVER_CMD = [sys.executable, _MOCK_SERVER]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_tool_def(name: str = "dummy", handler=None) -> ToolDefinition:
    return ToolDefinition(
        name=name,
        description=f"A {name} tool.",
        input_schema={"type": "object", "properties": {}, "required": []},
        handler=handler,
    )


async def _noop(input: dict) -> str:  # noqa: A002
    return "ok"


async def _exploding(input: dict) -> None:  # noqa: A002
    raise ValueError("tool exploded")


async def _slow(input: dict) -> str:  # noqa: A002
    await asyncio.sleep(10)
    return "never"


# ---------------------------------------------------------------------------
# ToolDefinition and @tool decorator
# ---------------------------------------------------------------------------


class TestToolDefinition:
    def test_fields_set(self):
        td = ToolDefinition(
            name="read_file",
            description="Read a file.",
            input_schema={"type": "object"},
        )
        assert td.name == "read_file"
        assert td.description == "Read a file."
        assert td.input_schema == {"type": "object"}
        assert td.handler is None

    def test_handler_field(self):
        async def h(i):
            return i

        td = ToolDefinition(name="x", description="", input_schema={}, handler=h)
        assert td.handler is h


class TestToolDecorator:
    def test_attaches_tool_meta(self):
        @tool(name="ping", description="Ping.", schema={"type": "object"})
        async def ping(input: dict) -> str:  # noqa: A002
            return "pong"

        meta = ping._tool_meta  # type: ignore[attr-defined]
        assert isinstance(meta, ToolDefinition)
        assert meta.name == "ping"
        assert meta.description == "Ping."
        assert meta.handler is ping

    def test_decorated_function_still_callable(self):
        @tool(name="noop", description="", schema={})
        async def noop(input: dict) -> str:  # noqa: A002
            return "noop"

        assert callable(noop)

    def test_schema_preserved(self):
        schema = {
            "type": "object",
            "properties": {"path": {"type": "string"}},
            "required": ["path"],
        }

        @tool(name="rf", description="read", schema=schema)
        async def rf(input: dict) -> str:  # noqa: A002
            return ""

        assert rf._tool_meta.input_schema is schema  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# ToolResult
# ---------------------------------------------------------------------------


class TestToolResult:
    def test_defaults(self):
        r = ToolResult(tool_name="t", input={}, output="x")
        assert r.error is None
        assert r.duration_ms == 0.0
        assert r.is_error is False

    def test_error_result(self):
        r = ToolResult(
            tool_name="t", input={}, output=None, error="boom", is_error=True
        )
        assert r.is_error is True
        assert r.error == "boom"
        assert r.output is None


# ---------------------------------------------------------------------------
# ToolRegistry — registration and query
# ---------------------------------------------------------------------------


class TestToolRegistryRegistration:
    def test_register_native_has_tool(self):
        reg = ToolRegistry("agent-1")
        reg.register_native(_make_tool_def("x"))
        assert reg.has_tool("x")
        assert not reg.has_tool("y")

    def test_list_definitions_single(self):
        reg = ToolRegistry("agent-1")
        td = ToolDefinition(
            name="calc",
            description="Does math.",
            input_schema={"type": "object"},
        )
        reg.register_native(td)
        defs = reg.list_definitions()
        assert len(defs) == 1
        assert defs[0]["name"] == "calc"
        assert defs[0]["description"] == "Does math."
        assert defs[0]["input_schema"] == {"type": "object"}

    def test_list_definitions_multiple(self):
        reg = ToolRegistry("a")
        reg.register_native(_make_tool_def("a"))
        reg.register_native(_make_tool_def("b"))
        names = {d["name"] for d in reg.list_definitions()}
        assert names == {"a", "b"}

    def test_list_definitions_empty(self):
        assert ToolRegistry("a").list_definitions() == []

    def test_overwrite_same_name(self):
        reg = ToolRegistry("a")
        td1 = ToolDefinition(name="x", description="first", input_schema={})
        td2 = ToolDefinition(name="x", description="second", input_schema={})
        reg.register_native(td1)
        reg.register_native(td2)
        defs = reg.list_definitions()
        assert len(defs) == 1
        assert defs[0]["description"] == "second"

    @pytest.mark.anyio
    async def test_register_mcp_adds_definitions(self):
        reg = ToolRegistry("a")
        manager = AsyncMock()
        manager.list_tools.return_value = [
            ToolDefinition(
                name="search",
                description="Web search.",
                input_schema={"type": "object"},
            ),
            ToolDefinition(
                name="fetch", description="HTTP fetch.", input_schema={"type": "object"}
            ),
        ]
        await reg.register_mcp("brave", manager)
        assert reg.has_tool("search")
        assert reg.has_tool("fetch")
        names = {d["name"] for d in reg.list_definitions()}
        assert names == {"search", "fetch"}

    @pytest.mark.anyio
    async def test_register_mcp_and_native_coexist(self):
        reg = ToolRegistry("a")
        reg.register_native(_make_tool_def("native_tool"))
        manager = AsyncMock()
        manager.list_tools.return_value = [
            ToolDefinition(name="mcp_tool", description="", input_schema={})
        ]
        await reg.register_mcp("srv", manager)
        assert reg.has_tool("native_tool")
        assert reg.has_tool("mcp_tool")


# ---------------------------------------------------------------------------
# ToolRegistry — call dispatch
# ---------------------------------------------------------------------------


class TestToolRegistryCall:
    @pytest.mark.anyio
    async def test_call_native_success(self):
        reg = ToolRegistry("a")
        reg.register_native(_make_tool_def("noop", handler=_noop))
        result = await reg.call("noop", {})
        assert result.tool_name == "noop"
        assert result.output == "ok"
        assert result.is_error is False
        assert result.error is None

    @pytest.mark.anyio
    async def test_call_returns_input_in_result(self):
        reg = ToolRegistry("a")
        reg.register_native(_make_tool_def("noop", handler=_noop))
        result = await reg.call("noop", {"key": "val"})
        assert result.input == {"key": "val"}

    @pytest.mark.anyio
    async def test_call_records_duration(self):
        reg = ToolRegistry("a")
        reg.register_native(_make_tool_def("noop", handler=_noop))
        result = await reg.call("noop", {})
        assert result.duration_ms >= 0

    @pytest.mark.anyio
    async def test_call_unknown_tool_raises(self):
        reg = ToolRegistry("a")
        with pytest.raises(ToolNotFoundError):
            await reg.call("ghost", {})

    @pytest.mark.anyio
    async def test_call_native_exception_captured(self):
        reg = ToolRegistry("a")
        reg.register_native(_make_tool_def("boom", handler=_exploding))
        result = await reg.call("boom", {})
        assert result.is_error is True
        assert "tool exploded" in (result.error or "")
        assert result.output is None

    @pytest.mark.anyio
    async def test_call_timeout_captured(self):
        reg = ToolRegistry("a", tool_timeout_seconds=0.05)
        reg.register_native(_make_tool_def("slow", handler=_slow))
        result = await reg.call("slow", {})
        assert result.is_error is True
        assert "timed out" in (result.error or "").lower()

    @pytest.mark.anyio
    async def test_call_mcp_tool_via_manager(self):
        reg = ToolRegistry("a")
        manager = AsyncMock()
        manager.list_tools.return_value = [
            ToolDefinition(name="search", description="", input_schema={})
        ]
        manager.call_tool = AsyncMock(return_value="result text")
        await reg.register_mcp("srv", manager)

        result = await reg.call("search", {"q": "hello"})
        assert result.output == "result text"
        assert result.is_error is False
        manager.call_tool.assert_awaited_once_with("srv", "search", {"q": "hello"})

    @pytest.mark.anyio
    async def test_call_mcp_exception_captured(self):
        reg = ToolRegistry("a")
        manager = AsyncMock()
        manager.list_tools.return_value = [
            ToolDefinition(name="flaky", description="", input_schema={})
        ]
        manager.call_tool = AsyncMock(side_effect=RuntimeError("mcp error"))
        await reg.register_mcp("srv", manager)

        result = await reg.call("flaky", {})
        assert result.is_error is True
        assert "mcp error" in (result.error or "")

    @pytest.mark.anyio
    async def test_mcp_server_crash_deregisters_tools(self):
        """MCPServerCrashError causes all tools from that server to be removed."""
        reg = ToolRegistry("a")
        manager = AsyncMock()
        manager.list_tools.return_value = [
            ToolDefinition(name="t1", description="", input_schema={}),
            ToolDefinition(name="t2", description="", input_schema={}),
        ]
        manager.call_tool = AsyncMock(side_effect=MCPServerCrashError("server died"))
        await reg.register_mcp("crashed_srv", manager)

        assert reg.has_tool("t1")
        assert reg.has_tool("t2")

        result = await reg.call("t1", {})
        assert result.is_error is True

        # Both tools from crashed_srv should now be gone.
        assert not reg.has_tool("t1")
        assert not reg.has_tool("t2")

    @pytest.mark.anyio
    async def test_mcp_tool_error_not_crash(self):
        """MCPToolError does not de-register tools."""
        reg = ToolRegistry("a")
        manager = AsyncMock()
        manager.list_tools.return_value = [
            ToolDefinition(name="failable", description="", input_schema={})
        ]
        manager.call_tool = AsyncMock(side_effect=MCPToolError("bad input"))
        await reg.register_mcp("srv", manager)

        result = await reg.call("failable", {})
        assert result.is_error is True
        # Tool is still registered after a non-crash error.
        assert reg.has_tool("failable")


# ---------------------------------------------------------------------------
# MCPServerManager with mock subprocess
# ---------------------------------------------------------------------------


@pytest.fixture
async def manager():
    """Fresh MCPServerManager that is torn down after each test."""
    mgr = MCPServerManager()
    yield mgr
    await mgr.stop_all()


class TestMCPServerManager:
    @pytest.mark.anyio
    async def test_start_succeeds(self, manager):
        await manager.start("mock", command=_SERVER_CMD)
        assert "mock" in manager._procs

    @pytest.mark.anyio
    async def test_list_tools_returns_definitions(self, manager):
        await manager.start("mock", command=_SERVER_CMD)
        tools = await manager.list_tools("mock")
        names = {t.name for t in tools}
        assert names == {"echo", "add"}

    @pytest.mark.anyio
    async def test_list_tools_tool_definition_types(self, manager):
        await manager.start("mock", command=_SERVER_CMD)
        tools = await manager.list_tools("mock")
        for t in tools:
            assert isinstance(t, ToolDefinition)
            assert isinstance(t.name, str)
            assert isinstance(t.description, str)
            assert isinstance(t.input_schema, dict)

    @pytest.mark.anyio
    async def test_call_tool_echo(self, manager):
        await manager.start("mock", command=_SERVER_CMD)
        result = await manager.call_tool("mock", "echo", {"message": "hello world"})
        assert result == "hello world"

    @pytest.mark.anyio
    async def test_call_tool_add(self, manager):
        await manager.start("mock", command=_SERVER_CMD)
        result = await manager.call_tool("mock", "add", {"a": 3, "b": 4})
        assert result == "7"

    @pytest.mark.anyio
    async def test_call_tool_mcp_error_raises(self, manager):
        await manager.start("mock", command=_SERVER_CMD + ["--error-on-call"])
        with pytest.raises(MCPToolError, match="simulated tool error"):
            await manager.call_tool("mock", "echo", {"message": "hi"})

    @pytest.mark.anyio
    async def test_call_tool_jsonrpc_error_is_mcp_tool_error(self, manager):
        await manager.start("mock", command=_SERVER_CMD)
        with pytest.raises(MCPToolError, match="Unknown tool"):
            await manager.call_tool("mock", "not_a_real_tool", {})

    @pytest.mark.anyio
    async def test_call_tool_not_running_raises(self, manager):
        with pytest.raises(KeyError):
            await manager.call_tool("nonexistent", "echo", {})

    @pytest.mark.anyio
    async def test_list_tools_not_running_raises(self, manager):
        with pytest.raises(KeyError):
            await manager.list_tools("nonexistent")

    @pytest.mark.anyio
    async def test_stop_removes_server(self, manager):
        await manager.start("mock", command=_SERVER_CMD)
        await manager.stop("mock")
        assert "mock" not in manager._procs

    @pytest.mark.anyio
    async def test_stop_nonexistent_is_noop(self, manager):
        await manager.stop("ghost")  # should not raise

    @pytest.mark.anyio
    async def test_stop_all_clears_all(self, manager):
        await manager.start("s1", command=_SERVER_CMD)
        await manager.start("s2", command=_SERVER_CMD)
        await manager.stop_all()
        assert manager._procs == {}

    @pytest.mark.anyio
    async def test_multiple_calls_same_server(self, manager):
        await manager.start("mock", command=_SERVER_CMD)
        r1 = await manager.call_tool("mock", "echo", {"message": "a"})
        r2 = await manager.call_tool("mock", "add", {"a": 1, "b": 2})
        assert r1 == "a"
        assert r2 == "3"

    @pytest.mark.anyio
    async def test_multiple_servers_independent(self, manager):
        await manager.start("s1", command=_SERVER_CMD)
        await manager.start("s2", command=_SERVER_CMD)
        r1 = await manager.call_tool("s1", "echo", {"message": "from-s1"})
        r2 = await manager.call_tool("s2", "echo", {"message": "from-s2"})
        assert r1 == "from-s1"
        assert r2 == "from-s2"

    @pytest.mark.anyio
    async def test_crash_raises_mcp_server_crash_error(self, manager):
        """Server that exits after init — call_tool should raise MCPServerCrashError."""
        await manager.start("crasher", command=_SERVER_CMD + ["--exit-after-init"])
        with pytest.raises(MCPServerCrashError):
            await manager.call_tool("crasher", "echo", {"message": "hi"})

    @pytest.mark.anyio
    async def test_start_bad_command_raises_mcp_startup_error(self, manager):
        with pytest.raises(MCPStartupError):
            await manager.start("bad", command=["no_such_binary_xyz_abc"])


# ---------------------------------------------------------------------------
# _extract_content helper
# ---------------------------------------------------------------------------


class TestExtractContent:
    def test_single_text_returns_string(self):
        result = {"content": [{"type": "text", "text": "hello"}], "isError": False}
        assert _extract_content(result) == "hello"

    def test_empty_content_returns_none(self):
        result = {"content": [], "isError": False}
        assert _extract_content(result) is None

    def test_missing_content_returns_none(self):
        assert _extract_content({"isError": False}) is None

    def test_multi_item_returns_list(self):
        content = [
            {"type": "text", "text": "a"},
            {"type": "text", "text": "b"},
        ]
        result = {"content": content, "isError": False}
        assert _extract_content(result) == content

    def test_is_error_raises_mcp_tool_error(self):
        result = {
            "content": [{"type": "text", "text": "something failed"}],
            "isError": True,
        }
        with pytest.raises(MCPToolError, match="something failed"):
            _extract_content(result)

    def test_is_error_empty_content_default_message(self):
        with pytest.raises(MCPToolError, match="MCP tool error"):
            _extract_content({"content": [], "isError": True})

    def test_non_dict_passed_through(self):
        assert _extract_content("raw string") == "raw string"
        assert _extract_content(42) == 42


# ---------------------------------------------------------------------------
# load_mcp_registry
# ---------------------------------------------------------------------------


class TestLoadMcpRegistry:
    def test_loads_and_merges_command_args(self, tmp_path):
        cfg = {
            "filesystem": {
                "command": ["npx", "-y", "@mcp/fs"],
                "args": ["/data"],
                "env": {},
            }
        }
        p = tmp_path / "mcp_servers.json"
        p.write_text(__import__("json").dumps(cfg))
        registry = load_mcp_registry(str(p))
        assert registry["filesystem"]["command"] == ["npx", "-y", "@mcp/fs", "/data"]

    def test_accepts_string_command_without_char_split(self, tmp_path):
        cfg = {
            "timeutils": {
                "command": "uvx",
                "args": ["mcp-server-time@2026.1.26"],
                "env": {},
            }
        }
        p = tmp_path / "mcp_servers.json"
        p.write_text(__import__("json").dumps(cfg))
        registry = load_mcp_registry(str(p))
        assert registry["timeutils"]["command"] == ["uvx", "mcp-server-time@2026.1.26"]

    def test_resolves_env_vars(self, tmp_path, monkeypatch):
        monkeypatch.setenv("MY_API_KEY", "secret123")
        cfg = {
            "search": {
                "command": ["npx"],
                "args": [],
                "env": {"API_KEY": "${MY_API_KEY}"},
            }
        }
        p = tmp_path / "mcp_servers.json"
        p.write_text(__import__("json").dumps(cfg))
        registry = load_mcp_registry(str(p))
        assert registry["search"]["env"]["API_KEY"] == "secret123"

    def test_missing_env_var_raises(self, tmp_path, monkeypatch):
        monkeypatch.delenv("MISSING_VAR", raising=False)
        cfg = {
            "srv": {
                "command": ["x"],
                "args": [],
                "env": {"KEY": "${MISSING_VAR}"},
            }
        }
        p = tmp_path / "mcp_servers.json"
        p.write_text(__import__("json").dumps(cfg))
        with pytest.raises(MCPStartupError, match="MISSING_VAR"):
            load_mcp_registry(str(p))

    def test_empty_env_allowed(self, tmp_path):
        cfg = {"srv": {"command": ["x"], "args": [], "env": {}}}
        p = tmp_path / "mcp_servers.json"
        p.write_text(__import__("json").dumps(cfg))
        registry = load_mcp_registry(str(p))
        assert registry["srv"]["env"] == {}

    def test_multiple_servers(self, tmp_path):
        cfg = {
            "a": {"command": ["a"], "args": [], "env": {}},
            "b": {"command": ["b"], "args": [], "env": {}},
        }
        p = tmp_path / "mcp_servers.json"
        p.write_text(__import__("json").dumps(cfg))
        registry = load_mcp_registry(str(p))
        assert set(registry) == {"a", "b"}
