"""Session orchestration.

Three cooperating tasks:

* the pump reads events, folds them into the world state and answers login
  prompts;
* the sender drains the outbound queue, pacing commands against the server's
  prompt rather than against a fixed sleep;
* the decider consults reflexes first and the planner only at decision points.

Commands are gated on ``IAC GA``. The previous client slept 0.5 seconds after
every command and hoped; here a command waits for the server to say it is
ready, with a timeout so a missing Go-Ahead cannot deadlock the session.
"""

from __future__ import annotations

import asyncio
import logging
import random
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Protocol

from ..agent.reflexes import LoopDetector, ReflexEngine
from ..agent.safety import CommandGuard, Decision, RateLimiter, Refusal, Source
from ..config import Settings
from ..protocol.events import (
    ConnectionFailed,
    ConnectionSucceeded,
    Disconnected,
    Event,
    PlayerInfo,
    Prompt,
)
from ..protocol.telnet import Connection
from ..world.state import WorldState
from .login import LoginController, LoginMode, LoginPhase

log = logging.getLogger(__name__)

DECISION_TRIGGERS = frozenset({"room", "combat", "login", "level", "status"})
"""State changes worth waking the planner for."""


class Planner(Protocol):
    """The part of the agent that costs money."""

    @property
    def available(self) -> bool: ...

    async def decide(
        self, state: WorldState, trigger: str, feedback: str = ""
    ) -> Decision | None: ...


@dataclass(slots=True)
class SessionHooks:
    """Everything the UI wants to be told about."""

    on_event: Callable[[Event], None] = lambda event: None
    on_sent: Callable[[Decision], None] = lambda decision: None
    on_refused: Callable[[Refusal], None] = lambda refusal: None
    on_status: Callable[[str], None] = lambda message: None
    on_pending: Callable[[Decision | None], None] = lambda decision: None


class ApprovalGate:
    """Holds a proposed command until a human accepts, edits or rejects it.

    This is what makes co-pilot the default mode: without approval enabled the
    gate is a pass-through, with it every planner and reflex decision stops
    here first.
    """

    def __init__(self, enabled: bool) -> None:
        self.enabled = enabled
        self._pending: Decision | None = None
        self._future: asyncio.Future[Decision | None] | None = None

    @property
    def pending(self) -> Decision | None:
        return self._pending

    async def submit(self, decision: Decision) -> Decision | None:
        if not self.enabled or decision.auto:
            return decision
        loop = asyncio.get_running_loop()
        self._pending = decision
        self._future = loop.create_future()
        try:
            return await self._future
        finally:
            self._pending = None
            self._future = None

    def resolve(self, command: str | None) -> bool:
        """Accept (optionally edited) or reject the pending decision."""
        future, decision = self._future, self._pending
        if future is None or future.done() or decision is None:
            return False
        if command is None:
            future.set_result(None)
        elif command == decision.command:
            future.set_result(decision)
        else:
            future.set_result(
                Decision(
                    command=command,
                    source=Source.MANUAL,
                    reason=f"edited from {decision.command!r}",
                )
            )
        return True

    def cancel(self) -> None:
        if self._future is not None and not self._future.done():
            self._future.set_result(None)


@dataclass(slots=True)
class SessionRunner:
    """Runs a BatMUD session from connect to disconnect."""

    settings: Settings
    state: WorldState
    reflexes: ReflexEngine
    planner: Planner | None = None
    hooks: SessionHooks = field(default_factory=SessionHooks)
    mode: LoginMode = LoginMode.LOGIN

    connection: Connection | None = None
    guard: CommandGuard | None = None
    limiter: RateLimiter | None = None
    loops: LoopDetector = field(default_factory=LoopDetector)
    approvals: ApprovalGate | None = None
    login: LoginController | None = None

    paused: bool = False

    _outbox: asyncio.Queue[Decision] = field(default_factory=asyncio.Queue)
    _wake: asyncio.Event = field(default_factory=asyncio.Event)
    _prompt_ready: asyncio.Event = field(default_factory=asyncio.Event)
    _stopping: bool = False
    _feedback: str = ""
    _last_trigger: str = "start"

    def __post_init__(self) -> None:
        safety = self.settings.safety
        if self.guard is None:
            secret = self.settings.character.password.get_secret_value()
            self.guard = CommandGuard(safety, secrets=(secret,) if secret else ())
        if self.limiter is None:
            self.limiter = RateLimiter(safety.max_commands_per_minute, safety.min_command_interval)
        if self.approvals is None:
            self.approvals = ApprovalGate(enabled=not self.settings.agent.autonomous)
        if self.login is None:
            self.login = LoginController(self.settings.character, mode=self.mode)

    # --- lifecycle ----------------------------------------------------------

    async def run(self) -> None:
        """Connect, run a session, and reconnect until stopped."""
        connection = self.settings.connection
        delay = connection.reconnect_delay
        while not self._stopping:
            try:
                await self._run_once()
                delay = connection.reconnect_delay
            except (TimeoutError, OSError) as error:
                self.hooks.on_status(f"Connection failed: {error}")
                log.warning("session ended with an error: %s", error)
            if self._stopping or not connection.reconnect:
                break
            jitter = random.uniform(0, delay * 0.2)
            self.hooks.on_status(f"Reconnecting in {delay + jitter:.0f}s...")
            await asyncio.sleep(delay + jitter)
            delay = min(delay * 2, connection.reconnect_max_delay)

    async def _run_once(self) -> None:
        settings = self.settings.connection
        self.connection = Connection(
            settings.host,
            settings.port,
            use_tls=settings.tls,
            verify_tls=settings.verify_tls,
            enable_batclient=settings.batclient,
        )
        self.login = LoginController(self.settings.character, mode=self.mode)
        self.state.connected = True
        self.state.reset_room_parser()
        self.loops.clear()

        await self.connection.connect()
        self.hooks.on_status(f"Connected to {settings.host}:{settings.port}")

        tasks = [
            asyncio.create_task(self._pump(), name="pump"),
            asyncio.create_task(self._sender(), name="sender"),
            asyncio.create_task(self._decider(), name="decider"),
        ]
        try:
            done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for task in pending:
                task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)
            for task in done:
                task.result()
        finally:
            if self.approvals is not None:
                self.approvals.cancel()
            await self.connection.close()
            self.state.connected = False

    async def stop(self) -> None:
        self._stopping = True
        if self.approvals is not None:
            self.approvals.cancel()
        if self.connection is not None:
            await self.connection.close()

    # --- tasks --------------------------------------------------------------

    async def _pump(self) -> None:
        """Read events, update state, and answer login prompts."""
        assert self.connection is not None
        async for event in self.connection.events():
            self.hooks.on_event(event)
            changed = self.state.apply(event)

            if self.login is not None:
                self.login.observe(event)

            match event:
                case Prompt():
                    self._prompt_ready.set()
                    await self._handle_login_prompt(event.text)
                case ConnectionSucceeded() | PlayerInfo():
                    self._announce_login()
                case ConnectionFailed():
                    self.hooks.on_status(f"Login refused: {event.reason}")
                case Disconnected():
                    detail = f": {event.reason}" if event.reason else ""
                    self.hooks.on_status(f"Disconnected{detail}")
                    return

            if changed & DECISION_TRIGGERS:
                self._last_trigger = ", ".join(sorted(changed & DECISION_TRIGGERS))
                self._wake.set()

    async def _handle_login_prompt(self, prompt: str) -> None:
        controller = self.login
        if controller is None or controller.finished:
            return
        reply = controller.respond(prompt)
        if reply is None:
            if controller.phase is LoginPhase.FAILED:
                self.hooks.on_status(f"Login failed: {controller.failure}")
            return
        await self._outbox.put(
            Decision(
                command=reply.command,
                source=Source.LOGIN,
                reason=reply.note,
                secret=reply.secret,
                auto=True,
            )
        )

    def _announce_login(self) -> None:
        if not self.state.logged_in:
            return
        if not self.state.batclient_active:
            return
        self.hooks.on_status("Logged in, BatClient protocol active")

    async def _sender(self) -> None:
        """Drain the outbox, pacing on the server's prompt."""
        assert self.connection is not None and self.guard is not None
        assert self.limiter is not None
        while True:
            decision = await self._outbox.get()

            refusal = self.guard.check(decision)
            if refusal is not None:
                log.info("refused %r: %s", refusal.command, refusal.reason)
                self.hooks.on_refused(refusal)
                self._feedback = f"Command {refusal.command!r} was refused: {refusal.reason}"
                self._wake.set()
                continue

            await self._await_prompt()
            delay = self.limiter.delay()
            if delay > 0:
                await asyncio.sleep(delay)

            try:
                await self.connection.send(decision.command)
            except (OSError, ConnectionError) as error:
                self.hooks.on_status(f"Send failed: {error}")
                return

            self.limiter.record()
            self._prompt_ready.clear()
            if not decision.secret:
                self.state.note_command(decision.command)
                self.loops.record(decision.command)
            self.hooks.on_sent(decision)

    async def _await_prompt(self) -> None:
        """Wait until the server is ready, but never forever."""
        timeout = self.settings.safety.prompt_timeout
        if timeout <= 0:
            return
        try:
            await asyncio.wait_for(self._prompt_ready.wait(), timeout)
        except TimeoutError:
            log.debug("no prompt within %.1fs, sending anyway", timeout)

    async def _decider(self) -> None:
        """Choose the next command: reflexes first, planner second."""
        idle = self.settings.agent.idle_seconds
        while True:
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=idle)
                trigger = self._last_trigger
            except TimeoutError:
                trigger = "idle"
            self._wake.clear()

            if self.paused:
                continue
            if not self.state.logged_in or not self.reflexes.should_act(self.state):
                continue
            if self.login is not None and not self.login.finished:
                continue
            if self.approvals is not None and self.approvals.pending is not None:
                continue

            decision = self.reflexes.evaluate(self.state, self.state.last_prompt)
            if decision is None:
                decision = await self._ask_planner(trigger)
            if decision is None:
                continue

            await self._propose(decision)

    async def _ask_planner(self, trigger: str) -> Decision | None:
        if self.planner is None or not self.planner.available:
            return None
        feedback, self._feedback = self._feedback, ""
        if (loop := self.loops.detect()) is not None:
            feedback = f"{feedback}\nYou are repeating: {loop}. Try something different.".strip()
            self.loops.clear()
        try:
            return await self.planner.decide(self.state, trigger, feedback)
        except Exception as error:
            log.exception("planner failed")
            self.hooks.on_status(f"Planner error: {error}")
            return None

    async def _propose(self, decision: Decision) -> None:
        """Route a decision through approval and onto the outbox."""
        assert self.approvals is not None
        needs_approval = self.approvals.enabled and not decision.auto
        self.hooks.on_pending(decision if needs_approval else None)
        approved = await self.approvals.submit(decision)
        if needs_approval:
            self.hooks.on_pending(None)
        if approved is not None:
            await self._outbox.put(approved)

    # --- external input -----------------------------------------------------

    async def send_manual(self, command: str) -> None:
        """Queue a command typed by the user."""
        await self._outbox.put(Decision(command=command, source=Source.MANUAL, auto=True))

    def approve(self, command: str | None = None) -> bool:
        """Accept the pending proposal, optionally with an edited command."""
        if self.approvals is None:
            return False
        pending = self.approvals.pending
        if pending is None:
            return False
        return self.approvals.resolve(pending.command if command is None else command)

    def reject(self) -> bool:
        return self.approvals.resolve(None) if self.approvals is not None else False

    def wake(self) -> None:
        """Ask the decider to reconsider, e.g. after the user unpauses."""
        self._wake.set()

    def set_paused(self, paused: bool) -> None:
        """Stop or resume the agent. Game output keeps flowing either way."""
        self.paused = paused
        if paused:
            self.reject()
        else:
            self.wake()

    def set_autonomous(self, autonomous: bool) -> None:
        """Switch between co-pilot approval and unattended play."""
        if self.approvals is None:
            return
        if autonomous:
            self.approvals.cancel()
        self.approvals.enabled = not autonomous
        self.wake()

    @property
    def autonomous(self) -> bool:
        return self.approvals is not None and not self.approvals.enabled
