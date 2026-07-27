"""Widgets for the terminal interface.

The game view is a ``RichLog``, which appends. The previous client kept a list
of up to 5000 lines in a ``Static`` and re-joined and re-rendered all of them on
every packet, which is why it felt sluggish.

BatClient colour codes are translated into Rich styles rather than stripped, so
the game looks the way the game intends.
"""

from __future__ import annotations

from dataclasses import dataclass

from rich.style import Style as RichStyle
from rich.text import Text
from textual.reactive import reactive
from textual.widgets import RichLog, Static

from ..agent.safety import Decision, Source
from ..protocol.events import Line, Style
from ..world.state import Vital, WorldState

BAR_FILLED = "\u2588"
BAR_EMPTY = "\u2591"

SOURCE_COLOURS = {
    Source.LOGIN: "#7f7fff",
    Source.REFLEX: "#ffcc00",
    Source.PLANNER: "#00ff88",
    Source.MANUAL: "#ffffff",
}


def to_rich_style(style: Style) -> RichStyle:
    """Translate a BatClient style into a Rich style."""
    return RichStyle(
        color=f"#{style.fg}" if style.fg else None,
        bgcolor=f"#{style.bg}" if style.bg else None,
        bold=style.bold or None,
        italic=style.italic or None,
        underline=style.underline or None,
        blink=style.blink or None,
        link=style.href or None,
    )


def to_rich_text(line: Line, default: str = "") -> Text:
    """Render a parsed line, preserving the styles the server sent."""
    if not line.spans:
        return Text("")
    text = Text()
    for span in line.spans:
        style = to_rich_style(span.style)
        if not span.style.fg and default:
            style += RichStyle(color=default)
        text.append(span.text, style=style)
    return text


def render_bar(label: str, vital: Vital, colour: str, width: int = 14) -> Text:
    """A labelled bar with the exact numbers beside it."""
    if not vital.known:
        return Text.assemble((f"{label} ", "dim"), ("unknown", "dim"))

    fraction = max(0.0, min(1.0, vital.fraction))
    filled = round(fraction * width)
    bar = Text()
    bar.append(f"{label} ", style="bold")
    bar.append(BAR_FILLED * filled, style=colour)
    bar.append(BAR_EMPTY * (width - filled), style="#333333")
    bar.append(f" {vital.current:,}/{vital.maximum:,}", style="dim")
    return bar


class GameLog(RichLog):
    """The main game output."""

    def __init__(self, *, default_colour: str = "#33dd55", **kwargs: object) -> None:
        super().__init__(highlight=False, markup=False, wrap=True, auto_scroll=True, **kwargs)  # type: ignore[arg-type]
        self.default_colour = default_colour

    def add_line(self, line: Line) -> None:
        self.write(to_rich_text(line, self.default_colour))

    def add_notice(self, message: str, colour: str = "#ffcc00") -> None:
        self.write(Text(message, style=f"bold {colour}"))


class VitalsPanel(Static):
    """Health, spell and endurance points, plus the character's identity."""

    def show(self, state: WorldState) -> None:
        lines = [
            render_bar("HP", state.hp, "#00dd44"),
            render_bar("SP", state.sp, "#3399ff"),
            render_bar("EP", state.ep, "#ffaa22"),
        ]
        identity = Text()
        if state.name:
            identity.append(f"{state.name}", style="bold #ffffff")
            if state.race:
                identity.append(f" the {state.race}", style="#aaaaaa")
            identity.append(f"  lvl {state.level}", style="#aaaaaa")
        if state.experience:
            identity.append(f"\nexp {state.experience:,}", style="dim")
            if state.free_experience:
                identity.append(f"  free {state.free_experience:,}", style="dim")

        body = Text("\n").join(lines)
        if identity:
            body.append("\n")
            body.append(identity)
        self.update(body)


class StatusPanel(Static):
    """Position, target, effects and anything wrong with the character."""

    def show(self, state: WorldState, extra: str = "") -> None:
        text = Text()

        if state.room is not None:
            text.append(f"{state.room.title}\n", style="bold #ffffff")
            exits = ", ".join(state.room.exit_list) or "none"
            text.append(f"exits: {exits}\n", style="#aaaaaa")
        if state.coordinates is not None:
            text.append(f"{state.continent} ({state.x}, {state.y}, {state.z})\n", style="dim")

        if state.target is not None:
            text.append("target ", style="#aaaaaa")
            text.append(f"{state.target} ", style="bold #ff6644")
            text.append(f"~{state.target_health}%\n", style="#ff9977")
        elif state.in_combat:
            text.append("in combat\n", style="bold #ff6644")

        if state.action is not None:
            text.append(
                f"casting {state.action.name} ({state.action.rounds_left})\n",
                style="#ffcc00",
            )

        flags = [
            name
            for name, value in (
                ("UNCONSCIOUS", state.unconscious),
                ("STUNNED", state.stunned),
                ("DEAD", state.dead),
            )
            if value
        ]
        if flags:
            text.append(" ".join(flags) + "\n", style="bold #ff2222")

        if state.effects:
            effects = ", ".join(f"{name} {left}s" for name, left in sorted(state.effects.items()))
            text.append(f"effects: {effects}\n", style="#66ddff")

        if extra:
            text.append(extra, style="dim")

        self.update(text or Text("Not connected", style="dim"))


class PlanPanel(Static):
    """The current goal and the last thing the planner said."""

    def show(self, goal: str, reason: str, budget: str, note: str = "") -> None:
        text = Text()
        text.append("goal\n", style="bold #aaaaaa")
        text.append(f"{goal or 'none'}\n\n", style="#ffffff")
        if reason:
            text.append("last decision\n", style="bold #aaaaaa")
            text.append(f"{reason}\n\n", style="#cccccc")
        if note:
            text.append(f"{note}\n\n", style="#ffcc00")
        text.append("budget\n", style="bold #aaaaaa")
        text.append(budget, style="dim")
        self.update(text)


class DecisionLog(RichLog):
    """A timestamped record of every command and refusal."""

    def __init__(self, **kwargs: object) -> None:
        super().__init__(highlight=False, markup=False, wrap=True, auto_scroll=True, **kwargs)  # type: ignore[arg-type]

    def add_decision(self, timestamp: str, decision: Decision) -> None:
        colour = SOURCE_COLOURS.get(decision.source, "#ffffff")
        text = Text()
        text.append(f"{timestamp} ", style="dim")
        text.append(f"{decision.source.value:<7} ", style=colour)
        text.append(decision.display, style=f"bold {colour}")
        if decision.reason and not decision.secret:
            text.append(f"\n         {decision.reason}", style="dim")
        self.write(text)

    def add_refusal(self, timestamp: str, command: str, reason: str) -> None:
        text = Text()
        text.append(f"{timestamp} ", style="dim")
        text.append("refused ", style="#ff5555")
        text.append(command, style="bold #ff5555")
        text.append(f"\n         {reason}", style="dim")
        self.write(text)

    def add_notice(self, timestamp: str, message: str) -> None:
        text = Text()
        text.append(f"{timestamp} ", style="dim")
        text.append(message, style="#ffcc00")
        self.write(text)


@dataclass(frozen=True, slots=True)
class ApprovalState:
    """What the approval bar should say."""

    decision: Decision | None = None
    autonomous: bool = False
    paused: bool = False


class ApprovalBar(Static):
    """Shows the command awaiting a decision, or the current mode."""

    pending: reactive[Decision | None] = reactive(None)

    def show(self, approval: ApprovalState) -> None:
        text = Text()
        if approval.paused:
            text.append(" PAUSED ", style="bold white on #aa3300")
            text.append("  the agent is idle; type commands to play manually", style="dim")
        elif approval.decision is not None:
            text.append(" APPROVE ", style="bold black on #ffcc00")
            text.append(f"  {approval.decision.display}", style="bold #ffffff")
            if approval.decision.reason:
                text.append(f"   {approval.decision.reason}", style="dim")
            text.append("\n enter accept   f4 reject   type to edit", style="dim")
        elif approval.autonomous:
            text.append(" AUTONOMOUS ", style="bold black on #ff5555")
            text.append("  commands are sent without approval", style="dim")
        else:
            text.append(" CO-PILOT ", style="bold black on #00dd88")
            text.append("  waiting for the agent to propose a command", style="dim")
        self.update(text)
