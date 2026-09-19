from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, Field

type QaTaskType = Literal[
    "ordinary_review",
    "widget_review",
    "epic_analysis",
    "requirements_analysis",
    "qa_planning",
    "autotest_implementation",
    "other",
]
type QaTaskOutcome = Literal["completed", "partial", "blocked"]


class ReviewAgent(StrEnum):
    PR_TEST_ANALYZER = "pr_test_analyzer"
    CODE_REVIEWER = "code_reviewer"
    SECURITY_REVIEWER = "security_reviewer"
    SILENT_FAILURE_HUNTER = "silent_failure_hunter"
    CODE_EXPLORER = "code_explorer"
    TYPESCRIPT_REVIEWER = "typescript_reviewer"
    REACT_REVIEWER = "react_reviewer"


class ReviewRoute(BaseModel):
    profile: ReviewAgent
    focus: str = Field(min_length=1)
    required_sections: list[str] = Field(min_length=1)
    constraints: list[str] = Field(min_length=1)
    escalation_signals: list[str] = Field(default_factory=list)
    read_only: bool = True
    host_owns_decisions: bool = True


class QaTaskOutcomeReceipt(BaseModel):
    status: Literal["recorded", "unavailable"]
