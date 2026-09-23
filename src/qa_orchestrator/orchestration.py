from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from threading import RLock
from types import MappingProxyType
from typing import Literal
from uuid import uuid4

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    TypeAdapter,
    ValidationError,
    field_validator,
    model_validator,
)

from qa_orchestrator.contracts import QaTaskOutcome, QaTaskType, ReviewAgent, ReviewBundle
from qa_orchestrator.model_policy import (
    DEFAULT_MODEL_SELECTION,
    ModelProvider,
    ModelSelection,
    ReasoningEffort,
    is_valid_model_id,
)
from qa_orchestrator.review_profiles import REVIEW_BUNDLES, bundle_profiles


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
    LUNA = "gpt-6-luna"
    SOL = "gpt-6-sol"


class OrchestrationReason(StrEnum):
    EVIDENCE_GAP = "evidence_gap"
    CROSS_REPOSITORY = "cross_repository"
    SECURITY_SENSITIVE = "security_sensitive"
    PAYMENT_SENSITIVE = "payment_sensitive"
    FRAUD_SENSITIVE = "fraud_sensitive"
    ROOT_CAUSE = "root_cause"
    HIGH_BLAST_RADIUS = "high_blast_radius"
    HIGH_RISK_DOMAIN = "high_risk_domain"
    EVIDENCE_CONFLICT = "evidence_conflict"
    NON_REPRODUCIBLE = "non_reproducible"


class DeepReviewRule(StrEnum):
    HIGH_RISK_WITH_UNCERTAINTY = "high_risk_with_uncertainty"
    MULTIPLE_COMPLEXITY_SIGNALS = "multiple_complexity_signals"
    CRITICAL_EVIDENCE_CONFLICT = "critical_evidence_conflict"


class DeepReviewSignals(BaseModel):
    """Content-free, host-supplied signals used for deterministic escalation."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    high_risk_domain: bool = False
    evidence_uncertain: bool = False
    cross_system_scope: bool = False
    multiple_plausible_causes: bool = False
    evidence_conflict: bool = False
    non_reproducible: bool = False
    high_blast_radius: bool = False


class DeepReviewAssessment(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    should_escalate: bool
    triggered_rules: tuple[DeepReviewRule, ...] = ()
    reason_codes: tuple[OrchestrationReason, ...] = ()
    complexity_signal_count: int = Field(ge=0, le=4)


_COMPLEXITY_SIGNAL_REASONS = (
    ("cross_system_scope", OrchestrationReason.CROSS_REPOSITORY),
    ("multiple_plausible_causes", OrchestrationReason.ROOT_CAUSE),
    ("non_reproducible", OrchestrationReason.NON_REPRODUCIBLE),
    ("high_blast_radius", OrchestrationReason.HIGH_BLAST_RADIUS),
)


def assess_deep_review(signals: DeepReviewSignals) -> DeepReviewAssessment:
    """Apply the fixed deep-review rules to structured host signals."""
    complexity_signal_count = sum(
        getattr(signals, field_name) for field_name, _ in _COMPLEXITY_SIGNAL_REASONS
    )
    triggered_rules: list[DeepReviewRule] = []
    reason_codes: list[OrchestrationReason] = []

    def add_reason(reason: OrchestrationReason) -> None:
        if reason not in reason_codes:
            reason_codes.append(reason)

    if signals.high_risk_domain and signals.evidence_uncertain:
        triggered_rules.append(DeepReviewRule.HIGH_RISK_WITH_UNCERTAINTY)
        add_reason(OrchestrationReason.HIGH_RISK_DOMAIN)
        add_reason(OrchestrationReason.EVIDENCE_GAP)

    if complexity_signal_count >= 2:
        triggered_rules.append(DeepReviewRule.MULTIPLE_COMPLEXITY_SIGNALS)
        for field_name, reason in _COMPLEXITY_SIGNAL_REASONS:
            if getattr(signals, field_name):
                add_reason(reason)

    if signals.evidence_conflict and (
        signals.high_risk_domain
        or signals.cross_system_scope
        or signals.high_blast_radius
    ):
        triggered_rules.append(DeepReviewRule.CRITICAL_EVIDENCE_CONFLICT)
        add_reason(OrchestrationReason.EVIDENCE_CONFLICT)
        if signals.high_blast_radius:
            add_reason(OrchestrationReason.HIGH_BLAST_RADIUS)

    return DeepReviewAssessment(
        should_escalate=bool(triggered_rules),
        triggered_rules=tuple(triggered_rules),
        reason_codes=tuple(reason_codes),
        complexity_signal_count=complexity_signal_count,
    )


class ModelPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    provider: ModelProvider = ModelProvider.OPENAI
    model: OrchestrationModel | str
    reasoning: ReasoningEffort
    speed: Literal[1.0] = 1.0

    @field_validator("model")
    @classmethod
    def validate_model_id(cls, value: OrchestrationModel | str) -> OrchestrationModel | str:
        if not is_valid_model_id(str(value)):
            raise ValueError("model must be a safe model identifier")
        return value


def _model_value(model: str) -> OrchestrationModel | str:
    try:
        return OrchestrationModel(model)
    except ValueError:
        return model


def build_model_policies(selection: ModelSelection) -> MappingProxyType:
    return MappingProxyType(
        {
            OrchestrationStep.LUNA_TRIAGE: ModelPolicy(
                provider=selection.provider,
                model=_model_value(selection.triage_model),
                reasoning=selection.triage_reasoning,
            ),
            OrchestrationStep.TERRA_PRIMARY_REVIEW: ModelPolicy(
                provider=selection.provider,
                model=_model_value(selection.primary_model),
                reasoning=selection.primary_reasoning,
            ),
            OrchestrationStep.SOL_DEEP_REVIEW: ModelPolicy(
                provider=selection.provider,
                model=_model_value(selection.deep_model),
                reasoning=selection.deep_reasoning,
            ),
            OrchestrationStep.TERRA_SYNTHESIS: ModelPolicy(
                provider=selection.provider,
                model=_model_value(selection.synthesis_model),
                reasoning=selection.synthesis_reasoning,
            ),
        }
    )


MODEL_POLICIES = build_model_policies(DEFAULT_MODEL_SELECTION)


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
    current_profile: ReviewAgent | None = None
    completed_profiles: list[ReviewAgent] = Field(default_factory=list)
    deep_assessment: DeepReviewAssessment | None = None
    deep_reason_code: OrchestrationReason | None = None
    model_policy: ModelPolicy | None = None
    next_action: str = Field(min_length=1)
    read_only: Literal[True] = True
    host_owns_decisions: Literal[True] = True
    outcome_recorded: bool = False
    expires_at: datetime


class AdvanceQaOrchestrationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    run_id: str = Field(pattern=r"^qar-[0-9a-f]{32}$")
    completed_step: OrchestrationStep
    status: QaTaskOutcome
    selected_bundle: ReviewBundle | None = None
    selected_profile: ReviewAgent | None = None
    completed_profile: ReviewAgent | None = None
    risk_signals: DeepReviewSignals | None = None
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

        if self.completed_step is OrchestrationStep.TERRA_PRIMARY_REVIEW:
            if self.status == "completed" and self.completed_profile is None:
                raise ValueError("completed_profile is required after Terra primary review")
            if self.status != "completed" and self.completed_profile is not None:
                raise ValueError("completed_profile requires a completed Terra primary review")
        elif self.completed_profile is not None:
            raise ValueError("completed_profile may only be supplied after Terra primary review")

        if self.risk_signals is not None:
            if (
                self.completed_step is not OrchestrationStep.TERRA_PRIMARY_REVIEW
                or self.status != "completed"
            ):
                raise ValueError(
                    "risk signals must be sent with the final Terra primary profile"
                )
            if self.needs_deep_analysis or self.reason_code is not None:
                raise ValueError("risk signals cannot be combined with a manual deep request")

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
        OrchestrationStep.TERRA_PRIMARY_REVIEW: "Host runs Sol primary review with the selected ordered profiles.",
        OrchestrationStep.SOL_DEEP_REVIEW: "Host runs Sol deep read-only analysis for the fixed escalation reason.",
        OrchestrationStep.TERRA_SYNTHESIS: "Host runs Sol synthesis and validates the final QA result.",
        OrchestrationStep.AWAITING_HOST_OUTCOME: "Host records the final QA outcome.",
    }
)
_TERMINAL_ACTIONS = MappingProxyType(
    {
        OrchestrationStatus.COMPLETED: "Host recorded the final QA outcome.",
        OrchestrationStatus.PARTIAL: "Host records the partial QA outcome.",
        OrchestrationStatus.BLOCKED: "Host records the blocked QA outcome.",
    }
)
_RECORDED_TERMINAL_ACTIONS = MappingProxyType(
    {
        OrchestrationStatus.COMPLETED: "Host recorded the final QA outcome.",
        OrchestrationStatus.PARTIAL: "Host recorded the partial QA outcome.",
        OrchestrationStatus.BLOCKED: "Host recorded the blocked QA outcome.",
    }
)
_ACTIVE_SESSION_STATUSES = frozenset(
    {OrchestrationStatus.ACTIVE, OrchestrationStatus.AWAITING_HOST_OUTCOME}
)
_TERMINAL_SESSION_STATUSES = frozenset(
    {
        OrchestrationStatus.COMPLETED,
        OrchestrationStatus.PARTIAL,
        OrchestrationStatus.BLOCKED,
    }
)
_TASK_TYPE_ADAPTER = TypeAdapter(QaTaskType)


class QaOrchestrator:
    def __init__(
        self,
        *,
        ttl_seconds: int,
        max_sessions: int,
        model_selection: ModelSelection | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        if ttl_seconds <= 0:
            raise ValueError("ttl_seconds must be positive")
        if max_sessions <= 0:
            raise ValueError("max_sessions must be positive")
        self._ttl_seconds = ttl_seconds
        self._max_sessions = max_sessions
        self._model_selection = model_selection or DEFAULT_MODEL_SELECTION
        self._model_policies = build_model_policies(self._model_selection)
        self._clock = clock
        self._sessions: dict[str, QaOrchestrationSession] = {}
        self._outcome_fingerprints: dict[str, str] = {}
        self._lock = RLock()

    @property
    def model_selection(self) -> ModelSelection:
        return self._model_selection

    def start(self, task_type: QaTaskType) -> QaOrchestrationSession:
        try:
            validated_task_type = _TASK_TYPE_ADAPTER.validate_python(task_type)
        except ValidationError as exc:
            raise OrchestrationError("invalid task type") from exc

        with self._lock:
            now = self._clock()
            self._purge_expired(now)
            if self._active_session_count() >= self._max_sessions:
                raise OrchestrationError("session limit")
            self._evict_terminal_sessions_until_capacity()

            session = QaOrchestrationSession(
                run_id=f"qar-{uuid4().hex}",
                task_type=validated_task_type,
                status=OrchestrationStatus.ACTIVE,
                current_step=OrchestrationStep.LUNA_TRIAGE,
                allowed_profiles=list(ReviewAgent),
                allowed_bundles=list(REVIEW_BUNDLES),
                model_policy=self._model_policies[OrchestrationStep.LUNA_TRIAGE],
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
        completed_profile: ReviewAgent | None = None,
        risk_signals: DeepReviewSignals | None = None,
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
                completed_profile=completed_profile,
                risk_signals=risk_signals,
                needs_deep_analysis=needs_deep_analysis,
                reason_code=reason_code,
            )
        except ValidationError as exc:
            raise OrchestrationError("invalid transition signal") from exc

        with self._lock:
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
        with self._lock:
            now = self._clock()
            self._purge_expired(now, keep_run_id=run_id)
            return self._copy(self._require_session(run_id, now))

    def get_outcome_fingerprint(self, run_id: str) -> str | None:
        with self._lock:
            now = self._clock()
            self._purge_expired(now, keep_run_id=run_id)
            self._require_session(run_id, now)
            return self._outcome_fingerprints.get(run_id)

    def finish(self, *, run_id: str, outcome: QaTaskOutcome) -> QaOrchestrationSession:
        """Record the host-owned final outcome for a completed orchestration flow."""
        try:
            final_status = OrchestrationStatus(outcome)
        except ValueError as exc:
            raise OrchestrationError("invalid final outcome") from exc

        if final_status not in {
            OrchestrationStatus.COMPLETED,
            OrchestrationStatus.PARTIAL,
            OrchestrationStatus.BLOCKED,
        }:
            raise OrchestrationError("invalid final outcome")

        with self._lock:
            now = self._clock()
            self._purge_expired(now, keep_run_id=run_id)
            session = self._require_session(run_id, now)

            if session.status in _TERMINAL_SESSION_STATUSES:
                if session.status is not final_status:
                    raise OrchestrationError("conflicting final outcome")
                return self._copy(session)

            if session.status is not OrchestrationStatus.AWAITING_HOST_OUTCOME:
                raise OrchestrationError("outcome is not ready")

            updated = session.model_copy(
                update={
                    "status": final_status,
                    "model_policy": None,
                    "next_action": _TERMINAL_ACTIONS[final_status],
                }
            )
            self._sessions[run_id] = updated
            return self._copy(updated)

    def mark_outcome_recorded(
        self,
        *,
        run_id: str,
        outcome: QaTaskOutcome,
        fingerprint: str,
    ) -> QaOrchestrationSession:
        with self._lock:
            now = self._clock()
            self._purge_expired(now, keep_run_id=run_id)
            session = self._require_session(run_id, now)
            if session.status is not OrchestrationStatus(outcome):
                raise OrchestrationError("conflicting final outcome")
            recorded_fingerprint = self._outcome_fingerprints.get(run_id)
            if session.outcome_recorded and recorded_fingerprint != fingerprint:
                raise OrchestrationError("conflicting outcome payload")
            if not session.outcome_recorded:
                session = session.model_copy(
                    update={
                        "outcome_recorded": True,
                        "next_action": _RECORDED_TERMINAL_ACTIONS[session.status],
                    }
                )
                self._sessions[run_id] = session
                self._outcome_fingerprints[run_id] = fingerprint
            return self._copy(session)

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
                    "current_profile": selected_profile,
                    "completed_profiles": [],
                    "deep_assessment": None,
                    "deep_reason_code": None,
                    "model_policy": self._model_policies[OrchestrationStep.TERRA_PRIMARY_REVIEW],
                    "next_action": _NEXT_ACTIONS[OrchestrationStep.TERRA_PRIMARY_REVIEW],
                }
            )

        if session.current_step is OrchestrationStep.TERRA_PRIMARY_REVIEW:
            if session.current_profile is None or not session.review_profiles:
                raise OrchestrationError("missing current profile")
            next_profile_index = len(session.completed_profiles)
            if next_profile_index >= len(session.review_profiles):
                raise OrchestrationError("all review profiles are already completed")
            expected_profile = session.review_profiles[next_profile_index]
            if session.current_profile is not expected_profile:
                raise OrchestrationError("current profile state is inconsistent")
            if request.completed_profile is not expected_profile:
                raise OrchestrationError("completed profile does not match current profile")
            is_last_profile = next_profile_index == len(session.review_profiles) - 1
            if request.needs_deep_analysis and not is_last_profile:
                raise OrchestrationError("deep analysis requires the final review profile")
            if request.risk_signals is not None and not is_last_profile:
                raise OrchestrationError(
                    "risk signals must be sent with the final Terra primary profile"
                )

            completed_profiles = [*session.completed_profiles, expected_profile]
            if not is_last_profile:
                next_profile = session.review_profiles[next_profile_index + 1]
                return session.model_copy(
                    update={
                        "current_profile": next_profile,
                        "completed_profiles": completed_profiles,
                        "model_policy": self._model_policies[OrchestrationStep.TERRA_PRIMARY_REVIEW],
                        "next_action": _NEXT_ACTIONS[OrchestrationStep.TERRA_PRIMARY_REVIEW],
                    }
                )

            assessment = (
                assess_deep_review(request.risk_signals)
                if request.risk_signals is not None
                else None
            )
            should_escalate = request.needs_deep_analysis or (
                assessment is not None and assessment.should_escalate
            )
            if should_escalate:
                reason_code = (
                    assessment.reason_codes[0]
                    if assessment is not None and assessment.reason_codes
                    else request.reason_code
                )
                if reason_code is None:
                    raise OrchestrationError("deep analysis requires a reason code")
                return session.model_copy(
                    update={
                        "current_step": OrchestrationStep.SOL_DEEP_REVIEW,
                        "current_profile": None,
                        "completed_profiles": completed_profiles,
                        "deep_assessment": assessment,
                        "deep_reason_code": reason_code,
                        "model_policy": self._model_policies[OrchestrationStep.SOL_DEEP_REVIEW],
                        "next_action": _NEXT_ACTIONS[OrchestrationStep.SOL_DEEP_REVIEW],
                    }
                )
            return session.model_copy(
                update={
                    "current_step": OrchestrationStep.TERRA_SYNTHESIS,
                    "current_profile": None,
                    "completed_profiles": completed_profiles,
                    "deep_assessment": assessment,
                    "deep_reason_code": None,
                    "model_policy": self._model_policies[OrchestrationStep.TERRA_SYNTHESIS],
                    "next_action": _NEXT_ACTIONS[OrchestrationStep.TERRA_SYNTHESIS],
                }
            )

        if session.current_step is OrchestrationStep.SOL_DEEP_REVIEW:
            return session.model_copy(
                update={
                    "current_step": OrchestrationStep.TERRA_SYNTHESIS,
                    "model_policy": self._model_policies[OrchestrationStep.TERRA_SYNTHESIS],
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
                self._outcome_fingerprints.pop(run_id, None)

    def _active_session_count(self) -> int:
        return sum(
            session.status in _ACTIVE_SESSION_STATUSES
            for session in self._sessions.values()
        )

    def _evict_terminal_sessions_until_capacity(self) -> None:
        required_evictions = len(self._sessions) - self._max_sessions + 1
        if required_evictions <= 0:
            return
        terminal_run_ids = sorted(
            (
                (session.expires_at, run_id)
                for run_id, session in self._sessions.items()
                if session.status in _TERMINAL_SESSION_STATUSES
            ),
        )
        if len(terminal_run_ids) < required_evictions:
            raise OrchestrationError("session limit")
        for _, run_id in terminal_run_ids[:required_evictions]:
            del self._sessions[run_id]
            self._outcome_fingerprints.pop(run_id, None)

    @staticmethod
    def _copy(session: QaOrchestrationSession) -> QaOrchestrationSession:
        return session.model_copy(deep=True)
