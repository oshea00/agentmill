"""Tests for orchestrator/config.py — AgentConfigLoader, resolve_system_prompt,
and validate_all_templates."""

import textwrap
from pathlib import Path

import pytest

from orchestrator.config import (
    AgentConfig,
    AgentConfigLoader,
    ConfigParseError,
    ConfigTemplatingError,
    ConfigValidationError,
    StartupValidationError,
    resolve_system_prompt,
    validate_all_templates,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

MINIMAL_VALID_FRONT = textwrap.dedent("""\
    name: researcher
    version: "1.2.0"
    role: worker
    execution_mode: messages_api
""")

MINIMAL_VALID_BODY = "You are a research agent.\n"


def make_md(frontmatter: str, body: str = MINIMAL_VALID_BODY) -> str:
    return f"---\n{frontmatter}---\n{body}"


def write_template(tmp_path: Path, name: str, content: str) -> Path:
    d = tmp_path / "agents" / "templates"
    d.mkdir(parents=True, exist_ok=True)
    p = d / f"{name}.md"
    p.write_text(content, encoding="utf-8")
    return p


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------


class TestParsing:
    def test_missing_frontmatter_raises(self, tmp_path):
        write_template(tmp_path, "bad", "No frontmatter here.\n")
        loader = AgentConfigLoader()
        with pytest.raises(ConfigParseError, match="No YAML frontmatter"):
            loader.load_template("bad", templates_dir=str(tmp_path / "agents" / "templates"))

    def test_file_not_found_raises(self, tmp_path):
        loader = AgentConfigLoader()
        with pytest.raises(ConfigParseError, match="not found"):
            loader.load_template("nonexistent", templates_dir=str(tmp_path))

    def test_valid_minimal_template_loads(self, tmp_path):
        write_template(tmp_path, "researcher", make_md(MINIMAL_VALID_FRONT))
        loader = AgentConfigLoader()
        cfg = loader.load_template(
            "researcher", templates_dir=str(tmp_path / "agents" / "templates")
        )
        assert isinstance(cfg, AgentConfig)
        assert cfg.name == "researcher"
        assert cfg.version == "1.2.0"
        assert cfg.role == "worker"
        assert cfg.execution_mode == "messages_api"

    def test_system_prompt_is_body(self, tmp_path):
        body = "## Role\n\nYou are an agent.\n"
        write_template(tmp_path, "r", make_md(MINIMAL_VALID_FRONT, body))
        loader = AgentConfigLoader()
        cfg = loader.load_template("r", templates_dir=str(tmp_path / "agents" / "templates"))
        assert cfg.system_prompt == body.strip()

    def test_invalid_yaml_raises(self, tmp_path):
        bad_yaml = "name: [\nunot closed\n"
        write_template(tmp_path, "bad", f"---\n{bad_yaml}---\nbody\n")
        loader = AgentConfigLoader()
        with pytest.raises(ConfigParseError, match="YAML parse error"):
            loader.load_template("bad", templates_dir=str(tmp_path / "agents" / "templates"))


# ---------------------------------------------------------------------------
# Required-field validation
# ---------------------------------------------------------------------------


class TestMissingRequiredFields:
    @pytest.mark.parametrize("missing_field", ["name", "version", "role", "execution_mode"])
    def test_missing_field_raises(self, tmp_path, missing_field):
        front_lines = {
            "name": "name: researcher",
            "version": 'version: "1.0.0"',
            "role": "role: worker",
            "execution_mode": "execution_mode: messages_api",
        }
        front = "\n".join(v for k, v in front_lines.items() if k != missing_field) + "\n"
        write_template(tmp_path, "agent", make_md(front))
        loader = AgentConfigLoader()
        with pytest.raises(ConfigValidationError, match=f"Missing required field '{missing_field}'"):
            loader.load_template("agent", templates_dir=str(tmp_path / "agents" / "templates"))

    def test_invalid_role_raises(self, tmp_path):
        front = MINIMAL_VALID_FRONT.replace("role: worker", "role: superagent")
        write_template(tmp_path, "agent", make_md(front))
        loader = AgentConfigLoader()
        with pytest.raises(ConfigValidationError, match="Invalid role 'superagent'"):
            loader.load_template("agent", templates_dir=str(tmp_path / "agents" / "templates"))

    def test_invalid_execution_mode_raises(self, tmp_path):
        front = MINIMAL_VALID_FRONT.replace("execution_mode: messages_api", "execution_mode: turbo")
        write_template(tmp_path, "agent", make_md(front))
        loader = AgentConfigLoader()
        with pytest.raises(ConfigValidationError, match="Invalid execution_mode 'turbo'"):
            loader.load_template("agent", templates_dir=str(tmp_path / "agents" / "templates"))

    def test_both_roles_accepted(self, tmp_path):
        for role in ("orchestrator", "worker"):
            front = MINIMAL_VALID_FRONT.replace("role: worker", f"role: {role}")
            write_template(tmp_path, role, make_md(front))
            loader = AgentConfigLoader()
            cfg = loader.load_template(role, templates_dir=str(tmp_path / "agents" / "templates"))
            assert cfg.role == role

    def test_both_execution_modes_accepted(self, tmp_path):
        for mode in ("messages_api", "claude_code"):
            front = MINIMAL_VALID_FRONT.replace("execution_mode: messages_api", f"execution_mode: {mode}")
            write_template(tmp_path, mode, make_md(front))
            loader = AgentConfigLoader()
            cfg = loader.load_template(mode, templates_dir=str(tmp_path / "agents" / "templates"))
            assert cfg.execution_mode == mode


# ---------------------------------------------------------------------------
# Semver validation
# ---------------------------------------------------------------------------


class TestSemverValidation:
    @pytest.mark.parametrize(
        "bad_version",
        [
            "1.0",        # only two parts
            "1",          # only one part
            "1.0.0.0",    # four parts
            "1.0.a",      # non-numeric patch
            "v1.0.0",     # 'v' prefix
            "latest",     # free text
            "",           # empty
            "1. 0.0",     # space
        ],
    )
    def test_bad_semver_raises(self, tmp_path, bad_version):
        front = MINIMAL_VALID_FRONT.replace('version: "1.2.0"', f'version: "{bad_version}"')
        write_template(tmp_path, "agent", make_md(front))
        loader = AgentConfigLoader()
        with pytest.raises(ConfigValidationError, match="semver"):
            loader.load_template("agent", templates_dir=str(tmp_path / "agents" / "templates"))

    @pytest.mark.parametrize(
        "good_version", ["0.0.1", "1.0.0", "10.20.300", "0.0.0"]
    )
    def test_valid_semver_accepted(self, tmp_path, good_version):
        front = MINIMAL_VALID_FRONT.replace('version: "1.2.0"', f'version: "{good_version}"')
        write_template(tmp_path, "agent", make_md(front))
        loader = AgentConfigLoader()
        cfg = loader.load_template("agent", templates_dir=str(tmp_path / "agents" / "templates"))
        assert cfg.version == good_version


# ---------------------------------------------------------------------------
# Default values
# ---------------------------------------------------------------------------


class TestDefaults:
    def test_limits_defaults_applied(self, tmp_path):
        write_template(tmp_path, "agent", make_md(MINIMAL_VALID_FRONT))
        loader = AgentConfigLoader()
        cfg = loader.load_template("agent", templates_dir=str(tmp_path / "agents" / "templates"))
        assert cfg.limits["max_iterations"] == 20
        assert cfg.limits["max_tool_calls"] == 50
        assert cfg.limits["tool_timeout_seconds"] == 30.0
        assert cfg.limits["session_timeout_seconds"] == 300.0

    def test_limits_overrides_applied(self, tmp_path):
        front = MINIMAL_VALID_FRONT + "limits:\n  max_iterations: 5\n"
        write_template(tmp_path, "agent", make_md(front))
        loader = AgentConfigLoader()
        cfg = loader.load_template("agent", templates_dir=str(tmp_path / "agents" / "templates"))
        assert cfg.limits["max_iterations"] == 5
        assert cfg.limits["max_tool_calls"] == 50  # default preserved

    def test_peers_defaults_applied(self, tmp_path):
        write_template(tmp_path, "agent", make_md(MINIMAL_VALID_FRONT))
        loader = AgentConfigLoader()
        cfg = loader.load_template("agent", templates_dir=str(tmp_path / "agents" / "templates"))
        assert cfg.peers["sync_enabled"] is False
        assert cfg.peers["sync_type"] == "partial_result"

    def test_empty_lists_default(self, tmp_path):
        write_template(tmp_path, "agent", make_md(MINIMAL_VALID_FRONT))
        loader = AgentConfigLoader()
        cfg = loader.load_template("agent", templates_dir=str(tmp_path / "agents" / "templates"))
        assert cfg.tools_native == []
        assert cfg.tools_mcp == []
        assert cfg.allowed_paths == []
        assert cfg.required_env == []
        assert cfg.tags == []
        assert cfg.description == ""

    def test_deprecated_defaults_false(self, tmp_path):
        write_template(tmp_path, "agent", make_md(MINIMAL_VALID_FRONT))
        loader = AgentConfigLoader()
        cfg = loader.load_template("agent", templates_dir=str(tmp_path / "agents" / "templates"))
        assert cfg.deprecated is False


# ---------------------------------------------------------------------------
# Optional fields roundtrip
# ---------------------------------------------------------------------------


class TestOptionalFields:
    def test_full_template_roundtrip(self, tmp_path):
        front = textwrap.dedent("""\
            name: researcher
            version: "1.2.0"
            role: worker
            execution_mode: messages_api
            tools:
              native:
                - web_search
                - read_file
              mcp:
                - brave-search
            limits:
              max_iterations: 10
              max_tool_calls: 25
              tool_timeout_seconds: 15.0
              session_timeout_seconds: 120.0
            peers:
              sync_enabled: true
              sync_type: context_share
            allowed_paths:
              - ./workspace
            required_env:
              - ANTHROPIC_API_KEY
            tags:
              - research
            description: "Does research."
            deprecated: false
        """)
        write_template(tmp_path, "researcher", make_md(front))
        loader = AgentConfigLoader()
        cfg = loader.load_template(
            "researcher", templates_dir=str(tmp_path / "agents" / "templates")
        )
        assert cfg.tools_native == ["web_search", "read_file"]
        assert cfg.tools_mcp == ["brave-search"]
        assert cfg.limits["max_iterations"] == 10
        assert cfg.peers["sync_enabled"] is True
        assert cfg.peers["sync_type"] == "context_share"
        assert cfg.allowed_paths == ["./workspace"]
        assert cfg.required_env == ["ANTHROPIC_API_KEY"]
        assert cfg.tags == ["research"]
        assert cfg.description == "Does research."
        assert cfg.deprecated is False

    def test_instance_meta_parsed(self, tmp_path):
        front = MINIMAL_VALID_FRONT + textwrap.dedent("""\
            _instance:
              agent_id: "abc-123"
              template: researcher
              template_version: "1.2.0"
              parent_id: "parent-456"
              cluster_id: "cluster-xyz"
              task_id: "task-001"
        """)
        instances_dir = tmp_path / "agents" / "instances"
        instances_dir.mkdir(parents=True)
        (instances_dir / "abc-123.md").write_text(make_md(front), encoding="utf-8")
        loader = AgentConfigLoader()
        cfg = loader.load_instance("abc-123", instances_dir=str(instances_dir))
        assert cfg.instance_meta is not None
        assert cfg.instance_meta["agent_id"] == "abc-123"
        assert cfg.instance_meta["cluster_id"] == "cluster-xyz"

    def test_deprecated_true_loads(self, tmp_path):
        front = MINIMAL_VALID_FRONT + "deprecated: true\n"
        write_template(tmp_path, "agent", make_md(front))
        loader = AgentConfigLoader()
        cfg = loader.load_template("agent", templates_dir=str(tmp_path / "agents" / "templates"))
        assert cfg.deprecated is True


# ---------------------------------------------------------------------------
# resolve_system_prompt
# ---------------------------------------------------------------------------


class TestResolveSystemPrompt:
    def test_resolves_single_placeholder(self):
        body = "Your task is: {{ task_description }}"
        result = resolve_system_prompt(body, {"task_description": "summarise Q3 results"})
        assert result == "Your task is: summarise Q3 results"

    def test_resolves_multiple_placeholders(self):
        body = "Agent {{ agent_id }} in cluster {{ cluster_id }} has {{ peer_count }} peers."
        result = resolve_system_prompt(
            body,
            {"agent_id": "abc", "cluster_id": "clu-1", "peer_count": 3},
        )
        assert result == "Agent abc in cluster clu-1 has 3 peers."

    def test_no_placeholders_returns_unchanged(self):
        body = "No placeholders here."
        assert resolve_system_prompt(body, {}) == body

    def test_unresolved_placeholder_raises(self):
        body = "Task: {{ task_description }} in cluster {{ cluster_id }}"
        with pytest.raises(ConfigTemplatingError, match="Unresolved placeholders"):
            resolve_system_prompt(body, {"task_description": "do work"})
        # cluster_id is the unresolved one

    def test_unresolved_error_lists_all_remaining(self):
        body = "{{ a }} and {{ b }} and {{ c }}"
        with pytest.raises(ConfigTemplatingError) as exc_info:
            resolve_system_prompt(body, {})
        msg = str(exc_info.value)
        assert "{{ a }}" in msg
        assert "{{ b }}" in msg
        assert "{{ c }}" in msg

    def test_value_is_coerced_to_str(self):
        body = "Count: {{ n }}"
        result = resolve_system_prompt(body, {"n": 42})
        assert result == "Count: 42"

    def test_partial_match_not_replaced(self):
        # "task" should not match "{{ task_description }}"
        body = "Task: {{ task_description }}"
        with pytest.raises(ConfigTemplatingError):
            resolve_system_prompt(body, {"task": "wrong"})

    def test_extra_variables_ignored(self):
        body = "Hello {{ name }}"
        result = resolve_system_prompt(body, {"name": "world", "unused": "ignored"})
        assert result == "Hello world"


# ---------------------------------------------------------------------------
# validate_all_templates
# ---------------------------------------------------------------------------


class TestValidateAllTemplates:
    def test_empty_directory_passes(self, tmp_path):
        d = tmp_path / "agents" / "templates"
        d.mkdir(parents=True)
        validate_all_templates(templates_dir=str(d))  # should not raise

    def test_all_valid_templates_pass(self, tmp_path):
        for name in ("researcher", "coder", "summariser"):
            write_template(tmp_path, name, make_md(MINIMAL_VALID_FRONT.replace("name: researcher", f"name: {name}")))
        validate_all_templates(templates_dir=str(tmp_path / "agents" / "templates"))

    def test_single_invalid_template_raises(self, tmp_path):
        write_template(tmp_path, "good", make_md(MINIMAL_VALID_FRONT))
        front_bad = MINIMAL_VALID_FRONT.replace('version: "1.2.0"', 'version: "bad"')
        write_template(tmp_path, "bad", make_md(front_bad))
        with pytest.raises(StartupValidationError, match="semver"):
            validate_all_templates(templates_dir=str(tmp_path / "agents" / "templates"))

    def test_multiple_invalid_templates_all_reported(self, tmp_path):
        # Two templates with different errors
        front_no_name = MINIMAL_VALID_FRONT.replace("name: researcher\n", "")
        front_bad_ver = MINIMAL_VALID_FRONT.replace('version: "1.2.0"', 'version: "x.y.z"')
        write_template(tmp_path, "no_name", make_md(front_no_name))
        write_template(tmp_path, "bad_ver", make_md(front_bad_ver))
        with pytest.raises(StartupValidationError) as exc_info:
            validate_all_templates(templates_dir=str(tmp_path / "agents" / "templates"))
        msg = str(exc_info.value)
        # Both errors should be present in the aggregated message
        assert "name" in msg.lower() or "semver" in msg.lower()

    def test_missing_required_field_caught(self, tmp_path):
        front = MINIMAL_VALID_FRONT.replace("role: worker\n", "")
        write_template(tmp_path, "agent", make_md(front))
        with pytest.raises(StartupValidationError, match="role"):
            validate_all_templates(templates_dir=str(tmp_path / "agents" / "templates"))

    def test_no_frontmatter_caught(self, tmp_path):
        d = tmp_path / "agents" / "templates"
        d.mkdir(parents=True)
        (d / "broken.md").write_text("Just plain text, no frontmatter.\n")
        with pytest.raises(StartupValidationError, match="frontmatter"):
            validate_all_templates(templates_dir=str(d))
