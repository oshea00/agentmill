# Spec: Observability, Tracing & Logging

## Overview

Observability is built on two outputs: OpenTelemetry (OTel) traces for
structured, machine-readable telemetry, and human-readable console output for
developer experience. Both are always active. There are no metrics endpoints or
external collectors required — OTel traces are written to a local OTLP file
exporter by default, with an optional OTLP HTTP endpoint for integration with
Jaeger, Honeycomb, or similar.

---

## Instrumentation Layers

| Layer | What is instrumented |
|---|---|
| Agent lifecycle | Every state transition; span per agent run |
| Message bus | Every `send`, `receive`, `acknowledge`; span per message |
| Tool calls | Every tool invocation; span per call with input/output |
| Claude API calls | Every `messages.create` call; span with model, tokens, stop reason |
| MCP server ops | Server start/stop, `tools/list`, `tool/call` |

---

## OTel Setup

### Initialisation (call once at process startup)

```python
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.resources import Resource

def init_tracing(service_name: str, otlp_endpoint: str = None) -> trace.Tracer:
    resource = Resource.create({"service.name": service_name})
    provider = TracerProvider(resource=resource)

    # Always export to a local OTLP file (JSON Lines)
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter as HTTPExporter
    # ... file exporter setup

    # Optionally export to remote collector
    if otlp_endpoint:
        provider.add_span_processor(
            BatchSpanProcessor(OTLPSpanExporter(endpoint=otlp_endpoint))
        )

    trace.set_tracer_provider(provider)
    return trace.get_tracer(service_name)
```

### Tracer access

```python
# Module-level, resolved at import time after init_tracing() is called
tracer = trace.get_tracer("agent_orchestrator")
```

---

## Span Conventions

All spans follow OpenTelemetry semantic conventions where applicable. Custom
attributes use the `agent.` namespace.

### Common attributes (all spans)

| Attribute | Type | Example |
|---|---|---|
| `agent.id` | string | `"3f2a...uuid"` |
| `agent.role` | string | `"orchestrator"` |
| `agent.cluster_id` | string | `"cluster-abc"` |

### Agent lifecycle span

```
Name: agent.run
Kind: INTERNAL
```

| Attribute | Value |
|---|---|
| `agent.state.from` | previous state |
| `agent.state.to` | new state |
| `agent.transition.reason` | human-readable reason string |

Transitions are emitted as **span events** on the parent `agent.run` span, not
as child spans — this keeps the trace tree flat and readable.

```python
with tracer.start_as_current_span("agent.run", attributes={"agent.id": agent.id}) as span:
    span.add_event("state_transition", attributes={
        "agent.state.from": from_state,
        "agent.state.to": to_state,
        "agent.transition.reason": reason,
    })
```

### Claude API call span

```
Name: llm.call
Kind: CLIENT
```

| Attribute | Value |
|---|---|
| `llm.model` | `"claude-opus-4-5"` |
| `llm.input_tokens` | integer |
| `llm.output_tokens` | integer |
| `llm.stop_reason` | `"end_turn"` / `"tool_use"` / `"max_tokens"` |
| `llm.turn_index` | integer (0-based turn within task) |

### Tool call span

```
Name: tool.call
Kind: INTERNAL
Parent: llm.call (the turn that triggered it)
```

| Attribute | Value |
|---|---|
| `tool.name` | `"read_file"` |
| `tool.source` | `"native"` / `"mcp"` |
| `tool.mcp_server` | server name if MCP |
| `tool.duration_ms` | float |
| `tool.is_error` | boolean |

Tool inputs and outputs are **not** added as span attributes (may contain
sensitive data). They are logged at DEBUG level by the console logger instead.

### Message bus span

```
Name: message.send / message.receive
Kind: PRODUCER / CONSUMER
```

| Attribute | Value |
|---|---|
| `message.id` | UUID |
| `message.type` | `MessageType` value |
| `message.sender_id` | agent UUID |
| `message.recipient_id` | agent UUID or `"broadcast"` |

Use OTel context propagation (`TraceContextTextMapPropagator`) to link
`message.send` and `message.receive` spans across agents into a single trace.
Inject the trace context into `message.payload._otel_context` and extract it
on receive.

---

## Console Logging

### Logger setup

```python
import logging
import sys

def get_logger(name: str) -> logging.Logger:
    logger = logging.getLogger(name)
    if not logger.handlers:
        handler = logging.StreamHandler(sys.stdout)
        handler.setFormatter(ConsoleFormatter())
        logger.addHandler(handler)
    logger.setLevel(logging.DEBUG)
    return logger
```

### `ConsoleFormatter`

Human-readable, one line per event. Format:

```
[HH:MM:SS] LEVEL  agent/<short-id>  MESSAGE  {key=value ...}
```

Examples:
```
[14:03:01] INFO   agent/3f2a  State transition  from=IDLE to=RUNNING reason="task assigned"
[14:03:01] DEBUG  agent/3f2a  Tool call         tool=read_file source=native
[14:03:02] DEBUG  agent/3f2a  Tool result       tool=read_file duration_ms=12.4 is_error=False
[14:03:05] INFO   agent/3f2a  LLM turn          model=claude-opus-4-5 input_tokens=842 output_tokens=234 stop_reason=tool_use
[14:03:05] INFO   agent/3f2a  Message sent      type=TASK_RESULT recipient=orch/a1b2
[14:03:05] INFO   agent/3f2a  State transition  from=RUNNING to=IDLE
```

`short-id` is the first 4 characters of the agent UUID.

### Log levels

| Level | Used for |
|---|---|
| `DEBUG` | Tool inputs/outputs, raw message payloads, MCP protocol messages |
| `INFO` | State transitions, LLM turns, messages sent/received, MCP server start/stop |
| `WARNING` | Retried tool calls, MCP server restarts, messages not acknowledged within TTL |
| `ERROR` | Agent transitions to `FAILED`, unhandled exceptions |
| `CRITICAL` | Process-level failures (DB unreachable, tracer init failure) |

---

## Trace Propagation Across Agents

When an orchestrator spawns a worker, it passes the current OTel trace context
into the worker's `context.otel_trace_context` field. The worker uses this as
its root span parent, linking all worker activity into the orchestrator's trace.

```python
from opentelemetry.propagate import inject, extract

# Orchestrator: inject trace context before spawning worker
carrier = {}
inject(carrier)
worker_context["otel_trace_context"] = carrier

# Worker: extract and set as parent on startup
ctx = extract(agent.context.get("otel_trace_context", {}))
with tracer.start_as_current_span("agent.run", context=ctx, ...):
    ...
```

This produces a single unified trace per orchestrated task, with all worker
spans nested under the orchestrator's root span.

---

## Local Trace Storage

Spans are written to `./traces/` as JSON Lines (`.jsonl`), one file per day:

```
./traces/
  2026-03-28.jsonl
  2026-03-27.jsonl
```

This allows post-mortem inspection with `jq` or any OTel-compatible tool
without requiring a running collector.

---

## Design Decisions

| Decision | Choice | Rationale |
|---|---|---|
| Tracing library | OpenTelemetry SDK (official) | Vendor-neutral; works with Jaeger, Honeycomb, Datadog |
| Default exporter | Local JSONL file | Zero infra requirement for development |
| Console format | Single-line key=value | Grep-friendly; survives log aggregation |
| Tool I/O in traces | No — log only at DEBUG | Prevents sensitive data in trace backends |
| Cross-agent context | OTel context propagation via message payload | Standard approach; no custom linking logic |
| Metrics | Omitted from this version | OTel metrics can be layered on top of the existing tracer setup later |
