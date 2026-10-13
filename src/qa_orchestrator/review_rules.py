"""Explicit, scoped review standards; guards are advice, never automatic filtering."""

import argparse
import fnmatch
import hashlib
import json
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class ReviewRule(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    rule_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
    kind: Literal["raise", "guard"]
    paths: tuple[str, ...] = Field(min_length=1, max_length=32)
    condition: str = Field(min_length=1, max_length=2000)
    text: str = Field(min_length=1, max_length=2000)
    rationale: str = Field(min_length=1, max_length=2000)

    @field_validator("condition", "text", "rationale")
    @classmethod
    def nonblank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("rule text, condition and rationale must not be blank")
        return value.strip()

    @field_validator("paths")
    @classmethod
    def relative_patterns(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        for pattern in value:
            if (
                not pattern.strip()
                or pattern.startswith("/")
                or "\\" in pattern
                or ":" in pattern
                or "\x00" in pattern
                or any(p in {"", ".", ".."} for p in pattern.split("/"))
            ):
                raise ValueError("rule paths must be repository-relative globs")
        return tuple(sorted(set(value)))


class ReviewRuleset(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = 1
    rules: tuple[ReviewRule, ...] = Field(default=(), max_length=100)

    @model_validator(mode="after")
    def unique_ids(self) -> "ReviewRuleset":
        if len({r.rule_id for r in self.rules}) != len(self.rules):
            raise ValueError("review rule IDs must be unique")
        return self

    @property
    def digest(self) -> str:
        data = {
            "schema_version": 1,
            "rules": [
                r.model_dump(mode="json") for r in sorted(self.rules, key=lambda r: r.rule_id)
            ],
        }
        return hashlib.sha256(
            json.dumps(data, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()

    def for_paths(self, paths: tuple[str, ...]) -> "ReviewRuleset":
        # POSIX, case-sensitive fnmatch. '*' may match '/' as documented.
        return ReviewRuleset(
            rules=tuple(
                r
                for r in self.rules
                if any(fnmatch.fnmatchcase(path, pattern) for path in paths for pattern in r.paths)
            )
        )

    def render(self) -> str:
        if not self.rules:
            return ""
        return (
            "Repository review rules (evaluate conditions against evidence; "
            "guards never suppress a different/new failure mode):\n"
            + json.dumps([r.model_dump(mode="json") for r in self.rules], ensure_ascii=False)
        )


def rules_main(argv: list[str]) -> int:
    from qa_orchestrator.contracts import ReviewContext

    parser = argparse.ArgumentParser(prog="qa-orch rules")
    parser.add_argument("--context", required=True, type=Path)
    parser.add_argument("--rules", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args(argv)
    context = ReviewContext.model_validate_json(args.context.read_bytes())
    if args.rules.stat().st_size > 65536:
        raise ValueError("review rules file exceeds 64 KB")
    rules = ReviewRuleset.model_validate_json(args.rules.read_bytes())
    bound = ReviewContext.model_validate({**context.model_dump(), "review_rules": rules})
    args.output.write_text(bound.model_dump_json(indent=2) + "\n", encoding="utf-8")
    return 0
