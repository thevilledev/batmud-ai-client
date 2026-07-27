"""Interface tests driven through Textual's own test pilot."""

from __future__ import annotations

import asyncio

import pytest
from pydantic import SecretStr
from rich.text import Text
from textual.pilot import Pilot
from textual.widgets import RichLog, TabbedContent

from batmud.agent.safety import Decision, Refusal, Source
from batmud.config import Settings
from batmud.protocol.events import Line, Span, Style, Target, Vitals
from batmud.tui.app import BatMudApp
from batmud.tui.widgets import ApprovalState, render_bar, to_rich_style, to_rich_text
from batmud.world.state import Vital

# --- style translation ------------------------------------------------------


def test_colours_survive_the_trip_to_rich() -> None:
    style = to_rich_style(Style(fg="ff0000", bg="0000ff", bold=True, underline=True))
    assert style.color is not None and style.color.triplet is not None
    assert style.color.triplet.hex == "#ff0000"
    assert style.bgcolor is not None and style.bgcolor.triplet is not None
    assert style.bgcolor.triplet.hex == "#0000ff"
    assert style.bold and style.underline


def test_hyperlinks_are_preserved() -> None:
    assert to_rich_style(Style(href="https://www.bat.org")).link == "https://www.bat.org"


def test_a_styled_line_becomes_styled_rich_text() -> None:
    line = Line(
        spans=(Span("plain "), Span("red", Style(fg="ff0000"))),
    )
    text = to_rich_text(line)
    assert text.plain == "plain red"
    coloured = [span for span in text.spans if span.style.color is not None]
    assert len(coloured) == 1
    assert (coloured[0].start, coloured[0].end) == (6, 9)


def test_the_default_colour_only_fills_in_where_the_game_gave_none() -> None:
    line = Line(spans=(Span("plain"), Span("red", Style(fg="ff0000"))))
    text = to_rich_text(line, default="#33dd55")
    first, second = text.spans
    assert isinstance(first.style, object) and isinstance(second.style, object)
    assert "33dd55" in str(first.style)
    assert "ff0000" in str(second.style)


# --- bars -------------------------------------------------------------------


def test_bar_shows_the_exact_numbers() -> None:
    bar = render_bar("HP", Vital(current=50, maximum=200), "#00dd44")
    assert "50/200" in bar.plain
    assert "HP" in bar.plain


def test_bar_reports_unknown_before_any_control_code() -> None:
    assert "unknown" in render_bar("HP", Vital(), "#00dd44").plain


@pytest.mark.parametrize(("current", "maximum"), [(0, 100), (100, 100), (150, 100)])
def test_bar_stays_within_its_width(current: int, maximum: int) -> None:
    bar = render_bar("HP", Vital(current=current, maximum=maximum), "#00dd44", width=10)
    filled = bar.plain.count("\u2588")
    empty = bar.plain.count("\u2591")
    assert filled + empty == 10


# --- approval bar -----------------------------------------------------------


def _plain(widget: object) -> str:
    """The visible text of a Static-derived widget."""
    content = widget.content  # type: ignore[attr-defined]
    return content.plain if isinstance(content, Text) else str(content)


async def _read_log(pilot: Pilot[None], tabs: str, tab: str, selector: str) -> str:
    """Read a RichLog, making its tab visible first.

    A RichLog only renders, and so only has lines to read, once it has been
    given a size, which does not happen while its tab is hidden.
    """
    app = pilot.app
    app.query_one(f"#{tabs}", TabbedContent).active = tab
    await pilot.pause()
    widget = app.query_one(selector, RichLog)
    return "\n".join(strip.text for strip in widget.lines)


async def _game_text(pilot: Pilot[None]) -> str:
    return await _read_log(pilot, "left-tabs", "tab-game", "#game-log")


async def _decision_text(pilot: Pilot[None]) -> str:
    return await _read_log(pilot, "right-tabs", "tab-decisions", "#decision-log")


# --- the application --------------------------------------------------------


@pytest.fixture
def settings(tmp_path) -> Settings:
    base = Settings()
    return base.model_copy(
        update={
            "data_dir": tmp_path,
            "character": base.character.model_copy(
                update={"name": "Killer", "password": SecretStr("hunter2")}
            ),
        }
    )


def app_for(settings: Settings) -> BatMudApp:
    """An app that lays out and reacts, but never opens a socket."""
    return BatMudApp(settings, connect=False)


async def test_the_app_starts_and_lays_out(settings: Settings) -> None:
    async with app_for(settings).run_test() as pilot:
        await pilot.pause()
        app = pilot.app
        assert app.query_one("#game-log")
        assert app.query_one("#vitals")
        assert app.query_one("#command")
        assert "co-pilot" in await _game_text(pilot)


async def test_a_missing_api_key_is_announced_rather_than_fatal(settings: Settings) -> None:
    async with app_for(settings).run_test() as pilot:
        await pilot.pause()
        text = await _game_text(pilot)
        assert "Planner disabled" in text
        assert "no API key" in text


async def test_approval_bar_shows_each_mode(settings: Settings) -> None:
    app = app_for(settings)
    async with app.run_test() as pilot:
        await pilot.pause()
        bar = app.approval

        bar.show(ApprovalState(paused=True))
        assert "PAUSED" in _plain(bar)

        bar.show(ApprovalState(autonomous=True))
        assert "AUTONOMOUS" in _plain(bar)

        bar.show(ApprovalState())
        assert "CO-PILOT" in _plain(bar)

        bar.show(ApprovalState(decision=Decision("north", Source.PLANNER, reason="explore")))
        text = _plain(bar)
        assert "APPROVE" in text and "north" in text and "explore" in text


async def test_game_lines_are_routed_by_channel(settings: Settings) -> None:
    app = app_for(settings)
    async with app.run_test() as pilot:
        await pilot.pause()
        app._on_event(Line(spans=(Span("A road."),)))
        app._on_event(Line(spans=(Span("The orc hits you."),), channel="spec_battle"))
        app._on_event(Line(spans=(Span("Gore sells a sword"),), channel="chan_sales"))
        app._on_event(Line(spans=(Span("# # #"),), channel="spec_map"))
        await pilot.pause()

        assert "A road." in await _game_text(pilot)
        assert "The orc hits you." in await _read_log(
            pilot, "left-tabs", "tab-battle", "#battle-log"
        )
        assert "sales" in await _read_log(pilot, "left-tabs", "tab-channels", "#channel-log")
        assert "# # #" in await _read_log(pilot, "left-tabs", "tab-map", "#map-log")


async def test_vitals_and_status_reflect_the_control_codes(settings: Settings) -> None:
    app = app_for(settings)
    async with app.run_test() as pilot:
        await pilot.pause()
        app.state.apply(Vitals(120, 200, 30, 60, 40, 80))
        app.state.apply(Target("orc", 45))
        app._refresh_panels()
        await pilot.pause()

        assert "120/200" in _plain(app.query_one("#vitals"))
        status = _plain(app.query_one("#status"))
        assert "orc" in status and "45%" in status


async def test_typing_a_command_sends_it_when_nothing_is_pending(settings: Settings) -> None:
    app = app_for(settings)
    async with app.run_test() as pilot:
        await pilot.pause()
        app.command.value = "look"
        await pilot.press("enter")
        await pilot.pause()

        queued = app.runner._outbox.get_nowait()
        assert queued.command == "look"
        assert queued.source is Source.MANUAL
        assert app.command.value == ""


async def test_letters_reach_the_command_box_instead_of_quitting(settings: Settings) -> None:
    # The previous client bound 'q' to quit at the application level, which
    # made the letter impossible to type into the command box.
    app = app_for(settings)
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("q", "u", "i", "t")
        await pilot.pause()
        assert app.command.value == "quit"
        assert app.is_running


async def test_enter_accepts_a_pending_proposal(settings: Settings) -> None:
    app = app_for(settings)
    async with app.run_test() as pilot:
        await pilot.pause()
        proposal = Decision("north", Source.PLANNER, reason="explore")
        waiting = asyncio.create_task(app.runner.approvals.submit(proposal))
        await pilot.pause()
        app._on_pending(proposal)
        await pilot.pause()
        assert "APPROVE" in _plain(app.query_one("#approval"))

        await pilot.press("enter")
        assert await waiting is proposal


async def test_typing_over_a_proposal_edits_it(settings: Settings) -> None:
    app = app_for(settings)
    async with app.run_test() as pilot:
        await pilot.pause()
        proposal = Decision("north", Source.PLANNER)
        waiting = asyncio.create_task(app.runner.approvals.submit(proposal))
        await pilot.pause()
        app._on_pending(proposal)
        app.command.value = "south"
        await pilot.press("enter")

        result = await waiting
        assert result is not None
        assert result.command == "south"
        assert result.source is Source.MANUAL


async def test_rejecting_a_proposal_drops_it(settings: Settings) -> None:
    app = app_for(settings)
    async with app.run_test() as pilot:
        await pilot.pause()
        proposal = Decision("kill dragon", Source.PLANNER)
        waiting = asyncio.create_task(app.runner.approvals.submit(proposal))
        await pilot.pause()
        app._on_pending(proposal)
        await pilot.press("f4")
        assert await waiting is None


async def test_pause_and_autonomy_toggles(settings: Settings) -> None:
    app = app_for(settings)
    async with app.run_test() as pilot:
        await pilot.pause()
        assert not app.runner.autonomous

        await pilot.press("f2")
        await pilot.pause()
        assert app.runner.paused
        assert "PAUSED" in _plain(app.query_one("#approval"))

        await pilot.press("f2")
        await pilot.pause()
        assert not app.runner.paused

        await pilot.press("f3")
        await pilot.pause()
        assert app.runner.autonomous
        assert "AUTONOMOUS" in _plain(app.query_one("#approval"))
        assert "unattended play" in await _game_text(pilot)


async def test_refusals_are_shown_in_both_logs(settings: Settings) -> None:
    app = app_for(settings)
    async with app.run_test() as pilot:
        await pilot.pause()
        app._on_refused(Refusal("quit", "'quit' is on the denied command list"))
        await pilot.pause()
        assert "Refused 'quit'" in await _game_text(pilot)
        assert "refused" in await _decision_text(pilot)


async def test_sent_commands_appear_in_the_decision_log(settings: Settings) -> None:
    app = app_for(settings)
    async with app.run_test() as pilot:
        await pilot.pause()
        app._on_sent(Decision("kill orc", Source.PLANNER, reason="attack(target='orc')"))
        await pilot.pause()
        log = await _decision_text(pilot)
        assert "kill orc" in log and "planner" in log


async def test_secret_commands_are_masked_in_the_log(settings: Settings) -> None:
    app = app_for(settings)
    async with app.run_test() as pilot:
        await pilot.pause()
        app._on_sent(Decision("hunter2", Source.LOGIN, reason="password", secret=True))
        await pilot.pause()
        log = await _decision_text(pilot)
        assert "hunter2" not in log
        assert "********" in log


async def test_memory_is_saved_on_exit(settings: Settings) -> None:
    app = app_for(settings)
    async with app.run_test() as pilot:
        await pilot.pause()
        app.memory.remember("something worth keeping")
    assert settings.memory_path.is_file()
    assert "something worth keeping" in settings.memory_path.read_text(encoding="utf-8")
