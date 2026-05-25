from __future__ import annotations

import os
import threading
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field


class Theme(BaseModel):
    name: str
    keywords: list[str] = Field(default_factory=list)
    priority: Literal["critical", "high", "medium", "low"] = "medium"
    similarity_threshold: float | None = None


class Person(BaseModel):
    slack_id: str
    name: str
    reason: str = ""


class DigestSchedule(BaseModel):
    schedule: list[str] = ["09:00"]
    timezone: str = "Europe/Rome"
    lookback_hours: int = 24
    max_messages_per_channel: int = 200


class TargetUser(BaseModel):
    slack_id: str | None = None
    email: str | None = None


class ScoringConfig(BaseModel):
    similarity_threshold: float = 0.25


class TuningConfig(BaseModel):
    auto_apply: bool = False
    min_votes: int = 10
    cooldown_days: int = 3
    pain_threshold: float = 0.3


class AgentConfig(BaseModel):
    model: str = "claude-sonnet-4-5-20250514"
    max_budget_usd: float = 0.50
    max_turns: int = 10


class DigestConfig(BaseModel):
    digest: DigestSchedule = Field(default_factory=DigestSchedule)
    target_user: TargetUser = Field(default_factory=TargetUser)
    themes: list[Theme] = Field(default_factory=list)
    people: list[Person] = Field(default_factory=list)
    exclude_channels: list[str] = Field(default_factory=list)
    include_channels: list[str] = Field(default_factory=list)
    scoring: ScoringConfig = Field(default_factory=ScoringConfig)
    tuning: TuningConfig = Field(default_factory=TuningConfig)
    agent: AgentConfig = Field(default_factory=AgentConfig)


_config: DigestConfig | None = None
_config_lock = threading.Lock()
_config_path: Path | None = None


def _resolve_config_path() -> Path:
    env_path = os.environ.get("DIGEST_CONFIG_PATH")
    if env_path:
        return Path(env_path).resolve()
    return Path.cwd() / "config.yaml"


def load_config(path: Path | None = None) -> DigestConfig:
    global _config, _config_path
    _config_path = path or _resolve_config_path()

    if not _config_path.exists():
        _config = DigestConfig()
        save_config(_config)
        return _config

    with open(_config_path) as f:
        raw = yaml.safe_load(f) or {}

    _config = DigestConfig.model_validate(raw)
    return _config


def save_config(config: DigestConfig) -> None:
    global _config
    path = _config_path or _resolve_config_path()
    with _config_lock:
        _config = config
        data = config.model_dump(mode="json")
        with open(path, "w") as f:
            yaml.dump(data, f, default_flow_style=False, sort_keys=False)


def get_config() -> DigestConfig:
    if _config is None:
        return load_config()
    return _config


def add_theme(theme: Theme) -> DigestConfig:
    with _config_lock:
        config = get_config().model_copy(deep=True)
        config.themes = [t for t in config.themes if t.name != theme.name]
        config.themes.append(theme)
    save_config(config)
    return config


def remove_theme(name: str) -> DigestConfig:
    with _config_lock:
        config = get_config().model_copy(deep=True)
        config.themes = [t for t in config.themes if t.name != name]
    save_config(config)
    return config


def add_person(person: Person) -> DigestConfig:
    with _config_lock:
        config = get_config().model_copy(deep=True)
        config.people = [p for p in config.people if p.slack_id != person.slack_id]
        config.people.append(person)
    save_config(config)
    return config


def remove_person(slack_id: str) -> DigestConfig:
    with _config_lock:
        config = get_config().model_copy(deep=True)
        config.people = [p for p in config.people if p.slack_id != slack_id]
    save_config(config)
    return config
