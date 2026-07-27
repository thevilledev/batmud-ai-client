"""OpenRouter-compatible chat client.

Wraps the OpenAI SDK with the retry, timeout and accounting behaviour the
planner needs. The previous client's retry loop neither slept between attempts
nor returned anything on final failure; here each attempt backs off and the
caller is told explicitly whether it got a completion.
"""

from __future__ import annotations

import asyncio
import json
import logging
import random
from dataclasses import dataclass, field
from typing import Any

import openai

from ..agent.safety import Budget
from ..config import LLMSettings

log = logging.getLogger(__name__)

REFERER = "https://github.com/thevilledev/batmud-ai-client"
TITLE = "BatMUD AI Client"

_RETRYABLE = (
    openai.APIConnectionError,
    openai.APITimeoutError,
    openai.RateLimitError,
    openai.InternalServerError,
)


@dataclass(frozen=True, slots=True)
class ToolCall:
    """A tool the model asked to run."""

    id: str
    name: str
    arguments: dict[str, Any]


@dataclass(frozen=True, slots=True)
class Completion:
    """One model response."""

    content: str = ""
    tool_calls: tuple[ToolCall, ...] = ()
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0
    finish_reason: str = ""

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens


class LLMUnavailable(RuntimeError):
    """The model could not be reached, or the budget is spent."""


@dataclass(slots=True)
class LLMClient:
    """A thin, budget-aware chat client."""

    settings: LLMSettings
    budget: Budget = field(default_factory=Budget)
    client: openai.AsyncOpenAI | None = None
    last_error: str = ""

    def __post_init__(self) -> None:
        if self.client is None and self.settings.configured:
            self.client = openai.AsyncOpenAI(
                api_key=self.settings.api_key.get_secret_value(),
                base_url=self.settings.base_url,
                timeout=self.settings.request_timeout,
                max_retries=0,  # retries are handled here so backoff is visible
                default_headers={"HTTP-Referer": REFERER, "X-Title": TITLE},
            )

    @property
    def available(self) -> bool:
        return self.client is not None and self.budget.exhausted() is None

    @property
    def unavailable_reason(self) -> str:
        if self.client is None:
            return "no API key configured (set OPENROUTER_API_KEY)"
        return self.budget.exhausted() or ""

    async def complete(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        *,
        tool_choice: str = "auto",
    ) -> Completion:
        """Request a completion, retrying transient failures with backoff."""
        if self.client is None:
            raise LLMUnavailable("no API key configured")
        if (reason := self.budget.exhausted()) is not None:
            raise LLMUnavailable(reason)

        request: dict[str, Any] = {
            "model": self.settings.model,
            "messages": messages,
            "max_tokens": self.settings.max_output_tokens,
            "temperature": self.settings.temperature,
            # OpenRouter reports the actual cost of the call when asked.
            "extra_body": {"usage": {"include": True}},
        }
        if tools:
            request["tools"] = tools
            request["tool_choice"] = tool_choice

        delay = 1.0
        for attempt in range(1, self.settings.max_retries + 1):
            try:
                response = await self.client.chat.completions.create(**request)
            except _RETRYABLE as error:
                self.last_error = f"{type(error).__name__}: {error}"
                if attempt == self.settings.max_retries:
                    raise LLMUnavailable(self.last_error) from error
                wait = delay + random.uniform(0, delay / 2)
                log.warning(
                    "model request failed (attempt %d/%d), retrying in %.1fs: %s",
                    attempt,
                    self.settings.max_retries,
                    wait,
                    error,
                )
                await asyncio.sleep(wait)
                delay = min(delay * 2, 30.0)
                continue
            except openai.APIStatusError as error:
                # 4xx other than rate limiting will not improve on retry.
                self.last_error = f"HTTP {error.status_code}: {error}"
                raise LLMUnavailable(self.last_error) from error

            completion = _to_completion(response)
            self.budget.record(
                completion.input_tokens, completion.output_tokens, completion.cost_usd
            )
            self.last_error = ""
            return completion

        raise LLMUnavailable(self.last_error or "model request failed")


def _to_completion(response: Any) -> Completion:
    if not getattr(response, "choices", None):
        return Completion(finish_reason="empty")

    choice = response.choices[0]
    message = choice.message
    calls: list[ToolCall] = []
    for call in getattr(message, "tool_calls", None) or []:
        function = getattr(call, "function", None)
        if function is None:
            continue
        calls.append(
            ToolCall(
                id=getattr(call, "id", "") or "",
                name=function.name,
                arguments=_parse_arguments(function.arguments),
            )
        )

    usage = getattr(response, "usage", None)
    return Completion(
        content=(message.content or "").strip(),
        tool_calls=tuple(calls),
        input_tokens=int(getattr(usage, "prompt_tokens", 0) or 0),
        output_tokens=int(getattr(usage, "completion_tokens", 0) or 0),
        cost_usd=float(getattr(usage, "cost", 0.0) or 0.0),
        finish_reason=getattr(choice, "finish_reason", "") or "",
    )


def _parse_arguments(raw: str | None) -> dict[str, Any]:
    """Tolerate a model that emits invalid JSON for its own tool call."""
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        log.warning("tool arguments were not valid JSON: %r", raw[:200])
        return {}
    return parsed if isinstance(parsed, dict) else {}
