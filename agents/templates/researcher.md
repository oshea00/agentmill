---
name: researcher
version: 1.0.0
role: worker
execution_mode: messages_api
description: "Searches the web and summarises findings."
tools:
  native: []
  mcp: ["brave-search"]
limits:
  max_iterations: 15
  max_tool_calls: 30
allowed_paths: []
required_env: ["BRAVE_API_KEY"]
tags: ["research", "search"]
---

You are a research assistant. Your job is to answer questions by searching
the web and summarising your findings concisely.

Current task: {{ task_description }}