"""Evidence-backed escalation signals and fixed deep-review decisions."""

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, model_validator

from qa_orchestrator.contracts import EvidenceRefId


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
    """Host-supplied escalation signals with the rationale and refs that support them."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    high_risk_domain: bool = Field(
        default=False,
        description="The change affects a sensitive security, identity, payment, fraud, privacy, or access-control area.",
    )
    evidence_uncertain: bool = Field(
        default=False,
        description=(
            "A specific material conclusion remains uncertain, and a scoped read-only review "
            "could plausibly resolve it. Do not set solely because CI, Stage, or runtime checks "
            "were not run."
        ),
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
    rationale: str | None = Field(
        default=None,
        max_length=2000,
        description="Short explanation for the enabled signals; required when any signal is true.",
    )
    evidence_refs: tuple[EvidenceRefId, ...] = Field(
        default=(),
        max_length=32,
        description="Evidence Packet references supporting the enabled signals.",
    )

    @model_validator(mode="after")
    def validate_signal_evidence(self) -> "DeepReviewSignals":
        signal_values = self.model_dump(exclude={"rationale", "evidence_refs"})
        any_signal = any(signal_values.values())
        if any_signal and (
            not self.rationale or not self.rationale.strip() or not self.evidence_refs
        ):
            raise ValueError("enabled risk signals require rationale and evidence_refs")
        if not any_signal and (self.rationale is not None or self.evidence_refs):
            raise ValueError("rationale and evidence_refs require an enabled risk signal")
        if len(set(self.evidence_refs)) != len(self.evidence_refs):
            raise ValueError("risk signal evidence_refs must be unique")
        return self


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


