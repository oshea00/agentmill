"""Telemetry: OTel tracing initialisation, JSONL file export, and console logging.

Standalone module — no imports from other framework modules.

Usage
-----
Call ``init_tracing()`` once at process startup, then obtain per-module
tracers and loggers::

    from orchestrator.telemetry import init_tracing, get_logger
    from opentelemetry import trace

    init_tracing("agent_orchestrator")
    tracer = trace.get_tracer(__name__)
    log = get_logger("agent/3f2a1c8d-...")

For cross-agent context propagation use OTel's standard helpers directly::

    from opentelemetry.propagate import inject, extract
"""

from __future__ import annotations

import json
import logging
import os
import sys
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, Sequence

from opentelemetry import trace
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import ReadableSpan, TracerProvider
from opentelemetry.sdk.trace.export import (
    BatchSpanProcessor,
    SimpleSpanProcessor,
    SpanExporter,
    SpanExportResult,
)
from opentelemetry.trace import SpanKind, StatusCode

# ---------------------------------------------------------------------------
# Internal constants
# ---------------------------------------------------------------------------

_SPAN_KIND_NAME: dict[SpanKind, str] = {
    SpanKind.INTERNAL: "INTERNAL",
    SpanKind.SERVER: "SERVER",
    SpanKind.CLIENT: "CLIENT",
    SpanKind.PRODUCER: "PRODUCER",
    SpanKind.CONSUMER: "CONSUMER",
}

_STATUS_CODE_NAME: dict[StatusCode, str] = {
    StatusCode.UNSET: "UNSET",
    StatusCode.OK: "OK",
    StatusCode.ERROR: "ERROR",
}

# Standard LogRecord fields — excluded from the key=value extras column.
_STDLIB_LOG_ATTRS = frozenset({
    "args", "created", "exc_info", "exc_text", "filename", "funcName",
    "levelname", "levelno", "lineno", "message", "module", "msecs",
    "msg", "name", "pathname", "process", "processName", "relativeCreated",
    "stack_info", "taskName", "thread", "threadName",
})

# ---------------------------------------------------------------------------
# JSONL file exporter
# ---------------------------------------------------------------------------


class JsonlFileExporter(SpanExporter):
    """Write one span per line as JSON into ``<traces_dir>/YYYY-MM-DD.jsonl``.

    Files rotate at UTC midnight.  Thread-safe: a single lock serialises
    writes across all threads.
    """

    def __init__(self, traces_dir: str = "./traces") -> None:
        self._dir = Path(traces_dir)
        self._dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._shutdown = False

    # ------------------------------------------------------------------
    # SpanExporter interface
    # ------------------------------------------------------------------

    def export(self, spans: Sequence[ReadableSpan]) -> SpanExportResult:
        if self._shutdown:
            return SpanExportResult.FAILURE
        try:
            lines_by_date: dict[str, list[str]] = {}
            for span in spans:
                date_key = _utc_date(span.end_time or span.start_time)
                lines_by_date.setdefault(date_key, []).append(
                    json.dumps(_span_to_dict(span))
                )
            with self._lock:
                for date_key, lines in lines_by_date.items():
                    path = self._dir / f"{date_key}.jsonl"
                    with path.open("a", encoding="utf-8") as fh:
                        fh.write("\n".join(lines) + "\n")
            return SpanExportResult.SUCCESS
        except Exception:  # noqa: BLE001
            return SpanExportResult.FAILURE

    def shutdown(self) -> None:
        self._shutdown = True

    def force_flush(self, timeout_millis: int = 30_000) -> bool:  # noqa: ARG002
        return True


def _utc_date(nanos: Optional[int]) -> str:
    """Convert nanosecond epoch timestamp to ``YYYY-MM-DD`` UTC string."""
    if nanos is None:
        return datetime.now(tz=timezone.utc).strftime("%Y-%m-%d")
    return datetime.fromtimestamp(nanos / 1e9, tz=timezone.utc).strftime("%Y-%m-%d")


def _span_to_dict(span: ReadableSpan) -> dict:
    ctx = span.context
    trace_id = format(ctx.trace_id, "032x") if ctx else ""
    span_id = format(ctx.span_id, "016x") if ctx else ""
    parent_id = (
        format(span.parent.span_id, "016x")
        if span.parent and span.parent.span_id
        else None
    )
    start_s = span.start_time / 1e9 if span.start_time else None
    end_s = span.end_time / 1e9 if span.end_time else None
    duration_ms = (
        (span.end_time - span.start_time) / 1e6
        if span.start_time and span.end_time
        else None
    )
    return {
        "trace_id": trace_id,
        "span_id": span_id,
        "parent_span_id": parent_id,
        "name": span.name,
        "kind": _SPAN_KIND_NAME.get(span.kind, str(span.kind)),
        "start_time": start_s,
        "end_time": end_s,
        "duration_ms": round(duration_ms, 3) if duration_ms is not None else None,
        "status": {
            "code": _STATUS_CODE_NAME.get(span.status.status_code, "UNSET"),
            "description": span.status.description or "",
        },
        "attributes": dict(span.attributes) if span.attributes else {},
        "events": [
            {
                "name": e.name,
                "timestamp": e.timestamp / 1e9 if e.timestamp else None,
                "attributes": dict(e.attributes) if e.attributes else {},
            }
            for e in (span.events or [])
        ],
        "resource": dict(span.resource.attributes) if span.resource else {},
    }


# ---------------------------------------------------------------------------
# Tracing initialisation
# ---------------------------------------------------------------------------


def init_tracing(
    service_name: str,
    otlp_endpoint: Optional[str] = None,
    traces_dir: str = "./traces",
) -> trace.Tracer:
    """Initialise the global OTel ``TracerProvider`` and return a tracer.

    Always installs a :class:`JsonlFileExporter` writing to *traces_dir*.
    If *otlp_endpoint* is set, also exports to that OTLP HTTP endpoint via a
    :class:`BatchSpanProcessor`.

    Call once at process startup before any spans are created.
    """
    resource = Resource.create({"service.name": service_name})
    provider = TracerProvider(resource=resource)

    # Local JSONL — always on.
    provider.add_span_processor(
        SimpleSpanProcessor(JsonlFileExporter(traces_dir=traces_dir))
    )

    # Remote OTLP HTTP — optional.
    if otlp_endpoint:
        try:
            from opentelemetry.exporter.otlp.proto.http.trace_exporter import (  # noqa: PLC0415
                OTLPSpanExporter,
            )
            provider.add_span_processor(
                BatchSpanProcessor(OTLPSpanExporter(endpoint=otlp_endpoint))
            )
        except ImportError:
            logging.getLogger(__name__).warning(
                "opentelemetry-exporter-otlp-proto-http is not installed; "
                "remote OTLP export to %s is disabled",
                otlp_endpoint,
            )

    trace.set_tracer_provider(provider)
    return trace.get_tracer(service_name)


# ---------------------------------------------------------------------------
# Console logging
# ---------------------------------------------------------------------------


class ConsoleFormatter(logging.Formatter):
    """Single-line, human-readable log formatter.

    Output format::

        [HH:MM:SS] LEVEL    agent/<short-id>  MESSAGE  key=value key=value
    """

    def format(self, record: logging.LogRecord) -> str:
        record.message = record.getMessage()

        ts = datetime.fromtimestamp(record.created, tz=timezone.utc).strftime("%H:%M:%S")
        level = f"{record.levelname:<8}"
        name = _format_logger_name(record.name)
        msg = record.message

        extras = {
            k: v
            for k, v in record.__dict__.items()
            if k not in _STDLIB_LOG_ATTRS and not k.startswith("_")
        }

        parts = [f"[{ts}]", level, f"{name:<14}", msg]
        if extras:
            parts.append("  ".join(f"{k}={_quote_value(v)}" for k, v in extras.items()))

        line = "  ".join(parts)

        if record.exc_info:
            line += "\n" + self.formatException(record.exc_info)

        return line


def _format_logger_name(name: str) -> str:
    """Return a compact display name for the logger.

    ``agent/3f2a1c8d-...`` → ``agent/3f2a``.
    Other names are returned as-is (truncated to 14 chars if needed).
    """
    if "/" in name:
        prefix, _, rest = name.partition("/")
        return f"{prefix}/{rest[:4]}"
    return name[:14]


def _quote_value(v: object) -> str:
    """Format a log extra value; quote strings that contain spaces."""
    if isinstance(v, str) and (" " in v or not v):
        return f'"{v}"'
    return str(v)


def get_logger(name: str) -> logging.Logger:
    """Return a :class:`logging.Logger` with :class:`ConsoleFormatter` attached.

    The logger level is set from the ``LOG_LEVEL`` environment variable
    (default ``INFO``).  The handler is added only once, so this function is
    safe to call multiple times with the same *name*.
    """
    logger = logging.getLogger(name)
    if not logger.handlers:
        handler = logging.StreamHandler(sys.stdout)
        handler.setFormatter(ConsoleFormatter())
        logger.addHandler(handler)
    level_name = os.environ.get("LOG_LEVEL", "INFO").upper()
    logger.setLevel(getattr(logging, level_name, logging.INFO))
    logger.propagate = False
    return logger
