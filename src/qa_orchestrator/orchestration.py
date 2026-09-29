from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from threading import RLock
from types import MappingProxyType
from typing import Literal
from uuid import uuid4

from pydantic import (
    AwareDatetime,
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
    reasoning_capabilities_verified,
)
from qa_orchestrator.review_profiles import (
    REVIEW_BUNDLES,
    bundle_profiles,
    recommended_bundles_for,
)
from qa_orchestrator.session_store import SqliteSessionStore


class OrchestrationStep(StrEnum):
    TRIAGE = "triage"
    PRIMARY_REVIEW = "primary_review"
    DEEP_REVIEW = "deep_review"
    SYNTHESIS = "synthesis"
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

    high_risk_domain: bool = Field(
        default=False,
        description="The change affects a sensitive security, identity, payment, fraud, privacy, or access-control area.",
    )
    evidence_uncertain: bool = Field(
        default=False,
        description="A material conclusion depends on evidence that is missing or cannot be verified.",
    )
    cross_system_scope: bool = Field(
        default=False,
        description="The relevant behavior crosses repository, service, or system boundaries.",
    )
    multiple_plausible_causes: bool = Field(
        default=False,
        description="More than one plausible cause remains after investigation.",
    )
    evidence_conflict: bool = Field(
        default=False,
        description="Relevant authoritative evidence sources disagree.",
    )
    non_reproducible: bool = Field(
        default=False,
        description="The reported behavior cannot be reproduced under expected conditions.",
    )
    high_blast_radius: bool = Field(
        default=False,
        description="The change could affect many consumers, services, or data records.",
    )


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
        signals.high_risk_domain or signals.cross_system_scope or signals.high_blast_radius
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
    reasoning_capabilities_verified: bool = False

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
            OrchestrationStep.TRIAGE: ModelPolicy(
                provider=selection.provider,
                model=_model_value(selection.triage_model),
                reasoning=selection.triage_reasoning,
                reasoning_capabilities_verified=reasoning_capabilities_verified(
                    selection.provider, selection.triage_model
                ),
            ),
            OrchestrationStep.PRIMARY_REVIEW: ModelPolicy(
                provider=selection.provider,
                model=_model_value(selection.primary_model),
                reasoning=selection.primary_reasoning,
                reasoning_capabilities_verified=reasoning_capabilities_verified(
                    selection.provider, selection.primary_model
                ),
            ),
            OrchestrationStep.DEEP_REVIEW: ModelPolicy(
                provider=selection.provider,
                model=_model_value(selection.deep_model),
                reasoning=selection.deep_reasoning,
                reasoning_capabilities_verified=reasoning_capabilities_verified(
                    selection.provider, selection.deep_model
                ),
            ),
            OrchestrationStep.SYNTHESIS: ModelPolicy(
                provider=selection.provider,
                model=_model_value(selection.synthesis_model),
                reasoning=selection.synthesis_reasoning,
                reasoning_capabilities_verified=reasoning_capabilities_verified(
                    selection.provider, selection.synthesis_model
                ),
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
    recommended_bundles: list[ReviewBundle] = Field(default_factory=list)
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
    expires_at: AwareDatetime


class AdvanceQaOrchestrationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    run_id: str = Field(pattern=r"^qar-[0-9a-f]{32}$")
    completed_step: OrchestrationStep
    status: QaTaskOutcome
    selected_bundle: ReviewBundle | None = None
    selected_profile: ReviewAgent | None = None
    completed_profile: ReviewAgent | None = None
    risk_signals: DeepReviewSignals | None = None

    @model_validator(mode="after")
    def validate_transition_signal(self) -> "AdvanceQaOrchestrationRequest":
        if self.completed_step is OrchestrationStep.TRIAGE and self.status == "completed":
            if (self.selected_bundle is None) == (self.selected_profile is None):
                raise ValueError("exactly one selection is required after triage")
        elif self.selected_bundle is not None or self.selected_profile is not None:
            raise ValueError("selection may only be supplied after triage")

        if self.completed_step is OrchestrationStep.PRIMARY_REVIEW:
            if self.status == "completed" and self.completed_profile is None:
                raise ValueError("completed_profile is required after primary review")
            if self.status != "completed" and self.completed_profile is not None:
                raise ValueError("completed_profile requires a completed primary review")
        elif self.completed_profile is not None:
            raise ValueError("completed_profile may only be supplied after primary review")

        if self.risk_signals is not None and (
            self.completed_step is not OrchestrationStep.PRIMARY_REVIEW
            or self.status != "completed"
        ):
            raise ValueError("risk signals must be sent with the final primary-review profile")
        return self


class OrchestrationError(ValueError):
    """Raised when a host submits an invalid orchestration operation."""


_NEXT_ACTIONS = MappingProxyType(
    {
        OrchestrationStep.TRIAGE: "Host performs triage and submits the selected review bundle or profile.",
        OrchestrationStep.PRIMARY_REVIEW: "Host performs primary review with the selected ordered profiles.",
        OrchestrationStep.DEEP_REVIEW: "Host performs optional read-only deep analysis for the fixed escalation reason.",
        OrchestrationStep.SYNTHESIS: "Host performs final synthesis and validates the QA result.",
        OrchestrationStep.AWAITING_HOST_OUTCOME: "Host submits the final QA outcome with finish_qa_orchestration.",
    }
)
_TERMINAL_ACTIONS = MappingProxyType(
    {
        OrchestrationStatus.PARTIAL: "Host must finalize the partial QA outcome.",
        OrchestrationStatus.BLOCKED: "Host must finalize the blocked QA outcome.",
    }
)
_FINALIZED_TERMINAL_ACTIONS = MappingProxyType(
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
        session_store: SqliteSessionStore | None = None,
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
        self._session_store = session_store
        self._sessions: dict[str, QaOrchestrationSession] = {}
        if session_store is not None:
            for session in session_store.load_all():
                if session.next_action in _FINALIZED_TERMINAL_ACTIONS.values():
                    session_store.delete(session.run_id)
                    continue
                self._sessions[session.run_id] = session
        self._lock = RLock()
        if session_store is not None:
            now = self._clock()
            self._purge_expired(now)
            if self._active_session_count() > self._max_sessions:
                raise ValueError("session store exceeds the configured active session limit")
            self._evict_terminal_sessions_until_capacity(slots_required=0)

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
                current_step=OrchestrationStep.TRIAGE,
                allowed_profiles=list(ReviewAgent),
                allowed_bundles=list(REVIEW_BUNDLES),
                recommended_bundles=list(recommended_bundles_for(validated_task_type)),
                model_policy=self._model_policies[OrchestrationStep.TRIAGE],
                next_action=_NEXT_ACTIONS[OrchestrationStep.TRIAGE],
                expires_at=now + timedelta(seconds=self._ttl_seconds),
            )
            self._save_session(session)
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
                updated = self._refresh_expiry(
                    session.model_copy(
                        update={
                            "status": OrchestrationStatus(request.status),
                            "model_policy": None,
                            "next_action": _TERMINAL_ACTIONS[OrchestrationStatus(request.status)],
                        }
                    ),
                    now,
                )
                self._save_session(updated)
                return self._copy(updated)

            if (
                request.selected_profile is not None
                and session.selected_profile is not None
                and request.selected_profile != session.selected_profile
            ):
                raise OrchestrationError("changed profile after triage")

            updated = self._refresh_expiry(self._completed_transition(session, request), now)
            self._save_session(updated)
            return self._copy(updated)

    def get(self, run_id: str) -> QaOrchestrationSession:
        with self._lock:
            now = self._clock()
            self._purge_expired(now, keep_run_id=run_id)
            return self._copy(self._require_session(run_id, now))

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
                final_action = _FINALIZED_TERMINAL_ACTIONS[final_status]
                if session.next_action == final_action:
                    return self._copy(session)
                updated = session.model_copy(update={"next_action": final_action})
                updated = self._refresh_expiry(updated, now)
                self._save_session(updated, persist=False)
                return self._copy(updated)

            if session.status is not OrchestrationStatus.AWAITING_HOST_OUTCOME:
                raise OrchestrationError("outcome is not ready")

            updated = self._refresh_expiry(
                session.model_copy(
                    update={
                        "status": final_status,
                        "model_policy": None,
                        "next_action": _FINALIZED_TERMINAL_ACTIONS[final_status],
                    }
                ),
                now,
            )
            self._save_session(updated, persist=False)
            return self._copy(updated)

    def _completed_transition(
        self,
        session: QaOrchestrationSession,
        request: AdvanceQaOrchestrationRequest,
    ) -> QaOrchestrationSession:
        if session.current_step is OrchestrationStep.TRIAGE:
            if request.selected_bundle is not None:
                selected_profiles = bundle_profiles(request.selected_bundle)
                selected_profile = selected_profiles[0]
            elif request.selected_profile is not None:
                selected_profiles = (request.selected_profile,)
                selected_profile = request.selected_profile
            else:
                raise OrchestrationError("missing profile or bundle after triage")
            return session.model_copy(
                update={
                    "current_step": OrchestrationStep.PRIMARY_REVIEW,
                    "selected_bundle": request.selected_bundle,
                    "selected_profile": selected_profile,
                    "review_profiles": list(selected_profiles),
                    "current_profile": selected_profile,
                    "completed_profiles": [],
                    "deep_assessment": None,
                    "deep_reason_code": None,
                    "model_policy": self._model_policies[OrchestrationStep.PRIMARY_REVIEW],
                    "next_action": _NEXT_ACTIONS[OrchestrationStep.PRIMARY_REVIEW],
                }
            )

        if session.current_step is OrchestrationStep.PRIMARY_REVIEW:
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
            completed_profiles = [*session.completed_profiles, expected_profile]
            if not is_last_profile:
                if request.risk_signals is not None:
                    raise OrchestrationError(
                        "risk signals must be sent with the final primary-review profile"
                    )
                next_profile = session.review_profiles[next_profile_index + 1]
                return session.model_copy(
                    update={
                        "current_profile": next_profile,
                        "completed_profiles": completed_profiles,
                        "model_policy": self._model_policies[OrchestrationStep.PRIMARY_REVIEW],
                        "next_action": _NEXT_ACTIONS[OrchestrationStep.PRIMARY_REVIEW],
                    }
                )

            if request.risk_signals is None:
                raise OrchestrationError(
                    "risk_signals are required for the final primary-review profile"
                )
            assessment = assess_deep_review(request.risk_signals)
            if assessment.should_escalate:
                return session.model_copy(
                    update={
                        "current_step": OrchestrationStep.DEEP_REVIEW,
                        "current_profile": None,
                        "completed_profiles": completed_profiles,
                        "deep_assessment": assessment,
                        "deep_reason_code": assessment.reason_codes[0],
                        "model_policy": self._model_policies[OrchestrationStep.DEEP_REVIEW],
                        "next_action": _NEXT_ACTIONS[OrchestrationStep.DEEP_REVIEW],
                    }
                )
            return session.model_copy(
                update={
                    "current_step": OrchestrationStep.SYNTHESIS,
                    "current_profile": None,
                    "completed_profiles": completed_profiles,
                    "deep_assessment": assessment,
                    "deep_reason_code": None,
                    "model_policy": self._model_policies[OrchestrationStep.SYNTHESIS],
                    "next_action": _NEXT_ACTIONS[OrchestrationStep.SYNTHESIS],
                }
            )

        if session.current_step is OrchestrationStep.DEEP_REVIEW:
            return session.model_copy(
                update={
                    "current_step": OrchestrationStep.SYNTHESIS,
                    "model_policy": self._model_policies[OrchestrationStep.SYNTHESIS],
                    "next_action": _NEXT_ACTIONS[OrchestrationStep.SYNTHESIS],
                }
            )

        if session.current_step is OrchestrationStep.SYNTHESIS:
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
            if self._session_store is not None:
                self._session_store.delete(run_id)
            raise OrchestrationError("expired session")
        return session

    def _purge_expired(self, now: datetime, *, keep_run_id: str | None = None) -> None:
        expired_run_ids = [
            run_id
            for run_id, session in self._sessions.items()
            if run_id != keep_run_id and session.expires_at <= now
        ]
        if self._session_store is not None:
            self._session_store.delete_many(expired_run_ids)
        for run_id in expired_run_ids:
            self._sessions.pop(run_id, None)

    def _active_session_count(self) -> int:
        return sum(
            session.status in _ACTIVE_SESSION_STATUSES for session in self._sessions.values()
        )

    def _evict_terminal_sessions_until_capacity(self, *, slots_required: int = 1) -> None:
        required_evictions = len(self._sessions) - self._max_sessions + slots_required
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
            self._delete_session(run_id)

    def _save_session(
        self,
        session: QaOrchestrationSession,
        *,
        persist: bool = True,
    ) -> None:
        if self._session_store is not None and persist:
            self._session_store.save(session)
        elif self._session_store is not None:
            self._session_store.delete(session.run_id)
        self._sessions[session.run_id] = session

    def _delete_session(self, run_id: str) -> None:
        if self._session_store is not None:
            self._session_store.delete(run_id)
        self._sessions.pop(run_id, None)

    def _refresh_expiry(
        self,
        session: QaOrchestrationSession,
        now: datetime,
    ) -> QaOrchestrationSession:
        return session.model_copy(update={"expires_at": now + timedelta(seconds=self._ttl_seconds)})

    @staticmethod
    def _copy(session: QaOrchestrationSession) -> QaOrchestrationSession:
        return session.model_copy(deep=True)
