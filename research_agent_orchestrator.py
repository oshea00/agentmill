"""Research agent example: wire up a researcher worker and run one task."""

import asyncio
import os

import anthropic

from orchestrator.agent import Agent, AgentRepository, AgentRole, AgentState
from orchestrator.bus import Message, MessageBus, MessageType
from orchestrator.config import AgentConfigLoader, resolve_system_prompt
from orchestrator.db import get_connection, init_schema
from orchestrator.mcp import MCPServerManager, MCPStartupError, load_mcp_registry
from orchestrator.registry import ToolRegistry
from orchestrator.runner import MessagesAPIRunner, run_worker
from orchestrator.telemetry import get_logger, init_tracing


async def main() -> None:
    tracer = init_tracing("research_example")
    logger = get_logger("orchestrator")

    conn = get_connection()
    init_schema(conn)
    repo = AgentRepository(conn)
    bus = MessageBus(conn)

    # Load researcher template and build system prompt
    loader = AgentConfigLoader()
    cfg = loader.load_template("researcher")
    system_prompt = resolve_system_prompt(
        cfg.system_prompt,
        {"task_description": "What are the main features of Python 3.13?"},
    )

    # Create orchestrator agent (receives results from the worker)
    orchestrator = Agent(role=AgentRole.ORCHESTRATOR, cluster_id="cluster-1")
    repo.create(orchestrator)
    repo.transition(orchestrator.id, AgentState.INITIALIZING)
    repo.transition(orchestrator.id, AgentState.IDLE)

    # Create researcher worker agent
    worker = Agent(
        role=AgentRole.WORKER,
        cluster_id="cluster-1",
        parent_id=orchestrator.id,
        context={
            "execution_mode": cfg.execution_mode,
            "system_prompt": system_prompt,
            "max_iterations": cfg.limits["max_iterations"],
            "max_tool_calls": cfg.limits["max_tool_calls"],
            "mcp_servers": cfg.tools_mcp,
        },
    )
    repo.create(worker)
    repo.transition(worker.id, AgentState.INITIALIZING)
    repo.transition(worker.id, AgentState.IDLE)

    # Start MCP servers listed in the template (skip any not in mcp_servers.json)
    mcp_mgr = MCPServerManager()
    try:
        registry = load_mcp_registry("mcp_servers.json")
        for server_name in cfg.tools_mcp:
            if server_name in registry:
                spec = registry[server_name]
                await mcp_mgr.start(server_name, spec["command"], env=spec.get("env"))
                logger.info("mcp.started", extra={"server": server_name})
            else:
                logger.warning("mcp.not_configured", extra={"server": server_name})
    except MCPStartupError as exc:
        logger.warning("mcp.startup_failed", extra={"error": str(exc)})

    # Register MCP tools in the tool registry
    tool_registry = ToolRegistry(worker.id)
    for server_name in cfg.tools_mcp:
        try:
            discovered = await mcp_mgr.list_tools(server_name)
            logger.info(
                "mcp.tools_discovered",
                extra={
                    "server": server_name,
                    "count": len(discovered),
                    "names": ",".join(sorted(t.name for t in discovered)),
                },
            )
            await tool_registry.register_mcp(server_name, mcp_mgr)
            logger.info(
                "mcp.tools_registered",
                extra={
                    "server": server_name,
                    "names": ",".join(
                        sorted(d["name"] for d in tool_registry.list_definitions())
                    ),
                },
            )
        except Exception as exc:
            logger.warning(
                "mcp.register_failed",
                extra={"server": server_name, "error": str(exc)},
            )

    logger.info(
        "worker.tools_ready",
        extra={
            "count": len(tool_registry.list_definitions()),
            "names": ",".join(
                sorted(d["name"] for d in tool_registry.list_definitions())
            ),
        },
    )

    # Build the Messages API runner
    client = anthropic.AsyncAnthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
    api_runner = MessagesAPIRunner(
        agent_id=worker.id,
        client=client,
        registry=tool_registry,
        tracer=tracer,
        logger=logger,
    )

    # Send the task to the worker
    await bus.send(
        Message(
            type=MessageType.TASK_ASSIGN,
            sender_id=orchestrator.id,
            recipient_id=worker.id,
            cluster_id="cluster-1",
            payload={
                "task_id": "task-001",
                "description": "What are the main features of Python 3.13?",
            },
        )
    )

    # Run the worker: IDLE → RUNNING → process task → IDLE
    await run_worker(
        agent=worker,
        bus=bus,
        repo=repo,
        tracer=tracer,
        logger=logger,
        api_runner=api_runner,
    )

    # Receive and print the result
    result_msg = await bus.receive(orchestrator.id, timeout=5.0)
    if result_msg is None:
        logger.warning("orchestrator.no_result")
    elif result_msg.type == MessageType.TASK_RESULT:
        await bus.acknowledge(result_msg.id)
        print("\n--- Research Result ---")
        print(result_msg.payload.get("output", ""))
    elif result_msg.type == MessageType.TASK_ERROR:
        await bus.acknowledge(result_msg.id)
        print(f"Task failed: {result_msg.payload.get('message')}")

    # Terminate the worker
    await bus.send(
        Message(
            type=MessageType.CONTROL,
            sender_id=orchestrator.id,
            recipient_id=worker.id,
            cluster_id="cluster-1",
            payload={"command": "TERMINATE", "reason": "task_complete"},
        )
    )
    await run_worker(
        agent=worker,
        bus=bus,
        repo=repo,
        tracer=tracer,
        logger=logger,
        api_runner=api_runner,
    )

    await mcp_mgr.stop_all()
    logger.info("done")


if __name__ == "__main__":
    asyncio.run(main())
