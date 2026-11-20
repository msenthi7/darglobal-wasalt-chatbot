"""Typed runtime settings from environment variables, validated at startup (blueprint C03/C08)."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# Verified as $0 models against OpenRouter's public /models pricing at startup; any that are no
# longer free or no longer listed are dropped (free model line-ups change often).
DEFAULT_MODELS: tuple[str, ...] = (
    "google/gemma-4-31b-it:free",
    "nvidia/nemotron-3-ultra-550b-a55b:free",
    "google/gemma-4-26b-a4b-it:free",
    "openrouter/free",
)
_MODEL_ID = re.compile(r"^[a-z0-9][a-z0-9._-]*/[a-z0-9][a-z0-9._:-]*$")


class ConfigError(ValueError):
    pass


def _int(name: str, default: int, lo: int, hi: int) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    if not raw.isdigit() or not lo <= int(raw) <= hi:
        raise ConfigError(f"{name} must be an integer between {lo} and {hi}")
    return int(raw)


def _models(raw: str | None) -> tuple[str, ...]:
    if not raw or not raw.strip():
        return DEFAULT_MODELS
    models = tuple(dict.fromkeys(m.strip() for m in raw.split(",") if m.strip()))
    bad = [m for m in models if not _MODEL_ID.match(m)]
    if bad or not models or len(models) > 8:
        raise ConfigError("OPENROUTER_MODELS must be 1-8 comma-separated model ids like vendor/model:free")
    return models


@dataclass(frozen=True)
class Settings:
    openrouter_api_key: str = field(default="", repr=False)
    models: tuple[str, ...] = DEFAULT_MODELS
    openrouter_base_url: str = "https://openrouter.ai/api/v1"
    public_url: str = ""
    source_url: str = ""
    corpus_path: Path = ROOT / "data" / "corpus.jsonl"
    manifest_path: Path = ROOT / "data" / "manifest.json"
    trap_report_path: Path = ROOT / "data" / "trap_report.json"
    connect_timeout_s: float = 5.0
    request_timeout_s: float = 45.0
    total_deadline_s: float = 75.0
    max_attempts_per_model: int = 2
    max_output_tokens: int = 900
    temperature: float = 0.2
    per_client_per_minute: int = 6
    per_client_per_day: int = 60
    global_per_minute: int = 15
    upstream_per_day: int = 45
    max_concurrent_upstream: int = 3
    model_refresh_s: int = 6 * 3600

    @classmethod
    def from_env(cls) -> Settings:
        key = os.environ.get("OPENROUTER_API_KEY", "").strip()
        if key and (len(key) > 200 or any(c.isspace() for c in key)):
            raise ConfigError("OPENROUTER_API_KEY is malformed")
        public_url = os.environ.get("PUBLIC_URL", "").strip()
        if public_url and not re.match(r"^https://[A-Za-z0-9.-]+(?::\d+)?/?$", public_url):
            raise ConfigError("PUBLIC_URL must be an https origin")
        source_url = os.environ.get("SOURCE_URL", "").strip()
        if source_url and not re.match(r"^https://github\.com/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+/?$", source_url):
            raise ConfigError("SOURCE_URL must be a https://github.com/<owner>/<repo> URL")
        return cls(
            openrouter_api_key=key,
            models=_models(os.environ.get("OPENROUTER_MODELS")),
            public_url=public_url,
            source_url=source_url,
            per_client_per_minute=_int("RATE_PER_CLIENT_PER_MINUTE", cls.per_client_per_minute, 1, 120),
            per_client_per_day=_int("RATE_PER_CLIENT_PER_DAY", cls.per_client_per_day, 1, 5000),
            global_per_minute=_int("RATE_GLOBAL_PER_MINUTE", cls.global_per_minute, 1, 600),
            upstream_per_day=_int("UPSTREAM_CALLS_PER_DAY", cls.upstream_per_day, 1, 100_000),
        )

    @property
    def llm_configured(self) -> bool:
        return bool(self.openrouter_api_key)
