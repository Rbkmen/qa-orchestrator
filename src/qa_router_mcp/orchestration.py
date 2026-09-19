from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from types import MappingProxyType
from typing import Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, ValidationError, model_validator

from qa_router_mcp.contracts import QaTaskOutcome, QaTaskType, ReviewAgent, ReviewBundle
from qa_router_mcp.review_profiles import REVIEW_BUNDLES, bundle_profiles


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
    allowed_profiles: list[ReviewAgent] = Field(default_factory=lambda: list(ReviewAgent))
    allowed_bundles: list[ReviewBundle] = Field(default_factory=lambda: list(REVIEW_BUNDLES))
    selected_bundle: ReviewBundle | None = None
    selected_profile: ReviewAgent | None = None
    review_profiles: list[ReviewAgent] = Field(default_factory=list)
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
    selected_bundle: ReviewBundle | None = None
    selected_profile: ReviewAgent | None = None
    needs_deep_analysis: bool = False
    reason_code: OrchestrationReason | None = None

    @model_validator(mode="after")
    def validate_transition_signal(self) -> "AdvanceQaOrchestrationRequest":
        if (
            self.completed_step is OrchestrationStep.LUNA_TRIAGE
            and self.status == "completed"
        ):
            if (self.selected_bundle is None) == (self.selected_profile is None):
                raise ValueError("exactly one selection is required after Luna triage")
        elif self.selected_bundle is not None or self.selected_profile is not None:
            raise ValueError("selection may only be supplied after Luna")

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


class OrchestrationError(ValueError):
    """Raised when a host submits an invalid orchestration operation."""


_NEXT_ACTIONS = MappingProxyType(
    {
        OrchestrationStep.LUNA_TRIAGE: "Host runs Luna triage and submits the selected review bundle or profile.",
        OrchestrationStep.TERRA_PRIMARY_REVIEW: "Host runs Terra primary review with the selected ordered profiles.",
        OrchestrationStep.SOL_DEEP_REVIEW: "Host runs Sol deep read-only analysis for the fixed escalation reason.",
        OrchestrationStep.TERRA_SYNTHESIS: "Host runs Terra synthesis and validates the final QA result.",
        OrchestrationStep.AWAITING_HOST_OUTCOME: "Host records the final QA outcome.",
    }
)
_TERMINAL_ACTIONS = MappingProxyType(
    {
        OrchestrationStatus.PARTIAL: "Host records the partial QA outcome.",
        OrchestrationStatus.BLOCKED: "Host records the blocked QA outcome.",
    }
)
_TASK_TYPE_ADAPTER = TypeAdapter(QaTaskType)


class QaOrchestrator:
    def __init__(
        self,
        *,
        ttl_seconds: int,
        max_sessions: int,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        if ttl_seconds <= 0:
            raise ValueError("ttl_seconds must be positive")
        if max_sessions <= 0:
            raise ValueError("max_sessions must be positive")
        self._ttl_seconds = ttl_seconds
        self._max_sessions = max_sessions
        self._clock = clock
        self._sessions: dict[str, QaOrchestrationSession] = {}

    def start(self, task_type: QaTaskType) -> QaOrchestrationSession:
        try:
            validated_task_type = _TASK_TYPE_ADAPTER.validate_python(task_type)
        except ValidationError as exc:
            raise OrchestrationError("invalid task type") from exc

        now = self._clock()
        self._purge_expired(now)
        if len(self._sessions) >= self._max_sessions:
            raise OrchestrationError("session limit")

        session = QaOrchestrationSession(
            run_id=f"qar-{uuid4().hex}",
            task_type=validated_task_type,
            status=OrchestrationStatus.ACTIVE,
            current_step=OrchestrationStep.LUNA_TRIAGE,
            allowed_profiles=list(ReviewAgent),
            allowed_bundles=list(REVIEW_BUNDLES),
            model_policy=MODEL_POLICIES[OrchestrationStep.LUNA_TRIAGE],
            next_action=_NEXT_ACTIONS[OrchestrationStep.LUNA_TRIAGE],
            expires_at=now + timedelta(seconds=self._ttl_seconds),
        )
        self._sessions[session.run_id] = session
        return self._copy(session)

    def advance(
        self,
        *,
        run_id: str,
        completed_step: OrchestrationStep,
        status: QaTaskOutcome,
        selected_bundle: ReviewBundle | None = None,
        selected_profile: ReviewAgent | None = None,
        needs_deep_analysis: bool = False,
        reason_code: OrchestrationReason | None = None,
    ) -> QaOrchestrationSession:
        try:
            request = AdvanceQaOrchestrationRequest(
                run_id=run_id,
                completed_step=completed_step,
                status=status,
                selected_bundle=selected_bundle,
                selected_profile=selected_profile,
                needs_deep_analysis=needs_deep_analysis,
                reason_code=reason_code,
            )
        except ValidationError as exc:
            raise OrchestrationError("invalid transition signal") from exc

        now = self._clock()
        self._purge_expired(now, keep_run_id=request.run_id)
        session = self._require_session(request.run_id, now)
        if session.status is not OrchestrationStatus.ACTIVE:
            raise OrchestrationError("terminal session")
        if request.completed_step is not session.current_step:
            raise OrchestrationError("illegal transition")

        if request.status in ("partial", "blocked"):
            updated = session.model_copy(
                update={
                    "status": OrchestrationStatus(request.status),
                    "model_policy": None,
                    "next_action": _TERMINAL_ACTIONS[OrchestrationStatus(request.status)],
                }
            )
            self._sessions[request.run_id] = updated
            return self._copy(updated)

        if (
            request.selected_profile is not None
            and session.selected_profile is not None
            and request.selected_profile != session.selected_profile
        ):
            raise OrchestrationError("changed profile after triage")

        updated = self._completed_transition(session, request)
        self._sessions[request.run_id] = updated
        return self._copy(updated)

    def get(self, run_id: str) -> QaOrchestrationSession:
        now = self._clock()
        self._purge_expired(now, keep_run_id=run_id)
        return self._copy(self._require_session(run_id, now))

    def _completed_transition(
        self,
        session: QaOrchestrationSession,
        request: AdvanceQaOrchestrationRequest,
    ) -> QaOrchestrationSession:
        if session.current_step is OrchestrationStep.LUNA_TRIAGE:
            if request.selected_bundle is not None:
                selected_profiles = bundle_profiles(request.selected_bundle)
                selected_profile = selected_profiles[0]
            elif request.selected_profile is not None:
                selected_profiles = (request.selected_profile,)
                selected_profile = request.selected_profile
            else:
                raise OrchestrationError("missing profile or bundle after Luna triage")
            return session.model_copy(
                update={
                    "current_step": OrchestrationStep.TERRA_PRIMARY_REVIEW,
                    "selected_bundle": request.selected_bundle,
                    "selected_profile": selected_profile,
                    "review_profiles": list(selected_profiles),
                    "model_policy": MODEL_POLICIES[OrchestrationStep.TERRA_PRIMARY_REVIEW],
                    "next_action": _NEXT_ACTIONS[OrchestrationStep.TERRA_PRIMARY_REVIEW],
                }
            )

        if session.current_step is OrchestrationStep.TERRA_PRIMARY_REVIEW:
            if request.needs_deep_analysis:
                return session.model_copy(
                    update={
                        "current_step": OrchestrationStep.SOL_DEEP_REVIEW,
                        "model_policy": MODEL_POLICIES[OrchestrationStep.SOL_DEEP_REVIEW],
                        "next_action": _NEXT_ACTIONS[OrchestrationStep.SOL_DEEP_REVIEW],
                    }
                )
            return session.model_copy(
                update={
                    "current_step": OrchestrationStep.TERRA_SYNTHESIS,
                    "model_policy": MODEL_POLICIES[OrchestrationStep.TERRA_PRIMARY_REVIEW],
                    "next_action": _NEXT_ACTIONS[OrchestrationStep.TERRA_SYNTHESIS],
                }
            )

        if session.current_step is OrchestrationStep.SOL_DEEP_REVIEW:
            return session.model_copy(
                update={
                    "current_step": OrchestrationStep.TERRA_SYNTHESIS,
                    "model_policy": MODEL_POLICIES[OrchestrationStep.TERRA_PRIMARY_REVIEW],
                    "next_action": _NEXT_ACTIONS[OrchestrationStep.TERRA_SYNTHESIS],
                }
            )

        if session.current_step is OrchestrationStep.TERRA_SYNTHESIS:
            return session.model_copy(
                update={
                    "status": OrchestrationStatus.AWAITING_HOST_OUTCOME,
                    "current_step": OrchestrationStep.AWAITING_HOST_OUTCOME,
                    "model_policy": None,
                    "next_action": _NEXT_ACTIONS[OrchestrationStep.AWAITING_HOST_OUTCOME],
                }
            )

        raise OrchestrationError("illegal transition")

    def _require_session(self, run_id: str, now: datetime) -> QaOrchestrationSession:
        session = self._sessions.get(run_id)
        if session is None:
            raise OrchestrationError("unknown run_id")
        if session.expires_at <= now:
            raise OrchestrationError("expired session")
        return session

    def _purge_expired(self, now: datetime, *, keep_run_id: str | None = None) -> None:
        for run_id, session in tuple(self._sessions.items()):
            if run_id != keep_run_id and session.expires_at <= now:
                del self._sessions[run_id]

    @staticmethod
    def _copy(session: QaOrchestrationSession) -> QaOrchestrationSession:
        return session.model_copy(deep=True)
