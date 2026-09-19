from datetime import datetime
from enum import StrEnum
from types import MappingProxyType
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from qa_router_mcp.contracts import QaTaskOutcome, QaTaskType, ReviewAgent


class OrchestrationStep(StrEnum):
    LUNA_TRIAGE = "luna_triage"
    TERRA_PRIMARY_REVIEW = "terra_primary_review"
    SOL_DEEP_REVIEW = "sol_deep_review"
    TERRA_SYNTHESIS = "terra_synthesis"
    AWAITING_HOST_OUTCOME = "awaiting_host_outcome"


class OrchestrationStatus(StrEnum):
    ACTIVE = "active"
    AWAITING_HOST_OUTCOME = "awaiting_host_outcome"
    COMPLETED = "completed"
    PARTIAL = "partial"
    BLOCKED = "blocked"
    EXPIRED = "expired"


class OrchestrationModel(StrEnum):
    LUNA = "gpt-5.6-luna"
    TERRA = "gpt-5.6-terra"
    SOL = "gpt-5.6-sol"


class OrchestrationReason(StrEnum):
    EVIDENCE_GAP = "evidence_gap"
    CROSS_REPOSITORY = "cross_repository"
    SECURITY_SENSITIVE = "security_sensitive"
    PAYMENT_SENSITIVE = "payment_sensitive"
    ROOT_CAUSE = "root_cause"
    HIGH_BLAST_RADIUS = "high_blast_radius"


class ModelPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    model: OrchestrationModel
    reasoning: Literal["medium", "high", "max"]


MODEL_POLICIES = MappingProxyType(
    {
        OrchestrationStep.LUNA_TRIAGE: ModelPolicy(
            model=OrchestrationModel.LUNA,
            reasoning="max",
        ),
        OrchestrationStep.TERRA_PRIMARY_REVIEW: ModelPolicy(
            model=OrchestrationModel.TERRA,
            reasoning="medium",
        ),
        OrchestrationStep.SOL_DEEP_REVIEW: ModelPolicy(
            model=OrchestrationModel.SOL,
            reasoning="high",
        ),
    }
)


class QaOrchestrationSession(BaseModel):
    model_config = ConfigDict(extra="forbid")

    run_id: str = Field(pattern=r"^qar-[0-9a-f]{32}$")
    task_type: QaTaskType
    status: OrchestrationStatus
    current_step: OrchestrationStep
    selected_profile: ReviewAgent | None = None
    model_policy: ModelPolicy | None = None
    next_action: str = Field(min_length=1)
    read_only: bool = True
    host_owns_decisions: bool = True
    expires_at: datetime


class AdvanceQaOrchestrationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    run_id: str = Field(pattern=r"^qar-[0-9a-f]{32}$")
    completed_step: OrchestrationStep
    status: QaTaskOutcome
    selected_profile: ReviewAgent | None = None
    needs_deep_analysis: bool = False
    reason_code: OrchestrationReason | None = None

    @model_validator(mode="after")
    def validate_transition_signal(self) -> "AdvanceQaOrchestrationRequest":
        if (
            self.completed_step is OrchestrationStep.LUNA_TRIAGE
            and self.status == "completed"
            and self.selected_profile is None
        ):
            raise ValueError("selected_profile is required after Luna triage")

        if self.needs_deep_analysis:
            if self.completed_step is not OrchestrationStep.TERRA_PRIMARY_REVIEW:
                raise ValueError("deep analysis can only follow Terra primary review")
            if self.status != "completed":
                raise ValueError("deep analysis requires a completed Terra primary review")
            if self.reason_code is None:
                raise ValueError("reason_code is required for deep analysis")
        elif self.reason_code is not None:
            raise ValueError("reason_code requires deep analysis")

        return self
