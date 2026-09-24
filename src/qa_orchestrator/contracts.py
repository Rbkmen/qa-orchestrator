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
    RUBY_REVIEWER = "ruby_reviewer"
    PYTHON_REVIEWER = "python_reviewer"
    MOBILE_REVIEWER = "mobile_reviewer"


class ReviewBundle(StrEnum):
    ORDINARY_MR = "ordinary_mr"
    WIDGET = "widget"
    WIDGET_JS = "widget_js"
    RUBY_BACKEND = "ruby_backend"
    PYTHON_BACKEND = "python_backend"
    MOBILE = "mobile"
    SECURITY = "security"
    AUTOTEST = "autotest"
    REQUIREMENTS = "requirements"


class ReviewRoute(BaseModel):
    profile: ReviewAgent
    display_name: str = Field(min_length=1)
    focus: str = Field(
        min_length=1,
        description="The profile's review boundary and primary responsibility.",
    )
    required_sections: list[str] = Field(
        min_length=1,
        description="Profile-specific output sections; use these instead of a generic template.",
    )
    constraints: list[str] = Field(
        min_length=1,
        description="Shared limits for this read-only review profile.",
    )
    escalation_signals: list[str] = Field(
        default_factory=list,
        description=(
            "Conditions to assess against evidence. A listed condition is not itself a deep-review trigger; "
            "set risk_signals only when the corresponding evidence-based condition applies."
        ),
    )
    read_only: Literal[True] = True
    host_owns_decisions: Literal[True] = True
