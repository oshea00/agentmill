"""Reusable task runner: template-driven agent lifecycle for one or many tasks."""

from __future__ import annotations

import os
from typing import Optional

import anthropic

from orchestrator.agent import Agent, AgentRepository, AgentRole, AgentState
from orchestrator.bus import Message, MessageBus, MessageType
from orchestrator.config import AgentConfigLoader, resolve_system_prompt
from orchestrator.db import get_connection, init_schema
from orchestrator.mcp import MCPServerManager, MCPStartupError, load_mcp_registry
from orchestrator.registry import ToolRegistry
from orchestrator.runner import MessagesAPIRunner, run_worker
from orchestrator.telemetry import get_logger, init_tracing


class TaskRunner:
    """Run one or more template-driven worker tasks sequentially.

    Infrastructure (DB, agents, MCP servers, tool registry, runner) is set up
    once.  Each task description is dispatched, executed, and collected in
    order.  The worker is terminated and MCP servers stopped after all tasks
    complete.

    Pass a single-item list for single-task usage.
    """

    def __init__(
        self,
        template_name: str,
        template_vars: dict[str, str],
        task_descriptions: list[str],
        *,
        task_id_prefix: str = "task",
        cluster_id: str = "cluster-1",
        mcp_registry_path: str = "mcp_servers.json",
        receive_timeout: float = 30.0,
    ) -> None:
        self._template_name = template_name
        self._template_vars = template_vars
        self._task_descriptions = task_descriptions
        self._task_id_prefix = task_id_prefix
        self._cluster_id = cluster_id
        self._mcp_registry_path = mcp_registry_path
        self._receive_timeout = receive_timeout

    async def run(self) -> list[Optional[str]]:
        """Execute all tasks and return a list of worker text outputs.

        Each element is None when no result was received within the timeout.
        Raises RuntimeError on the first TASK_ERROR encountered.
        """
        tracer = init_tracing(f"{self._template_name}_task")
        logger = get_logger("orchestrator")

        conn = get_connection()
        init_schema(conn)
        repo = AgentRepository(conn)
        bus = MessageBus(conn)

        # --- template + prompt ---
        cfg = AgentConfigLoader().load_template(self._template_name)
        system_prompt = resolve_system_prompt(cfg.system_prompt, self._template_vars)

        # --- agents ---
        orchestrator = Agent(role=AgentRole.ORCHESTRATOR, cluster_id=self._cluster_id)
        repo.create(orchestrator)
        repo.transition(orchestrator.id, AgentState.INITIALIZING)
        repo.transition(orchestrator.id, AgentState.IDLE)

        worker = Agent(
            role=AgentRole.WORKER,
            cluster_id=self._cluster_id,
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

        # --- MCP + tools ---
        mcp_mgr = MCPServerManager()
        tool_registry = ToolRegistry(worker.id)
        try:
            registry = load_mcp_registry(self._mcp_registry_path)
            for name in cfg.tools_mcp:
                if name in registry:
                    spec = registry[name]
                    await mcp_mgr.start(name, spec["command"], env=spec.get("env"))
                    await tool_registry.register_mcp(name, mcp_mgr)
                    logger.info("mcp.started", extra={"server": name})
                else:
                    logger.warning("mcp.not_configured", extra={"server": name})
        except MCPStartupError as exc:
            logger.warning("mcp.startup_failed", extra={"error": str(exc)})

        # --- runner ---
        client = anthropic.AsyncAnthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
        api_runner = MessagesAPIRunner(
            agent_id=worker.id,
            client=client,
            registry=tool_registry,
            tracer=tracer,
            logger=logger,
        )

        # --- dispatch, execute, collect for each task ---
        results: list[Optional[str]] = []
        for idx, description in enumerate(self._task_descriptions, start=1):
            task_id = f"{self._task_id_prefix}-{idx:03d}"
            logger.info(
                "task.dispatch",
                extra={
                    "task_id": task_id,
                    "index": idx,
                    "total": len(self._task_descriptions),
                },
            )

            await bus.send(
                Message(
                    type=MessageType.TASK_ASSIGN,
                    sender_id=orchestrator.id,
                    recipient_id=worker.id,
                    cluster_id=self._cluster_id,
                    payload={"task_id": task_id, "description": description},
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

            result_msg = await bus.receive(
                orchestrator.id, timeout=self._receive_timeout
            )
            if result_msg is None:
                logger.warning("orchestrator.no_result", extra={"task_id": task_id})
                results.append(None)
            elif result_msg.type == MessageType.TASK_RESULT:
                await bus.acknowledge(result_msg.id)
                results.append(result_msg.payload.get("output", ""))
            elif result_msg.type == MessageType.TASK_ERROR:
                await bus.acknowledge(result_msg.id)
                raise RuntimeError(result_msg.payload.get("message", "task failed"))

        # --- teardown ---
        await bus.send(
            Message(
                type=MessageType.CONTROL,
                sender_id=orchestrator.id,
                recipient_id=worker.id,
                cluster_id=self._cluster_id,
                payload={"command": "TERMINATE", "reason": "all_tasks_complete"},
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
        logger.info("done", extra={"tasks_completed": len(results)})

        return results
