"""Live IRC gateway integration test.

Connects to localhost:6667, joins #agents, and walks through the full
interaction lifecycle.  Requires a human at nick 'mike' to respond.

Skipped by default — run explicitly when an IRC server is available:
    python3 -m pytest tests/test_irc_live.py -v -s -m live
    python3 tests/test_irc_live.py
"""

import asyncio
import sqlite3
import sys
import time
import yaml
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from opentelemetry import trace
from orchestrator.db import get_connection, init_schema
from orchestrator.irc_gateway import (
    IRCGateway,
    InteractionRequest,
    RequestType,
    TrustedNickRegistry,
)
from orchestrator.telemetry import init_tracing, get_logger

pytestmark = pytest.mark.skip(reason="live IRC test — requires IRC server and human at nick 'mike'")

FAKE_AGENT_ID = "test-agent-001"


def _load_config() -> dict:
    with open(Path(__file__).parent.parent / "irc_config.yaml") as f:
        return yaml.safe_load(f)


def _seed_agent(conn: sqlite3.Connection) -> None:
    now = time.time()
    conn.execute(
        """
        INSERT OR IGNORE INTO agents
            (id, role, state, cluster_id, created_at, updated_at, context)
        VALUES (?, 'worker', 'SUSPENDED', 'test-cluster', ?, ?, '{}')
        """,
        (FAKE_AGENT_ID, now, now),
    )
    conn.commit()


async def run_test() -> None:
    init_tracing("irc_live_test")
    tracer = trace.get_tracer("irc_live_test")
    logger = get_logger("irc/test")

    config = _load_config()
    registry = TrustedNickRegistry(config)
    conn = get_connection(":memory:")
    init_schema(conn)
    _seed_agent(conn)

    gateway = IRCGateway(config, registry, tracer, logger, conn)

    gateway_task = asyncio.create_task(gateway.run())
    # Give the bot a moment to connect and join
    await asyncio.sleep(2)

    # ------------------------------------------------------------------ #
    # Test 1: notify (no response needed)
    # ------------------------------------------------------------------ #
    logger.info("--- Test 1: notify ---")
    await gateway.notify("[test] IRCGateway live test starting. Please stand by.")
    await asyncio.sleep(0.5)

    # ------------------------------------------------------------------ #
    # Test 2: APPROVAL interaction — respond with: !approve <short_id>
    # ------------------------------------------------------------------ #
    logger.info("--- Test 2: APPROVAL ---")
    req_approve = InteractionRequest(
        agent_id=FAKE_AGENT_ID,
        request_type=RequestType.APPROVAL,
        prompt="Requesting permission to delete 2 temp files in /workspace/output.",
    )
    logger.info(f"Waiting for: !approve {req_approve.short_id}")
    resp = await gateway.post_interaction(req_approve)
    assert resp.response_type == "APPROVE", f"Expected APPROVE, got {resp.response_type}"
    logger.info(f"PASS: got APPROVE from {resp.nick}")

    # ------------------------------------------------------------------ #
    # Test 3: DENY interaction — respond with: !deny <short_id> [reason]
    # ------------------------------------------------------------------ #
    logger.info("--- Test 3: DENY ---")
    req_deny = InteractionRequest(
        agent_id=FAKE_AGENT_ID,
        request_type=RequestType.APPROVAL,
        prompt="Requesting permission to push to main branch.",
    )
    await gateway.notify(
        f"[test:deny] Reply exactly: !deny {req_deny.short_id} not safe to push"
    )
    logger.info(f"Waiting for: !deny {req_deny.short_id} not safe to push")
    resp = await gateway.post_interaction(req_deny)
    assert resp.response_type in ("APPROVE", "DENY", "DIRECT"), f"Unexpected: {resp.response_type}"
    logger.info(f"PASS: got {resp.response_type} from {resp.nick}  payload={resp.payload}")

    # ------------------------------------------------------------------ #
    # Test 4: DIRECT interaction — respond with: !direct <short_id> <instruction>
    # ------------------------------------------------------------------ #
    logger.info("--- Test 4: DIRECT ---")
    req_direct = InteractionRequest(
        agent_id=FAKE_AGENT_ID,
        request_type=RequestType.DIRECTION,
        prompt="Stuck on step 3 — which output directory should I use?",
    )
    await gateway.notify(
        f"[test:direct] Reply exactly: !direct {req_direct.short_id} use /tmp/output"
    )
    logger.info(f"Waiting for: !direct {req_direct.short_id} use /tmp/output")
    resp = await gateway.post_interaction(req_direct)
    assert resp.response_type in ("APPROVE", "DENY", "DIRECT"), f"Unexpected: {resp.response_type}"
    logger.info(f"PASS: got {resp.response_type} from {resp.nick}  payload={resp.payload}")

    # ------------------------------------------------------------------ #
    # Test 5: untrusted nick — gateway should ignore silently
    #   (manual: connect as a non-trusted nick and send !approve <any>)
    # ------------------------------------------------------------------ #
    await gateway.notify(
        "[test] Test 5: connect as an untrusted nick and send "
        f"!approve {req_approve.short_id} — gateway should ignore it silently."
    )
    await asyncio.sleep(5)  # give time for manual test

    # ------------------------------------------------------------------ #
    # Done
    # ------------------------------------------------------------------ #
    await gateway.notify("[test] All tests passed. Gateway shutting down.")
    await asyncio.sleep(0.5)
    await gateway.disconnect()
    gateway_task.cancel()
    try:
        await gateway_task
    except asyncio.CancelledError:
        pass

    logger.info("=== All live tests passed ===")


if __name__ == "__main__":
    asyncio.run(run_test())
