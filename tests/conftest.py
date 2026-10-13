"""Keep tests independent of the user's model policy and recovery store."""

import pytest

from qa_orchestrator.contracts import (
    FinalReviewFinding,
    FinalReviewReport,
    ProfileExecutionMetrics,
    ProfileReviewReport,
    ReviewAgent,
    ReviewCandidateDecision,
    ReviewContext,
    ReviewFindingCandidate,
    ReviewFindingLocation,
    ReviewFindingSource,
)


@pytest.fixture(autouse=True)
def isolated_orchestrator_environment(monkeypatch, tmp_path):
    for name in (
        "QA_ORCHESTRATOR_MODEL_POLICY_PATH",
        "QA_ORCHESTRATOR_SESSION_STORE_PATH",
        "QA_ORCHESTRATOR_ORCHESTRATION_TTL_SECONDS",
        "QA_ORCHESTRATOR_ORCHESTRATION_MAX_SESSIONS",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("QA_ORCHESTRATOR_DATA_DIR", str(tmp_path))


@pytest.fixture
def bound_review_context():
    return ReviewContext(
        repository="gitlab.task4work.info/group/service",
        base_sha="0" * 40,
        head_sha="1" * 40,
        diff_sha256="2" * 64,
        evidence_packet_sha256="3" * 64,
        changed_files=("src/service.py",),
        evidence_refs=("E1", "E2"),
    )


@pytest.fixture
def review_finding_candidate():
    return ReviewFindingCandidate(
        candidate_id="F-01",
        title="Candidate finding",
        description="A changed branch can return an incorrect result.",
        confidence="high",
        verification_gap="No runtime verification was performed.",
        evidence_refs=("E1",),
        source_locations=(ReviewFindingLocation(path="src/service.py", start_line=42),),
        affected_area="service response",
        impact="The caller can receive an incorrect result.",
    )


@pytest.fixture
def make_profile_review_report():
    def make(
        profile: ReviewAgent,
        *,
        findings=(),
        coverage_gaps=(),
        unverified=(),
        evidence_map=(),
        execution_metrics: ProfileExecutionMetrics | None = None,
    ):
        return ProfileReviewReport(
            profile=profile,
            scope="Reviewed src/service.py and its changed behavior.",
            finding_candidates=tuple(findings),
            coverage_gaps=tuple(coverage_gaps),
            unverified=tuple(unverified),
            evidence_map=tuple(evidence_map),
            execution_metrics=execution_metrics,
        )

    return make


@pytest.fixture
def make_final_review_report():
    def make_finding(finding, sources):
        profile_sources = tuple(sources)
        return FinalReviewFinding(
            **finding.model_dump(),
            severity="medium",
            source_profiles=profile_sources,
            source_candidates=tuple(
                ReviewFindingSource(profile=profile, candidate_id=finding.candidate_id)
                for profile in profile_sources
            ),
        )

    def make(profiles, *, findings=(), coverage_gaps=(), unverified=()):
        final_findings = tuple(make_finding(finding, sources) for finding, sources in findings)
        return FinalReviewReport(
            summary="Completed synthesis for the selected review profiles.",
            profiles_reviewed=tuple(profiles),
            finding_candidates=final_findings,
            coverage_gaps=tuple(coverage_gaps),
            unverified=tuple(unverified),
            candidate_decisions=tuple(
                ReviewCandidateDecision(
                    source=source, verdict="confirmed", rationale="Validated against source.",
                    evidence_refs=finding.evidence_refs, final_finding_id=finding.candidate_id,
                )
                for finding in final_findings for source in finding.source_candidates
            ),
        )

    return make
