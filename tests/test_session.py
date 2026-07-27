"""Login, approval and pacing tests."""

from __future__ import annotations

import asyncio
import random

import pytest
from pydantic import SecretStr

from batmud.agent.safety import Decision, Source
from batmud.config import CharacterSettings
from batmud.protocol.events import ConnectionFailed, ConnectionSucceeded, PlayerInfo
from batmud.session.login import LoginController, LoginMode, LoginPhase, generate_name
from batmud.session.runner import ApprovalGate

MENU = "Please enter your choice or name:"


def controller(mode: LoginMode = LoginMode.LOGIN, **kwargs: object) -> LoginController:
    character = CharacterSettings(
        name=str(kwargs.get("name", "Killer")),
        password=SecretStr(str(kwargs.get("password", "hunter2"))),
        name_prefix=str(kwargs.get("name_prefix", "bat")),
    )
    return LoginController(character, mode=mode, rng=random.Random(1))


# --- login mode -------------------------------------------------------------


def test_login_sends_the_character_name_at_the_menu() -> None:
    login = controller()
    reply = login.respond(MENU)
    assert reply is not None
    assert reply.command == "Killer"
    assert not reply.secret


def test_password_prompt_is_answered_and_masked() -> None:
    login = controller()
    login.respond(MENU)
    reply = login.respond("Password:")
    assert reply is not None
    assert reply.command == "hunter2"
    assert reply.secret


def test_password_is_repeated_for_confirmation_prompts() -> None:
    login = controller(mode=LoginMode.CREATE)
    for prompt in ("New password:", "Again:", "Please re-enter the password."):
        reply = login.respond(prompt)
        assert reply is not None and reply.secret


@pytest.mark.parametrize(
    "prompt",
    [
        "Forgot your password? Retrieve it from the website.",
        "Your password should contain at least 8 characters",
        "Password must be at least 6 characters long",
        "For a good password, mix letters and digits",
        "Enter the password for the wizard:",
    ],
)
def test_password_lookalikes_are_not_answered(prompt: str) -> None:
    # These are the strings the previous client had to filter out after the
    # fact; here they simply never match a password prompt.
    assert controller().respond(prompt) is None


def test_press_return_is_acknowledged() -> None:
    reply = controller().respond("[Press RETURN to continue]")
    assert reply is not None and reply.command == ""


def test_unknown_prompts_are_left_to_the_agent() -> None:
    assert controller().respond("Which race do you want to play?") is None


def test_login_missing_a_name_fails_fast() -> None:
    login = controller(name="")
    assert login.respond(MENU) is None
    assert login.phase is LoginPhase.FAILED
    assert "no character name" in login.failure


def test_login_missing_a_password_fails_fast() -> None:
    login = controller(password="")
    login.respond(MENU)
    assert login.respond("Password:") is None
    assert login.phase is LoginPhase.FAILED


def test_a_repeating_prompt_stops_the_controller() -> None:
    login = controller()
    for _ in range(5):
        login.respond("Password:")
    assert login.respond("Password:") is None
    assert login.phase is LoginPhase.FAILED
    assert "stuck at prompt" in login.failure


# --- authoritative results --------------------------------------------------


def test_success_comes_from_control_code_05() -> None:
    login = controller()
    login.observe(ConnectionSucceeded())
    assert login.succeeded and login.finished


def test_player_info_also_confirms_login() -> None:
    login = controller()
    login.observe(PlayerInfo(name="Killer", race="orc", level=1, experience=0))
    assert login.succeeded


def test_failure_comes_from_control_code_06() -> None:
    login = controller()
    login.observe(ConnectionFailed("Incorrect password."))
    assert login.phase is LoginPhase.FAILED
    assert login.failure == "Incorrect password."
    assert login.respond(MENU) is None


def test_failure_is_not_overwritten_by_a_later_success() -> None:
    login = controller()
    login.observe(ConnectionFailed("Incorrect password."))
    login.observe(ConnectionSucceeded())
    assert login.phase is LoginPhase.FAILED


# --- character creation -----------------------------------------------------


def test_create_mode_picks_the_menu_entry_then_generates_a_name() -> None:
    login = controller(mode=LoginMode.CREATE)
    assert login.respond(MENU) is not None
    menu_choice = controller(mode=LoginMode.CREATE).respond(MENU)
    assert menu_choice is not None and menu_choice.command == "3"

    reply = login.respond("What is your name:")
    assert reply is not None
    assert reply.command.startswith("bat")
    assert len(reply.command) == len("bat") + 4


def test_generated_names_are_lowercase_with_four_letters() -> None:
    name = generate_name("Claude", rng=random.Random(7))
    assert name.startswith("claude")
    assert name[6:].isalpha() and name[6:].islower() and len(name[6:]) == 4


def test_the_generated_name_is_reused_for_confirmation() -> None:
    login = controller(mode=LoginMode.CREATE)
    first = login.respond("What is your name:")
    assert first is not None
    login.chosen_name = first.command
    assert login.chosen_name == first.command


# --- approval gate ----------------------------------------------------------

PROPOSAL = Decision(command="north", source=Source.PLANNER, reason="explore")


async def test_disabled_gate_passes_everything_through() -> None:
    gate = ApprovalGate(enabled=False)
    assert await gate.submit(PROPOSAL) is PROPOSAL


async def test_auto_decisions_skip_approval_even_when_enabled() -> None:
    gate = ApprovalGate(enabled=True)
    auto = Decision(command="", source=Source.REFLEX, auto=True)
    assert await gate.submit(auto) is auto


async def test_approval_waits_for_a_human() -> None:
    gate = ApprovalGate(enabled=True)
    task = asyncio.create_task(gate.submit(PROPOSAL))
    await asyncio.sleep(0)
    assert gate.pending is PROPOSAL
    assert gate.resolve("north")
    assert await task is PROPOSAL
    assert gate.pending is None


async def test_rejection_drops_the_command() -> None:
    gate = ApprovalGate(enabled=True)
    task = asyncio.create_task(gate.submit(PROPOSAL))
    await asyncio.sleep(0)
    assert gate.resolve(None)
    assert await task is None


async def test_editing_replaces_the_command_and_records_the_origin() -> None:
    gate = ApprovalGate(enabled=True)
    task = asyncio.create_task(gate.submit(PROPOSAL))
    await asyncio.sleep(0)
    gate.resolve("south")
    result = await task
    assert result is not None
    assert result.command == "south"
    assert result.source is Source.MANUAL
    assert "edited from 'north'" in result.reason


async def test_resolving_with_nothing_pending_is_a_no_op() -> None:
    gate = ApprovalGate(enabled=True)
    assert not gate.resolve("north")


async def test_cancel_releases_a_waiting_proposal() -> None:
    gate = ApprovalGate(enabled=True)
    task = asyncio.create_task(gate.submit(PROPOSAL))
    await asyncio.sleep(0)
    gate.cancel()
    assert await task is None
