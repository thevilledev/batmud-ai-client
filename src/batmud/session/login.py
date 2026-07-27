"""Deterministic login and character creation.

The previous client asked the language model to type the character name and the
password, which meant the password had to be interpolated into the system
prompt on every request. Nothing here goes near the model: prompts are matched
against a fixed table, the reply is chosen in code, and secrets are marked so
neither the transcript nor the planner context can contain them.

Success and failure come from control codes 05 and 06 rather than from matching
the wording of an error message.
"""

from __future__ import annotations

import logging
import random
import re
import string
from dataclasses import dataclass, field
from enum import StrEnum

from ..config import CharacterSettings
from ..protocol.events import ConnectionFailed, ConnectionSucceeded, Event, PlayerInfo, Prompt

log = logging.getLogger(__name__)


class LoginMode(StrEnum):
    LOGIN = "login"
    CREATE = "create"


class LoginPhase(StrEnum):
    MENU = "menu"
    NAME = "name"
    PASSWORD = "password"
    CREATING = "creating"
    DONE = "done"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class LoginReply:
    """A response to a login prompt."""

    command: str
    secret: bool = False
    note: str = ""


# Ordered: the first pattern that matches the prompt wins.
_MENU = re.compile(r"enter your choice or name", re.IGNORECASE)
_NAME = re.compile(r"(what is your name|enter your name|your name)\s*[:?]", re.IGNORECASE)
# Deliberately broad: "Password:", "New password:", "Enter your password:" and
# "Give me a password:" all end in the word followed by a prompt character. The
# lookalike table below is what keeps it from over-matching.
_PASSWORD = re.compile(r"\bpasswords?\s*[:?]", re.IGNORECASE)
_PASSWORD_AGAIN = re.compile(
    r"(^again|re-?enter the password|verify.*password|password.*again)",
    re.IGNORECASE,
)
_PRESS_RETURN = re.compile(r"press\s+(return|enter)\s+to\s+continue", re.IGNORECASE)

# Prompts that look like a password request but are not one. The previous
# client filtered these after the fact; here they simply never match.
_NOT_A_PASSWORD_PROMPT = re.compile(
    r"(forgot your password|password must be|password should|for a good password"
    r"|password hint|retrieve it from|wizard)",
    re.IGNORECASE,
)


def generate_name(prefix: str, *, rng: random.Random | None = None) -> str:
    """A creation-mode name: prefix plus four lowercase letters."""
    source = rng or random.SystemRandom()
    suffix = "".join(source.choice(string.ascii_lowercase) for _ in range(4))
    return f"{prefix.lower()}{suffix}"


@dataclass(slots=True)
class LoginController:
    """Answers login prompts until the session is in the game."""

    character: CharacterSettings
    mode: LoginMode = LoginMode.LOGIN
    rng: random.Random | None = None

    phase: LoginPhase = LoginPhase.MENU
    failure: str = ""
    chosen_name: str = ""
    _seen: dict[str, int] = field(default_factory=dict)
    _max_repeats: int = 4

    @property
    def finished(self) -> bool:
        return self.phase in (LoginPhase.DONE, LoginPhase.FAILED)

    @property
    def succeeded(self) -> bool:
        return self.phase is LoginPhase.DONE

    def observe(self, event: Event) -> None:
        """Watch for the authoritative login result."""
        match event:
            case ConnectionSucceeded() | PlayerInfo():
                if self.phase is not LoginPhase.FAILED:
                    self.phase = LoginPhase.DONE
            case ConnectionFailed():
                self.phase = LoginPhase.FAILED
                self.failure = event.reason
                log.warning("login refused: %s", event.reason)
            case Prompt():
                pass

    def respond(self, prompt: str) -> LoginReply | None:
        """Choose a reply for a prompt, or ``None`` to leave it to the agent."""
        if self.finished:
            return None

        text = prompt.strip()
        if not text:
            return None

        if self._repeating(text):
            self.phase = LoginPhase.FAILED
            self.failure = f"stuck at prompt: {text[:60]}"
            log.error("login gave up, prompt repeated: %r", text[:80])
            return None

        if _PRESS_RETURN.search(text):
            return LoginReply("", note="continue")

        if _MENU.search(text):
            return self._answer_menu()

        if _NAME.search(text):
            return self._answer_name()

        if _NOT_A_PASSWORD_PROMPT.search(text):
            return None

        if _PASSWORD_AGAIN.search(text) or _PASSWORD.search(text):
            return self._answer_password()

        return None

    # --- individual prompts -------------------------------------------------

    def _answer_menu(self) -> LoginReply | None:
        if self.mode is LoginMode.CREATE:
            self.phase = LoginPhase.CREATING
            return LoginReply("3", note="start character creation")
        if not self.character.name:
            self.phase = LoginPhase.FAILED
            self.failure = "no character name configured for login mode"
            return None
        # The menu accepts a character name directly.
        self.phase = LoginPhase.PASSWORD
        self.chosen_name = self.character.name
        return LoginReply(self.character.name, note="character name")

    def _answer_name(self) -> LoginReply | None:
        if self.mode is LoginMode.CREATE:
            if not self.chosen_name:
                self.chosen_name = generate_name(self.character.name_prefix, rng=self.rng)
            self.phase = LoginPhase.PASSWORD
            return LoginReply(self.chosen_name, note="new character name")
        if not self.character.name:
            self.phase = LoginPhase.FAILED
            self.failure = "no character name configured for login mode"
            return None
        self.chosen_name = self.character.name
        self.phase = LoginPhase.PASSWORD
        return LoginReply(self.character.name, note="character name")

    def _answer_password(self) -> LoginReply | None:
        password = self.character.password.get_secret_value()
        if not password:
            self.phase = LoginPhase.FAILED
            self.failure = "no password configured"
            return None
        return LoginReply(password, secret=True, note="password")

    def _repeating(self, prompt: str) -> bool:
        """Guard against answering the same prompt forever."""
        key = prompt[:80]
        self._seen[key] = self._seen.get(key, 0) + 1
        return self._seen[key] > self._max_repeats
