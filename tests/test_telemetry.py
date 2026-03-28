"""Tests for orchestrator/telemetry.py."""

from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path
from unittest.mock import patch

import pytest
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from orchestrator.telemetry import (
    ConsoleFormatter,
    JsonlFileExporter,
    _format_logger_name,
    _quote_value,
    _span_to_dict,
    get_logger,
    init_tracing,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def traces_dir(tmp_path):
    return tmp_path / "traces"


@pytest.fixture()
def file_exporter(traces_dir):
    return JsonlFileExporter(traces_dir=str(traces_dir))


@pytest.fixture()
def provider_with_memory():
    """A TracerProvider backed by an in-memory exporter — lets us inspect spans."""
    mem = InMemorySpanExporter()
    prov = TracerProvider()
    prov.add_span_processor(SimpleSpanProcessor(mem))
    return prov, mem


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_span(provider_with_memory, name="test.span", attrs=None):
    prov, mem = provider_with_memory
    tracer = prov.get_tracer("test")
    with tracer.start_as_current_span(name, attributes=attrs or {}) as span:
        pass  # span ends on exit
    return mem.get_finished_spans()[-1]


# ---------------------------------------------------------------------------
# JsonlFileExporter — directory creation
# ---------------------------------------------------------------------------


class TestJsonlFileExporter:
    def test_creates_traces_dir(self, tmp_path):
        d = tmp_path / "new" / "traces"
        assert not d.exists()
        JsonlFileExporter(traces_dir=str(d))
        assert d.is_dir()

    def test_export_writes_jsonl(self, provider_with_memory, traces_dir):
        exporter = JsonlFileExporter(traces_dir=str(traces_dir))
        span = _make_span(provider_with_memory, "agent.run", {"agent.id": "abc"})

        from opentelemetry.sdk.trace.export import SpanExportResult
        result = exporter.export([span])
        assert result == SpanExportResult.SUCCESS

        files = list(traces_dir.glob("*.jsonl"))
        assert len(files) == 1
        lines = files[0].read_text().strip().splitlines()
        assert len(lines) == 1
        obj = json.loads(lines[0])
        assert obj["name"] == "agent.run"
        assert obj["attributes"]["agent.id"] == "abc"

    def test_export_appends_multiple_spans(self, provider_with_memory, traces_dir):
        exporter = JsonlFileExporter(traces_dir=str(traces_dir))
        span1 = _make_span(provider_with_memory, "span.one")
        span2 = _make_span(provider_with_memory, "span.two")
        exporter.export([span1])
        exporter.export([span2])

        lines = list(traces_dir.glob("*.jsonl"))[0].read_text().strip().splitlines()
        assert len(lines) == 2
        names = {json.loads(l)["name"] for l in lines}
        assert names == {"span.one", "span.two"}

    def test_export_batch_writes_all(self, provider_with_memory, traces_dir):
        exporter = JsonlFileExporter(traces_dir=str(traces_dir))
        span1 = _make_span(provider_with_memory, "a")
        span2 = _make_span(provider_with_memory, "b")
        exporter.export([span1, span2])

        lines = list(traces_dir.glob("*.jsonl"))[0].read_text().strip().splitlines()
        assert len(lines) == 2

    def test_shutdown_returns_failure(self, traces_dir, provider_with_memory):
        exporter = JsonlFileExporter(traces_dir=str(traces_dir))
        exporter.shutdown()
        span = _make_span(provider_with_memory, "x")

        from opentelemetry.sdk.trace.export import SpanExportResult
        result = exporter.export([span])
        assert result == SpanExportResult.FAILURE

    def test_force_flush_returns_true(self, file_exporter):
        assert file_exporter.force_flush() is True


# ---------------------------------------------------------------------------
# _span_to_dict — serialisation
# ---------------------------------------------------------------------------


class TestSpanToDict:
    def test_required_keys_present(self, provider_with_memory):
        span = _make_span(provider_with_memory, "agent.run")
        d = _span_to_dict(span)
        for key in ("trace_id", "span_id", "name", "kind", "start_time",
                    "end_time", "duration_ms", "status", "attributes",
                    "events", "resource"):
            assert key in d, f"missing key: {key}"

    def test_name_matches(self, provider_with_memory):
        span = _make_span(provider_with_memory, "tool.call")
        assert _span_to_dict(span)["name"] == "tool.call"

    def test_trace_id_is_hex_string(self, provider_with_memory):
        span = _make_span(provider_with_memory)
        d = _span_to_dict(span)
        assert len(d["trace_id"]) == 32
        int(d["trace_id"], 16)  # must be valid hex

    def test_span_id_is_hex_string(self, provider_with_memory):
        span = _make_span(provider_with_memory)
        d = _span_to_dict(span)
        assert len(d["span_id"]) == 16
        int(d["span_id"], 16)

    def test_duration_ms_positive(self, provider_with_memory):
        span = _make_span(provider_with_memory)
        d = _span_to_dict(span)
        assert d["duration_ms"] is not None
        assert d["duration_ms"] >= 0

    def test_attributes_preserved(self, provider_with_memory):
        span = _make_span(provider_with_memory, attrs={"agent.id": "xyz", "agent.role": "worker"})
        d = _span_to_dict(span)
        assert d["attributes"]["agent.id"] == "xyz"
        assert d["attributes"]["agent.role"] == "worker"

    def test_no_parent_is_none(self, provider_with_memory):
        span = _make_span(provider_with_memory)
        assert _span_to_dict(span)["parent_span_id"] is None

    def test_parent_span_id_set_for_child(self, provider_with_memory):
        prov, mem = provider_with_memory
        tracer = prov.get_tracer("test")
        with tracer.start_as_current_span("parent") as parent:
            with tracer.start_as_current_span("child"):
                pass
        spans = mem.get_finished_spans()
        child = next(s for s in spans if s.name == "child")
        d = _span_to_dict(child)
        assert d["parent_span_id"] is not None
        assert len(d["parent_span_id"]) == 16

    def test_events_serialised(self, provider_with_memory):
        prov, mem = provider_with_memory
        tracer = prov.get_tracer("test")
        with tracer.start_as_current_span("agent.run") as span:
            span.add_event("state_transition", attributes={
                "agent.state.from": "IDLE",
                "agent.state.to": "RUNNING",
            })
        finished = mem.get_finished_spans()[-1]
        d = _span_to_dict(finished)
        assert len(d["events"]) == 1
        ev = d["events"][0]
        assert ev["name"] == "state_transition"
        assert ev["attributes"]["agent.state.from"] == "IDLE"

    def test_status_code_string(self, provider_with_memory):
        span = _make_span(provider_with_memory)
        d = _span_to_dict(span)
        assert d["status"]["code"] in ("UNSET", "OK", "ERROR")

    def test_resource_attributes_included(self, provider_with_memory):
        span = _make_span(provider_with_memory)
        d = _span_to_dict(span)
        assert isinstance(d["resource"], dict)

    def test_kind_string(self, provider_with_memory):
        span = _make_span(provider_with_memory)
        d = _span_to_dict(span)
        assert d["kind"] in ("INTERNAL", "SERVER", "CLIENT", "PRODUCER", "CONSUMER")


# ---------------------------------------------------------------------------
# init_tracing
# ---------------------------------------------------------------------------


class TestInitTracing:
    # OTel's global TracerProvider can only be set once per process.  Only the
    # first test here calls the real init_tracing; the others either patch
    # set_tracer_provider or exercise the components directly.

    def test_returns_tracer(self, traces_dir):
        # First call in this process — actually sets the global provider.
        tracer = init_tracing("test-svc-init", traces_dir=str(traces_dir))
        assert tracer is not None

    def test_creates_traces_directory(self, tmp_path):
        d = tmp_path / "nested" / "traces"
        assert not d.exists()
        with patch("opentelemetry.trace.set_tracer_provider"):
            init_tracing("svc", traces_dir=str(d))
        assert d.is_dir()

    def test_writes_span_to_file(self, tmp_path):
        # Test the full pipeline (exporter → JSONL) via a local provider so we
        # don't fight the once-only global OTel state.
        d = tmp_path / "traces"
        exporter = JsonlFileExporter(traces_dir=str(d))
        prov = TracerProvider()
        prov.add_span_processor(SimpleSpanProcessor(exporter))
        tracer = prov.get_tracer("test")
        with tracer.start_as_current_span("smoke.span"):
            pass

        files = list(d.glob("*.jsonl"))
        assert files, "no JSONL file created"
        line = json.loads(files[0].read_text().strip().splitlines()[-1])
        assert line["name"] == "smoke.span"

    def test_resource_service_name(self, tmp_path):
        d = tmp_path / "traces"
        exporter = JsonlFileExporter(traces_dir=str(d))
        resource = Resource.create({"service.name": "my-service"})
        prov = TracerProvider(resource=resource)
        prov.add_span_processor(SimpleSpanProcessor(exporter))
        with prov.get_tracer("my-service").start_as_current_span("check.resource"):
            pass
        line = json.loads(list(d.glob("*.jsonl"))[0].read_text().strip())
        assert line["resource"].get("service.name") == "my-service"

    def test_otlp_endpoint_skipped_gracefully_if_not_available(self, tmp_path):
        """Even if the OTLP exporter import fails, init_tracing should not raise."""
        import builtins
        real_import = builtins.__import__

        def block_otlp(name, *args, **kwargs):
            if "otlp" in name.lower():
                raise ImportError("simulated missing package")
            return real_import(name, *args, **kwargs)

        with patch("builtins.__import__", side_effect=block_otlp):
            with patch("opentelemetry.trace.set_tracer_provider"):
                tracer = init_tracing("svc", otlp_endpoint="http://localhost:4318",
                                      traces_dir=str(tmp_path / "traces"))
        assert tracer is not None


# ---------------------------------------------------------------------------
# ConsoleFormatter
# ---------------------------------------------------------------------------


class TestConsoleFormatter:
    @pytest.fixture()
    def formatter(self):
        return ConsoleFormatter()

    def _make_record(self, msg="hello", name="agent/abc123", level=logging.INFO, **extra):
        record = logging.LogRecord(
            name=name, level=level, pathname="", lineno=0,
            msg=msg, args=(), exc_info=None,
        )
        for k, v in extra.items():
            setattr(record, k, v)
        return record

    def test_timestamp_in_brackets(self, formatter):
        record = self._make_record()
        line = formatter.format(record)
        assert line.startswith("[")
        # First token should be [HH:MM:SS]
        token = line.split()[0]
        assert token.startswith("[") and token.endswith("]")
        inner = token[1:-1]
        parts = inner.split(":")
        assert len(parts) == 3
        assert all(p.isdigit() for p in parts)

    def test_level_name_present(self, formatter):
        record = self._make_record(level=logging.INFO)
        line = formatter.format(record)
        assert "INFO" in line

    def test_debug_level_present(self, formatter):
        record = self._make_record(level=logging.DEBUG)
        line = formatter.format(record)
        assert "DEBUG" in line

    def test_warning_level_present(self, formatter):
        record = self._make_record(level=logging.WARNING)
        line = formatter.format(record)
        assert "WARNING" in line

    def test_message_present(self, formatter):
        record = self._make_record(msg="State transition")
        line = formatter.format(record)
        assert "State transition" in line

    def test_extra_kv_appended(self, formatter):
        record = self._make_record(tool="read_file", source="native")
        line = formatter.format(record)
        assert "tool=read_file" in line
        assert "source=native" in line

    def test_extra_string_with_spaces_is_quoted(self, formatter):
        record = self._make_record(reason="task assigned")
        line = formatter.format(record)
        assert 'reason="task assigned"' in line

    def test_no_extra_fields_no_trailing_junk(self, formatter):
        record = self._make_record(msg="clean message")
        line = formatter.format(record)
        # Should not contain '=' outside of the timestamp
        stripped = line.split("]", 1)[1]
        assert "=" not in stripped

    def test_short_agent_name_displayed(self, formatter):
        record = self._make_record(name="agent/3f2a1c8d-0000-0000-0000-000000000000")
        line = formatter.format(record)
        assert "agent/3f2a" in line

    def test_non_agent_name_displayed(self, formatter):
        record = self._make_record(name="orchestrator")
        line = formatter.format(record)
        assert "orchestrator" in line

    def test_exception_info_appended(self, formatter):
        try:
            raise ValueError("boom")
        except ValueError:
            import sys
            record = logging.LogRecord(
                name="agent/test", level=logging.ERROR, pathname="",
                lineno=0, msg="error occurred", args=(), exc_info=sys.exc_info(),
            )
        line = formatter.format(record)
        assert "ValueError" in line
        assert "boom" in line

    def test_single_line_no_newline_without_exception(self, formatter):
        record = self._make_record(msg="simple")
        line = formatter.format(record)
        assert "\n" not in line


# ---------------------------------------------------------------------------
# _format_logger_name
# ---------------------------------------------------------------------------


class TestFormatLoggerName:
    @pytest.mark.parametrize("name,expected", [
        ("agent/3f2a1c8d-abcd", "agent/3f2a"),
        ("agent/abcd", "agent/abcd"),   # short id — kept as-is
        ("agent/ab", "agent/ab"),
        ("orchestrator", "orchestrator"),
        ("bus", "bus"),
        ("a" * 20, "a" * 14),           # truncated to 14
    ])
    def test_format(self, name, expected):
        assert _format_logger_name(name) == expected


# ---------------------------------------------------------------------------
# _quote_value
# ---------------------------------------------------------------------------


class TestQuoteValue:
    def test_string_without_spaces_not_quoted(self):
        assert _quote_value("read_file") == "read_file"

    def test_string_with_spaces_quoted(self):
        assert _quote_value("task assigned") == '"task assigned"'

    def test_empty_string_quoted(self):
        assert _quote_value("") == '""'

    def test_integer_no_quotes(self):
        assert _quote_value(42) == "42"

    def test_float_no_quotes(self):
        assert _quote_value(12.4) == "12.4"

    def test_bool_no_quotes(self):
        assert _quote_value(False) == "False"

    def test_none_no_quotes(self):
        assert _quote_value(None) == "None"


# ---------------------------------------------------------------------------
# get_logger
# ---------------------------------------------------------------------------


class TestGetLogger:
    def test_returns_logger(self):
        log = get_logger("agent/test-get-logger")
        assert isinstance(log, logging.Logger)

    def test_has_console_formatter(self):
        log = get_logger("agent/test-formatter-check")
        assert log.handlers
        assert isinstance(log.handlers[0].formatter, ConsoleFormatter)

    def test_does_not_duplicate_handlers(self):
        name = "agent/test-no-dup"
        get_logger(name)
        get_logger(name)
        log = logging.getLogger(name)
        assert len(log.handlers) == 1

    def test_log_level_from_env_info(self, monkeypatch):
        monkeypatch.setenv("LOG_LEVEL", "INFO")
        log = get_logger("agent/test-level-info")
        assert log.level == logging.INFO

    def test_log_level_from_env_debug(self, monkeypatch):
        monkeypatch.setenv("LOG_LEVEL", "DEBUG")
        log = get_logger("agent/test-level-debug-xyzzy")
        assert log.level == logging.DEBUG

    def test_log_level_default_info(self, monkeypatch):
        monkeypatch.delenv("LOG_LEVEL", raising=False)
        log = get_logger("agent/test-level-default-xyzzy2")
        assert log.level == logging.INFO

    def test_does_not_propagate(self):
        log = get_logger("agent/test-propagate")
        assert log.propagate is False

    def test_output_goes_to_stdout(self, capsys):
        log = get_logger("agent/test-stdout-xyzzy3")
        log.info("hello from test", extra={"k": "v"})
        captured = capsys.readouterr()
        assert "hello from test" in captured.out
        assert "k=v" in captured.out
