"""User-selected model policy and its local, non-secret configuration file."""

from __future__ import annotations

import json
import os
import re
from enum import StrEnum
from pathlib import Path
from tempfile import mkstemp
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

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
    if os.name != "nt":
        path.parent.chmod(0o700)

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
