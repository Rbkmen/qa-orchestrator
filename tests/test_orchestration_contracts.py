import hashlib
import json
from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from qa_orchestrator.contracts import (
    FinalReviewReport,
    ImpactCoverageLedger,
    ProfileChangedPathCoverage,
    ProfileExecutionMetrics,
    ProfileReviewReport,
    ReviewAgent,
    ReviewBundle,
    ReviewContext,
    ReviewCoverageLedger,
    ReviewDiffHunk,
    ReviewFindingLocation,
    ReviewInputConflict,
    ReviewInputManifest,
    ReviewInputSource,
    ReviewNote,
    ReviewRouteSelection,
    ReviewVerificationRecord,
    VerificationReceipt,
    review_diff_hunk_inventory_sha256,
)
from qa_orchestrator.orchestration import (
    MODEL_POLICIES,
    AdvanceQaOrchestrationRequest,
    DeepReviewSignals,
    ModelPolicy,
    OrchestrationModel,
    OrchestrationStatus,
    OrchestrationStep,
    QaOrchestrationSession,
)
from qa_orchestrator.review_profiles import build_review_route

_VERIFICATION_STARTED_AT = datetime(2026, 10, 20, 12, 0, tzinfo=UTC)
_VERIFICATION_COMPLETED_AT = datetime(2026, 10, 20, 12, 5, tzinfo=UTC)
_VERIFICATION_SCOPE = "pytest tests/test_service.py"
_VERIFICATION_SCOPE_SHA256 = hashlib.sha256(
    _VERIFICATION_SCOPE.encode("utf-8")
).hexdigest()


def _verification_record_payload(**overrides):
    payload = {
        "verification_id": "V-01",
        "check_type": "ci",
        "scenario": "unit test suite",
        "status": "passed",
        "head_sha": "1" * 40,
        "environment": "Linux, Python 3.13",
        "source": "ci_provider",
        "source_ref": "workflow 123 / job 456",
        "started_at": _VERIFICATION_STARTED_AT,
        "completed_at": _VERIFICATION_COMPLETED_AT,
    }
    return {**payload, **overrides}


def _provider_verification_record_payload(*, include_receipt=True):
    payload = _verification_record_payload(
        scope=_VERIFICATION_SCOPE,
        scope_sha256=_VERIFICATION_SCOPE_SHA256,
        run_id="github-actions:123:456",
        provenance="provider_verified",
    )
    if include_receipt:
        payload["receipt"] = {
            "verifier_id": "github-actions",
            "run_id": payload["run_id"],
            "head_sha": payload["head_sha"],
            "conclusion": payload["status"],
            "scope_sha256": payload["scope_sha256"],
            "source_response_sha256": "2" * 64,
            "verified_at": _VERIFICATION_COMPLETED_AT + timedelta(seconds=1),
        }
    return payload


def test_provider_verified_record_requires_matching_receipt():
    with pytest.raises(ValidationError):
        ReviewVerificationRecord.model_validate(
            _provider_verification_record_payload(include_receipt=False)
        )


def test_provider_verified_record_accepts_matching_verifier_receipt():
    record = ReviewVerificationRecord.model_validate(
        _provider_verification_record_payload()
    )

    assert record.provenance == "provider_verified"
    assert isinstance(record.receipt, VerificationReceipt)


def test_input_manifest_v2_requires_an_explicit_inventory_and_conflict_assessment():
    source = ReviewInputSource(
        source_id="JIRA-1",
        kind="issue_description",
        locator="https://jira.example/browse/JIRA-1",
        required=True,
        retrieval_status="complete",
        pagination_status="not_applicable",
        extraction_status="complete",
    )

    with pytest.raises(ValidationError, match="requires inventory and conflict statuses"):
        ReviewInputManifest(schema_version=2, sources=(source,))

    legacy = ReviewInputManifest(sources=(source,))
    assert legacy.schema_version == 1
    assert legacy.source_inventory_status is None


def test_input_manifest_v2_binds_conflicts_to_declared_sources():
    first = ReviewInputSource(
        source_id="JIRA-1",
        kind="issue_description",
        locator="https://jira.example/browse/JIRA-1",
        required=True,
    )
    second = ReviewInputSource(
        source_id="MR-1",
        kind="merge_request",
        locator="https://gitlab.example/group/repo/-/merge_requests/1",
        required=True,
    )
    conflict = ReviewInputConflict(
        conflict_id="CONFLICT-1",
        source_ids=("JIRA-1", "MR-1"),
        disposition="resolved",
        resolution_source_id="JIRA-1",
        summary="The two descriptions specify different retry behavior.",
        resolution_summary="The ticket is the approved behavior source.",
    )

    manifest = ReviewInputManifest(
        schema_version=2,
        sources=(first, second),
        source_inventory_status="complete",
        source_conflict_status="resolved",
        source_conflicts=(conflict,),
    )

    assert manifest.source_conflicts == (conflict,)
    with pytest.raises(ValidationError, match="undeclared input source"):
        ReviewInputManifest(
            schema_version=2,
            sources=(first,),
            source_inventory_status="complete",
            source_conflict_status="unresolved",
            source_conflicts=(
                ReviewInputConflict(
                    conflict_id="CONFLICT-2",
                    source_ids=("JIRA-1", "MR-1"),
                    disposition="unresolved",
                    summary="The sources disagree.",
                ),
            ),
        )


@pytest.mark.parametrize(
    "receipt_changes",
    [
        {"run_id": "github-actions:999:456"},
        {"head_sha": "f" * 40},
        {"conclusion": "failed"},
        {"scope_sha256": "f" * 64},
    ],
)
def test_provider_verified_record_rejects_receipt_binding_mismatches(receipt_changes):
    payload = _provider_verification_record_payload()
    payload["receipt"] = {**payload["receipt"], **receipt_changes}

    with pytest.raises(ValidationError):
        ReviewVerificationRecord.model_validate(payload)


def test_legacy_verification_record_defaults_to_host_reported():
    record = ReviewVerificationRecord.model_validate(
        _verification_record_payload(
            started_at=None,
            completed_at=None,
        )
    )

    assert record.provenance == "host_reported"
    assert "provenance" not in record.model_dump()


def test_local_reproduction_requires_artifact_digest():
    with pytest.raises(ValidationError):
        ReviewVerificationRecord.model_validate(
            _verification_record_payload(
                source="local_runner",
                provenance="local_reproduced",
                scope=_VERIFICATION_SCOPE,
                scope_sha256=_VERIFICATION_SCOPE_SHA256,
                run_id="local:run-1",
            )
        )


def test_local_reproduction_requires_run_identity_and_scope():
    with pytest.raises(ValidationError):
        ReviewVerificationRecord.model_validate(
            _verification_record_payload(
                source="local_runner",
                provenance="local_reproduced",
                artifact_sha256="3" * 64,
            )
        )


def test_review_route_selection_rejects_group_order_mismatch():
    routes = [
        build_review_route(ReviewAgent.CODE_EXPLORER),
        build_review_route(ReviewAgent.CODE_REVIEWER),
    ]

    with pytest.raises(ValidationError, match="execution_groups must match route profiles"):
        ReviewRouteSelection(
            routes=routes,
            execution_groups=[[ReviewAgent.CODE_REVIEWER], [ReviewAgent.CODE_EXPLORER]],
        )


def test_review_coverage_ledger_rejects_invalid_indexes_and_inclusion_claims():
    with pytest.raises(ValidationError, match="unique and sorted"):
        ProfileChangedPathCoverage(
            profile=ReviewAgent.CODE_REVIEWER,
            context_selection_recorded=True,
            source_excerpt_included_path_indexes=(0, 0),
        )

    with pytest.raises(ValidationError, match="outside changed paths"):
        ReviewCoverageLedger(
            changed_paths_sha256="a" * 64,
            changed_path_count=1,
            head_source_excerpt_path_indexes=(1,),
            profiles=(
                ProfileChangedPathCoverage(
                    profile=ReviewAgent.CODE_REVIEWER,
                    context_selection_recorded=True,
                ),
            ),
        )

    with pytest.raises(ValidationError, match="have verified text"):
        ReviewCoverageLedger(
            changed_paths_sha256="a" * 64,
            changed_path_count=1,
            registered_head_source_path_indexes=(0,),
            profiles=(
                ProfileChangedPathCoverage(
                    profile=ReviewAgent.CODE_REVIEWER,
                    context_selection_recorded=True,
                    source_excerpt_included_path_indexes=(0,),
                ),
            ),
        )


def test_review_coverage_ledger_accounts_for_every_bound_diff_hunk():
    profile = ProfileChangedPathCoverage(
        profile=ReviewAgent.CODE_REVIEWER,
        context_selection_recorded=True,
        reviewed_diff_hunk_indexes=(0,),
    )
    valid = ReviewCoverageLedger(
        changed_paths_sha256="a" * 64,
        changed_path_count=1,
        profiles=(profile,),
        diff_hunk_inventory_sha256="b" * 64,
        diff_hunk_count=2,
        unreviewed_diff_hunk_indexes=(1,),
    )

    assert valid.unreviewed_diff_hunk_indexes == (1,)
    with pytest.raises(ValidationError, match="must match reviewer attestations"):
        ReviewCoverageLedger(
            changed_paths_sha256="a" * 64,
            changed_path_count=1,
            profiles=(profile,),
            diff_hunk_inventory_sha256="b" * 64,
            diff_hunk_count=2,
            unreviewed_diff_hunk_indexes=(),
        )


def test_review_context_binds_ordered_hunk_inventory_to_exact_diff(
    bound_review_context,
):
    hunk = ReviewDiffHunk(
        index=0,
        changed_path_index=0,
        old_start=1,
        old_count=1,
        new_start=1,
        new_count=2,
        header="@@ -1 +1,2 @@",
        sha256="c" * 64,
    )
    inventory_digest = review_diff_hunk_inventory_sha256(
        bound_review_context.diff_sha256, (hunk,)
    )
    context = ReviewContext.model_validate(
        {
            **bound_review_context.model_dump(),
            "diff_hunks": (hunk.model_dump(mode="json"),),
            "diff_hunk_inventory_sha256": inventory_digest,
        }
    )

    assert context.diff_hunks == (hunk,)
    with pytest.raises(ValidationError, match="inventory digest does not match"):
        ReviewContext.model_validate(
            {
                **context.model_dump(),
                "diff_hunk_inventory_sha256": "d" * 64,
            }
        )


def test_impact_coverage_rejects_duplicate_candidates_and_invalid_dispositions():
    candidate = {
        "candidate_id": "src.module:caller",
        "relation": "caller",
        "locator": "src/module.py",
        "disposition": "reviewed",
        "rationale": "The caller's behavior was checked against the changed contract.",
        "evidence_refs": ["E1"],
    }
    ledger = {
        "head_sha": "1" * 40,
        "changed_paths_sha256": "2" * 64,
        "discovery_sha256": "3" * 64,
        "discovery_method": "pinned_git_symbol_scan",
        "discovery_version": 1,
        "discovery_state": "ready",
        "limits": [
            {
                "name": "candidate_limit",
                "value": 50,
                "reached": False,
                "uninspected": False,
            }
        ],
        "candidates": [candidate, candidate],
        "omissions": [],
    }

    with pytest.raises(ValidationError, match="candidate IDs must be unique"):
        ImpactCoverageLedger.model_validate(ledger)

    invalid_disposition = {
        **ledger,
        "candidates": [{**candidate, "disposition": "approved"}],
    }
    with pytest.raises(ValidationError):
        ImpactCoverageLedger.model_validate(invalid_disposition)

    incomplete_limit = {
        **ledger,
        "candidates": [candidate],
        "limits": [
            {
                "name": "candidate_limit",
                "value": 50,
                "reached": False,
                "uninspected": True,
            }
        ],
    }
    with pytest.raises(ValidationError, match="uninspected impact limit must have been reached"):
        ImpactCoverageLedger.model_validate(incomplete_limit)


def test_final_report_accepts_absent_legacy_impact_coverage():
    report = FinalReviewReport.model_validate(
        {
            "summary": "Legacy report.",
            "profiles_reviewed": [ReviewAgent.CODE_REVIEWER.value],
            "finding_candidates": [],
            "coverage_gaps": [],
            "unverified": [],
            "criterion_coverage": [],
            "verification_records": [],
            "coverage_ledger": None,
            "candidate_decisions": [],
        }
    )

    assert report.impact_coverage is None


@pytest.mark.parametrize(
    ("routes", "execution_groups"),
    [
        (
            [build_review_route(ReviewAgent.CODE_EXPLORER)],
            [[], [ReviewAgent.CODE_EXPLORER]],
        ),
        (
            [build_review_route(ReviewAgent.CODE_EXPLORER)] * 2,
            [[ReviewAgent.CODE_EXPLORER, ReviewAgent.CODE_EXPLORER]],
        ),
    ],
)
def test_review_route_selection_rejects_empty_groups_and_duplicate_profiles(
    routes, execution_groups
):
    with pytest.raises(ValidationError, match="execution_groups must match route profiles"):
        ReviewRouteSelection(routes=routes, execution_groups=execution_groups)


@pytest.mark.parametrize(
    ("step", "model", "reasoning"),
    [
        (OrchestrationStep.TRIAGE, "gpt-6-luna", "max"),
        (OrchestrationStep.PRIMARY_REVIEW, "gpt-6-sol", "medium"),
        (OrchestrationStep.DEEP_REVIEW, "gpt-6-sol", "high"),
        (OrchestrationStep.SYNTHESIS, "gpt-6-sol", "medium"),
    ],
)
def test_model_policy_assigns_requested_models_and_reasoning(step, model, reasoning):
    policy = MODEL_POLICIES[step]
    assert policy.model.value == model
    assert policy.reasoning == reasoning
    assert set(policy.model_dump(mode="json")) == {
        "mode",
        "provider",
        "model",
        "reasoning",
        "reasoning_capabilities_verified",
    }
    assert set(MODEL_POLICIES) == {
        OrchestrationStep.TRIAGE,
        OrchestrationStep.PRIMARY_REVIEW,
        OrchestrationStep.DEEP_REVIEW,
        OrchestrationStep.SYNTHESIS,
    }


def test_model_policy_rejects_orchestrator_speed_override():
    assert all("speed" not in policy.model_dump() for policy in MODEL_POLICIES.values())

    with pytest.raises(ValidationError):
        ModelPolicy(model=OrchestrationModel.SOL, reasoning="medium", speed=1.5)


def test_orchestration_session_rejects_disabled_ownership_flags():
    with pytest.raises(ValidationError):
        QaOrchestrationSession(
            run_id="qar-0123456789abcdef0123456789abcdef",
            task_type="ordinary_review",
            status=OrchestrationStatus.ACTIVE,
            current_step=OrchestrationStep.TRIAGE,
            next_action="Host performs triage.",
            read_only=False,
        )

    with pytest.raises(ValidationError):
        QaOrchestrationSession(
            run_id="qar-0123456789abcdef0123456789abcdef",
            task_type="ordinary_review",
            status=OrchestrationStatus.ACTIVE,
            current_step=OrchestrationStep.TRIAGE,
            next_action="Host performs triage.",
            host_owns_decisions=False,
            expires_at=datetime.now(UTC) + timedelta(minutes=5),
        )


def test_legacy_completed_session_without_completion_mode_loads_as_legacy(
    bound_review_context,
):
    legacy_context = ReviewContext.model_validate(
        {
            **bound_review_context.model_dump(),
            "input_manifest": None,
            "neighbor_discovery_sha256": None,
            "neighbor_discovery_candidate_count": None,
            "neighbor_discovery_candidate_set_sha256": None,
        }
    )
    session = QaOrchestrationSession(
        run_id="qar-0123456789abcdef0123456789abcdef",
        task_type="ordinary_review",
        status=OrchestrationStatus.COMPLETED,
        current_step=OrchestrationStep.AWAITING_HOST_OUTCOME,
        review_context=legacy_context,
        completed_profiles=[ReviewAgent.CODE_REVIEWER],
        final_review_report=FinalReviewReport(
            summary="Review completed.",
            profiles_reviewed=(ReviewAgent.CODE_REVIEWER,),
        ),
        next_action="Review completed.",
        expires_at=datetime.now(UTC) + timedelta(minutes=5),
    )
    payload = session.model_dump(mode="json")
    payload.pop("review_completion_mode", None)

    loaded = QaOrchestrationSession.model_validate(payload)

    assert loaded.review_completion_mode == "legacy"
    assert loaded.status is OrchestrationStatus.COMPLETED


def test_persisted_strict_completed_session_rejects_missing_scope_coverage(
    bound_review_context,
):
    with pytest.raises(ValidationError, match="strict completed session"):
        QaOrchestrationSession(
            run_id="qar-0123456789abcdef0123456789abcdef",
            task_type="ordinary_review",
            status=OrchestrationStatus.COMPLETED,
            current_step=OrchestrationStep.AWAITING_HOST_OUTCOME,
            review_context=bound_review_context,
            review_completion_mode="strict",
            completed_profiles=[ReviewAgent.CODE_REVIEWER],
            final_review_report=FinalReviewReport(
                summary="Review completed.",
                profiles_reviewed=(ReviewAgent.CODE_REVIEWER,),
            ),
            next_action="Review completed.",
            expires_at=datetime.now(UTC) + timedelta(minutes=5),
        )

def test_review_context_requires_full_unique_review_identity(bound_review_context):
    assert bound_review_context.base_sha == "0" * 40
    assert bound_review_context.diff_sha256 == "2" * 64

    with pytest.raises(ValidationError, match="different revisions"):
        ReviewContext(**{**bound_review_context.model_dump(), "head_sha": "0" * 40})

    with pytest.raises(ValidationError):
        ReviewContext(**{**bound_review_context.model_dump(), "diff_sha256": "not-a-digest"})

    with pytest.raises(ValidationError, match="evidence_refs must be unique"):
        ReviewContext(**{**bound_review_context.model_dump(), "evidence_refs": ("E1", "E1")})


def test_review_context_bounds_paths_and_rejects_repository_credentials(bound_review_context):
    context = bound_review_context.model_dump()
    assert ReviewContext(**{**context, "changed_files": ("x" * 1024,)}).changed_files == (
        "x" * 1024,
    )

    with pytest.raises(ValidationError):
        ReviewContext(**{**context, "changed_files": ("x" * 1025,)})

    for repository in (
        "https://user:secret@github.com/group/repo",
        "https://github.com/group/repo?token=secret",
        "github.com/group/repo?token=secret",
        "token@github.com/group/repo",
        "git@github.com:group/repo.git",
    ):
        with pytest.raises(ValidationError, match="credentials|parameters|clone|query or fragment"):
            ReviewContext(**{**context, "repository": repository})


def test_review_input_manifest_rejects_duplicate_source_ids_and_criteria_refs(
    bound_review_context,
):
    def source(source_id, locator, criterion_refs):
        return {
            "source_id": source_id,
            "kind": "jira_issue",
            "locator": locator,
            "required": True,
            "retrieval_status": "complete",
            "pagination_status": "complete",
            "extraction_status": "complete",
            "criterion_refs": criterion_refs,
        }

    context_payload = bound_review_context.model_dump()
    context_payload["input_manifest"] = {
        "sources": (
            source("jira-1", "jira:QA-1", ("REQ-1",)),
            source("spec-1", "docs/review-spec.md", ("REQ-2",)),
        )
    }
    valid_context = ReviewContext.model_validate(context_payload)

    duplicate_source_payload = valid_context.model_dump()
    duplicate_source_payload["input_manifest"] = {
        "sources": (
            source("jira-1", "jira:QA-1", ("REQ-1",)),
            source("jira-1", "docs/review-spec.md", ("REQ-2",)),
        )
    }
    with pytest.raises(ValidationError, match="source IDs must be unique"):
        ReviewContext.model_validate(duplicate_source_payload)

    duplicate_criterion_payload = valid_context.model_dump()
    duplicate_criterion_payload["input_manifest"] = {
        "sources": (
            source("jira-1", "jira:QA-1", ("REQ-1",)),
            source("spec-1", "docs/review-spec.md", ("REQ-1",)),
        )
    }
    with pytest.raises(ValidationError, match="criterion refs must be unique"):
        ReviewContext.model_validate(duplicate_criterion_payload)


def test_legacy_review_context_without_input_manifest_round_trips(bound_review_context):
    legacy_context = ReviewContext.model_validate(
        {
            **bound_review_context.model_dump(),
            "input_manifest": None,
            "neighbor_discovery_sha256": None,
            "neighbor_discovery_candidate_count": None,
            "neighbor_discovery_candidate_set_sha256": None,
        }
    )
    legacy_json = legacy_context.model_dump_json()

    context = ReviewContext.model_validate_json(legacy_json)

    assert context.input_manifest is None
    assert "input_manifest" not in context.model_dump()
    assert context.neighbor_discovery_sha256 is None
    assert "neighbor_discovery_sha256" not in context.model_dump()

    bound = ReviewContext.model_validate(
        {
            **bound_review_context.model_dump(),
            "neighbor_discovery_sha256": "A" * 64,
        }
    )
    assert bound.neighbor_discovery_sha256 == "a" * 64


def test_review_input_manifest_preserves_unresolved_criterion_refs(bound_review_context):
    payload = bound_review_context.model_dump()
    payload["input_manifest"] = {
        "sources": (
            {
                "source_id": "jira-1",
                "kind": "jira_issue",
                "locator": "jira:QA-1",
                "required": True,
                "retrieval_status": "complete",
                "pagination_status": "not_applicable",
                "extraction_status": "complete",
                "criterion_refs": ("REQ-MISSING",),
            },
        )
    }

    context = ReviewContext.model_validate(payload)

    assert context.input_manifest.sources[0].criterion_refs == ("REQ-MISSING",)


def test_review_input_manifest_rejects_registered_noncriterion_refs(bound_review_context):
    payload = bound_review_context.model_dump()
    payload["evidence_refs"] = ("REQ-1",)
    payload["evidence_registry"] = (
        {
            "evidence_ref": "REQ-1",
            "kind": "runtime",
            "sha256": "a" * 64,
            "locator": "testrail:run-1",
        },
    )
    payload["input_manifest"] = {
        "sources": (
            {
                "source_id": "jira-1",
                "kind": "jira_issue",
                "locator": "jira:QA-1",
                "required": True,
                "retrieval_status": "complete",
                "pagination_status": "not_applicable",
                "extraction_status": "complete",
                "criterion_refs": ("REQ-1",),
            },
        )
    }

    with pytest.raises(ValidationError, match="requirement or claim"):
        ReviewContext.model_validate(payload)


def test_review_input_manifest_accepts_registered_requirement_and_claim_refs(
    bound_review_context,
):
    payload = bound_review_context.model_dump()
    payload["evidence_refs"] = ("REQ-1", "CLAIM-1")
    payload["evidence_registry"] = (
        {
            "evidence_ref": "REQ-1",
            "kind": "requirement",
            "sha256": "a" * 64,
            "locator": "jira:QA-1",
        },
        {
            "evidence_ref": "CLAIM-1",
            "kind": "claim",
            "sha256": "b" * 64,
            "locator": "gitlab:MR-2",
        },
    )
    payload["input_manifest"] = {
        "sources": (
            {
                "source_id": "jira-1",
                "kind": "jira_issue",
                "locator": "jira:QA-1",
                "required": True,
                "retrieval_status": "complete",
                "pagination_status": "complete",
                "extraction_status": "complete",
                "criterion_refs": ("REQ-1",),
            },
            {
                "source_id": "mr-2",
                "kind": "merge_request",
                "locator": "gitlab:MR-2",
                "required": True,
                "retrieval_status": "complete",
                "pagination_status": "not_applicable",
                "extraction_status": "complete",
                "criterion_refs": ("CLAIM-1",),
            },
        )
    }

    context = ReviewContext.model_validate(payload)

    assert tuple(
        ref
        for source in context.input_manifest.sources
        for ref in source.criterion_refs
    ) == ("REQ-1", "CLAIM-1")


@pytest.mark.parametrize(
    "locator",
    (
        "https://user:secret@example.com/issue/QA-1",
        "https://example.com/issue/QA-1?token=secret",
        "docs/../private-spec.md",
        "docs/spec with spaces.md",
    ),
)
def test_review_input_source_rejects_unstable_or_sensitive_locators(locator):
    with pytest.raises(ValidationError):
        ReviewInputSource(
            source_id="jira-1",
            kind="jira_issue",
            locator=locator,
            required=True,
        )


def test_review_input_source_defaults_missing_statuses_to_unknown():
    source = ReviewInputSource(
        source_id="jira-1",
        kind="jira_issue",
        locator="jira:QA-1",
        required=True,
    )

    assert source.retrieval_status == "unknown"
    assert source.pagination_status == "unknown"
    assert source.extraction_status == "unknown"


def test_finding_locations_require_a_valid_line_range():
    assert ReviewFindingLocation(path="src/service.py", start_line=10, end_line=12)

    with pytest.raises(ValidationError, match="end_line must be greater than or equal"):
        ReviewFindingLocation(path="src/service.py", start_line=12, end_line=10)

    with pytest.raises(ValidationError):
        ReviewFindingLocation(path="src/service.py", start_line=0)


def test_profile_execution_metrics_require_an_ordered_aware_interval():
    started_at = datetime.now(UTC)
    metrics = ProfileExecutionMetrics(
        started_at=started_at,
        completed_at=started_at + timedelta(seconds=2),
        input_tokens=120,
        output_tokens=45,
    )
    assert metrics.output_tokens == 45

    with pytest.raises(ValidationError, match="completed_at must be at or after"):
        ProfileExecutionMetrics(
            started_at=started_at,
            completed_at=started_at - timedelta(seconds=1),
        )

    with pytest.raises(ValidationError):
        ProfileExecutionMetrics(
            started_at=started_at,
            completed_at=started_at + timedelta(seconds=1),
            input_tokens=-1,
        )


def test_profile_and_final_reports_have_serialized_size_limits():
    large_notes = tuple(ReviewNote(text="x" * 2000) for _ in range(41))

    with pytest.raises(ValidationError, match="profile report exceeds"):
        ProfileReviewReport(
            profile=ReviewAgent.CODE_REVIEWER,
            scope="Reviewed the change.",
            coverage_gaps=large_notes,
        )

    with pytest.raises(ValidationError, match="final review report exceeds"):
        FinalReviewReport(
            summary="Synthesis completed.",
            profiles_reviewed=(ReviewAgent.CODE_REVIEWER,),
            unverified=large_notes,
        )


@pytest.mark.parametrize("report_type", [ProfileReviewReport, FinalReviewReport])
def test_legacy_reports_near_the_old_size_limit_round_trip(report_type):
    is_final = report_type is FinalReviewReport
    candidates = []
    for index in range(100):
        candidate = {
            "candidate_id": f"F-{index:02d}",
            "title": "T" * 128,
            "description": "D",
            "confidence": "high",
            "verification_gap": "V" * 128,
            "evidence_refs": ["E1"],
            "source_locations": [],
            "affected_area": None,
            "impact": None,
            "failure_condition": None,
            "flip_condition": None,
        }
        if is_final:
            candidate.update(
                severity=None,
                source_profiles=[ReviewAgent.CODE_REVIEWER.value],
                source_candidates=[],
            )
        candidates.append(candidate)

    if is_final:
        payload = {
            "summary": "S",
            "review_rules_sha256": None,
            "profiles_reviewed": [ReviewAgent.CODE_REVIEWER.value],
            "finding_candidates": candidates,
            "coverage_gaps": [],
            "unverified": [],
            "candidate_decisions": [],
        }
    else:
        payload = {
            "profile": ReviewAgent.CODE_REVIEWER.value,
            "context_selection": None,
            "review_rules_sha256": None,
            "scope": "S",
            "finding_candidates": candidates,
            "coverage_gaps": [],
            "unverified": [],
            "evidence_map": [],
            "execution_metrics": None,
        }

    base_json = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    old_base_size = len(base_json.encode("utf-8"))
    expanded_base_size = len(
        report_type.model_validate_json(base_json).model_dump_json().encode("utf-8")
    )
    description_size = 1 + (64 * 1024 - old_base_size - 1) // len(candidates)
    for candidate in candidates:
        candidate["description"] = "D" * description_size

    legacy_json = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    legacy_size = len(legacy_json.encode("utf-8"))
    report = report_type.model_validate_json(legacy_json)
    current_json = report.model_dump_json()
    current_size = len(current_json.encode("utf-8"))

    assert legacy_size <= 64 * 1024 < current_size <= 80 * 1024
    assert report_type.model_validate_json(current_json) == report
    assert expanded_base_size > old_base_size


def test_session_has_an_aggregate_review_artifact_size_limit(bound_review_context):
    notes = tuple(ReviewNote(text="x" * 2000) for _ in range(26))
    profiles = tuple(ReviewAgent)
    reports = [
        ProfileReviewReport(profile=profile, scope="Reviewed the change.", coverage_gaps=notes)
        for profile in profiles
    ]

    with pytest.raises(ValidationError, match="session review artifacts exceed"):
        QaOrchestrationSession(
            run_id="qar-0123456789abcdef0123456789abcdef",
            task_type="ordinary_review",
            status=OrchestrationStatus.ACTIVE,
            current_step=OrchestrationStep.PRIMARY_REVIEW,
            completed_profiles=list(profiles),
            profile_reports=reports,
            review_context=bound_review_context,
            next_action="Host continues review.",
            expires_at=datetime.now(UTC) + timedelta(minutes=5),
        )


def test_enabled_risk_signals_require_rationale_and_evidence_refs():
    with pytest.raises(ValidationError, match="enabled risk signals require"):
        DeepReviewSignals(cross_system_scope=True)

    signals = DeepReviewSignals(
        cross_system_scope=True,
        rationale="The changed contract crosses two services.",
        evidence_refs=("E1",),
    )
    assert signals.cross_system_scope is True

    with pytest.raises(ValidationError, match="risk signal evidence_refs must be unique"):
        DeepReviewSignals(
            evidence_conflict=True,
            rationale="Two authoritative contracts disagree.",
            evidence_refs=("E1", "E1"),
        )


def test_profile_reports_keep_explorer_and_test_analyzer_within_their_scope(
    review_finding_candidate,
):
    for profile in (ReviewAgent.CODE_EXPLORER, ReviewAgent.PR_TEST_ANALYZER):
        with pytest.raises(ValidationError, match="cannot report implementation findings"):
            ProfileReviewReport(
                profile=profile,
                scope="Changed code and tests.",
                finding_candidates=(review_finding_candidate,),
            )


def test_advance_request_requires_profile_reports_in_completion_order(
    make_profile_review_report,
):
    with pytest.raises(ValidationError, match="profile_reports must match completed profiles"):
        AdvanceQaOrchestrationRequest(
            run_id="qar-0123456789abcdef0123456789abcdef",
            completed_step=OrchestrationStep.PRIMARY_REVIEW,
            status="completed",
            completed_profiles=[ReviewAgent.CODE_REVIEWER, ReviewAgent.PR_TEST_ANALYZER],
            profile_reports=[
                make_profile_review_report(ReviewAgent.PR_TEST_ANALYZER),
                make_profile_review_report(ReviewAgent.CODE_REVIEWER),
            ],
        )


def test_advance_request_rejects_free_form_fields():
    with pytest.raises(ValidationError):
        AdvanceQaOrchestrationRequest(
            run_id="qar-0123456789abcdef0123456789abcdef",
            completed_step=OrchestrationStep.TRIAGE,
            status="completed",
            selected_profile=ReviewAgent.CODE_REVIEWER,
            evidence="raw source text",
        )


@pytest.mark.parametrize(
    "step",
    ("luna_triage", "terra_primary_review", "sol_deep_review", "terra_synthesis"),
)
def test_transition_identifiers_are_model_neutral(step):
    with pytest.raises(ValueError):
        OrchestrationStep(step)


def test_legacy_manual_escalation_fields_are_rejected():
    with pytest.raises(ValidationError):
        AdvanceQaOrchestrationRequest(
            run_id="qar-0123456789abcdef0123456789abcdef",
            completed_step=OrchestrationStep.PRIMARY_REVIEW,
            status="completed",
            completed_profile=ReviewAgent.CODE_REVIEWER,
            needs_deep_analysis=True,
            reason_code="high_risk_domain",
        )


def test_risk_signals_are_final_primary_review_only():
    signals = DeepReviewSignals(
        high_risk_domain=True,
        evidence_uncertain=True,
        rationale="A permission conclusion remains uncertain.",
        evidence_refs=("E1",),
    )

    with pytest.raises(ValidationError, match="final primary-review profile"):
        AdvanceQaOrchestrationRequest(
            run_id="qar-0123456789abcdef0123456789abcdef",
            completed_step=OrchestrationStep.TRIAGE,
            status="completed",
            selected_profile=ReviewAgent.CODE_REVIEWER,
            risk_signals=signals,
        )

    with pytest.raises(ValidationError, match="final primary-review profile"):
        AdvanceQaOrchestrationRequest(
            run_id="qar-0123456789abcdef0123456789abcdef",
            completed_step=OrchestrationStep.SYNTHESIS,
            status="completed",
            risk_signals=signals,
        )


def test_risk_signals_require_a_completed_profile():
    with pytest.raises(ValidationError, match="completed_profile"):
        AdvanceQaOrchestrationRequest(
            run_id="qar-0123456789abcdef0123456789abcdef",
            completed_step=OrchestrationStep.PRIMARY_REVIEW,
            status="completed",
            risk_signals=DeepReviewSignals(
                cross_system_scope=True,
                rationale="The changed behavior crosses services.",
                evidence_refs=("E1",),
            ),
        )


def test_skip_synthesis_is_only_valid_after_a_completed_primary_review():
    with pytest.raises(ValidationError, match="skip_synthesis"):
        AdvanceQaOrchestrationRequest(
            run_id="qar-0123456789abcdef0123456789abcdef",
            completed_step=OrchestrationStep.TRIAGE,
            status="completed",
            selected_profile=ReviewAgent.CODE_REVIEWER,
            skip_synthesis=True,
        )


def test_completed_primary_review_requires_completed_profile():
    with pytest.raises(ValidationError, match="completed_profile"):
        AdvanceQaOrchestrationRequest(
            run_id="qar-0123456789abcdef0123456789abcdef",
            completed_step=OrchestrationStep.PRIMARY_REVIEW,
            status="completed",
        )


def test_completed_primary_review_accepts_a_group_of_completed_profiles():
    request = AdvanceQaOrchestrationRequest(
        run_id="qar-0123456789abcdef0123456789abcdef",
        completed_step=OrchestrationStep.PRIMARY_REVIEW,
        status="completed",
        completed_profiles=[ReviewAgent.CODE_REVIEWER, ReviewAgent.PR_TEST_ANALYZER],
        risk_signals=DeepReviewSignals(),
    )

    assert request.completed_profiles == [
        ReviewAgent.CODE_REVIEWER,
        ReviewAgent.PR_TEST_ANALYZER,
    ]


def test_completed_profiles_must_be_nonempty_and_exclusive_with_legacy_field():
    base = {
        "run_id": "qar-0123456789abcdef0123456789abcdef",
        "completed_step": OrchestrationStep.PRIMARY_REVIEW,
        "status": "completed",
    }

    with pytest.raises(ValidationError, match="at least one"):
        AdvanceQaOrchestrationRequest(**base, completed_profiles=[])

    with pytest.raises(ValidationError, match="mutually exclusive"):
        AdvanceQaOrchestrationRequest(
            **base,
            completed_profile=ReviewAgent.CODE_REVIEWER,
            completed_profiles=[ReviewAgent.CODE_REVIEWER],
        )


def test_completed_profile_is_rejected_outside_primary_review():
    with pytest.raises(ValidationError, match="only be supplied"):
        AdvanceQaOrchestrationRequest(
            run_id="qar-0123456789abcdef0123456789abcdef",
            completed_step=OrchestrationStep.TRIAGE,
            status="completed",
            selected_profile=ReviewAgent.CODE_REVIEWER,
            completed_profile=ReviewAgent.CODE_REVIEWER,
        )


def test_partial_primary_review_rejects_ignored_completed_profile():
    with pytest.raises(ValidationError, match="completed_profile"):
        AdvanceQaOrchestrationRequest(
            run_id="qar-0123456789abcdef0123456789abcdef",
            completed_step=OrchestrationStep.PRIMARY_REVIEW,
            status="partial",
            completed_profile=ReviewAgent.CODE_REVIEWER,
        )


def test_completed_triage_requires_exactly_one_selection():
    with pytest.raises(ValidationError, match="exactly one"):
        AdvanceQaOrchestrationRequest(
            run_id="qar-0123456789abcdef0123456789abcdef",
            completed_step=OrchestrationStep.TRIAGE,
            status="completed",
        )

    with pytest.raises(ValidationError, match="exactly one"):
        AdvanceQaOrchestrationRequest(
            run_id="qar-0123456789abcdef0123456789abcdef",
            completed_step=OrchestrationStep.TRIAGE,
            status="completed",
            selected_profile=ReviewAgent.CODE_REVIEWER,
            selected_bundle=ReviewBundle.ORDINARY_MR,
        )


def test_completed_triage_accepts_one_fixed_bundle():
    request = AdvanceQaOrchestrationRequest(
        run_id="qar-0123456789abcdef0123456789abcdef",
        completed_step=OrchestrationStep.TRIAGE,
        status="completed",
        selected_bundle=ReviewBundle.ORDINARY_MR,
    )

    assert request.selected_bundle is ReviewBundle.ORDINARY_MR


def test_selection_is_rejected_after_triage():
    with pytest.raises(ValidationError, match="only be supplied after triage"):
        AdvanceQaOrchestrationRequest(
            run_id="qar-0123456789abcdef0123456789abcdef",
            completed_step=OrchestrationStep.PRIMARY_REVIEW,
            status="completed",
            completed_profile=ReviewAgent.CODE_REVIEWER,
            selected_bundle=ReviewBundle.ORDINARY_MR,
        )


def test_arbitrary_profile_order_is_rejected():
    with pytest.raises(ValidationError):
        AdvanceQaOrchestrationRequest(
            run_id="qar-0123456789abcdef0123456789abcdef",
            completed_step=OrchestrationStep.TRIAGE,
            status="completed",
            selected_profile=ReviewAgent.CODE_REVIEWER,
            review_profiles=[ReviewAgent.CODE_REVIEWER, ReviewAgent.CODE_EXPLORER],
        )
