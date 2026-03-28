# Spec: Human-in-the-Loop IRC Gateway

## Overview

The IRC gateway is the channel through which agents pause and seek input from
the human owner or their delegates. It connects to a local `ngircd` instance
at `localhost:6667`, joins a designated channel, and bridges IRC messages into
the orchestration framework's interaction model.

When an agent needs human input it transitions to `SUSPENDED`, posts a
structured request to IRC, and waits on an `asyncio.Event`. A trusted nick
responds in the channel; the gateway parses the response, fires the event, and
the agent resumes.

The existing `AgentIRC` class in the repository is the foundation. This spec
describes what is kept, what changes, and what is added.

---

## Infrastructure

IRC server: **ngircd** via Docker Compose (already in repo). No changes needed.

```yaml
# docker-compose.yml (existing — keep as-is)
services:
  irc:
    image: ghcr.io/ngircd/ngircd:latest
    container_name: agentirc
    ports:
      - "127.0.0.1:6667:6667"
    volumes:
      - ./ngircd.conf:/opt/ngircd/etc/ngircd.conf:ro
    restart: unless-stopped
```

The gateway always connects to `127.0.0.1:6667`. No TLS, no remote IRC — this
is a local human-control channel only.

---

## What Changes From the Existing Code

| Existing | Change | Reason |
|---|---|---|
| `socket` + `threading` | `asyncio` streams + `asyncio.Lock` | Framework is async-first throughout |
| `OpenAI` client in `_ask()` | Removed from gateway entirely | Gateway routes; agents reason. LLM calls belong in the agent run loop |
| `threading.Thread` for responses | `asyncio.create_task()` | Consistent with the event loop |
| No nick trust check | `TrustedNickRegistry` gate on all input | Required for security |
| Ad-hoc trigger parsing | Structured `InteractionResponse` parser | Agents need typed responses, not freeform text |
| `history` list | Removed from gateway | Conversation history is the agent's responsibility (stored in `agent.context`) |
| `OPENAI_API_KEY` env check | Removed | Not needed in gateway |

What is **kept unchanged**: `split_irc()`, the raw line parser in `handle_line()`,
`_send()`, `send_message()`, the PING/PONG handler, the `001` join sequence,
and the `PRIVMSG` parsing logic. These are solid and reused verbatim.

---

## Trusted Nick Registry

Trusted nicks are defined in the agent template frontmatter (or a dedicated
`irc_config.yaml` at the project root). Only messages from trusted nicks are
acted upon. All other messages are silently ignored.

### `irc_config.yaml`

```yaml
server:
  host: "127.0.0.1"
  port: 6667

bot:
  nick: "orchestrator"
  channel: "#agents"

trusted_nicks:
  owner:
    - "mike"
    - "moshea"
  delegate:
    - "alice"
    - "bob"

timeouts:
  interaction_wait_seconds: null   # null = wait indefinitely (agent stays SUSPENDED)
```

### `TrustedNickRegistry`

```python
from enum import Enum
from dataclasses import dataclass

class TrustLevel(str, Enum):
    OWNER    = "owner"      # can do anything
    DELEGATE = "delegate"   # can approve/deny/direct; cannot terminate

@dataclass
class TrustedNick:
    nick: str
    level: TrustLevel

class TrustedNickRegistry:
    def __init__(self, config: dict):
        self._nicks: dict[str, TrustedNick] = {}
        for nick in config.get("trusted_nicks", {}).get("owner", []):
            self._nicks[nick.lower()] = TrustedNick(nick=nick, level=TrustLevel.OWNER)
        for nick in config.get("trusted_nicks", {}).get("delegate", []):
            self._nicks[nick.lower()] = TrustedNick(nick=nick, level=TrustLevel.DELEGATE)

    def get(self, nick: str) -> TrustedNick | None:
        return self._nicks.get(nick.lower())

    def is_trusted(self, nick: str) -> bool:
        return nick.lower() in self._nicks
```

---

## Interaction Types

Four interaction types are supported. Each maps to an IRC command a trusted
nick can send in the channel.

| Type | IRC syntax | Who can use | Agent behaviour |
|---|---|---|---|
| `APPROVE` | `!approve <interaction_id>` | owner, delegate | Resume with approval; agent continues |
| `DENY` | `!deny <interaction_id> [reason]` | owner, delegate | Resume with denial; agent handles refusal |
| `DIRECT` | `!direct <interaction_id> <instruction>` | owner, delegate | Resume with freeform instruction injected into agent context |
| `TERMINATE` | `!terminate <agent_id>` | owner only | Agent transitions `SUSPENDED → TERMINATED` |

`<interaction_id>` is a short token (first 8 chars of a UUID) posted by the
agent in its IRC request, used to match responses to waiting agents.

Freeform channel messages from trusted nicks (not prefixed with `!`) are logged
at DEBUG level but do not trigger any agent action.

---

## Data Model

### `interaction_requests` table (SQLite)

```sql
CREATE TABLE interaction_requests (
    id              TEXT PRIMARY KEY,     -- UUID v4
    short_id        TEXT NOT NULL,        -- first 8 chars, used in IRC
    agent_id        TEXT NOT NULL REFERENCES agents(id),
    interaction_type TEXT NOT NULL,       -- RequestType enum
    prompt          TEXT NOT NULL,        -- message posted to IRC
    status          TEXT NOT NULL DEFAULT 'PENDING',  -- PENDING | RESPONDED | TIMED_OUT
    response_type   TEXT,                 -- APPROVE | DENY | DIRECT | TERMINATE
    response_nick   TEXT,
    response_payload TEXT,               -- JSON: reason, instruction, etc.
    created_at      REAL NOT NULL,
    responded_at    REAL
);

CREATE INDEX idx_ir_short_id ON interaction_requests(short_id, status);
CREATE INDEX idx_ir_agent    ON interaction_requests(agent_id, status);
```

### `RequestType` (what the agent is asking for)

```python
class RequestType(str, Enum):
    APPROVAL   = "APPROVAL"    # agent wants permission to proceed with an action
    DIRECTION  = "DIRECTION"   # agent is stuck and needs human guidance
    ERROR      = "ERROR"       # agent hit an unrecoverable error, needs triage
    SUSPENSION = "SUSPENSION"  # agent is being suspended by orchestrator, notify human
```

---

## Python Interface

### `InteractionRequest` and `InteractionResponse`

```python
from dataclasses import dataclass, field
from typing import Optional
import uuid, time

@dataclass
class InteractionRequest:
    agent_id: str
    request_type: RequestType
    prompt: str                          # human-readable question/summary
    context: dict = field(default_factory=dict)   # optional structured data
    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    short_id: str = ""
    created_at: float = field(default_factory=time.time)

    def __post_init__(self):
        self.short_id = self.id[:8]

@dataclass
class InteractionResponse:
    interaction_id: str
    short_id: str
    response_type: str                   # APPROVE | DENY | DIRECT | TERMINATE
    nick: str
    trust_level: TrustLevel
    payload: dict = field(default_factory=dict)   # reason, instruction, etc.
    timestamp: float = field(default_factory=time.time)
```

### `IRCGateway`

```python
class IRCGateway:
    def __init__(self, config: dict, registry: TrustedNickRegistry, tracer, logger): ...

    async def connect(self) -> None: ...
    async def disconnect(self) -> None: ...
    async def run(self) -> None:
        """Main receive loop — run as a long-lived asyncio task."""
        ...

    async def post_interaction(self, request: InteractionRequest) -> InteractionResponse:
        """
        Post a request to IRC and suspend until a trusted nick responds.
        Returns the InteractionResponse when one arrives.
        Caller is responsible for transitioning agent state before/after.
        """
        ...

    async def notify(self, message: str) -> None:
        """Post an informational message to the channel (no response expected)."""
        ...
```

---

## Agent Suspension Flow

This is the critical integration point with the lifecycle spec.

```
Agent (RUNNING) needs human input
    │
    ├─ AgentRepository.transition(agent_id, SUSPENDED, reason="awaiting human input")
    ├─ Build InteractionRequest(agent_id, RequestType.APPROVAL, prompt="...")
    ├─ IRCGateway.post_interaction(request)   ← posts to IRC, then awaits asyncio.Event
    │
    │  [IRC channel]
    │  orchestrator: "[req:a1b2c3d4] Agent researcher-42 requests approval to delete
    │                  3 files in /workspace/output. Reply: !approve a1b2c3d4 or
    │                  !deny a1b2c3d4 [reason]"
    │
    │  mike: "!approve a1b2c3d4"
    │
    ├─ Gateway parses response → InteractionResponse(APPROVE, nick="mike", ...)
    ├─ Persists response to interaction_requests table
    ├─ Fires asyncio.Event → post_interaction() returns
    │
    ├─ AgentRepository.transition(agent_id, IDLE)
    └─ Agent resumes with response in hand
```

### `post_interaction()` internals

```python
async def post_interaction(self, request: InteractionRequest) -> InteractionResponse:
    # 1. Persist to DB
    self._repo.create_request(request)

    # 2. Register a pending asyncio.Event keyed by short_id
    event = asyncio.Event()
    self._pending[request.short_id] = (event, None)

    # 3. Format and post IRC message
    irc_msg = self._format_request(request)
    await self.notify(irc_msg)

    # 4. Emit OTel span
    with self._tracer.start_as_current_span("irc.interaction.wait") as span:
        span.set_attribute("agent.id", request.agent_id)
        span.set_attribute("irc.short_id", request.short_id)
        span.set_attribute("irc.request_type", request.request_type)

        # 5. Wait indefinitely (agent stays SUSPENDED until human responds)
        await event.wait()

        response = self._pending.pop(request.short_id)[1]
        span.set_attribute("irc.response_type", response.response_type)
        span.set_attribute("irc.response_nick", response.nick)

    # 6. Persist response
    self._repo.record_response(request.short_id, response)
    return response
```

---

## IRC Message Formatting

### Request format (agent → channel)

```
[req:a1b2c3d4] researcher-42 (APPROVAL): Requesting permission to delete 3 files
in /workspace/output: report_draft.txt, temp_1.json, temp_2.json.
Reply: !approve a1b2c3d4  |  !deny a1b2c3d4 [reason]  |  !direct a1b2c3d4 <instruction>
```

Lines are split via the existing `split_irc()` with `MAX_LINE = 400`. The
`[req:a1b2c3d4]` token always appears at the start of the first line.

### Error escalation format

```
[req:e5f6a7b8] researcher-42 (ERROR): MCP server 'filesystem' failed after 2 retries.
Task: "Summarise Q3 reports". Last error: Connection refused.
Reply: !direct e5f6a7b8 <instruction>  |  !terminate researcher-42
```

### Informational notify (no response needed)

```
[info] researcher-42 transitioned to TERMINATED. Task completed successfully.
```

---

## Response Parsing

The `_dispatch()` method from the original code is extended. Trusted-nick check
gates all command handling. The existing trigger patterns (`botnick:`,
`botnick,`, `!`) are preserved for backwards compatibility, but structured
commands take precedence.

```python
async def _dispatch(self, nick: str, channel: str, message: str) -> None:
    # Existing: ignore own messages
    if nick == self._nick:
        return

    # New: trust gate — ignore untrusted nicks entirely
    trusted = self._registry.get(nick)
    if not trusted:
        return

    # New: structured command parsing
    if message.startswith("!"):
        await self._handle_command(nick, trusted, message[1:].strip())
        return

    # Existing trigger patterns retained for freeform queries to the bot
    prompt = None
    if message.lower().startswith(f"{self._nick.lower()}:"):
        prompt = message[len(self._nick) + 1:].strip()
    elif message.lower().startswith(f"{self._nick.lower()},"):
        prompt = message[len(self._nick) + 1:].strip()

    if prompt:
        # Route freeform owner queries to the orchestrator agent's context
        # (not to an LLM directly — the orchestrator handles it)
        await self._route_freeform(nick, trusted, prompt)

async def _handle_command(self, nick: str, trusted: TrustedNick, cmd: str) -> None:
    parts = cmd.split(None, 2)
    verb = parts[0].lower() if parts else ""

    if verb == "approve" and len(parts) >= 2:
        short_id = parts[1]
        self._resolve(short_id, InteractionResponse(
            interaction_id="", short_id=short_id,
            response_type="APPROVE", nick=nick, trust_level=trusted.level,
        ))

    elif verb == "deny" and len(parts) >= 2:
        short_id = parts[1]
        reason = parts[2] if len(parts) == 3 else ""
        self._resolve(short_id, InteractionResponse(
            interaction_id="", short_id=short_id,
            response_type="DENY", nick=nick, trust_level=trusted.level,
            payload={"reason": reason},
        ))

    elif verb == "direct" and len(parts) >= 3:
        short_id = parts[1]
        instruction = parts[2]
        self._resolve(short_id, InteractionResponse(
            interaction_id="", short_id=short_id,
            response_type="DIRECT", nick=nick, trust_level=trusted.level,
            payload={"instruction": instruction},
        ))

    elif verb == "terminate" and len(parts) >= 2:
        if trusted.level != TrustLevel.OWNER:
            await self.notify(f"{nick}: only owners can terminate agents.")
            return
        agent_id_fragment = parts[1]
        await self._route_terminate(nick, agent_id_fragment)

    elif verb == "reset":
        # Existing !reset — kept for operational convenience
        await self.notify("No conversation history to reset (history lives in agents).")

    elif verb == "model":
        await self.notify("This gateway uses claude-opus-4-5 via the orchestration framework.")

    else:
        await self.notify(f"{nick}: unknown command '{verb}'.")

def _resolve(self, short_id: str, response: InteractionResponse) -> None:
    """Fire the asyncio.Event for a pending interaction."""
    if short_id not in self._pending:
        self._logger.warning("irc.unknown_short_id", extra={"short_id": short_id})
        return
    event, _ = self._pending[short_id]
    self._pending[short_id] = (event, response)
    event.set()
```

---

## Agent Response Handling

After `post_interaction()` returns, the agent inspects `response.response_type`
and acts accordingly:

```python
response = await gateway.post_interaction(request)
repo.transition(agent.id, AgentState.IDLE, reason=f"human:{response.response_type}")

if response.response_type == "APPROVE":
    # proceed with the proposed action
    ...
elif response.response_type == "DENY":
    reason = response.payload.get("reason", "no reason given")
    # abandon the action, record reason, continue task with alternative plan
    messages.append({"role": "user", "content": f"Action denied by {response.nick}: {reason}"})
    ...
elif response.response_type == "DIRECT":
    instruction = response.payload.get("instruction", "")
    # inject instruction as a user turn in the conversation
    messages.append({"role": "user", "content": instruction})
    ...
```

`TERMINATE` is handled by the orchestrator directly (routed via `MessageBus`),
not by the worker — the worker's next `receive()` will get a `CONTROL:TERMINATE`
message.

---

## asyncio Conversion Notes

The existing `AgentIRC` uses `threading` throughout. The rewrite targets
`asyncio` to match the rest of the framework. Key changes:

```python
# Old: blocking socket recv in while loop
data = self.sock.recv(4096).decode(...)

# New: asyncio StreamReader
reader, writer = await asyncio.open_connection(host, port)
data = await reader.read(4096)

# Old: threading.Lock
self.history_lock = threading.Lock()
with self.history_lock: ...

# New: asyncio.Lock
self._lock = asyncio.Lock()
async with self._lock: ...

# Old: threading.Thread for _ask()
threading.Thread(target=self._ask, ...).start()

# New: asyncio.create_task()
asyncio.create_task(self._handle_command(...))

# Old: time.sleep(0.05) flood prevention in send_message()
time.sleep(0.05)

# New: asyncio.sleep
await asyncio.sleep(0.05)
```

The gateway's `run()` coroutine is started as a long-lived `asyncio.Task` at
framework startup, alongside the orchestrator's main loop.

---

## Startup & Shutdown

```python
# In framework entrypoint
config = load_irc_config("irc_config.yaml")
registry = TrustedNickRegistry(config)
gateway = IRCGateway(config, registry, tracer, logger)

gateway_task = asyncio.create_task(gateway.run())

try:
    await orchestrator.run()
finally:
    await gateway.disconnect()
    gateway_task.cancel()
```

On `QUIT`, the gateway sends `QUIT :orchestrator shutting down\r\n` and posts
an `[info]` message to the channel before disconnecting.

---

## OTel Instrumentation

| Span | Trigger |
|---|---|
| `irc.connect` | Gateway connects to ngircd |
| `irc.interaction.wait` | `post_interaction()` called; span ends when event fires |
| `irc.command.received` | Trusted nick sends a `!` command |
| `irc.notify` | Informational message posted to channel |

All spans carry `agent.id` and `irc.nick` where applicable. `irc.interaction.wait`
additionally carries `irc.short_id`, `irc.request_type`, `irc.response_type`,
`irc.response_nick`, and `irc.wait_duration_seconds`.

Human interaction events are also logged at `INFO` to the console:

```
[14:07:22] INFO   agent/3f2a  IRC interaction posted   type=APPROVAL short_id=a1b2c3d4
[14:07:45] INFO   agent/3f2a  IRC response received    type=APPROVE nick=mike wait_s=23.1
[14:07:45] INFO   agent/3f2a  State transition         from=SUSPENDED to=IDLE reason=human:APPROVE
```

---

## Environment Variables

| Variable | Default | Purpose |
|---|---|---|
| `IRC_HOST` | `127.0.0.1` | ngircd host (keep default; local only) |
| `IRC_PORT` | `6667` | ngircd port |
| `IRC_NICK` | `orchestrator` | Bot nick on IRC |
| `IRC_CHANNEL` | `#agents` | Channel for all agent–human interaction |

These are kept from the existing project, with `IRC_NICK` defaulting to
`orchestrator` rather than `agentbot`.

---

## Design Decisions

| Decision | Choice | Rationale |
|---|---|---|
| Reuse existing connection/parsing code | Yes | `split_irc`, `handle_line`, `_send`, PING/PONG are correct and tested |
| Remove LLM from gateway | Yes | Gateway routes; reasoning belongs in agents |
| Trust gate | Nick allowlist in `irc_config.yaml` | Simple, auditable, no auth protocol needed on localhost |
| Timeout policy | Indefinite wait (`SUSPENDED`) | Matches user requirement; avoids silent incorrect agent behaviour |
| Interaction ID | 8-char short_id in IRC, full UUID in DB | Short enough to type; full UUID for audit trail |
| `!terminate` owner-only | Yes | Termination is irreversible; delegates cannot trigger it |
| Freeform owner messages | Routed to orchestrator context, not LLM | Keeps LLM calls auditable and inside the agent loop |
| asyncio over threading | Yes | Consistent with the rest of the framework |
