FROM python:3.12-slim AS base

# ---------------------------------------------------------------------------
# System dependencies
# Node.js 22 LTS is required for the claude CLI (subprocess fallback for
# ClaudeCodeRunner).  uv provides uvx for running MCP server packages.
# ---------------------------------------------------------------------------

RUN apt-get update && apt-get install -y --no-install-recommends \
        curl \
        gnupg \
        ca-certificates \
    && curl -fsSL https://deb.nodesource.com/setup_22.x | bash - \
    && apt-get install -y --no-install-recommends nodejs \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

# claude CLI  (non-interactive, --print mode used by ClaudeCodeRunner)
RUN npm install -g @anthropic-ai/claude-code

# uv / uvx  (used by MCP server entries that specify "uvx" as command)
RUN pip install --no-cache-dir uv

# ---------------------------------------------------------------------------
# Python package
# ---------------------------------------------------------------------------

WORKDIR /app

# Install dependencies first for better layer caching
COPY pyproject.toml .
RUN pip install --no-cache-dir \
        anthropic \
        opentelemetry-api \
        opentelemetry-sdk \
        pyyaml

# Copy source and install the package itself (no --no-deps: already installed above)
COPY orchestrator/ orchestrator/
RUN pip install --no-cache-dir --no-deps -e .

# ---------------------------------------------------------------------------
# Runtime layout
# ---------------------------------------------------------------------------

# /data  — persistent volume for SQLite DB and OTel trace files
# /app/agents/instances — runtime-generated instance configs (gitignored)
RUN mkdir -p /data/traces /app/agents/templates /app/agents/instances

ENV DB_PATH=/data/agent_orchestrator.db
ENV LOG_LEVEL=INFO

# Agent templates are part of the image but can be overlaid by a bind mount.
# Instances are written at runtime and should live on the /data volume.
COPY agents/templates/ /app/agents/templates/

# Health check: verify the package imports cleanly
HEALTHCHECK --interval=30s --timeout=10s --start-period=5s --retries=3 \
    CMD python -c "from orchestrator.db import get_connection; get_connection(':memory:')" \
    || exit 1
