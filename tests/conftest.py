"""Keep tests independent of the user's model policy and recovery store."""

from datetime import UTC, datetime

import pytest

from qa_orchestrator.contracts import (
    FinalReviewFinding,
    FinalReviewReport,
    ImpactCoverageLedger,
    ProfileAttemptMetrics,
    ProfileContextSelection,
    ProfileExecutionMetrics,
    ProfileReviewReport,
    ReviewAgent,
    ReviewCandidateDecision,
    ReviewContext,
    ReviewFindingCandidate,
    ReviewFindingLocation,
    ReviewFindingSource,
    impact_candidate_set_sha256,
    review_changed_paths_sha256,
    review_diff_hunk_inventory_sha256,
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
        input_manifest={
            "sources": (
                {
                    "source_id": "MR-123",
                    "kind": "merge_request",
                    "locator": "https://gitlab.example/group/service/-/merge_requests/123",
                    "required": True,
                    "retrieval_status": "complete",
                    "pagination_status": "not_applicable",
                    "extraction_status": "complete",
                    "criterion_refs": (),
                },
            )
        },
        neighbor_discovery_sha256="4" * 64,
        neighbor_discovery_candidate_count=0,
        neighbor_discovery_candidate_set_sha256=impact_candidate_set_sha256(()),
        diff_hunks=(),
        diff_hunk_inventory_sha256=review_diff_hunk_inventory_sha256("2" * 64, ()),
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
        failure_condition="A repeated request reaches the write operation twice.",
        flip_condition="A source-backed idempotency guard prevents the second write.",
        expected_behavior="One logical request produces at most one write.",
        actual_behavior="Under a retry, the changed path reaches the write operation twice.",
    )


@pytest.fixture
def make_profile_review_report(bound_review_context):
    def make(
        profile: ReviewAgent,
        *,
        context_selection=None,
        omit_context_selection=False,
        reviewed_changed_paths=None,
        reviewed_diff_hunk_indexes=None,
        omit_input_receipt=False,
        findings=(),
        coverage_gaps=(),
        unverified=(),
        evidence_map=(),
        execution_metrics: ProfileExecutionMetrics | None = None,
    ):
        if context_selection is None and not omit_context_selection:
            context_selection = ProfileContextSelection(
                source_packet_sha256=bound_review_context.evidence_packet_sha256,
                selected_packet_sha256="8" * 64,
                included_refs=bound_review_context.evidence_refs,
                changed_files_count=len(bound_review_context.changed_files),
                max_bytes=1024,
                selected_bytes=512,
            )
        if execution_metrics is None and context_selection is not None and not omit_input_receipt:
            attempt_time = datetime(2026, 10, 20, 12, tzinfo=UTC)
            included_refs = set(context_selection.included_refs)
            included_source_paths = tuple(
                sorted(
                    {
                        entry.path
                        for entry in bound_review_context.evidence_registry
                        if entry.kind == "source"
                        and entry.revision == bound_review_context.head_sha
                        and entry.source_text_available is True
                        and entry.path in bound_review_context.changed_files
                        and entry.evidence_ref in included_refs
                    }
                )
            )
            attempt = ProfileAttemptMetrics(
                profile=profile,
                attempt=1,
                outcome="completed",
                review_head_sha=bound_review_context.head_sha,
                review_diff_sha256=bound_review_context.diff_sha256,
                started_at=attempt_time,
                completed_at=attempt_time,
                queue_seconds=0,
                executor_seconds=0,
                submitted_prompt_sha256="9" * 64,
                prompt_write_status="written",
                source_packet_sha256=context_selection.source_packet_sha256,
                selected_packet_sha256=context_selection.selected_packet_sha256,
                included_evidence_refs=context_selection.included_refs,
                omitted_evidence_refs=context_selection.omitted_refs,
                included_source_paths=included_source_paths,
            )
            execution_metrics = ProfileExecutionMetrics(
                started_at=attempt_time,
                completed_at=attempt_time,
                attempts=(attempt,),
            )
        return ProfileReviewReport(
            profile=profile,
            context_selection=context_selection,
            scope="Reviewed src/service.py and its changed behavior.",
            finding_candidates=tuple(findings),
            reviewed_changed_paths=tuple(
                bound_review_context.changed_files
                if reviewed_changed_paths is None
                else reviewed_changed_paths
            ),
            reviewed_diff_hunk_indexes=tuple(
                range(len(bound_review_context.diff_hunks))
                if reviewed_diff_hunk_indexes is None
                else reviewed_diff_hunk_indexes
            ),
            coverage_gaps=tuple(coverage_gaps),
            unverified=tuple(unverified),
            evidence_map=tuple(evidence_map),
            execution_metrics=execution_metrics,
        )

    return make


@pytest.fixture
def make_impact_coverage():
    def make(context):
        if (
            context.neighbor_discovery_sha256 is None
            or context.neighbor_discovery_candidate_count is None
            or context.neighbor_discovery_candidate_set_sha256 is None
        ):
            return None
        return ImpactCoverageLedger(
            head_sha=context.head_sha,
            changed_paths_sha256=review_changed_paths_sha256(context.changed_files),
            discovery_sha256=context.neighbor_discovery_sha256,
            discovery_method="test_fixture",
            discovery_version=1,
            discovery_state="ready",
            limits=(
                {
                    "name": "candidate_limit",
                    "value": 200,
                    "reached": False,
                    "uninspected": False,
                },
            ),
            candidates=(),
            omissions=(),
        )

    return make


@pytest.fixture
def make_final_review_report(bound_review_context, make_impact_coverage):
    from qa_orchestrator.refutation import make_refutation_check

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

    def make(profiles, *, findings=(), coverage_gaps=(), unverified=(), context=None):
        final_findings = tuple(make_finding(finding, sources) for finding, sources in findings)
        review_context = context or bound_review_context
        decisions = []
        for finding, sources in findings:
            for profile in sources:
                source = ReviewFindingSource(
                    profile=profile, candidate_id=finding.candidate_id
                )
                decisions.append(
                    ReviewCandidateDecision(
                        source=source,
                        verdict="confirmed",
                        rationale="Validated against source.",
                        evidence_refs=finding.evidence_refs,
                        final_finding_id=finding.candidate_id,
                        refutation=make_refutation_check(
                            review_context,
                            finding,
                            status="not_refuted",
                            rationale=(
                                "The relevant counterevidence was checked against the bound source."
                            ),
                            evidence_refs=finding.evidence_refs,
                        ),
                    )
                )
        return FinalReviewReport(
            summary="Completed synthesis for the selected review profiles.",
            profiles_reviewed=tuple(profiles),
            finding_candidates=final_findings,
            coverage_gaps=tuple(coverage_gaps),
            unverified=tuple(unverified),
            impact_coverage=make_impact_coverage(review_context),
            candidate_decisions=tuple(decisions),
        )

    return make
