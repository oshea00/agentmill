"""Agent configuration: loading, validation, templating, and startup checks."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import yaml

FRONTMATTER_RE = re.compile(r"^---\n(.*?)\n---\n(.*)", re.DOTALL)
PLACEHOLDER_RE = re.compile(r"\{\{.*?\}\}")

_VALID_ROLES = frozenset({"orchestrator", "worker"})
_VALID_EXEC_MODES = frozenset({"messages_api", "claude_code"})
_VALID_SYNC_TYPES = frozenset({"partial_result", "context_share"})

_DEFAULT_LIMITS = {
    "max_iterations": 20,
    "max_tool_calls": 50,
    "tool_timeout_seconds": 30.0,
    "session_timeout_seconds": 300.0,
}
_DEFAULT_PEERS = {
    "sync_enabled": False,
    "sync_type": "partial_result",
}


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class ConfigParseError(ValueError):
    """Raised when a config file cannot be parsed (e.g. missing frontmatter)."""


class ConfigValidationError(ValueError):
    """Raised when a config file fails schema validation."""


class ConfigTemplatingError(ValueError):
    """Raised when system prompt placeholders cannot be resolved."""


class StartupValidationError(RuntimeError):
    """Raised by validate_all_templates() when any template is invalid."""


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------


@dataclass
class AgentConfig:
    name: str
    version: str
    role: str
    execution_mode: str
    tools_native: list[str]
    tools_mcp: list[str]
    limits: dict
    peers: dict
    allowed_paths: list[str]
    required_env: list[str]
    tags: list[str]
    description: str
    system_prompt: str
    deprecated: bool = False
    instance_meta: Optional[dict] = field(default=None)


# ---------------------------------------------------------------------------
# Loader
# ---------------------------------------------------------------------------


class AgentConfigLoader:
    """Read, validate, and deserialise agent config files."""

    def load_template(self, name: str, templates_dir: str = "agents/templates") -> AgentConfig:
        """Load and validate a template by name (without the .md extension)."""
        path = Path(templates_dir) / f"{name}.md"
        return self._parse(path)

    def load_instance(self, agent_id: str, instances_dir: str = "agents/instances") -> AgentConfig:
        """Load an instance config by agent ID."""
        path = Path(instances_dir) / f"{agent_id}.md"
        return self._parse(path)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _parse(self, path: Path) -> AgentConfig:
        try:
            raw = path.read_text(encoding="utf-8")
        except FileNotFoundError:
            raise ConfigParseError(f"Config file not found: {path}")

        match = FRONTMATTER_RE.match(raw)
        if not match:
            raise ConfigParseError(f"No YAML frontmatter found in {path}")

        try:
            front = yaml.safe_load(match.group(1))
        except yaml.YAMLError as exc:
            raise ConfigParseError(f"YAML parse error in {path}: {exc}") from exc

        if not isinstance(front, dict):
            raise ConfigParseError(f"Frontmatter is not a YAML mapping in {path}")

        body = match.group(2).strip()
        self._validate(front, path)
        return self._to_config(front, body)

    def _validate(self, front: dict, path: Path) -> None:
        required = ["name", "version", "role", "execution_mode"]
        for f in required:
            if f not in front:
                raise ConfigValidationError(f"Missing required field '{f}' in {path}")

        if front["role"] not in _VALID_ROLES:
            raise ConfigValidationError(
                f"Invalid role '{front['role']}' in {path}; "
                f"must be one of {sorted(_VALID_ROLES)}"
            )

        if front["execution_mode"] not in _VALID_EXEC_MODES:
            raise ConfigValidationError(
                f"Invalid execution_mode '{front['execution_mode']}' in {path}; "
                f"must be one of {sorted(_VALID_EXEC_MODES)}"
            )

        parts = str(front["version"]).split(".")
        if len(parts) != 3 or not all(p.isdigit() for p in parts):
            raise ConfigValidationError(
                f"version must be semver (MAJOR.MINOR.PATCH) in {path}; "
                f"got '{front['version']}'"
            )

        peers = front.get("peers", {})
        if "sync_type" in peers and peers["sync_type"] not in _VALID_SYNC_TYPES:
            raise ConfigValidationError(
                f"Invalid peers.sync_type '{peers['sync_type']}' in {path}; "
                f"must be one of {sorted(_VALID_SYNC_TYPES)}"
            )

    def _to_config(self, front: dict, body: str) -> AgentConfig:
        tools = front.get("tools") or {}
        limits = {**_DEFAULT_LIMITS, **(front.get("limits") or {})}
        peers = {**_DEFAULT_PEERS, **(front.get("peers") or {})}
        return AgentConfig(
            name=front["name"],
            version=front["version"],
            role=front["role"],
            execution_mode=front["execution_mode"],
            tools_native=tools.get("native") or [],
            tools_mcp=tools.get("mcp") or [],
            limits=limits,
            peers=peers,
            allowed_paths=front.get("allowed_paths") or [],
            required_env=front.get("required_env") or [],
            tags=front.get("tags") or [],
            description=front.get("description") or "",
            deprecated=bool(front.get("deprecated", False)),
            system_prompt=body,
            instance_meta=front.get("_instance"),
        )


# ---------------------------------------------------------------------------
# Templating
# ---------------------------------------------------------------------------


def resolve_system_prompt(template_body: str, variables: dict) -> str:
    """Replace ``{{ key }}`` placeholders with values from *variables*.

    Raises :exc:`ConfigTemplatingError` if any placeholder remains unresolved
    after substitution.
    """
    for key, value in variables.items():
        template_body = template_body.replace("{{ " + key + " }}", str(value))

    unresolved = PLACEHOLDER_RE.findall(template_body)
    if unresolved:
        raise ConfigTemplatingError(f"Unresolved placeholders: {unresolved}")
    return template_body


# ---------------------------------------------------------------------------
# Startup validation
# ---------------------------------------------------------------------------


def validate_all_templates(templates_dir: str = "agents/templates") -> None:
    """Validate every ``*.md`` file in *templates_dir*.

    Raises :exc:`StartupValidationError` (aggregating all failures) if any
    template is invalid.  This is intended to be called once at framework
    startup — a single bad template is a hard startup error.
    """
    loader = AgentConfigLoader()
    errors: list[str] = []

    for path in sorted(Path(templates_dir).glob("*.md")):
        try:
            loader.load_template(path.stem, templates_dir=templates_dir)
        except (ConfigParseError, ConfigValidationError) as exc:
            errors.append(str(exc))

    if errors:
        raise StartupValidationError("\n".join(errors))
