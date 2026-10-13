from qa_orchestrator.orchestration import (
    DeepReviewRule,
    DeepReviewSignals,
    OrchestrationReason,
    assess_deep_review,
)


def _evidenced_signals(**signals):
    return DeepReviewSignals(
        **signals,
        rationale="The review evidence supports this escalation signal.",
        evidence_refs=("E1",),
    )


def test_deep_review_signal_schema_explains_each_boolean():
    properties = DeepReviewSignals.model_json_schema()["properties"]

    assert all(properties[name]["description"] for name in DeepReviewSignals.model_fields)
    assert "authoritative" in properties["evidence_conflict"]["description"]
    assert "many consumers" in properties["high_blast_radius"]["description"]


def test_evidence_uncertainty_is_specific_and_actionable():
    description = DeepReviewSignals.model_json_schema()["properties"][
        "evidence_uncertain"
    ]["description"]

    assert "specific material conclusion" in description
    assert "read-only review could plausibly resolve it" in description
    assert "CI, Stage, or runtime checks were not run" in description


def test_high_risk_with_uncertain_evidence_requires_deep_review():
    decision = assess_deep_review(
        _evidenced_signals(high_risk_domain=True, evidence_uncertain=True)
    )

    assert decision.should_escalate is True
    assert decision.triggered_rules == (DeepReviewRule.HIGH_RISK_WITH_UNCERTAINTY,)
    assert decision.reason_codes == (
        OrchestrationReason.HIGH_RISK_DOMAIN,
        OrchestrationReason.EVIDENCE_GAP,
    )


def test_two_complexity_signals_require_deep_review():
    decision = assess_deep_review(
        _evidenced_signals(cross_system_scope=True, non_reproducible=True)
    )

    assert decision.should_escalate is True
    assert decision.triggered_rules == (DeepReviewRule.MULTIPLE_COMPLEXITY_SIGNALS,)
    assert decision.complexity_signal_count == 2
    assert decision.reason_codes == (
        OrchestrationReason.CROSS_REPOSITORY,
        OrchestrationReason.NON_REPRODUCIBLE,
    )


def test_critical_evidence_conflict_requires_deep_review():
    decision = assess_deep_review(
        _evidenced_signals(evidence_conflict=True, high_blast_radius=True)
    )

    assert decision.should_escalate is True
    assert decision.triggered_rules == (DeepReviewRule.CRITICAL_EVIDENCE_CONFLICT,)
    assert decision.reason_codes == (
        OrchestrationReason.EVIDENCE_CONFLICT,
        OrchestrationReason.HIGH_BLAST_RADIUS,
    )


def test_single_low_risk_signal_does_not_require_deep_review():
    decision = assess_deep_review(_evidenced_signals(cross_system_scope=True))

    assert decision.should_escalate is False
    assert decision.triggered_rules == ()
    assert decision.reason_codes == ()
    assert decision.complexity_signal_count == 1
