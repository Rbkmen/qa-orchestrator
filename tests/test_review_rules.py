import pytest
from pydantic import ValidationError

from qa_orchestrator.contracts import ReviewContext


def rules():
    return {
        "schema_version": 1,
        "rules": [
            {
                "rule_id": "tests-assert",
                "kind": "guard",
                "paths": ["tests/**"],
                "condition": "The assertion belongs to test code.",
                "text": "Do not flag assert as a production security boundary.",
                "rationale": "Tests use assertion failures as their expected signal.",
            },
            {
                "rule_id": "auth",
                "kind": "raise",
                "paths": ["src/auth/**"],
                "condition": "A changed path handles authentication.",
                "text": "Trace failed-auth handling.",
                "rationale": "Fail-open permits access.",
            },
        ],
    }


def test_rules_are_bound_to_context_scoped_and_never_suppress_candidates(bound_review_context):
    from qa_orchestrator.review_rules import ReviewRuleset

    context = ReviewContext.model_validate(
        {**bound_review_context.model_dump(), "review_rules": rules()}
    )
    selected = context.review_rules.for_paths(("tests/test_api.py", "src/data.py"))
    assert [r.rule_id for r in selected.rules] == ["tests-assert"]
    assert "assert" in selected.render()
    assert "condition" in selected.render().lower()
    assert ReviewRuleset.model_validate(rules()).digest == context.review_rules.digest
    reversed_rules = rules()
    reversed_rules["rules"].reverse()
    assert ReviewRuleset.model_validate(reversed_rules).digest == context.review_rules.digest


@pytest.mark.parametrize(
    "change",
    [
        {"condition": " "},
        {"rationale": ""},
        {"paths": ["../**"]},
        {"paths": ["/etc/**"]},
        {"paths": []},
    ],
)
def test_invalid_or_unbounded_guard_is_rejected(change):
    from qa_orchestrator.review_rules import ReviewRuleset

    data = rules()
    data["rules"][0].update(change)
    with pytest.raises(ValidationError):
        ReviewRuleset.model_validate(data)


async def test_host_passes_rules_and_binds_report_digest(tmp_path, bound_review_context):
    from qa_orchestrator.contracts import ProfileReviewReport
    from qa_orchestrator.host import HostReviewAdapter
    from qa_orchestrator.orchestration import AdvanceQaOrchestrationRequest, DeepReviewSignals
    from qa_orchestrator.service import OrchestratorService

    context = ReviewContext.model_validate(
        {**bound_review_context.model_dump(), "review_rules": rules()}
    )
    service = OrchestratorService.from_settings(data_dir=tmp_path)
    run = service.start_qa_orchestration("ordinary_review", context)
    service.advance_qa_orchestration(
        AdvanceQaOrchestrationRequest(
            run_id=run.run_id,
            completed_step="triage",
            status="completed",
            selected_profile="python_reviewer",
        )
    )

    async def execute(job):
        assert job.review_rules is not None
        return ProfileReviewReport(profile=job.profile, scope="Source inspected")

    result = await HostReviewAdapter(service).run_primary_review(
        run.run_id, execute, lambda _: DeepReviewSignals()
    )
    assert not result.failures
    assert result.session.profile_reports[0].review_rules_sha256 == context.review_rules.digest
    from qa_orchestrator.contracts import FinalReviewReport
    from qa_orchestrator.orchestration import OrchestrationError

    service.advance_qa_orchestration(
        AdvanceQaOrchestrationRequest(
            run_id=run.run_id, completed_step="synthesis", status="partial"
        )
    )
    final = FinalReviewReport(
        summary="Partial source review", profiles_reviewed=("python_reviewer",)
    )
    with pytest.raises(OrchestrationError, match="rules digest"):
        service.finish_qa_orchestration(
            run_id=run.run_id, outcome="partial", final_review_report=final
        )
    finished = service.finish_qa_orchestration(
        run_id=run.run_id,
        outcome="partial",
        final_review_report={
            **final.model_dump(),
            "review_rules_sha256": context.review_rules.digest,
        },
    )
    assert finished.final_review_report.review_rules_sha256 == context.review_rules.digest


def test_rules_cli_binds_explicit_file_to_context(tmp_path, bound_review_context):
    import json

    from qa_orchestrator.cli import main

    context, rules_file, output = (
        tmp_path / name for name in ("context.json", "rules.json", "bound.json")
    )
    context.write_text(bound_review_context.model_dump_json())
    rules_file.write_text(json.dumps(rules()))
    assert (
        main(
            [
                "rules",
                "--context",
                str(context),
                "--rules",
                str(rules_file),
                "--output",
                str(output),
            ]
        )
        == 0
    )
    bound = ReviewContext.model_validate_json(output.read_bytes())
    assert len(bound.review_rules.rules) == 2


def test_report_rule_digest_is_case_insensitive():
    from qa_orchestrator.contracts import FinalReviewReport, ProfileReviewReport

    profile = ProfileReviewReport(
        profile="python_reviewer", scope="Inspected", review_rules_sha256="A" * 64
    )
    final = FinalReviewReport(
        summary="Inspected", profiles_reviewed=("python_reviewer",), review_rules_sha256="A" * 64
    )
    assert profile.review_rules_sha256 == final.review_rules_sha256 == "a" * 64


def test_server_rejects_reports_from_a_different_ruleset(bound_review_context):
    from qa_orchestrator.contracts import ProfileReviewReport
    from qa_orchestrator.orchestration import OrchestrationError, QaOrchestrator

    context = ReviewContext.model_validate(
        {**bound_review_context.model_dump(), "review_rules": rules()}
    )
    service = QaOrchestrator(ttl_seconds=1800, max_sessions=10)
    run = service.start("ordinary_review", context)
    service.advance(
        run_id=run.run_id,
        completed_step="triage",
        status="completed",
        selected_profile="python_reviewer",
    )
    with pytest.raises(OrchestrationError, match="rules digest"):
        service.advance(
            run_id=run.run_id,
            completed_step="primary_review",
            status="completed",
            completed_profile="python_reviewer",
            risk_signals={},
            profile_reports=[ProfileReviewReport(profile="python_reviewer", scope="Old rules")],
        )
