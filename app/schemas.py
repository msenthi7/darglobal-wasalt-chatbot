"""Typed request/response/error records (blueprint C02/C04/A03)."""

from __future__ import annotations

import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
MAX_MESSAGE_CHARS = 1000
MAX_TURN_CHARS = 4000
MAX_HISTORY_TURNS = 12


def _clean(value: str) -> str:
    return _CONTROL.sub("", value).strip()


class ChatTurn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    role: Literal["user", "assistant"]
    content: str = Field(min_length=1, max_length=MAX_TURN_CHARS)

    @field_validator("content")
    @classmethod
    def _content(cls, v: str) -> str:
        v = _clean(v)
        if not v:
            raise ValueError("turn content is empty")
        return v


class ChatRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    message: str = Field(min_length=1, max_length=MAX_MESSAGE_CHARS)
    history: list[ChatTurn] = Field(default_factory=list, max_length=MAX_HISTORY_TURNS)

    @field_validator("message")
    @classmethod
    def _message(cls, v: str) -> str:
        v = _clean(v)
        if not v:
            raise ValueError("message is empty")
        return v


class Citation(BaseModel):
    n: int
    title: str
    url: str
    site: Literal["darglobal", "wasalt"]
    source_type: str
    captured_at: str
    archive_url: str | None = None


class ChatResponse(BaseModel):
    status: Literal["ok"] = "ok"
    answer: str
    grounding: Literal["cited", "uncited", "no_sources"]
    truncated: bool = False
    citations: list[Citation]
    related: list[Citation]
    model: str
    fallback_used: bool
    request_id: str
    latency_ms: int


class ErrorBody(BaseModel):
    code: str
    message: str
    retryable: bool
    request_id: str
    retry_after_s: int | None = None


class ErrorResponse(BaseModel):
    status: Literal["error"] = "error"
    error: ErrorBody
