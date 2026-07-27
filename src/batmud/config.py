"""Configuration.

Settings come from, in increasing order of precedence: built-in defaults, a
TOML file, environment variables, then command line flags. The password is a
``SecretStr`` throughout and is deliberately never placed anywhere the language
model can see it.
"""

from __future__ import annotations

import os
import tomllib
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field, SecretStr, field_validator

DEFAULT_CONFIG_PATHS = (
    Path("batmud.toml"),
    Path.home() / ".config" / "batmud" / "config.toml",
)

DEFAULT_DATA_DIR = Path.home() / ".local" / "share" / "batmud"


class ConnectionSettings(BaseModel):
    """Where and how to connect.

    Port 2022 is TLS; 23 and 2023 are plaintext. TLS is the default because the
    server presents a valid certificate and there is no reason not to.
    """

    host: str = "batmud.bat.org"
    port: int = 2022
    tls: bool = True
    verify_tls: bool = True
    batclient: bool = True
    reconnect: bool = True
    reconnect_delay: float = 3.0
    reconnect_max_delay: float = 120.0


class CharacterSettings(BaseModel):
    """Credentials. Handled only by the login state machine."""

    name: str = ""
    password: SecretStr = SecretStr("")
    name_prefix: str = "bat"

    @property
    def has_credentials(self) -> bool:
        return bool(self.name and self.password.get_secret_value())


class LLMSettings(BaseModel):
    """Planner model and the budget it is allowed to spend."""

    model: str = "openrouter/auto-beta"
    api_key: SecretStr = SecretStr("")
    base_url: str = "https://openrouter.ai/api/v1"
    temperature: float = 0.3
    max_output_tokens: int = 1024
    request_timeout: float = 60.0
    max_retries: int = 3

    max_requests_per_minute: int = 12
    max_requests: int = 0
    max_total_tokens: int = 0
    max_spend_usd: float = 0.0

    @property
    def configured(self) -> bool:
        return bool(self.api_key.get_secret_value())


class SafetySettings(BaseModel):
    """Limits applied to every outbound command, whoever proposed it."""

    hp_retreat_fraction: float = Field(default=0.35, ge=0.0, le=1.0)
    hp_emergency_fraction: float = Field(default=0.15, ge=0.0, le=1.0)
    retreat_command: str = "flee"
    emergency_commands: tuple[str, ...] = ("flee",)

    min_command_interval: float = Field(default=0.75, ge=0.0)
    max_commands_per_minute: int = Field(default=40, ge=1)
    prompt_timeout: float = Field(default=5.0, ge=0.0)

    denied_commands: tuple[str, ...] = (
        "quit",
        "suicide",
        "delete",
        "reincarnate",
        "password",
        "shutdown",
        "who",
    )
    denied_patterns: tuple[str, ...] = (
        r"^\s*(give|drop)\s+all\b",
        r"^\s*sell\s+all\b",
        r"^\s*(shout|yell)\b",
    )
    allow_communication: bool = False

    @field_validator("denied_commands", "denied_patterns", "emergency_commands", mode="before")
    @classmethod
    def _as_tuple(cls, value: Any) -> Any:
        if isinstance(value, str):
            return tuple(part.strip() for part in value.split(",") if part.strip())
        return value


class AgentSettings(BaseModel):
    """How much rope the planner gets."""

    autonomous: bool = False
    goal: str = "Explore safely, gain experience, and stay alive."
    idle_seconds: float = Field(default=20.0, ge=1.0)
    max_recent_lines: int = Field(default=60, ge=5)
    anti_idle: bool = True
    anti_idle_seconds: float = Field(default=240.0, ge=30.0)


class Settings(BaseModel):
    """The whole configuration."""

    connection: ConnectionSettings = Field(default_factory=ConnectionSettings)
    character: CharacterSettings = Field(default_factory=CharacterSettings)
    llm: LLMSettings = Field(default_factory=LLMSettings)
    safety: SafetySettings = Field(default_factory=SafetySettings)
    agent: AgentSettings = Field(default_factory=AgentSettings)

    data_dir: Path = DEFAULT_DATA_DIR
    log_file: Path | None = None
    log_level: str = "INFO"

    @property
    def map_path(self) -> Path:
        return self.data_dir / "world.sqlite"

    @property
    def memory_path(self) -> Path:
        return self.data_dir / "memory.json"


# Environment variables, including the ones the previous client used.
_ENV_MAP: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("BATMUD_HOST", ("connection", "host")),
    ("BATMUD_PORT", ("connection", "port")),
    ("BATMUD_TLS", ("connection", "tls")),
    ("BATMUD_CHARACTER", ("character", "name")),
    ("BATMUD_NAME", ("character", "name")),
    ("BATMUD_PASSWORD", ("character", "password")),
    ("BATMUD_NAME_PREFIX", ("character", "name_prefix")),
    ("OPENROUTER_API_KEY", ("llm", "api_key")),
    ("OPENROUTER_MODEL", ("llm", "model")),
    ("OPENROUTER_BASE_URL", ("llm", "base_url")),
    ("BATMUD_MODEL", ("llm", "model")),
    ("BATMUD_GOAL", ("agent", "goal")),
    ("BATMUD_AUTONOMOUS", ("agent", "autonomous")),
    ("BATMUD_DATA_DIR", ("data_dir",)),
    ("BATMUD_LOG_LEVEL", ("log_level",)),
)

_BOOLEAN_TRUE = {"1", "true", "yes", "on"}
_BOOLEAN_FALSE = {"0", "false", "no", "off"}


def _coerce(value: str) -> Any:
    lowered = value.strip().lower()
    if lowered in _BOOLEAN_TRUE:
        return True
    if lowered in _BOOLEAN_FALSE:
        return False
    return value


def _assign(target: dict[str, Any], path: tuple[str, ...], value: Any) -> None:
    node = target
    for part in path[:-1]:
        node = node.setdefault(part, {})
    node[path[-1]] = value


def _merge(base: dict[str, Any], overlay: dict[str, Any]) -> dict[str, Any]:
    result = dict(base)
    for key, value in overlay.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = _merge(result[key], value)
        else:
            result[key] = value
    return result


def _from_environment(environ: dict[str, str]) -> dict[str, Any]:
    data: dict[str, Any] = {}
    for name, path in _ENV_MAP:
        value = environ.get(name)
        if value:
            _assign(data, path, _coerce(value))
    return data


def find_config_file() -> Path | None:
    return next((path for path in DEFAULT_CONFIG_PATHS if path.is_file()), None)


def load_settings(
    config_file: Path | None = None,
    overrides: dict[str, Any] | None = None,
    environ: dict[str, str] | None = None,
) -> Settings:
    """Build settings from file, environment and explicit overrides."""
    path = config_file if config_file is not None else find_config_file()
    data: dict[str, Any] = {}
    if path is not None:
        if not path.is_file():
            raise FileNotFoundError(f"config file not found: {path}")
        data = tomllib.loads(path.read_text(encoding="utf-8"))

    data = _merge(data, _from_environment(dict(os.environ) if environ is None else environ))
    if overrides:
        data = _merge(data, overrides)
    return Settings.model_validate(data)
