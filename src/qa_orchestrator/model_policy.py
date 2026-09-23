"""User-selected model policy and its local, non-secret configuration file."""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from tempfile import mkstemp
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

MODEL_ID_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$"
ReasoningEffort = Literal["none", "minimal", "low", "medium", "high", "xhigh", "max"]
REASONING_EFFORTS: tuple[ReasoningEffort, ...] = (
    "none",
    "minimal",
    "low",
    "medium",
    "high",
    "xhigh",
    "max",
)


def is_valid_model_id(value: object) -> bool:
    return isinstance(value, str) and re.fullmatch(MODEL_ID_PATTERN, value) is not None


class ModelProvider(StrEnum):
    OPENAI = "openai"
    ANTHROPIC = "anthropic"


@dataclass(frozen=True, slots=True)
class ModelCatalogEntry:
    """A recommended model and the reasoning values supported by that model."""

    label: str
    model_id: str
    reasoning_options: tuple[ReasoningEffort, ...]


_OPENAI_REASONING = ("none", "low", "medium", "high", "xhigh", "max")
_OPENAI_NO_REASONING = ("none",)
_OPENAI_NO_MAX_REASONING = ("none", "low", "medium", "high", "xhigh")
_OPENAI_ASTRA_REASONING = ("low", "medium", "high", "xhigh", "max")
_ANTHROPIC_EFFORT = ("low", "medium", "high", "xhigh", "max")
_ANTHROPIC_NO_EFFORT = ("none",)

# This is a deliberately short list of recommended models. Any other exact
# provider model ID remains available through the custom-ID option.
MODEL_CATALOGS: dict[ModelProvider, tuple[ModelCatalogEntry, ...]] = {
    ModelProvider.OPENAI: (
        ModelCatalogEntry("GPT-6 Astra — максимальное качество", "gpt-6-astra", _OPENAI_ASTRA_REASONING),
        ModelCatalogEntry("GPT-6 Sol — сложный код и агентные задачи", "gpt-6-sol", _OPENAI_REASONING),
        ModelCatalogEntry("GPT-6 Luna — быстрее и экономичнее", "gpt-6-luna", _OPENAI_REASONING),
        ModelCatalogEntry("GPT-5.6 Sol", "gpt-5.6-sol", _OPENAI_REASONING),
        ModelCatalogEntry("GPT-5.6 Terra — баланс качества и цены", "gpt-5.6-terra", _OPENAI_REASONING),
        ModelCatalogEntry("GPT-5.6 Luna — экономичный вариант", "gpt-5.6-luna", _OPENAI_REASONING),
        ModelCatalogEntry("GPT-4.1 — reasoning не поддерживается", "gpt-4.1", _OPENAI_NO_REASONING),
    ),
    ModelProvider.ANTHROPIC: (
        ModelCatalogEntry("Claude Fable 5.1 — сложные reasoning-задачи", "claude-fable-5-1", _ANTHROPIC_EFFORT),
        ModelCatalogEntry("Claude Opus 5.5 — максимальное качество", "claude-opus-5-5", _ANTHROPIC_EFFORT),
        ModelCatalogEntry("Claude Opus 5", "claude-opus-5", _ANTHROPIC_EFFORT),
        ModelCatalogEntry("Claude Sonnet 5 — баланс качества и скорости", "claude-sonnet-5", _ANTHROPIC_EFFORT),
        ModelCatalogEntry(
            "Claude Haiku 4.5 — быстрый, без effort",
            "claude-haiku-4-5-20251001",
            _ANTHROPIC_NO_EFFORT,
        ),
    ),
}

# Keep capability information for IDs that may still exist in an older local
# policy or can be entered manually, without advertising them in the menu.
_MODEL_REASONING_OPTIONS: dict[str, tuple[ReasoningEffort, ...]] = {
    entry.model_id: entry.reasoning_options
    for entries in MODEL_CATALOGS.values()
    for entry in entries
}
_MODEL_REASONING_OPTIONS.update(
    {
        "gpt-5.5": _OPENAI_NO_MAX_REASONING,
        "gpt-5.4": _OPENAI_NO_MAX_REASONING,
        "gpt-5.4-mini": _OPENAI_NO_MAX_REASONING,
        "gpt-5.4-nano": _OPENAI_NO_MAX_REASONING,
        "claude-opus-4-8": _ANTHROPIC_EFFORT,
        "claude-opus-4-7": _ANTHROPIC_EFFORT,
        "claude-opus-4-6": _ANTHROPIC_EFFORT,
        "claude-sonnet-4-6": _ANTHROPIC_EFFORT,
    }
)
_MODEL_PROVIDER_BY_ID = {
    model_id: provider
    for provider, entries in MODEL_CATALOGS.items()
    for model_id in (entry.model_id for entry in entries)
}
_MODEL_PROVIDER_BY_ID.update(
    {
        "gpt-5.5": ModelProvider.OPENAI,
        "gpt-5.4": ModelProvider.OPENAI,
        "gpt-5.4-mini": ModelProvider.OPENAI,
        "gpt-5.4-nano": ModelProvider.OPENAI,
        "claude-opus-4-8": ModelProvider.ANTHROPIC,
        "claude-opus-4-7": ModelProvider.ANTHROPIC,
        "claude-opus-4-6": ModelProvider.ANTHROPIC,
        "claude-sonnet-4-6": ModelProvider.ANTHROPIC,
    }
)


def model_provider_hint(model_id: str) -> ModelProvider | None:
    """Return a provider hint for known IDs and common provider prefixes."""

    if model_id in _MODEL_PROVIDER_BY_ID:
        return _MODEL_PROVIDER_BY_ID[model_id]
    normalized = model_id.casefold()
    if normalized.startswith(("claude-", "anthropic.")):
        return ModelProvider.ANTHROPIC
    if normalized.startswith(("gpt-", "chatgpt-", "o1", "o3", "o4", "o5", "o6")):
        return ModelProvider.OPENAI
    return None


def reasoning_options_for(provider: ModelProvider, model_id: str) -> tuple[ReasoningEffort, ...]:
    """Return model-specific reasoning values, or a provider-safe custom fallback."""

    if model_id in _MODEL_REASONING_OPTIONS:
        return _MODEL_REASONING_OPTIONS[model_id]
    if provider is ModelProvider.ANTHROPIC:
        # ``none`` means omit output_config.effort for an unknown model whose
        # capabilities must be checked by the host.
        return ("none", *_ANTHROPIC_EFFORT)
    return REASONING_EFFORTS


class ModelSelection(BaseModel):
    """The model IDs the host agent should use for each orchestration role."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    provider: ModelProvider
    triage_model: str = Field(min_length=1, max_length=128, pattern=MODEL_ID_PATTERN)
    primary_model: str = Field(min_length=1, max_length=128, pattern=MODEL_ID_PATTERN)
    deep_model: str = Field(min_length=1, max_length=128, pattern=MODEL_ID_PATTERN)
    synthesis_model: str = Field(min_length=1, max_length=128, pattern=MODEL_ID_PATTERN)
    triage_reasoning: ReasoningEffort = "max"
    primary_reasoning: ReasoningEffort = "medium"
    deep_reasoning: ReasoningEffort = "high"
    synthesis_reasoning: ReasoningEffort = "medium"

    @model_validator(mode="after")
    def validate_provider_and_reasoning(self) -> Self:
        for model_field, reasoning_field in (
            ("triage_model", "triage_reasoning"),
            ("primary_model", "primary_reasoning"),
            ("deep_model", "deep_reasoning"),
            ("synthesis_model", "synthesis_reasoning"),
        ):
            model_id = getattr(self, model_field)
            hinted_provider = model_provider_hint(model_id)
            if hinted_provider is not None and hinted_provider is not self.provider:
                raise ValueError(
                    f"{model_field} {model_id!r} does not belong to provider {self.provider.value}"
                )
            reasoning = getattr(self, reasoning_field)
            if reasoning not in reasoning_options_for(self.provider, model_id):
                allowed = ", ".join(reasoning_options_for(self.provider, model_id))
                raise ValueError(
                    f"{reasoning_field} {reasoning!r} is not supported by {model_id!r}; "
                    f"use one of: {allowed}"
                )
        return self


DEFAULT_MODEL_SELECTION = ModelSelection(
    provider=ModelProvider.OPENAI,
    triage_model="gpt-6-luna",
    primary_model="gpt-6-sol",
    deep_model="gpt-6-sol",
    synthesis_model="gpt-6-sol",
    deep_reasoning="high",
)


def load_model_selection(path: Path) -> ModelSelection:
    """Load a local selection, or return the safe default when it is absent."""

    if not path.exists():
        return DEFAULT_MODEL_SELECTION
    if path.is_symlink() or not path.is_file():
        raise ValueError(f"model policy must be a regular file: {path}")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        return ModelSelection.model_validate(payload)
    except (OSError, json.JSONDecodeError, TypeError, ValidationError) as exc:
        raise ValueError(f"invalid model policy: {path}") from exc


def save_model_selection(path: Path, selection: ModelSelection) -> None:
    """Persist a selection atomically without storing credentials."""

    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)

    file_descriptor, temporary_name = mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        if os.name != "nt":
            os.fchmod(file_descriptor, 0o600)
        with os.fdopen(file_descriptor, "w", encoding="utf-8") as temporary:
            temporary.write(json.dumps(selection.model_dump(mode="json"), indent=2) + "\n")
            temporary.flush()
            os.fsync(temporary.fileno())
        os.replace(temporary_name, path)
        if os.name != "nt":
            path.chmod(0o600)
    finally:
        try:
            os.unlink(temporary_name)
        except FileNotFoundError:
            pass
