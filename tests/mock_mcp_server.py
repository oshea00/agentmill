#!/usr/bin/env python3
"""Standalone mock MCP server for testing.

Speaks JSON-RPC 2.0 over stdio.  Supports two tools:

    echo(message: str) -> str          returns message verbatim
    add(a: number, b: number) -> str   returns str(a + b)

Flags
-----
--exit-after-init
    Exit (status 0) immediately after processing the
    ``notifications/initialized`` notification.  Used to simulate a server
    that crashes before any tool calls.

--error-on-call
    Return ``isError: true`` for every ``tools/call`` request.
"""

import argparse
import json
import sys


TOOLS = [
    {
        "name": "echo",
        "description": "Echo the message back.",
        "inputSchema": {
            "type": "object",
            "properties": {"message": {"type": "string"}},
            "required": ["message"],
        },
    },
    {
        "name": "add",
        "description": "Add two numbers and return the result as a string.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "a": {"type": "number"},
                "b": {"type": "number"},
            },
            "required": ["a", "b"],
        },
    },
]


def _send(obj: dict) -> None:
    print(json.dumps(obj), flush=True)


def _ok(msg_id: int, result: object) -> None:
    _send({"jsonrpc": "2.0", "id": msg_id, "result": result})


def _err(msg_id: int, code: int, message: str) -> None:
    _send({"jsonrpc": "2.0", "id": msg_id, "error": {"code": code, "message": message}})


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--exit-after-init", action="store_true")
    parser.add_argument("--error-on-call", action="store_true")
    args = parser.parse_args()

    for raw_line in sys.stdin:
        raw_line = raw_line.strip()
        if not raw_line:
            continue

        try:
            msg = json.loads(raw_line)
        except json.JSONDecodeError:
            continue

        # Notifications have no id — handle then move on.
        if "id" not in msg:
            method = msg.get("method", "")
            if method == "notifications/initialized" and args.exit_after_init:
                sys.exit(0)
            continue

        msg_id = msg["id"]
        method = msg.get("method", "")

        if method == "initialize":
            _ok(
                msg_id,
                {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": "mock-mcp", "version": "0.1.0"},
                },
            )

        elif method == "tools/list":
            _ok(msg_id, {"tools": TOOLS})

        elif method == "tools/call":
            params = msg.get("params") or {}
            tool_name = params.get("name", "")
            tool_args = params.get("arguments") or {}

            if args.error_on_call:
                _ok(
                    msg_id,
                    {
                        "content": [{"type": "text", "text": "simulated tool error"}],
                        "isError": True,
                    },
                )
                continue

            if tool_name == "echo":
                text = str(tool_args.get("message", ""))
                _ok(
                    msg_id,
                    {"content": [{"type": "text", "text": text}], "isError": False},
                )
            elif tool_name == "add":
                result = tool_args.get("a", 0) + tool_args.get("b", 0)
                _ok(
                    msg_id,
                    {
                        "content": [{"type": "text", "text": str(result)}],
                        "isError": False,
                    },
                )
            else:
                _err(msg_id, -32601, f"Unknown tool: {tool_name}")

        else:
            _err(msg_id, -32601, f"Method not found: {method}")


if __name__ == "__main__":
    main()
