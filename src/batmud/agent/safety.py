"""Guards applied to every outbound command.

Nothing reaches the socket without passing through here, whether it came from a
reflex, the planner or the keyboard. The guard is deliberately dumb and
declarative: it does not try to improve a command, it either allows it or
refuses it with a reason the planner can be told about.
"""

from __future__ import annotations

import re
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import StrEnum

from ..config import SafetySettings


class Source(StrEnum):
    """Who proposed a command. Determines approval and display rules."""

    LOGIN = "login"
    REFLEX = "reflex"
    PLANNER = "planner"
    MANUAL = "manual"


@dataclass(frozen=True, slots=True)
class Decision:
    """A command someone wants to send.

    ``auto`` marks the command as not needing human approval even in co-pilot
    mode. It is reserved for the login sequence and for acknowledging pagers,
    which are protocol plumbing rather than gameplay.
    """

    command: str
    source: Source
    reason: str = ""
    secret: bool = False
    auto: bool = False

    @property
    def display(self) -> str:
        return "********" if self.secret else (self.command or "<enter>")


@dataclass(frozen=True, slots=True)
class Refusal:
    """Why a command was not sent."""

    command: str
    reason: str


COMMUNICATION_COMMANDS = frozenset(
    {
        "say",
        "tell",
        "shout",
        "yell",
        "chat",
        "whisper",
        "reply",
        "ask",
        "channel",
        "gossip",
        "newbie",
        "sales",
        "party",
    }
)


class CommandGuard:
    """Decides whether a proposed command may be sent."""

    def __init__(self, settings: SafetySettings, secrets: tuple[str, ...] = ()) -> None:
        self.settings = settings
        self._patterns = [
            re.compile(pattern, re.IGNORECASE) for pattern in settings.denied_patterns
        ]
        self._secrets = tuple(secret for secret in secrets if secret)

    def check(self, decision: Decision) -> Refusal | None:
        """Return a refusal, or ``None`` if the command may be sent."""
        command = decision.command.strip()

        # The login controller owns the credential path and is trusted with it.
        if decision.source is Source.LOGIN:
            return None

        if not command:
            return None

        if self._leaks_a_secret(command):
            return Refusal(command, "command contains a configured secret")

        if "\n" in decision.command or "\r" in decision.command:
            return Refusal(command, "command contains a newline")

        verb = command.split(maxsplit=1)[0].lower().lstrip("'\"")

        if verb in {word.lower() for word in self.settings.denied_commands}:
            return Refusal(command, f"'{verb}' is on the denied command list")

        if not self.settings.allow_communication and verb in COMMUNICATION_COMMANDS:
            return Refusal(
                command,
                f"'{verb}' is a communication command; set safety.allow_communication to permit it",
            )

        for pattern in self._patterns:
            if pattern.search(command):
                return Refusal(command, f"matches denied pattern {pattern.pattern!r}")

        return None

    def _leaks_a_secret(self, command: str) -> bool:
        return any(secret in command for secret in self._secrets)


@dataclass(slots=True)
class RateLimiter:
    """A sliding-window limiter with a floor on the gap between commands."""

    max_per_minute: int
    min_interval: float
    clock: Callable[[], float] = time.monotonic
    _sent: deque[float] = field(default_factory=deque)

    def delay(self) -> float:
        """Seconds to wait before the next command may be sent."""
        now = self.clock()
        self._expire(now)

        waits = [0.0]
        if self._sent:
            waits.append(self.min_interval - (now - self._sent[-1]))
        if len(self._sent) >= self.max_per_minute:
            waits.append(60.0 - (now - self._sent[0]))
        return max(waits)

    def record(self) -> None:
        now = self.clock()
        self._expire(now)
        self._sent.append(now)

    def _expire(self, now: float) -> None:
        while self._sent and now - self._sent[0] >= 60.0:
            self._sent.popleft()


@dataclass(slots=True)
class Budget:
    """Caps on how much the planner may spend. Zero means no limit."""

    max_requests: int = 0
    max_total_tokens: int = 0
    max_spend_usd: float = 0.0

    requests: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    spend_usd: float = 0.0

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens

    def record(self, input_tokens: int, output_tokens: int, cost_usd: float = 0.0) -> None:
        self.requests += 1
        self.input_tokens += input_tokens
        self.output_tokens += output_tokens
        self.spend_usd += cost_usd

    def exhausted(self) -> str | None:
        """The reason the budget is spent, or ``None`` while it is not."""
        if self.max_requests and self.requests >= self.max_requests:
            return f"request cap reached ({self.max_requests})"
        if self.max_total_tokens and self.total_tokens >= self.max_total_tokens:
            return f"token cap reached ({self.max_total_tokens:,})"
        if self.max_spend_usd and self.spend_usd >= self.max_spend_usd:
            return f"spend cap reached (${self.max_spend_usd:.2f})"
        return None

    def summary(self) -> str:
        parts = [
            f"{self.requests:,} requests",
            f"{self.input_tokens:,} in",
            f"{self.output_tokens:,} out",
        ]
        if self.spend_usd:
            parts.append(f"${self.spend_usd:.4f}")
        return " | ".join(parts)
