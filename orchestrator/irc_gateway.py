"""IRC gateway: human-in-the-loop interface for the agent orchestration framework.

Agents suspend themselves, post an InteractionRequest here, and await an
asyncio.Event.  A trusted IRC nick responds with a structured command
(!approve, !deny, !direct, !terminate); the gateway fires the event and the
agent resumes with an InteractionResponse in hand.

All threading has been replaced with asyncio throughout (asyncio.Lock,
asyncio.Event, asyncio.open_connection, asyncio.sleep).
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Awaitable, Callable, Optional

from opentelemetry import trace as otel_trace

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

MAX_LINE = 400  # maximum IRC message body length before splitting


# ---------------------------------------------------------------------------
# Trust model
# ---------------------------------------------------------------------------


class TrustLevel(str, Enum):
    OWNER = "owner"        # can do anything
    DELEGATE = "delegate"  # can approve/deny/direct; cannot terminate


@dataclass
class TrustedNick:
    nick: str
    level: TrustLevel


class TrustedNickRegistry:
    """Immutable set of trusted IRC nicks loaded from irc_config.yaml."""

    def __init__(self, config: dict) -> None:
        self._nicks: dict[str, TrustedNick] = {}
        for nick in config.get("trusted_nicks", {}).get("owner", []):
            self._nicks[nick.lower()] = TrustedNick(nick=nick, level=TrustLevel.OWNER)
        for nick in config.get("trusted_nicks", {}).get("delegate", []):
            self._nicks[nick.lower()] = TrustedNick(nick=nick, level=TrustLevel.DELEGATE)

    def get(self, nick: str) -> Optional[TrustedNick]:
        return self._nicks.get(nick.lower())

    def is_trusted(self, nick: str) -> bool:
        return nick.lower() in self._nicks


# ---------------------------------------------------------------------------
# Interaction data model
# ---------------------------------------------------------------------------


class RequestType(str, Enum):
    APPROVAL = "APPROVAL"      # agent wants permission to proceed
    DIRECTION = "DIRECTION"    # agent needs human guidance
    ERROR = "ERROR"            # agent hit an unrecoverable error, needs triage
    SUSPENSION = "SUSPENSION"  # agent being suspended by orchestrator, notify human


@dataclass
class InteractionRequest:
    agent_id: str
    request_type: RequestType
    prompt: str
    context: dict = field(default_factory=dict)
    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    short_id: str = ""
    created_at: float = field(default_factory=time.time)

    def __post_init__(self) -> None:
        self.short_id = self.id[:8]


@dataclass
class InteractionResponse:
    interaction_id: str
    short_id: str
    response_type: str          # APPROVE | DENY | DIRECT | TERMINATE
    nick: str
    trust_level: TrustLevel
    payload: dict = field(default_factory=dict)
    timestamp: float = field(default_factory=time.time)


# ---------------------------------------------------------------------------
# SQLite repository for interaction_requests
# ---------------------------------------------------------------------------


class InteractionRepository:
    """Thin persistence layer for interaction_requests rows."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def create_request(self, req: InteractionRequest) -> None:
        self._conn.execute(
            """
            INSERT INTO interaction_requests
                (id, short_id, agent_id, interaction_type, prompt,
                 status, created_at)
            VALUES (?, ?, ?, ?, ?, 'PENDING', ?)
            """,
            (req.id, req.short_id, req.agent_id, req.request_type.value,
             req.prompt, req.created_at),
        )
        self._conn.commit()

    def record_response(self, short_id: str, resp: InteractionResponse) -> None:
        self._conn.execute(
            """
            UPDATE interaction_requests
            SET status           = 'RESPONDED',
                response_type    = ?,
                response_nick    = ?,
                response_payload = ?,
                responded_at     = ?
            WHERE short_id = ? AND status = 'PENDING'
            """,
            (
                resp.response_type,
                resp.nick,
                json.dumps(resp.payload),
                resp.timestamp,
                short_id,
            ),
        )
        self._conn.commit()


# ---------------------------------------------------------------------------
# IRC utility functions (kept verbatim across rewrites)
# ---------------------------------------------------------------------------


def split_irc(text: str) -> list[str]:
    """Split *text* into chunks of at most MAX_LINE characters."""
    if len(text) <= MAX_LINE:
        return [text]
    chunks: list[str] = []
    while text:
        chunks.append(text[:MAX_LINE])
        text = text[MAX_LINE:]
    return chunks


# ---------------------------------------------------------------------------
# IRCGateway
# ---------------------------------------------------------------------------


class IRCGateway:
    """Async IRC gateway for human-in-the-loop agent interactions.

    Typical lifecycle::

        gateway = IRCGateway(config, registry, tracer, logger, conn)
        gateway_task = asyncio.create_task(gateway.run())
        try:
            await orchestrator.run()
        finally:
            await gateway.disconnect()
            gateway_task.cancel()
    """

    def __init__(
        self,
        config: dict,
        registry: TrustedNickRegistry,
        tracer: otel_trace.Tracer,
        logger,
        conn: sqlite3.Connection,
    ) -> None:
        server = config.get("server", {})
        bot = config.get("bot", {})

        self._host: str = server.get("host", "127.0.0.1")
        self._port: int = int(server.get("port", 6667))
        self._nick: str = bot.get("nick", "orchestrator")
        self._channel: str = bot.get("channel", "#agents")

        self._registry = registry
        self._tracer = tracer
        self._logger = logger
        self._repo = InteractionRepository(conn)

        # asyncio transport handles
        self._reader: Optional[asyncio.StreamReader] = None
        self._writer: Optional[asyncio.StreamWriter] = None
        self._lock = asyncio.Lock()

        # short_id → (asyncio.Event, InteractionResponse | None)
        self._pending: dict[str, tuple[asyncio.Event, Optional[InteractionResponse]]] = {}

        self._buf = ""

        # Optional hooks wired by the orchestrator at startup
        self.on_freeform: Optional[
            Callable[[str, TrustedNick, str], Awaitable[None]]
        ] = None
        self.on_terminate: Optional[
            Callable[[str, str], Awaitable[None]]
        ] = None

    # ------------------------------------------------------------------
    # Connection management
    # ------------------------------------------------------------------

    async def connect(self) -> None:
        """Open TCP connection to ngircd and register the bot nick."""
        with self._tracer.start_as_current_span("irc.connect") as span:
            span.set_attribute("irc.host", self._host)
            span.set_attribute("irc.port", self._port)
            span.set_attribute("irc.nick", self._nick)

            self._reader, self._writer = await asyncio.open_connection(
                self._host, self._port
            )
            await self._send(f"NICK {self._nick}")
            await self._send(f"USER {self._nick} 0 * :{self._nick}")
            self._logger.info(
                "irc.connected",
                extra={"host": self._host, "port": self._port, "nick": self._nick},
            )

    async def disconnect(self) -> None:
        """Send QUIT and close the connection gracefully."""
        if self._writer is None:
            return
        try:
            await self.notify(f"[info] {self._nick} shutting down.")
            await self._send("QUIT :orchestrator shutting down")
            self._writer.close()
            await self._writer.wait_closed()
        except Exception:  # noqa: BLE001
            pass
        finally:
            self._writer = None
            self._reader = None

    # ------------------------------------------------------------------
    # Main receive loop
    # ------------------------------------------------------------------

    async def run(self) -> None:
        """Main receive loop — run as a long-lived asyncio task."""
        await self.connect()
        assert self._reader is not None
        try:
            while True:
                data = await self._reader.read(4096)
                if not data:
                    self._logger.warning("irc.disconnected")
                    break
                self._buf += data.decode("utf-8", errors="replace")
                while "\r\n" in self._buf:
                    line, self._buf = self._buf.split("\r\n", 1)
                    if line:
                        await self.handle_line(line)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            self._logger.error("irc.run_error", extra={"error": str(exc)})

    # ------------------------------------------------------------------
    # Raw line I/O (kept verbatim)
    # ------------------------------------------------------------------

    async def _send(self, line: str) -> None:
        """Write a raw IRC line to the server."""
        if self._writer is None:
            return
        async with self._lock:
            self._writer.write((line + "\r\n").encode("utf-8", errors="replace"))
            await self._writer.drain()

    async def send_message(self, target: str, text: str) -> None:
        """Send PRIVMSG lines to *target* with flood-prevention delay."""
        for chunk in split_irc(text):
            await self._send(f"PRIVMSG {target} :{chunk}")
            await asyncio.sleep(0.05)

    # ------------------------------------------------------------------
    # Line parser (kept verbatim)
    # ------------------------------------------------------------------

    async def handle_line(self, line: str) -> None:
        """Parse a raw IRC server line and dispatch appropriately."""
        # PING — respond immediately to stay connected
        if line.startswith("PING"):
            token = line[5:].strip()
            await self._send(f"PONG :{token}")
            return

        parts = line.split(" ", 3)
        if len(parts) < 2:
            return

        command = parts[1]

        # 001 welcome — join the designated channel
        if command == "001":
            await self._send(f"JOIN {self._channel}")
            return

        # PRIVMSG — route to dispatcher
        if command == "PRIVMSG" and len(parts) >= 4:
            prefix = parts[0].lstrip(":")
            nick = prefix.split("!")[0]
            target = parts[2]
            message = parts[3].lstrip(":")
            if target in (self._channel, self._nick):
                await self._dispatch(nick, target, message)

    # ------------------------------------------------------------------
    # Command dispatch (trust gate + structured parser)
    # ------------------------------------------------------------------

    async def _dispatch(self, nick: str, _channel: str, message: str) -> None:
        # Ignore own messages
        if nick == self._nick:
            return

        # Trust gate — silently ignore untrusted nicks
        trusted = self._registry.get(nick)
        if not trusted:
            return

        # Structured command (starts with !)
        if message.startswith("!"):
            asyncio.create_task(
                self._handle_command(nick, trusted, message[1:].strip())
            )
            return

        # Freeform trigger patterns (retained for backwards compatibility)
        prompt: Optional[str] = None
        if message.lower().startswith(f"{self._nick.lower()}:"):
            prompt = message[len(self._nick) + 1:].strip()
        elif message.lower().startswith(f"{self._nick.lower()},"):
            prompt = message[len(self._nick) + 1:].strip()

        if prompt:
            await self._route_freeform(nick, trusted, prompt)
        else:
            # Freeform message not addressed to bot — log and ignore
            self._logger.debug(
                "irc.freeform_ignored",
                extra={"nick": nick, "message": message[:80]},
            )

    async def _handle_command(
        self, nick: str, trusted: TrustedNick, cmd: str
    ) -> None:
        with self._tracer.start_as_current_span("irc.command.received") as span:
            span.set_attribute("irc.nick", nick)
            span.set_attribute("irc.command", cmd[:120])

            parts = cmd.split(None, 2)
            verb = parts[0].lower() if parts else ""

            if verb == "approve" and len(parts) >= 2:
                short_id = parts[1]
                self._resolve(
                    short_id,
                    InteractionResponse(
                        interaction_id="",
                        short_id=short_id,
                        response_type="APPROVE",
                        nick=nick,
                        trust_level=trusted.level,
                    ),
                )

            elif verb == "deny" and len(parts) >= 2:
                short_id = parts[1]
                reason = parts[2] if len(parts) == 3 else ""
                self._resolve(
                    short_id,
                    InteractionResponse(
                        interaction_id="",
                        short_id=short_id,
                        response_type="DENY",
                        nick=nick,
                        trust_level=trusted.level,
                        payload={"reason": reason},
                    ),
                )

            elif verb == "direct" and len(parts) >= 3:
                short_id = parts[1]
                instruction = parts[2]
                self._resolve(
                    short_id,
                    InteractionResponse(
                        interaction_id="",
                        short_id=short_id,
                        response_type="DIRECT",
                        nick=nick,
                        trust_level=trusted.level,
                        payload={"instruction": instruction},
                    ),
                )

            elif verb == "terminate" and len(parts) >= 2:
                if trusted.level != TrustLevel.OWNER:
                    await self.notify(f"{nick}: only owners can terminate agents.")
                    return
                agent_id_fragment = parts[1]
                await self._route_terminate(nick, agent_id_fragment)

            elif verb == "reset":
                await self.notify(
                    "No conversation history to reset (history lives in agents)."
                )

            elif verb == "model":
                await self.notify(
                    "This gateway uses claude-opus-4-6 via the orchestration framework."
                )

            else:
                await self.notify(f"{nick}: unknown command '{verb}'.")

    def _resolve(self, short_id: str, response: InteractionResponse) -> None:
        """Fire the asyncio.Event for a pending interaction."""
        if short_id not in self._pending:
            self._logger.warning(
                "irc.unknown_short_id", extra={"short_id": short_id}
            )
            return
        event, _ = self._pending[short_id]
        self._pending[short_id] = (event, response)
        event.set()

    # ------------------------------------------------------------------
    # Interaction lifecycle
    # ------------------------------------------------------------------

    async def post_interaction(
        self, request: InteractionRequest
    ) -> InteractionResponse:
        """Post a request to IRC and suspend until a trusted nick responds.

        Caller is responsible for transitioning agent state before/after.
        """
        # 1. Persist to DB
        self._repo.create_request(request)

        # 2. Register pending event keyed by short_id
        event: asyncio.Event = asyncio.Event()
        self._pending[request.short_id] = (event, None)

        # 3. Format and post IRC message
        irc_msg = self._format_request(request)
        await self.notify(irc_msg)

        self._logger.info(
            "irc.interaction_posted",
            extra={
                "agent_id": request.agent_id,
                "type": request.request_type.value,
                "short_id": request.short_id,
            },
        )

        # 4. Wait for response (indefinite — agent stays SUSPENDED)
        wait_start = time.time()
        with self._tracer.start_as_current_span("irc.interaction.wait") as span:
            span.set_attribute("agent.id", request.agent_id)
            span.set_attribute("irc.short_id", request.short_id)
            span.set_attribute("irc.request_type", request.request_type.value)

            await event.wait()

            _raw = self._pending.pop(request.short_id)[1]
            if _raw is None:
                raise RuntimeError(
                    f"event fired for {request.short_id!r} but response is None"
                )
            response: InteractionResponse = _raw
            wait_s = round(time.time() - wait_start, 1)
            span.set_attribute("irc.response_type", response.response_type)
            span.set_attribute("irc.response_nick", response.nick)
            span.set_attribute("irc.wait_duration_seconds", wait_s)

        self._logger.info(
            "irc.response_received",
            extra={
                "agent_id": request.agent_id,
                "type": response.response_type,
                "nick": response.nick,
                "wait_s": wait_s,
            },
        )

        # 5. Persist response
        self._repo.record_response(request.short_id, response)
        return response

    async def notify(self, message: str) -> None:
        """Post an informational message to the channel (no response expected)."""
        with self._tracer.start_as_current_span("irc.notify"):
            await self.send_message(self._channel, message)

    # ------------------------------------------------------------------
    # Message formatting
    # ------------------------------------------------------------------

    def _format_request(self, request: InteractionRequest) -> str:
        """Build the IRC message string for an InteractionRequest."""
        sid = request.short_id
        rtype = request.request_type.value

        # Derive a short agent label from the agent_id
        agent_label = request.agent_id[:12]

        body = f"[req:{sid}] {agent_label} ({rtype}): {request.prompt}"

        if request.request_type in (RequestType.APPROVAL, RequestType.DIRECTION):
            reply_hint = (
                f"Reply: !approve {sid}  |  !deny {sid} [reason]"
                f"  |  !direct {sid} <instruction>"
            )
        elif request.request_type == RequestType.ERROR:
            reply_hint = (
                f"Reply: !direct {sid} <instruction>  |  !terminate {agent_label}"
            )
        else:
            reply_hint = f"Reply: !direct {sid} <instruction>"

        return f"{body}  {reply_hint}"

    # ------------------------------------------------------------------
    # Routing stubs (wired by orchestrator at startup)
    # ------------------------------------------------------------------

    async def _route_freeform(
        self, nick: str, trusted: TrustedNick, prompt: str
    ) -> None:
        """Route a freeform bot-addressed message to the orchestrator context.

        Default implementation logs and echoes back; the orchestrator can
        replace this by subclassing or by setting ``gateway.on_freeform``.
        """
        self._logger.debug(
            "irc.freeform",
            extra={"nick": nick, "trust": trusted.level.value, "prompt": prompt[:120]},
        )
        if self.on_freeform is not None:
            await self.on_freeform(nick, trusted, prompt)
        else:
            await self.notify(
                f"[info] Freeform message from {nick} received; "
                "no orchestrator handler registered."
            )

    async def _route_terminate(self, nick: str, agent_id_fragment: str) -> None:
        """Route a !terminate command to the orchestrator via MessageBus.

        Default implementation notifies the channel; the orchestrator registers
        a handler via ``gateway.on_terminate``.
        """
        self._logger.info(
            "irc.terminate_requested",
            extra={"nick": nick, "agent_fragment": agent_id_fragment},
        )
        if self.on_terminate is not None:
            await self.on_terminate(nick, agent_id_fragment)
        else:
            await self.notify(
                f"[info] Terminate requested by {nick} for agent '{agent_id_fragment}'; "
                "no orchestrator handler registered."
            )
