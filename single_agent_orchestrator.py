import asyncio
import anthropic
from opentelemetry import trace

from orchestrator.telemetry import init_tracing, get_logger
from orchestrator.db import get_connection, init_schema
from orchestrator.agent import Agent, AgentRole, AgentState, AgentRepository
from orchestrator.bus import Message, MessageBus, MessageType
from orchestrator.registry import ToolRegistry, tool
from orchestrator.runner import MessagesAPIRunner, run_worker

# --- Tool definition ---

@tool(
    name="echo",
    description="Return the input string unchanged.",
    schema={
        "type": "object",
        "properties": {"text": {"type": "string"}},
        "required": ["text"],
    },
)
async def echo(input: dict) -> str:
    return input["text"]


async def main():
    init_tracing("example")
    tracer = trace.get_tracer("example")
    logger = get_logger("example")

    conn = get_connection("./agent_orchestrator.db")
    init_schema(conn)
    repo = AgentRepository(conn)
    bus = MessageBus(conn)

    # Create an orchestrator and a worker in the same cluster
    orch = Agent(role=AgentRole.ORCHESTRATOR, cluster_id="demo")
    repo.create(orch)
    repo.transition(orch.id, AgentState.INITIALIZING)
    repo.transition(orch.id, AgentState.IDLE)

    worker = Agent(
        role=AgentRole.WORKER,
        cluster_id="demo",
        parent_id=orch.id,
        context={
            "execution_mode": "messages_api",
            "system_prompt": "You are a helpful assistant.",
            "max_iterations": 5,
            "max_tool_calls": 10,
        },
    )
    repo.create(worker)
    repo.transition(worker.id, AgentState.INITIALIZING)
    repo.transition(worker.id, AgentState.IDLE)

    # Build the runner with the echo tool
    registry = ToolRegistry(agent_id=worker.id)
    registry.register_native(echo._tool_meta)

    runner = MessagesAPIRunner(
        agent_id=worker.id,
        client=anthropic.AsyncAnthropic(),
        registry=registry,
        tracer=tracer,
        logger=logger,
    )

    # Send a task
    await bus.send(Message(
        type=MessageType.TASK_ASSIGN,
        sender_id=orch.id,
        recipient_id=worker.id,
        cluster_id="demo",
        payload={"task_id": "t-001", "description": "Echo back: hello world"},
    ))

    # Run one task cycle
    await run_worker(
        worker, bus, repo, tracer, logger,
        api_runner=runner,
        receive_timeout=10.0,
    )

    # Collect the result
    result = await bus.receive(orch.id, timeout=5.0)
    if result:
        print("Output:", result.payload["output"])
        await bus.acknowledge(result.id)


asyncio.run(main())
