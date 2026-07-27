"""The terminal interface.

One input line drives everything: with a proposal pending, Enter accepts it and
typing replaces it; with nothing pending, whatever you type is sent as a manual
command. Global actions are on function keys so they cannot be swallowed by, or
swallow, what you are typing. The previous client bound ``q`` to quit at the
application level, which made the letter unusable in the command box.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import ClassVar

from rich.text import Text
from textual.app import App, ComposeResult
from textual.binding import Binding, BindingType
from textual.containers import Horizontal, Vertical
from textual.widgets import Footer, Header, Input, RichLog, TabbedContent, TabPane

from ..agent.memory import Memory
from ..agent.planner import LLMPlanner
from ..agent.reflexes import ReflexEngine
from ..agent.safety import Budget, Decision, Refusal
from ..config import Settings
from ..llm.client import LLMClient
from ..protocol.events import ClearScreen, Disconnected, Event, Line, Prompt
from ..session.login import LoginMode
from ..session.runner import SessionHooks, SessionRunner
from ..world.map import WorldMap
from ..world.state import WorldState
from .widgets import (
    ApprovalBar,
    ApprovalState,
    DecisionLog,
    GameLog,
    PlanPanel,
    StatusPanel,
    VitalsPanel,
)

BATTLE_CHANNEL = "spec_battle"
MAP_CHANNEL = "spec_map"


class TuiLogHandler(logging.Handler):
    """Routes log records into the Log tab."""

    def __init__(self, sink: RichLog) -> None:
        super().__init__()
        self.sink = sink

    def emit(self, record: logging.LogRecord) -> None:
        try:
            colour = {
                logging.ERROR: "#ff5555",
                logging.CRITICAL: "#ff5555",
                logging.WARNING: "#ffcc00",
            }.get(record.levelno, "#888888")
            self.sink.write(
                Text.assemble(
                    (f"{record.levelname:<8}", colour),
                    (f"{record.name} ", "dim"),
                    (self.format(record), "#cccccc"),
                )
            )
        except Exception:
            self.handleError(record)


class BatMudApp(App[None]):
    """The BatMUD AI client."""

    TITLE = "BatMUD AI Client"
    CSS = """
    Screen { background: #0b0f0b; }
    Header { background: #12261a; color: #7dffb0; }
    Footer { background: #12261a; color: #7dffb0; }

    #body { height: 1fr; }
    #left { width: 3fr; }
    #right { width: 40; min-width: 32; }

    TabbedContent { height: 1fr; }
    RichLog {
        background: #0b0f0b;
        color: #33dd55;
        scrollbar-background: #12261a;
        scrollbar-color: #2f7f4f;
        padding: 0 1;
    }

    #vitals, #status, #plan {
        border: round #2f7f4f;
        background: #0b0f0b;
        color: #cccccc;
        padding: 0 1;
    }
    #vitals { height: 7; }
    #status { height: 11; }
    #plan { height: 1fr; min-height: 8; }

    #approval {
        height: 2;
        background: #12261a;
        padding: 0 1;
    }
    #command {
        border: round #2f7f4f;
        background: #0b0f0b;
        color: #7dffb0;
        height: 3;
    }
    """

    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("ctrl+q", "quit", "Quit", priority=True),
        Binding("f2", "toggle_pause", "Pause"),
        Binding("f3", "toggle_autonomous", "Autonomy"),
        Binding("f4", "reject", "Reject"),
        Binding("f5", "think", "Think now"),
        Binding("ctrl+l", "clear_game", "Clear", show=False),
    ]

    def __init__(
        self,
        settings: Settings,
        mode: LoginMode = LoginMode.LOGIN,
        *,
        connect: bool = True,
    ) -> None:
        super().__init__()
        self.settings = settings
        self.mode = mode
        self.connect_on_mount = connect

        self.world_map = WorldMap(settings.map_path)
        self.memory = Memory.load(settings.memory_path, default_goal=settings.agent.goal)
        self.state = WorldState(world_map=self.world_map)
        self.state.max_recent_lines = max(200, settings.agent.max_recent_lines * 3)

        self.budget = Budget(
            max_requests=settings.llm.max_requests,
            max_total_tokens=settings.llm.max_total_tokens,
            max_spend_usd=settings.llm.max_spend_usd,
        )
        self.llm = LLMClient(settings=settings.llm, budget=self.budget)
        secret = settings.character.password.get_secret_value()
        self.planner = LLMPlanner(
            settings=settings,
            llm=self.llm,
            memory=self.memory,
            world_map=self.world_map,
            secrets=(secret,) if secret else (),
        )
        self.runner = SessionRunner(
            settings=settings,
            state=self.state,
            reflexes=ReflexEngine(settings),
            planner=self.planner,
            hooks=SessionHooks(
                on_event=self._on_event,
                on_sent=self._on_sent,
                on_refused=self._on_refused,
                on_status=self._on_status,
                on_pending=self._on_pending,
            ),
            mode=mode,
        )

        self.game_log = GameLog(id="game-log")
        self.channel_log = GameLog(id="channel-log", default_colour="#66ddff")
        self.battle_log = GameLog(id="battle-log", default_colour="#ff9977")
        self.map_log = GameLog(id="map-log", default_colour="#aaaaaa")
        self.decisions = DecisionLog(id="decision-log")
        self.app_log = RichLog(id="app-log", highlight=False, markup=False, wrap=True)
        self.vitals = VitalsPanel(id="vitals")
        self.status = StatusPanel(id="status")
        self.plan = PlanPanel(id="plan")
        self.approval = ApprovalBar(id="approval")
        self.command = Input(placeholder="command (enter accepts a proposal)", id="command")
        self._notice = ""

    # --- layout -------------------------------------------------------------

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with Horizontal(id="body"):
            with Vertical(id="left"), TabbedContent(initial="tab-game", id="left-tabs"):
                with TabPane("Game", id="tab-game"):
                    yield self.game_log
                with TabPane("Battle", id="tab-battle"):
                    yield self.battle_log
                with TabPane("Channels", id="tab-channels"):
                    yield self.channel_log
                with TabPane("Map", id="tab-map"):
                    yield self.map_log
                with TabPane("Log", id="tab-log"):
                    yield self.app_log
            with Vertical(id="right"), TabbedContent(initial="tab-state", id="right-tabs"):
                with TabPane("State", id="tab-state"):
                    yield self.vitals
                    yield self.status
                    yield self.plan
                with TabPane("Decisions", id="tab-decisions"):
                    yield self.decisions
        yield self.approval
        yield self.command
        yield Footer()

    async def on_mount(self) -> None:
        self._install_log_handler()
        self.command.focus()
        self._refresh_panels()
        self.game_log.add_notice(
            f"BatMUD AI Client - {'autonomous' if self.runner.autonomous else 'co-pilot'} mode"
        )
        if not self.planner.available:
            self.game_log.add_notice(
                f"Planner disabled: {self.planner.unavailable_reason}. "
                "Reflexes and manual play still work."
            )
        self.set_interval(1.0, self._refresh_panels)
        if self.connect_on_mount:
            self.run_worker(self.runner.run(), name="session", exclusive=True)

    def _install_log_handler(self) -> None:
        handler = TuiLogHandler(self.app_log)
        handler.setFormatter(logging.Formatter("%(message)s"))
        root = logging.getLogger("batmud")
        root.addHandler(handler)
        root.setLevel(getattr(logging, self.settings.log_level.upper(), logging.INFO))

    async def on_unmount(self) -> None:
        self.memory.save()
        self.world_map.close()

    # --- session hooks ------------------------------------------------------

    def _on_event(self, event: Event) -> None:
        match event:
            case Line():
                self._write_line(event)
            case Prompt():
                if event.text.strip():
                    self.status.show(self.state, extra=event.text.strip())
            case ClearScreen():
                if event.window == MAP_CHANNEL:
                    self.map_log.clear()
            case Disconnected():
                self.game_log.add_notice("Disconnected.", "#ff5555")

    def _write_line(self, line: Line) -> None:
        channel = line.channel
        if channel == BATTLE_CHANNEL:
            self.battle_log.add_line(line)
        elif channel == MAP_CHANNEL:
            self.map_log.add_line(line)
        elif channel and channel.startswith("chan_"):
            self.channel_log.write(
                Text.assemble((f"[{channel.removeprefix('chan_')}] ", "dim"), line.text)
            )
        else:
            self.game_log.add_line(line)

    def _on_sent(self, decision: Decision) -> None:
        self.decisions.add_decision(_now(), decision)

    def _on_refused(self, refusal: Refusal) -> None:
        self.decisions.add_refusal(_now(), refusal.command, refusal.reason)
        self.game_log.add_notice(f"Refused {refusal.command!r}: {refusal.reason}", "#ff5555")

    def _on_status(self, message: str) -> None:
        self._notice = message
        self.decisions.add_notice(_now(), message)
        self.game_log.add_notice(message)
        self.sub_title = message

    def _on_pending(self, decision: Decision | None) -> None:
        self.approval.pending = decision
        self._render_approval()

    # --- panels -------------------------------------------------------------

    def _refresh_panels(self) -> None:
        self.vitals.show(self.state)
        self.status.show(self.state)
        note = ""
        if not self.planner.available:
            note = f"planner off: {self.planner.unavailable_reason}"
        elif self.planner.last_error:
            note = f"last planner issue: {self.planner.last_error}"
        elif not self.state.batclient_active and self.state.logged_in:
            note = "control codes not seen; using text parsing"
        self.plan.show(self.memory.goal, self.planner.last_feedback, self.budget.summary(), note)
        self._render_approval()

    def _render_approval(self) -> None:
        self.approval.show(
            ApprovalState(
                decision=self.approval.pending,
                autonomous=self.runner.autonomous,
                paused=self.runner.paused,
            )
        )

    # --- input --------------------------------------------------------------

    async def on_input_submitted(self, event: Input.Submitted) -> None:
        text = event.value
        event.input.value = ""

        if self.approval.pending is not None:
            # Empty accepts the proposal as-is; anything typed replaces it.
            self.runner.approve(text.strip() or None)
            return

        await self.runner.send_manual(text)

    # --- actions ------------------------------------------------------------

    def action_toggle_pause(self) -> None:
        self.runner.set_paused(not self.runner.paused)
        self._on_status("Agent paused" if self.runner.paused else "Agent resumed")
        self._render_approval()

    def action_toggle_autonomous(self) -> None:
        autonomous = not self.runner.autonomous
        self.runner.set_autonomous(autonomous)
        self._on_status(
            "Autonomous mode on - commands will be sent without approval. "
            "BatMUD's rules forbid unattended play."
            if autonomous
            else "Co-pilot mode on - commands need approval."
        )
        self._render_approval()

    def action_reject(self) -> None:
        if self.runner.reject():
            self.decisions.add_notice(_now(), "Proposal rejected")

    def action_think(self) -> None:
        self.runner.wake()

    def action_clear_game(self) -> None:
        self.game_log.clear()

    async def action_quit(self) -> None:
        await self.runner.stop()
        self.memory.save()
        self.exit()


def _now() -> str:
    return datetime.now().strftime("%H:%M:%S")
