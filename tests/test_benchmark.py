import hashlib
import json
import subprocess
from copy import deepcopy

import pytest
from pydantic import ValidationError

from qa_orchestrator.benchmark import (
    BenchmarkDataset,
    BenchmarkFailure,
    BenchmarkQualityGatePolicy,
    BenchmarkResults,
    benchmark_main,
    check_diff_budget,
    compare_speed,
    evaluate_quality_gate,
    evaluate_quality_policy,
    import_reviewbench,
    import_reviewbench_case_set,
    import_reviewbench_test_set,
    score_benchmark,
)


@pytest.fixture
def benchmark_inputs():
    dataset = {
        "kind": "synthetic",
        "source": "unit-test",
        "cases": [
            {
                "case_id": "case-1",
                "repository": "github.com/example/repo",
                "base_sha": "0" * 40,
                "head_sha": "1" * 40,
                "diff_sha256": "2" * 64,
                "gold_complete": True,
                "gold": [
                    {
                        "gold_id": f"G-{i}",
                        "description": "A verified defect",
                        "severity": severity,
                        "category": "correctness" if i == 1 else "security",
                        "path": "src/api.py",
                        "start_line": i,
                        "end_line": i,
                        "scope": "introduced-by-pr" if i == 1 else "exacerbated-by-pr",
                        "difficulty": "easy" if i == 1 else "hard",
                        "context_required": "diff-only" if i == 1 else "broader-project-context",
                    }
                    for i, severity in ((1, "high"), (2, "medium"))
                ],
            }
        ],
    }
    run = {
        "run_id": "run-1",
        "case_id": "case-1",
        "configuration_id": "baseline",
        "model": "host-model",
        "reasoning": "medium",
        "checklist_sha256": "3" * 64,
        "host_instructions_sha256": "4" * 64,
        "base_sha": "0" * 40,
        "head_sha": "1" * 40,
        "diff_sha256": "2" * 64,
        "elapsed_ms": 500,
        "evidence_packet_sha256": "6" * 64,
        "adjudicator": "fixture-reviewer",
        "candidates": [
            {
                "candidate_id": f"F-0{i}",
                "title": "Possible defect",
                "description": "A branch has a wrong result",
                "confidence": confidence,
                "verification_gap": "Runtime unavailable",
                "evidence_refs": ["E1"],
            }
            for i, confidence in ((1, "high"), (2, "low"), (3, "medium"))
        ],
        "adjudications": [
            {
                "candidate_id": "F-01",
                "verdict": "matched",
                "gold_id": "G-1",
                "rationale": "Same failure",
                "evidence_valid": True,
                "location_valid": True,
            },
            {
                "candidate_id": "F-02",
                "verdict": "duplicate",
                "gold_id": "G-1",
                "rationale": "Repeats F-01",
            },
            {
                "candidate_id": "F-03",
                "verdict": "false_positive",
                "rationale": "Contract permits it",
            },
        ],
    }
    return dataset, {"runs": [run]}


def _score_for_quality_policy(benchmark_inputs):
    dataset_data, results_data = deepcopy(benchmark_inputs)
    dataset_data["kind"] = "curated"
    return score_benchmark(
        BenchmarkDataset.model_validate(dataset_data),
        BenchmarkResults.model_validate(results_data),
    )


def _quality_policy(score, **overrides):
    policy = {
        "policy_id": "test-quality-policy",
        "dataset_kind": score["dataset_kind"],
        "dataset_sha256": score["dataset_sha256"],
        "min_cases": 1,
        "min_gold_findings": 1,
        "min_precision": 0.3,
        "min_recall": 0.5,
        "max_missed_high_impact": 0,
    }
    return BenchmarkQualityGatePolicy.model_validate({**policy, **overrides})


def _quality_result(score, policy):
    return evaluate_quality_policy(score, policy, configuration_id="baseline")


def test_named_quality_policy_rejects_synthetic_dataset(benchmark_inputs):
    score = _score_for_quality_policy(benchmark_inputs)
    synthetic_policy = {
        **_quality_policy(score).model_dump(mode="python"),
        "dataset_kind": "synthetic",
    }

    with pytest.raises(ValidationError, match="dataset_kind"):
        BenchmarkQualityGatePolicy.model_validate(synthetic_policy)


def _speed_fixture(benchmark_inputs, *, include_schedule=True, schedule_order=None):
    dataset_data, result_data = deepcopy(benchmark_inputs)
    dataset_data["kind"] = "curated"
    dataset = BenchmarkDataset.model_validate(dataset_data)
    runs = []
    run_order = []
    queue_waits = {
        "baseline": (10, 20, 30),
        "candidate": (40, None, 60),
    }
    elapsed = {"baseline": (600, 700, 800), "candidate": (400, 500, 600)}
    for attempt in range(1, 4):
        for configuration in ("baseline", "candidate"):
            run_id = f"{configuration}-{attempt}"
            run = deepcopy(result_data["runs"][0])
            run.update(
                run_id=run_id,
                configuration_id=configuration,
                attempt=attempt,
                elapsed_ms=elapsed[configuration][attempt - 1],
                queue_wait_ms=queue_waits[configuration][attempt - 1],
            )
            runs.append(run)
            run_order.append(run_id)
    result_payload = {"schema_version": 3, "runs": runs}
    if include_schedule:
        result_payload["schedule"] = {
            "schedule_id": "schedule-1",
            "strategy": "interleaved",
            "run_order": schedule_order or run_order,
        }
    results = BenchmarkResults.model_validate(result_payload)
    return dataset, results


def _speed_policy(dataset, results, **overrides):
    score = score_benchmark(dataset, results)
    policy = {
        "policy_id": "test-speed-policy",
        "dataset_kind": score["dataset_kind"],
        "dataset_sha256": score["dataset_sha256"],
        "min_cases": 1,
        "min_gold_findings": 1,
        "min_precision": 0.3,
        "min_recall": 0.5,
        "max_missed_high_impact": 0,
        "min_speed_repetitions": 3,
    }
    return BenchmarkQualityGatePolicy.model_validate({**policy, **overrides})


def test_quality_policy_blocks_missing_or_mismatched_dataset_digest(benchmark_inputs):
    score = _score_for_quality_policy(benchmark_inputs)
    policy = _quality_policy(score)

    mismatched_score = deepcopy(score)
    mismatched_score["dataset_sha256"] = "b" * 64
    mismatched = _quality_result(mismatched_score, policy)
    missing_score = deepcopy(score)
    missing_score.pop("dataset_sha256")
    missing = _quality_result(missing_score, policy)

    assert mismatched["state"] == "blocked"
    assert "dataset" in " ".join(mismatched["reasons"])
    assert missing["state"] == "blocked"
    assert "dataset" in " ".join(missing["reasons"])


def test_quality_policy_separates_threshold_failure_from_blocked(benchmark_inputs):
    score = _score_for_quality_policy(benchmark_inputs)
    policy = _quality_policy(score, min_recall=0.6)

    result = _quality_result(score, policy)

    assert result["state"] == "failed"
    assert any("recall" in reason for reason in result["reasons"])


def test_quality_policy_blocks_incomplete_coverage(benchmark_inputs):
    score = _score_for_quality_policy(benchmark_inputs)
    aggregate = score["configurations"]["baseline"]
    aggregate["coverage_complete"] = False
    aggregate["missing_cases"] = ["case-missing"]

    result = _quality_result(score, _quality_policy(score))

    assert result["state"] == "blocked"
    assert any("coverage" in reason for reason in result["reasons"])


def test_quality_policy_blocks_any_failed_attempt(benchmark_inputs):
    score = _score_for_quality_policy(benchmark_inputs)
    score["configurations"]["baseline"]["failed_attempts"] = 1

    result = _quality_result(score, _quality_policy(score))

    assert result["state"] == "blocked"
    assert any("failed attempt" in reason for reason in result["reasons"])


def test_quality_policy_reports_each_threshold_measurement(benchmark_inputs):
    score = _score_for_quality_policy(benchmark_inputs)
    policy = _quality_policy(score)

    result = _quality_result(score, policy)

    assert result["state"] == "passed"
    assert result["observed"]["recall"] == score["configurations"]["baseline"]["recall"]
    assert result["observed"]["gold_count"] == 2
    assert result["thresholds"]["min_gold_findings"] == policy.min_gold_findings
    assert score["configurations"]["baseline"]["gold_count"] == 2


def test_quality_policy_blocks_insufficient_gold_coverage(benchmark_inputs):
    score = _score_for_quality_policy(benchmark_inputs)
    score["configurations"]["baseline"]["gold_count"] = 0

    result = _quality_result(score, _quality_policy(score, min_gold_findings=1))

    assert result["state"] == "blocked"
    assert any("gold finding" in reason for reason in result["reasons"])


def test_quality_policy_blocks_invalid_configuration_identity(benchmark_inputs):
    score = _score_for_quality_policy(benchmark_inputs)
    policy = _quality_policy(score)

    result = evaluate_quality_policy(score, policy, configuration_id=[])

    assert result["state"] == "blocked"
    assert any("configuration_id" in reason for reason in result["reasons"])


def test_score_gold_coverage_is_unique_across_repetitions(benchmark_inputs):
    dataset_data, results_data = deepcopy(benchmark_inputs)
    results_data["schema_version"] = 2
    results_data["runs"][0]["attempt"] = 1
    repeated_run = deepcopy(results_data["runs"][0])
    repeated_run["run_id"] = "run-2"
    repeated_run["attempt"] = 2
    results_data["runs"].append(repeated_run)
    score = score_benchmark(
        BenchmarkDataset.model_validate(dataset_data),
        BenchmarkResults.model_validate(results_data),
    )

    aggregate = score["configurations"]["baseline"]
    assert aggregate["gold_count"] == 2
    assert aggregate["gold_attempt_count"] == 4
    assert aggregate["recall"] == 0.5


def test_scoring_uses_explicit_verdicts_and_counts_duplicate_noise(benchmark_inputs):
    dataset, results = benchmark_inputs
    score = score_benchmark(
        BenchmarkDataset.model_validate(dataset), BenchmarkResults.model_validate(results)
    )
    run = score["runs"][0]
    assert score["schema_version"] == 3
    assert run["precision"] == 1 / 3
    assert run["recall"] == 0.5
    assert run["matched_gold"] == 1
    assert run["missed_high_impact"] == 0
    assert run["gold_complete"] is True
    assert run["missed_gold"] == [
        {
            "gold_id": "G-2",
            "severity": "medium",
            "category": "security",
            "path": "src/api.py",
            "start_line": 2,
            "end_line": 2,
            "scope": "exacerbated-by-pr",
            "difficulty": "hard",
            "context_required": "broader-project-context",
            "diagnosis": "unclassified",
            "diagnosis_rationale": None,
            "evidence_refs": [],
        }
    ]
    assert run["confidence"]["high"]["valid"] == 1
    assert run["confidence"]["medium"]["valid"] == 0
    assert score["configurations"]["baseline"]["input_tokens"] is None
    strata = score["configurations"]["baseline"]["gold_recall_by"]
    assert strata["severity"] == {
        "high": {"matched_gold": 1, "gold_count": 1, "recall": 1.0},
        "medium": {"matched_gold": 0, "gold_count": 1, "recall": 0.0},
    }
    assert strata["category"]["security"]["recall"] == 0.0
    assert strata["scope"]["exacerbated-by-pr"]["recall"] == 0.0
    assert strata["difficulty"]["easy"]["recall"] == 1.0
    assert strata["context_required"]["broader-project-context"]["recall"] == 0.0
    assert score["configurations"]["baseline"]["p95_elapsed_ms"] == 500


def test_manual_missed_gold_diagnosis_is_post_run_and_does_not_change_metrics(benchmark_inputs):
    dataset_data, results_data = deepcopy(benchmark_inputs)
    results_data["schema_version"] = 4
    results_data["runs"][0]["queue_wait_ms"] = 75
    results_data["schedule"] = {
        "schedule_id": "schedule-1",
        "strategy": "interleaved",
        "run_order": ["run-1"],
    }
    results_data["runs"][0]["missed_gold_diagnoses"] = [
        {
            "gold_id": "G-2",
            "cause": "neighbor_trace_gap",
            "rationale": "E7 was present in the packet, but the report did not trace it to the detail fetch.",
            "evidence_refs": ["E7"],
        }
    ]
    dataset = BenchmarkDataset.model_validate(dataset_data)
    results = BenchmarkResults.model_validate(results_data)

    baseline = score_benchmark(
        dataset,
        BenchmarkResults.model_validate(benchmark_inputs[1]),
    )
    diagnosed = score_benchmark(dataset, results)

    assert diagnosed["runs"][0]["missed_gold"][0]["diagnosis"] == "neighbor_trace_gap"
    assert "detail fetch" in diagnosed["runs"][0]["missed_gold"][0]["diagnosis_rationale"]
    assert diagnosed["runs"][0]["missed_gold"][0]["evidence_refs"] == ["E7"]
    assert diagnosed["runs"][0]["precision"] == baseline["runs"][0]["precision"]
    assert diagnosed["runs"][0]["recall"] == baseline["runs"][0]["recall"]


@pytest.mark.parametrize("gold_id", ("G-1", "G-404"))
def test_score_rejects_diagnoses_for_matched_or_unknown_gold(gold_id, benchmark_inputs):
    dataset_data, results_data = deepcopy(benchmark_inputs)
    results_data["schema_version"] = 4
    results_data["runs"][0]["missed_gold_diagnoses"] = [
        {
            "gold_id": gold_id,
            "cause": "review_analysis_gap",
            "rationale": "The manually reviewed source path is recorded as E7.",
            "evidence_refs": ["E7"],
        }
    ]

    with pytest.raises(ValueError, match="known unmatched gold finding"):
        score_benchmark(
            BenchmarkDataset.model_validate(dataset_data),
            BenchmarkResults.model_validate(results_data),
        )


def test_incomplete_gold_cannot_receive_a_miss_diagnosis_but_still_marks_recall_unknown(
    benchmark_inputs,
):
    dataset_data, results_data = deepcopy(benchmark_inputs)
    dataset_data["cases"][0]["gold_complete"] = False
    dataset = BenchmarkDataset.model_validate(dataset_data)
    result = score_benchmark(dataset, BenchmarkResults.model_validate(results_data))
    assert result["runs"][0]["gold_complete"] is False
    assert result["runs"][0]["recall"] is None
    assert result["runs"][0]["missed_gold"][0]["diagnosis"] == "unclassified"

    results_data["schema_version"] = 4
    results_data["runs"][0]["missed_gold_diagnoses"] = [
        {
            "gold_id": "G-2",
            "cause": "evidence_packet_gap",
            "rationale": "The required related source was not in the pinned packet.",
        }
    ]
    with pytest.raises(ValueError, match="complete gold set"):
        score_benchmark(dataset, BenchmarkResults.model_validate(results_data))


def test_missed_gold_diagnoses_require_results_schema_four(benchmark_inputs):
    _, results_data = deepcopy(benchmark_inputs)
    results_data["runs"][0]["missed_gold_diagnoses"] = [
        {
            "gold_id": "G-2",
            "cause": "evidence_packet_gap",
            "rationale": "The required related source was not in the pinned packet.",
        }
    ]

    with pytest.raises(ValidationError, match="schema version 4"):
        BenchmarkResults.model_validate(results_data)


def test_schema_four_keeps_schedule_and_failed_attempt_metadata(benchmark_inputs):
    dataset_data, results_data = deepcopy(benchmark_inputs)
    results_data["schema_version"] = 4
    run = results_data["runs"][0]
    run["attempt"] = 1
    run["queue_wait_ms"] = 75
    run["input_tokens"] = 100
    run["output_tokens"] = 20
    run["token_usage_scope"] = "attempt_only"
    run["missed_gold_diagnoses"] = [
        {
            "gold_id": "G-2",
            "cause": "uncertain",
            "rationale": "The packet does not establish which reviewer step lost the finding.",
        }
    ]
    results_data["failures"] = [
        {
            "case_id": run["case_id"],
            "configuration_id": run["configuration_id"],
            "model": run["model"],
            "reasoning": run["reasoning"],
            "checklist_sha256": run["checklist_sha256"],
            "host_instructions_sha256": run["host_instructions_sha256"],
            "attempt": 2,
            "input_tokens": 40,
            "output_tokens": 5,
            "failure_code": "provider_failed",
            "error_type": "RuntimeError",
        }
    ]
    results_data["schedule"] = {
        "schedule_id": "schedule-1",
        "strategy": "interleaved",
        "run_order": [run["run_id"]],
    }

    score = score_benchmark(
        BenchmarkDataset.model_validate(dataset_data),
        BenchmarkResults.model_validate(results_data),
    )

    assert score["schema_version"] == 4
    assert score["configurations"]["baseline"]["failed_attempts"] == 1
    assert score["configurations"]["baseline"]["total_input_tokens_including_failures"] == 140
    assert score["configurations"]["baseline"]["total_output_tokens_including_failures"] == 25
    results_data["failures"][0]["input_tokens"] = None
    unknown = score_benchmark(
        BenchmarkDataset.model_validate(dataset_data),
        BenchmarkResults.model_validate(results_data),
    )
    assert unknown["configurations"]["baseline"]["total_input_tokens_including_failures"] is None
    assert score["runs"][0]["missed_gold"][0]["diagnosis"] == "uncertain"


def test_failed_attempt_tokens_are_not_double_counted_in_cumulative_host_usage(benchmark_inputs):
    dataset_data, results_data = deepcopy(benchmark_inputs)
    results_data["schema_version"] = 4
    run = results_data["runs"][0]
    run.update(attempt=2, input_tokens=140, output_tokens=25)
    results_data["failures"] = [{
        "case_id": run["case_id"], "configuration_id": run["configuration_id"],
        "model": run["model"], "reasoning": run["reasoning"],
        "checklist_sha256": run["checklist_sha256"],
        "host_instructions_sha256": run["host_instructions_sha256"],
        "attempt": 1, "failure_code": "retryable", "error_type": "RetryableProfileError",
        "input_tokens": 40, "output_tokens": 5,
    }]
    dataset = BenchmarkDataset.model_validate(dataset_data)
    for scope in (None, "includes_retries"):
        if scope is None:
            run.pop("token_usage_scope", None)
        else:
            run["token_usage_scope"] = scope
        score = score_benchmark(dataset, BenchmarkResults.model_validate(results_data))
        aggregate = score["configurations"]["baseline"]
        assert aggregate["total_input_tokens_including_failures"] is None
        assert aggregate["total_output_tokens_including_failures"] is None
        assert "may include failed attempts" in aggregate["token_total_limitation"]
    run.update(input_tokens=100, output_tokens=20, token_usage_scope="attempt_only")
    aggregate = score_benchmark(dataset, BenchmarkResults.model_validate(results_data))["configurations"]["baseline"]
    assert aggregate["total_input_tokens_including_failures"] == 140
    assert aggregate["total_output_tokens_including_failures"] == 25
    assert "token_total_limitation" not in aggregate


def test_successful_usage_scope_is_omitted_from_legacy_json(benchmark_inputs):
    _, data = benchmark_inputs
    run = BenchmarkResults.model_validate(data).runs[0]
    assert "token_usage_scope" not in run.model_dump(mode="json")


def test_quality_gate_checks_low_recall_strata_and_p95_latency(benchmark_inputs):
    dataset, results = benchmark_inputs
    score = score_benchmark(
        BenchmarkDataset.model_validate(dataset), BenchmarkResults.model_validate(results)
    )

    result = evaluate_quality_gate(
        score,
        configuration_id="baseline",
        min_cases=1,
        min_precision=0,
        min_recall=0,
        max_p95_elapsed_ms=400,
        min_stratum_recall=0.5,
        min_stratum_gold_count=1,
    )

    assert result["passed"] is False
    assert any("p95 elapsed time 500 ms" in reason for reason in result["reasons"])
    assert any("category=security recall 0.0000" in reason for reason in result["reasons"])
    assert any(
        check["dimension"] == "category"
        and check["value"] == "security"
        and check["status"] == "below_threshold"
        for check in result["observed"]["stratum_checks"]
    )


def test_quality_gate_does_not_claim_stratum_quality_without_enough_gold(benchmark_inputs):
    dataset, results = benchmark_inputs
    score = score_benchmark(
        BenchmarkDataset.model_validate(dataset), BenchmarkResults.model_validate(results)
    )

    result = evaluate_quality_gate(
        score,
        configuration_id="baseline",
        min_cases=1,
        min_precision=0,
        min_recall=0,
        min_stratum_recall=0,
        min_stratum_gold_count=3,
    )

    assert result["passed"] is False
    assert any("no gold-recall stratum meets" in reason for reason in result["reasons"])
    assert all(
        check["status"] == "insufficient_sample"
        for check in result["observed"]["stratum_checks"]
    )


def test_quality_gate_checks_complete_coverage_quality_and_latency():
    score = {
        "scoring": "local_explicit_adjudication",
        "dataset_kind": "curated",
        "configurations": {
            "candidate": {
                "coverage_complete": True,
                "cases": ["case-a", "case-b"],
                "missing_cases": [],
                "precision": 0.92,
                "recall": 0.86,
                "precision_is_lower_bound": False,
                "missed_high_impact": 0,
                "evidence_validation_rate": 0.95,
                "location_validation_rate": 0.9,
                "mean_elapsed_ms": 8000,
            }
        },
    }

    result = evaluate_quality_gate(
        score,
        configuration_id="candidate",
        min_cases=2,
        min_precision=0.9,
        min_recall=0.8,
        max_missed_high_impact=0,
        min_evidence_validation_rate=0.9,
        min_location_validation_rate=0.85,
        max_mean_elapsed_ms=10000,
    )

    assert result["passed"] is True
    assert result["observed"]["case_count"] == 2
    assert result["dataset_kind"] == "curated"


def test_quality_gate_can_pin_an_approved_adjudicated_dataset(benchmark_inputs):
    dataset_data, results_data = benchmark_inputs
    dataset_data["kind"] = "curated"
    dataset = BenchmarkDataset.model_validate(dataset_data)
    results = BenchmarkResults.model_validate(results_data)
    score = score_benchmark(dataset, results)
    edited_gold = deepcopy(dataset.model_dump(mode="json"))
    edited_gold["cases"][0]["gold"][0]["description"] = "A different adjudicated defect"
    edited_score = score_benchmark(BenchmarkDataset.model_validate(edited_gold), results)

    accepted = evaluate_quality_gate(
        score,
        configuration_id="baseline",
        min_precision=0,
        min_recall=0,
        required_dataset_kind="curated",
        required_dataset_sha256=score["dataset_sha256"],
    )
    rejected = evaluate_quality_gate(
        score,
        configuration_id="baseline",
        min_precision=0,
        min_recall=0,
        required_dataset_kind="reviewbench_reference",
        required_dataset_sha256="f" * 64,
    )

    assert accepted["passed"] is True
    assert accepted["dataset_sha256"] == score["dataset_sha256"]
    assert edited_score["dataset_sha256"] != score["dataset_sha256"]
    assert rejected["passed"] is False
    assert any("dataset kind curated" in reason for reason in rejected["reasons"])
    assert any("does not match the required pinned dataset" in reason for reason in rejected["reasons"])


def test_quality_gate_cli_accepts_required_dataset_identity(tmp_path):
    score_path = tmp_path / "score.json"
    score_path.write_text(
        json.dumps(
            {
                "scoring": "local_explicit_adjudication",
                "dataset_kind": "curated",
                "dataset_sha256": "a" * 64,
                "configurations": {
                    "candidate": {
                        "coverage_complete": True,
                        "cases": ["case-a"],
                        "missing_cases": [],
                        "precision": 1.0,
                        "recall": 1.0,
                        "precision_is_lower_bound": False,
                        "missed_high_impact": 0,
                    }
                },
            }
        ),
        encoding="utf-8",
    )

    result = benchmark_main(
        [
            "quality-gate",
            "--score",
            str(score_path),
            "--configuration",
            "candidate",
            "--min-precision",
            "1",
            "--min-recall",
            "1",
            "--require-dataset-kind",
            "curated",
            "--require-dataset-sha256",
            "a" * 64,
        ]
    )

    assert result == 0


def test_quality_gate_cli_loads_digest_pinned_policy(tmp_path, benchmark_inputs):
    dataset_data, results_data = benchmark_inputs
    dataset_data["kind"] = "curated"
    score = score_benchmark(
        BenchmarkDataset.model_validate(dataset_data),
        BenchmarkResults.model_validate(results_data),
    )
    score_file = tmp_path / "score.json"
    policy_file = tmp_path / "policy.json"
    output = tmp_path / "gate.json"
    score_file.write_text(json.dumps(score), encoding="utf-8")
    policy = BenchmarkQualityGatePolicy(
        policy_id="test",
        dataset_kind=score["dataset_kind"],
        dataset_sha256=score["dataset_sha256"],
        min_cases=1,
        min_gold_findings=1,
        min_precision=0,
        min_recall=0,
        max_missed_high_impact=0,
    )
    policy_file.write_text(policy.model_dump_json(), encoding="utf-8")

    result = benchmark_main(
        [
            "quality-gate",
            "--score",
            str(score_file),
            "--configuration",
            "baseline",
            "--policy",
            str(policy_file),
            "--output",
            str(output),
        ]
    )

    assert json.loads(output.read_text(encoding="utf-8"))["state"] == "passed"
    assert result == 0

    failing_policy = policy.model_copy(update={"min_recall": 0.9})
    policy_file.write_text(failing_policy.model_dump_json(), encoding="utf-8")
    failed_result = benchmark_main(
        [
            "quality-gate",
            "--score",
            str(score_file),
            "--configuration",
            "baseline",
            "--policy",
            str(policy_file),
            "--output",
            str(output),
        ]
    )
    assert json.loads(output.read_text(encoding="utf-8"))["state"] == "failed"
    assert failed_result == 1

    policy_file.write_text(policy.model_dump_json(), encoding="utf-8")
    score["dataset_sha256"] = "b" * 64
    score_file.write_text(json.dumps(score), encoding="utf-8")
    blocked_result = benchmark_main(
        [
            "quality-gate",
            "--score",
            str(score_file),
            "--configuration",
            "baseline",
            "--policy",
            str(policy_file),
            "--output",
            str(output),
        ]
    )
    blocked = json.loads(output.read_text(encoding="utf-8"))
    assert blocked["state"] == "blocked"
    assert any("dataset" in reason for reason in blocked["reasons"])
    assert blocked_result == 1


def test_quality_gate_cli_rejects_mixed_policy_and_experimental_thresholds(tmp_path):
    with pytest.raises(SystemExit):
        benchmark_main(
            [
                "quality-gate",
                "--score",
                str(tmp_path / "score.json"),
                "--configuration",
                "candidate",
                "--policy",
                str(tmp_path / "policy.json"),
                "--min-recall",
                "0.9",
            ]
        )


def test_quality_gate_fails_incomplete_or_unresolved_runs():
    score = {
        "scoring": "local_explicit_adjudication",
        "dataset_kind": "reviewbench_reference",
        "configurations": {
            "candidate": {
                "coverage_complete": False,
                "cases": ["case-a"],
                "missing_cases": ["case-b"],
                "precision": 0.99,
                "recall": None,
                "precision_is_lower_bound": True,
                "missed_high_impact": 1,
                "evidence_validation_rate": 0.5,
                "location_validation_rate": 0.5,
                "mean_elapsed_ms": 20000,
            }
        },
    }

    result = evaluate_quality_gate(
        score,
        configuration_id="candidate",
        min_cases=2,
        min_precision=0.9,
        min_recall=0.8,
        max_missed_high_impact=0,
        min_evidence_validation_rate=0.8,
        min_location_validation_rate=0.8,
        max_mean_elapsed_ms=10000,
    )

    assert result["passed"] is False
    assert result["reasons"]
    assert "case coverage is incomplete" in result["reasons"]
    assert "recall is unavailable" in result["reasons"]
    assert any(reason.startswith("precision is only a lower bound") for reason in result["reasons"])
    assert any(reason.startswith("missed 1 high-impact gold finding(s)") for reason in result["reasons"])


def test_quality_gate_rejects_malformed_score_aggregate():
    score = {
        "scoring": "local_explicit_adjudication",
        "configurations": {"candidate": []},
    }

    with pytest.raises(TypeError, match="configuration aggregate must be an object"):
        evaluate_quality_gate(
            score,
            configuration_id="candidate",
            min_precision=0.9,
            min_recall=0.8,
        )


def test_quality_gate_cli_returns_nonzero_when_thresholds_fail(tmp_path):
    score_path = tmp_path / "score.json"
    score_path.write_text(
        json.dumps(
            {
                "scoring": "local_explicit_adjudication",
                "dataset_kind": "curated",
                "configurations": {
                    "candidate": {
                        "coverage_complete": True,
                        "cases": ["case-a"],
                        "missing_cases": [],
                        "precision": 0.5,
                        "recall": 0.5,
                        "precision_is_lower_bound": False,
                        "missed_high_impact": 0,
                        "evidence_validation_rate": 1.0,
                        "location_validation_rate": 1.0,
                        "mean_elapsed_ms": 100,
                    }
                },
            }
        ),
        encoding="utf-8",
    )

    result = benchmark_main(
        [
            "quality-gate",
            "--score",
            str(score_path),
            "--configuration",
            "candidate",
            "--min-cases",
            "1",
            "--min-precision",
            "0.9",
            "--min-recall",
            "0.8",
        ]
    )

    assert result == 1


def test_scores_reject_different_source_snapshot(benchmark_inputs):
    dataset, results = benchmark_inputs
    results["runs"][0]["head_sha"] = "5" * 40
    with pytest.raises(ValueError, match="pinned dataset case"):
        score_benchmark(
            BenchmarkDataset.model_validate(dataset), BenchmarkResults.model_validate(results)
        )


def test_scores_require_the_pinned_task_context(benchmark_inputs):
    dataset, results = benchmark_inputs
    task_context = {
        "title": "A requirement title",
        "body": "The exact user-visible requirement.",
    }
    context_sha = hashlib.sha256(
        json.dumps(task_context, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    dataset["cases"][0].update(
        task_title=task_context["title"],
        task_body=task_context["body"],
        task_context_sha256=context_sha,
    )
    results["runs"][0]["task_context_sha256"] = context_sha

    score_benchmark(BenchmarkDataset.model_validate(dataset), BenchmarkResults.model_validate(results))

    results["runs"][0]["task_context_sha256"] = "8" * 64
    with pytest.raises(ValueError, match="task context"):
        score_benchmark(
            BenchmarkDataset.model_validate(dataset), BenchmarkResults.model_validate(results)
        )


def test_missing_and_double_credit_adjudications_are_rejected(benchmark_inputs):
    _, results = benchmark_inputs
    missing = deepcopy(results)
    missing["runs"][0]["adjudications"].pop()
    with pytest.raises(ValidationError, match="every candidate"):
        BenchmarkResults.model_validate(missing)
    results["runs"][0]["adjudications"][1]["verdict"] = "matched"
    with pytest.raises(ValidationError, match="only once"):
        BenchmarkResults.model_validate(results)


def test_unverified_is_a_lower_bound_and_partial_gold_has_no_recall(benchmark_inputs):
    dataset, results = benchmark_inputs
    dataset["cases"][0]["gold_complete"] = False
    results["runs"][0]["adjudications"][2]["verdict"] = "unverified"
    score = score_benchmark(
        BenchmarkDataset.model_validate(dataset), BenchmarkResults.model_validate(results)
    )
    assert score["runs"][0]["recall"] is None
    assert score["runs"][0]["precision_is_lower_bound"] is True
    assert score["configurations"]["baseline"]["gold_recall_by"]["severity"]["high"][
        "recall"
    ] is None


def test_configuration_id_cannot_mix_models(benchmark_inputs):
    _, results = benchmark_inputs
    second = deepcopy(results["runs"][0])
    second.update(run_id="run-2", model="different-model")
    results["runs"].append(second)
    with pytest.raises(ValidationError, match="different model or prompt"):
        BenchmarkResults.model_validate(results)


def test_benchmark_cli_writes_scoring_artifact(tmp_path, benchmark_inputs):
    dataset, results = benchmark_inputs
    dataset_path, results_path, output = (tmp_path / name for name in ("dataset", "runs", "score"))
    dataset_path.write_text(BenchmarkDataset.model_validate(dataset).model_dump_json())
    results_path.write_text(BenchmarkResults.model_validate(results).model_dump_json())
    assert (
        benchmark_main(
            [
                "score",
                "--dataset",
                str(dataset_path),
                "--results",
                str(results_path),
                "--output",
                str(output),
            ]
        )
        == 0
    )
    assert '"local_explicit_adjudication"' in output.read_text()


def test_missing_cases_are_visible_and_do_not_get_an_aggregate_recall(benchmark_inputs):
    dataset, results = benchmark_inputs
    additional = deepcopy(dataset["cases"][0])
    additional["case_id"] = "case-2"
    dataset["cases"].append(additional)
    score = score_benchmark(
        BenchmarkDataset.model_validate(dataset), BenchmarkResults.model_validate(results)
    )
    summary = score["configurations"]["baseline"]
    assert summary["coverage_complete"] is False
    assert summary["missing_cases"] == ["case-2"]
    assert summary["recall"] is None


def test_reviewbench_import_rejects_malformed_input_before_git(tmp_path, monkeypatch):
    source = tmp_path / "golden.json"
    source.write_text('{"pr": {}}')
    monkeypatch.setattr(
        "qa_orchestrator.benchmark.subprocess.run",
        lambda *args, **kwargs: pytest.fail("git must not be called"),
    )
    with pytest.raises(ValueError, match="invalid ReviewBench"):
        import_reviewbench(source, tmp_path)


def test_reviewbench_case_set_rejects_source_path_outside_corpus_before_io(tmp_path):
    with pytest.raises(ValueError, match="source path must be inside corpus"):
        import_reviewbench_case_set(
            tmp_path / "missing-tasks.json",
            tmp_path / "missing-findings.json",
            tmp_path / "mirrors",
            source_revision="a" * 40,
            source_path="corpus/../private",
        )


def test_diff_budget_marks_blocked_unknown_and_lower_bound_cases():
    dataset = BenchmarkDataset.model_validate(
        {
            "kind": "synthetic",
            "source": "unit-test",
            "cases": [
                {
                    "case_id": "fits",
                    "repository": "github.com/example/repo",
                    "base_sha": "0" * 40,
                    "head_sha": "1" * 40,
                    "diff_sha256": "2" * 64,
                    "diff_bytes": 99,
                    "gold": [],
                },
                {
                    "case_id": "too-large",
                    "repository": "github.com/example/repo",
                    "base_sha": "3" * 40,
                    "head_sha": "4" * 40,
                    "diff_sha256": "5" * 64,
                    "diff_bytes": 101,
                    "gold": [],
                },
                {
                    "case_id": "unknown",
                    "repository": "github.com/example/repo",
                    "base_sha": "6" * 40,
                    "head_sha": "7" * 40,
                    "diff_sha256": "8" * 64,
                    "gold": [],
                },
            ],
        }
    )

    report = check_diff_budget(dataset, max_context_bytes=100)

    assert report["over_budget_case_ids"] == ["too-large"]
    assert report["unknown_case_ids"] == ["unknown"]
    assert report["cases"] == [
        {"case_id": "fits", "diff_bytes": 99, "status": "raw_diff_within_budget"},
        {"case_id": "too-large", "diff_bytes": 101, "status": "raw_diff_over_budget"},
        {"case_id": "unknown", "diff_bytes": None, "status": "unknown"},
    ]
    assert "does not confirm" in report["scope"]


def test_legacy_dataset_wire_format_omits_new_nullable_fields(benchmark_inputs):
    dataset, _ = benchmark_inputs
    legacy_case = deepcopy(dataset["cases"][0])
    for field in (
        "task_title", "task_body", "pull_request_url", "language", "diff_bytes",
        "task_context_sha256",
    ):
        legacy_case.pop(field, None)
    for finding in legacy_case["gold"]:
        for field in ("scope", "difficulty", "context_required"):
            finding.pop(field, None)
    payload = BenchmarkDataset.model_validate(dataset).model_dump(mode="json")
    legacy_payload = BenchmarkDataset.model_validate(
        {**dataset, "cases": [legacy_case]}
    ).model_dump(mode="json")

    assert payload["schema_version"] == 1
    assert "source_revision_status" not in payload
    case_payload = legacy_payload["cases"][0]
    assert not {
        "task_title", "task_body", "pull_request_url", "language", "diff_bytes",
        "task_context_sha256",
    } & case_payload.keys()
    assert not {"scope", "difficulty", "context_required"} & case_payload["gold"][0].keys()


def test_legacy_results_wire_format_omits_task_context_hash(benchmark_inputs):
    _, results = benchmark_inputs
    payload = deepcopy(results)
    payload["runs"][0].pop("task_context_sha256", None)

    serialized = BenchmarkResults.model_validate(payload).model_dump(mode="json")

    assert "task_context_sha256" not in serialized["runs"][0]
    assert "missed_gold_diagnoses" not in serialized["runs"][0]


def test_benchmark_schedule_requires_unique_complete_run_order(benchmark_inputs):
    _, results_data = benchmark_inputs
    duplicate = deepcopy(results_data)
    duplicate["schema_version"] = 3
    duplicate["schedule"] = {
        "schedule_id": "schedule-1",
        "strategy": "interleaved",
        "run_order": ["run-1", "run-1"],
    }
    with pytest.raises(ValidationError, match="unique"):
        BenchmarkResults.model_validate(duplicate)

    incomplete = deepcopy(results_data)
    incomplete["schema_version"] = 3
    incomplete["schedule"] = {
        "schedule_id": "schedule-2",
        "strategy": "randomized",
        "run_order": ["other-run"],
    }
    with pytest.raises(ValidationError, match="every successful run"):
        BenchmarkResults.model_validate(incomplete)


def test_benchmark_schedule_excludes_failed_attempts(benchmark_inputs):
    _, results_data = benchmark_inputs
    results_data["schema_version"] = 3
    results_data["runs"][0]["attempt"] = 1
    run = results_data["runs"][0]
    results_data["failures"] = [
        {
            "case_id": run["case_id"],
            "configuration_id": run["configuration_id"],
            "model": run["model"],
            "reasoning": run["reasoning"],
            "checklist_sha256": run["checklist_sha256"],
            "host_instructions_sha256": run["host_instructions_sha256"],
            "attempt": 2,
            "failure_code": "provider_failed",
            "error_type": "RuntimeError",
        }
    ]
    results_data["schedule"] = {
        "schedule_id": "schedule-1",
        "strategy": "interleaved",
        "run_order": [run["run_id"]],
    }

    results = BenchmarkResults.model_validate(results_data)

    assert [item.run_id for item in results.runs] == ["run-1"]
    assert [item.attempt for item in results.failures] == [2]
    assert results.schedule.run_order == ("run-1",)


def test_benchmark_run_accepts_nonnegative_queue_wait(benchmark_inputs):
    _, results_data = benchmark_inputs
    results_data["schema_version"] = 2
    results_data["runs"][0]["queue_wait_ms"] = 120
    with pytest.raises(ValidationError, match="schema version 3"):
        BenchmarkResults.model_validate(results_data)

    results_data["schema_version"] = 3

    results = BenchmarkResults.model_validate(results_data)
    assert results.runs[0].queue_wait_ms == 120

    results_data["runs"][0]["queue_wait_ms"] = -1
    with pytest.raises(ValidationError):
        BenchmarkResults.model_validate(results_data)


def test_legacy_results_wire_format_omits_absent_schedule_and_queue_fields(benchmark_inputs):
    _, results_data = benchmark_inputs

    serialized = BenchmarkResults.model_validate(results_data).model_dump(mode="json")

    assert "schedule" not in serialized
    assert "queue_wait_ms" not in serialized["runs"][0]


def test_diff_budget_cli_writes_a_preflight_artifact(tmp_path):
    dataset_path = tmp_path / "dataset.json"
    output_path = tmp_path / "preflight.json"
    dataset_path.write_text(
        json.dumps(
            {
                "kind": "synthetic",
                "source": "unit-test",
                "cases": [
                    {
                        "case_id": "blocked",
                        "repository": "github.com/example/repo",
                        "base_sha": "0" * 40,
                        "head_sha": "1" * 40,
                        "diff_sha256": "2" * 64,
                        "diff_bytes": 101,
                        "gold": [],
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    assert benchmark_main(
        [
            "diff-budget",
            "--dataset",
            str(dataset_path),
            "--max-context-bytes",
            "100",
            "--output",
            str(output_path),
        ]
    ) == 0

    assert json.loads(output_path.read_text(encoding="utf-8"))["over_budget_case_ids"] == [
        "blocked"
    ]


@pytest.mark.parametrize(
    ("task_option", "source_path"),
    [("--test-set", "corpus/test"), ("--tasks", "corpus/manifest.json")],
)
def test_reviewbench_test_set_cli_imports_pinned_cases_and_metadata(
    tmp_path, task_option, source_path
):
    repositories_root = tmp_path / "mirrors"
    repository = repositories_root / "acme_repo"
    repository.mkdir(parents=True)

    def git(*args):
        return subprocess.run(
            ["git", "-C", str(repository), *args],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()

    subprocess.run(["git", "init", "--quiet", str(repository)], check=True)
    git("config", "user.name", "Benchmark Test")
    git("config", "user.email", "benchmark@example.invalid")
    (repository / "src.py").write_text("value = 1\n", encoding="utf-8")
    git("add", "src.py")
    git("commit", "--quiet", "-m", "base")
    base = git("rev-parse", "HEAD")
    (repository / "src.py").write_text("value = 2\n", encoding="utf-8")
    git("add", "src.py")
    git("commit", "--quiet", "-m", "head")
    head = git("rev-parse", "HEAD")

    case_id = f"acme_repo_1-{head[:8]}"
    test_set_path = tmp_path / "test.json"
    findings_path = tmp_path / "findings.json"
    output_path = tmp_path / "dataset.json"
    test_set_path.write_text(
        json.dumps(
            [
                {
                    "nwo": "acme/repo",
                    "pr_number": 1,
                    "repo": "https://github.com/acme/repo",
                    "pr_url": "https://github.com/acme/repo/pull/1",
                    "base": base,
                    "head": head,
                    "title": "Keep the value contract",
                    "body": "The changed value must preserve the documented contract.",
                    "language": "Python",
                }
            ]
        ),
        encoding="utf-8",
    )
    findings_path.write_text(
        json.dumps(
            {
                case_id: [
                    {
                        "file": "src.py",
                        "start_line": 1,
                        "end_line": 1,
                        "severity": "high",
                        "category": "correctness",
                        "message": "The changed value breaks the contract.",
                        "scope": "introduced-by-pr",
                        "difficulty": "easy",
                        "context_required": "diff-only",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )

    assert benchmark_main(
        [
            "import-reviewbench-set",
            task_option,
            str(test_set_path),
            "--findings",
            str(findings_path),
            "--repositories-root",
            str(repositories_root),
            "--source-revision",
            "a" * 40,
            "--source-path",
            source_path,
            "--output",
            str(output_path),
        ]
    ) == 0

    dataset = BenchmarkDataset.model_validate_json(output_path.read_bytes())
    assert dataset.schema_version == 2
    assert dataset.source_revision_status == "caller_supplied_unverified"
    assert dataset.source.endswith("/" + "a" * 40 + "/" + source_path)
    assert dataset.source_sha256 == hashlib.sha256(
        test_set_path.read_bytes() + b"\0" + findings_path.read_bytes()
    ).hexdigest()
    assert len(dataset.cases) == 1
    case = dataset.cases[0]
    assert case.case_id == case_id
    assert case.diff_sha256 != "0" * 64
    assert case.gold_complete is True
    assert case.gold[0].scope == "introduced-by-pr"
    assert case.gold[0].difficulty == "easy"
    assert case.gold[0].context_required == "diff-only"
    assert case.task_title == "Keep the value contract"
    assert case.task_body == "The changed value must preserve the documented contract."
    assert case.pull_request_url == "https://github.com/acme/repo/pull/1"
    assert case.language == "Python"
    assert case.diff_bytes > 0
    assert case.task_context_sha256 is not None
    tampered = dataset.model_dump(mode="json")
    tampered["cases"][0]["task_body"] = "A changed requirement body."
    with pytest.raises(ValidationError, match="task context hash"):
        BenchmarkDataset.model_validate(tampered)

    test_set = json.loads(test_set_path.read_text(encoding="utf-8"))
    test_set[0]["body"] = "x" * 30001
    test_set_path.write_text(json.dumps(test_set), encoding="utf-8")
    with pytest.raises(ValidationError, match="string_too_long"):
        import_reviewbench_test_set(
            test_set_path,
            findings_path,
            repositories_root,
            source_revision="a" * 40,
        )


def test_reviewbench_import_verifies_source_checkout_and_file_blobs(tmp_path):
    repositories_root = tmp_path / "mirrors"
    repository = repositories_root / "acme_repo"
    reviewbench_checkout = tmp_path / "ReviewBench"
    source_dir = reviewbench_checkout / "corpus" / "test"
    repository.mkdir(parents=True)
    source_dir.mkdir(parents=True)

    def git(repo, *args):
        return subprocess.run(
            ["git", "-C", str(repo), *args],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()

    for repo in (repository, reviewbench_checkout):
        subprocess.run(["git", "init", "--quiet", str(repo)], check=True)
        git(repo, "config", "user.name", "Benchmark Test")
        git(repo, "config", "user.email", "benchmark@example.invalid")

    (repository / "src.py").write_text("value = 1\n", encoding="utf-8")
    git(repository, "add", "src.py")
    git(repository, "commit", "--quiet", "-m", "base")
    base = git(repository, "rev-parse", "HEAD")
    (repository / "src.py").write_text("value = 2\n", encoding="utf-8")
    git(repository, "add", "src.py")
    git(repository, "commit", "--quiet", "-m", "head")
    head = git(repository, "rev-parse", "HEAD")

    case_id = f"acme_repo_1-{head[:8]}"
    test_set_path = source_dir / "test.json"
    findings_path = source_dir / "findings.json"
    test_set_path.write_text(
        json.dumps(
            [
                {
                    "nwo": "acme/repo",
                    "pr_number": 1,
                    "repo": "https://github.com/acme/repo",
                    "base": base,
                    "head": head,
                }
            ]
        ),
        encoding="utf-8",
    )
    findings_path.write_text(
        json.dumps(
            {
                case_id: [
                    {
                        "file": "src.py",
                        "start_line": 1,
                        "end_line": 1,
                        "severity": "high",
                        "category": "correctness",
                        "message": "The changed value breaks the contract.",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    git(reviewbench_checkout, "add", "corpus/test/test.json", "corpus/test/findings.json")
    git(reviewbench_checkout, "commit", "--quiet", "-m", "pin ReviewBench test data")
    source_revision = git(reviewbench_checkout, "rev-parse", "HEAD")
    output_path = tmp_path / "dataset.json"
    command = [
        "import-reviewbench-set",
        "--test-set",
        str(test_set_path),
        "--findings",
        str(findings_path),
        "--repositories-root",
        str(repositories_root),
        "--source-revision",
        source_revision,
        "--source-path",
        "corpus/test",
        "--source-checkout",
        str(reviewbench_checkout),
        "--output",
        str(output_path),
    ]

    assert benchmark_main(command) == 0
    dataset = BenchmarkDataset.model_validate_json(output_path.read_bytes())
    assert dataset.schema_version == 3
    assert dataset.source_revision_status == "checkout_verified"
    assert dataset.source.endswith(f"/{source_revision}/corpus/test")

    test_set_path.write_text(test_set_path.read_text(encoding="utf-8") + " ", encoding="utf-8")
    with pytest.raises(ValueError, match="does not match the committed ReviewBench source"):
        benchmark_main(command)

    test_set_path.write_text(test_set_path.read_text(encoding="utf-8").rstrip(), encoding="utf-8")
    source_revision_index = command.index("--source-revision")
    wrong_revision_command = [
        *command[: source_revision_index + 1],
        "f" * 40,
        *command[source_revision_index + 2 :],
    ]
    with pytest.raises(ValueError, match="does not resolve to a commit"):
        benchmark_main(wrong_revision_command)

    outside_test_set = tmp_path / "outside-test.json"
    outside_test_set.write_bytes(test_set_path.read_bytes())
    outside_command = command.copy()
    outside_command[outside_command.index("--test-set") + 1] = str(outside_test_set)
    with pytest.raises(ValueError, match="must be inside the source checkout"):
        benchmark_main(outside_command)


def test_reviewbench_test_set_import_rejects_missing_gold_before_git(tmp_path, monkeypatch):
    test_set_path = tmp_path / "test.json"
    findings_path = tmp_path / "findings.json"
    test_set_path.write_text(
        json.dumps(
            [
                {
                    "nwo": "acme/repo",
                    "pr_number": 1,
                    "repo": "https://github.com/acme/repo",
                    "base": "0" * 40,
                    "head": "1" * 40,
                }
            ]
        ),
        encoding="utf-8",
    )
    findings_path.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(
        "qa_orchestrator.benchmark.subprocess.run",
        lambda *args, **kwargs: pytest.fail("git must not run without matching gold"),
    )

    with pytest.raises(ValueError, match="missing ReviewBench findings"):
        import_reviewbench_test_set(
            test_set_path,
            findings_path,
            tmp_path / "mirrors",
            source_revision="a" * 40,
        )


def test_explicit_grouped_matches_get_credit_without_double_counting(benchmark_inputs):
    dataset, results = benchmark_inputs
    results["runs"][0]["adjudications"][0]["additional_gold_ids"] = ["G-2"]
    score = score_benchmark(
        BenchmarkDataset.model_validate(dataset), BenchmarkResults.model_validate(results)
    )
    assert score["runs"][0]["matched_gold"] == 2
    assert score["runs"][0]["recall"] == 1
    results["runs"][0]["adjudications"][2].update(verdict="matched", gold_id="G-2")
    with pytest.raises(ValidationError, match="only once"):
        BenchmarkResults.model_validate(results)


def test_digest_case_does_not_change_snapshot_or_configuration_identity(benchmark_inputs):
    dataset, results = benchmark_inputs
    dataset["cases"][0].update(head_sha="a" * 40, diff_sha256="b" * 64)
    results["runs"][0].update(head_sha="A" * 40, diff_sha256="B" * 64, checklist_sha256="C" * 64)
    second = deepcopy(results["runs"][0])
    second.update(
        run_id="run-2", head_sha="a" * 40, diff_sha256="b" * 64, checklist_sha256="c" * 64
    )
    results["runs"].append(second)
    score = score_benchmark(
        BenchmarkDataset.model_validate(dataset), BenchmarkResults.model_validate(results)
    )
    assert score["configurations"]["baseline"]["runs"] == 2


def test_reliability_measures_intersection_and_spread_without_best_run_bias(benchmark_inputs):
    from qa_orchestrator.benchmark import score_reliability

    dataset, results = benchmark_inputs
    second = deepcopy(results["runs"][0])
    second.update(run_id="run-2", elapsed_ms=900)
    second["adjudications"][0].update(gold_id="G-2")
    second["adjudications"][1].update(gold_id="G-2")
    results["runs"].append(second)
    result = score_reliability(
        BenchmarkDataset.model_validate(dataset),
        BenchmarkResults.model_validate(results),
        expected_repetitions=3,
    )
    case = result["groups"][0]
    assert case["repetition_coverage_complete"] is False
    assert case["always_found_gold"] == []
    assert case["ever_found_gold"] == ["G-1", "G-2"]
    assert case["pairwise_gold_jaccard"] == 0
    assert case["elapsed_ms"] == {"min": 500, "max": 900, "median": 700.0, "p95": 900}
    assert case["stable_gold_recall"] is None


def test_reliability_rejects_different_packets_for_the_same_case(benchmark_inputs):
    from qa_orchestrator.benchmark import score_reliability

    dataset, results = benchmark_inputs
    second = deepcopy(results["runs"][0])
    second.update(run_id="run-2", evidence_packet_sha256="f" * 64)
    results["runs"].append(second)
    with pytest.raises(ValueError, match="same Evidence Packet"):
        score_reliability(
            BenchmarkDataset.model_validate(dataset),
            BenchmarkResults.model_validate(results),
            expected_repetitions=2,
        )


def test_reliability_rejects_different_adjudicators_for_the_same_case(benchmark_inputs):
    from qa_orchestrator.benchmark import score_reliability

    dataset, results = benchmark_inputs
    second = deepcopy(results["runs"][0])
    second.update(run_id="run-2", adjudicator="another-reviewer")
    results["runs"].append(second)
    with pytest.raises(ValueError, match="same adjudicator"):
        score_reliability(
            BenchmarkDataset.model_validate(dataset),
            BenchmarkResults.model_validate(results),
            expected_repetitions=2,
        )


async def test_repetition_runner_executes_all_requested_attempts(benchmark_inputs):
    from qa_orchestrator.benchmark import BenchmarkRun, run_repetitions

    dataset, results = benchmark_inputs
    seen = []

    async def execute(case, attempt):
        seen.append((case.case_id, attempt))
        return BenchmarkRun.model_validate({**results["runs"][0], "run_id": f"real-{attempt}"})

    runs = await run_repetitions(BenchmarkDataset.model_validate(dataset), execute, repetitions=3)
    assert seen == [("case-1", 1), ("case-1", 2), ("case-1", 3)]
    assert len(runs.runs) == 3
    assert [run.attempt for run in runs.runs] == [1, 2, 3]


async def test_repetition_runner_persists_sanitized_failure_attempts(benchmark_inputs, tmp_path):
    import json

    from qa_orchestrator.benchmark import BenchmarkRun, run_repetitions, score_reliability

    dataset, source_results = benchmark_inputs
    dataset = BenchmarkDataset.model_validate(dataset)
    template = source_results["runs"][0]
    configuration_id = template["configuration_id"]

    async def execute(case, attempt):
        if attempt == 2:
            return BenchmarkFailure(
                case_id=case.case_id,
                configuration_id=configuration_id,
                model=template["model"],
                reasoning=template["reasoning"],
                checklist_sha256=template["checklist_sha256"],
                host_instructions_sha256=template["host_instructions_sha256"],
                attempt=attempt,
                failure_code="cli_failed",
                error_type="ValueError",
                exit_code=2,
                event_type="turn.failed",
                stderr_detail=(
                    "Error: access_token=private-value at /Users/private/.codex/log"
                ),
            )
        return BenchmarkRun.model_validate(
            {**source_results["runs"][0], "run_id": f"success-{attempt}"}
        )

    artifact = await run_repetitions(dataset, execute, repetitions=3)
    reloaded = BenchmarkResults.model_validate_json(artifact.model_dump_json())
    assert [run.attempt for run in reloaded.runs] == [1, 3]
    assert reloaded.failures[0].stderr_detail == "Error: access_token=[REDACTED] at [path]"

    summary = score_reliability(dataset, reloaded, expected_repetitions=3)
    group = summary["groups"][0]
    assert group["attempts"] == 2
    assert group["failed_attempts"] == 1
    assert group["attempts_recorded"] == 3
    assert group["repetition_coverage_complete"] is False
    assert summary["failures"][0]["exit_code"] == 2
    assert summary["failures"][0]["event_type"] == "turn.failed"

    dataset_path = tmp_path / "dataset.json"
    results_path = tmp_path / "results.json"
    output_path = tmp_path / "reliability.json"
    dataset_path.write_text(dataset.model_dump_json(), encoding="utf-8")
    results_path.write_text(reloaded.model_dump_json(), encoding="utf-8")
    assert benchmark_main(
        [
            "reliability",
            "--dataset",
            str(dataset_path),
            "--results",
            str(results_path),
            "--repetitions",
            "3",
            "--output",
            str(output_path),
        ]
    ) == 0
    cli_output = json.loads(output_path.read_text(encoding="utf-8"))
    assert cli_output["failures"][0]["stderr_detail"] == (
        "Error: access_token=[REDACTED] at [path]"
    )


async def test_repetition_runner_accepts_failure_only_artifacts(benchmark_inputs):
    from qa_orchestrator.benchmark import run_repetitions, score_reliability

    dataset, source_results = benchmark_inputs
    dataset = BenchmarkDataset.model_validate(dataset)
    template = source_results["runs"][0]
    configuration_id = template["configuration_id"]

    async def fail(case, attempt):
        return BenchmarkFailure(
            case_id=case.case_id,
            configuration_id=configuration_id,
            model=template["model"],
            reasoning=template["reasoning"],
            checklist_sha256=template["checklist_sha256"],
            host_instructions_sha256=template["host_instructions_sha256"],
            attempt=attempt,
            failure_code="cli_failed",
            error_type="RuntimeError",
        )

    artifact = await run_repetitions(dataset, fail, repetitions=2)
    assert artifact.runs == ()
    summary = score_reliability(dataset, artifact, expected_repetitions=2)
    assert summary["groups"][0]["elapsed_ms"] is None
    assert summary["groups"][0]["failed_attempts"] == 2


def test_speed_comparison_requires_same_model_packet_and_complete_repetitions(benchmark_inputs):
    dataset_data, result_data = benchmark_inputs
    baseline_times = (500, 600, 700)
    candidate_times = (400, 500, 600)
    runs = []
    for configuration, times in (
        ("baseline", baseline_times),
        ("candidate", candidate_times),
    ):
        for attempt, elapsed_ms in enumerate(times, 1):
            run = deepcopy(result_data["runs"][0])
            run.update(
                run_id=f"{configuration}-{attempt}",
                configuration_id=configuration,
                attempt=attempt,
                elapsed_ms=elapsed_ms,
                input_tokens=1000 if configuration == "baseline" else 800,
                output_tokens=100 if configuration == "baseline" else 120,
            )
            runs.append(run)
    dataset = BenchmarkDataset.model_validate(dataset_data)
    results = BenchmarkResults.model_validate({"schema_version": 2, "runs": runs})
    comparison = compare_speed(
        dataset,
        results,
        baseline_configuration="baseline",
        candidate_configuration="candidate",
        repetitions=3,
    )
    assert comparison["cases"][0]["baseline_median_ms"] == 600
    assert comparison["cases"][0]["candidate_median_ms"] == 500
    assert comparison["cases"][0]["baseline_median_input_tokens"] == 1000
    assert comparison["cases"][0]["candidate_median_input_tokens"] == 800
    assert comparison["cases"][0]["input_token_savings_percent"] == 20
    assert comparison["aggregate"]["median_speedup_ratio"] == 1.2
    assert comparison["cases"][0]["paired_median_speedup_ratio"] == pytest.approx(1.2)
    assert comparison["aggregate"]["median_paired_speedup_ratio"] == pytest.approx(1.2)
    assert comparison["aggregate"]["median_paired_improvement_percent"] == pytest.approx(
        100 / 6
    )
    assert comparison["aggregate"]["input_token_savings"] == 600
    assert comparison["aggregate"]["input_token_savings_percent"] == 20
    assert comparison["aggregate"]["baseline_total_output_tokens"] == 300
    assert comparison["aggregate"]["candidate_total_output_tokens"] == 360
    assert comparison["quality_by_configuration"]["baseline"]["precision"] == 1 / 3

    changed = deepcopy(runs)
    for run in changed[-3:]:
        run["model"] = "other-model"
    with pytest.raises(ValueError, match="same model"):
        compare_speed(
            dataset,
            BenchmarkResults.model_validate({"schema_version": 2, "runs": changed}),
            baseline_configuration="baseline",
            candidate_configuration="candidate",
            repetitions=3,
        )

    changed_packets = deepcopy(runs)
    for run in changed_packets[-3:]:
        run["evidence_packet_sha256"] = "f" * 64
    with pytest.raises(ValueError, match="same Evidence Packet across"):
        compare_speed(
            dataset,
            BenchmarkResults.model_validate({"schema_version": 2, "runs": changed_packets}),
            baseline_configuration="baseline",
            candidate_configuration="candidate",
            repetitions=3,
        )

    unpaired = deepcopy(runs)
    unpaired[0]["attempt"] = None
    with pytest.raises(ValueError, match="explicit attempt numbers"):
        compare_speed(
            dataset,
            BenchmarkResults.model_validate({"schema_version": 2, "runs": unpaired}),
            baseline_configuration="baseline",
            candidate_configuration="candidate",
            repetitions=3,
        )


def test_speed_comparison_reports_p95_queue_and_schedule(benchmark_inputs):
    dataset, results = _speed_fixture(benchmark_inputs)

    comparison = compare_speed(
        dataset,
        results,
        baseline_configuration="baseline",
        candidate_configuration="candidate",
        repetitions=3,
    )

    assert comparison["schedule_assessment"] == "interleaved_validated"
    assert comparison["aggregate"]["candidate_p95_elapsed_ms"] == 600
    assert comparison["aggregate"]["candidate_elapsed_sample_count"] == 3
    assert comparison["aggregate"]["candidate_queue_wait_ms"] == {
        "sample_count": 3,
        "observed_count": 2,
        "missing_count": 1,
        "median": 50,
        "p95": 60,
    }
    assert comparison["cases"][0]["candidate_p95_elapsed_ms"] == 600


def test_compare_speed_marks_non_alternating_interleaved_order_invalid(benchmark_inputs):
    order = [
        "baseline-1",
        "baseline-2",
        "candidate-1",
        "candidate-2",
        "baseline-3",
        "candidate-3",
    ]
    dataset, results = _speed_fixture(benchmark_inputs, schedule_order=order)

    comparison = compare_speed(
        dataset,
        results,
        baseline_configuration="baseline",
        candidate_configuration="candidate",
        repetitions=3,
    )

    assert comparison["schedule_assessment"] == "invalid"


def test_randomized_speed_schedule_remains_host_declared(benchmark_inputs):
    from qa_orchestrator.benchmark import evaluate_speed_candidate

    dataset, results = _speed_fixture(benchmark_inputs)
    results_payload = results.model_dump(mode="json")
    results_payload["schedule"]["strategy"] = "randomized"
    randomized_results = BenchmarkResults.model_validate(results_payload)
    comparison = compare_speed(
        dataset,
        randomized_results,
        baseline_configuration="baseline",
        candidate_configuration="candidate",
        repetitions=3,
    )

    eligibility = evaluate_speed_candidate(
        comparison, _speed_policy(dataset, randomized_results)
    )

    assert comparison["schedule_assessment"] == "randomized_host_declared"
    assert eligibility["state"] == "passed"
    assert "not independently verified" in eligibility["limitations"][0]


def test_speed_candidate_accepts_nonstandard_configuration_ids(benchmark_inputs):
    from qa_orchestrator.benchmark import evaluate_speed_candidate

    dataset, results = _speed_fixture(benchmark_inputs)
    results_payload = results.model_dump(mode="json")
    for run in results_payload["runs"]:
        run["configuration_id"] = (
            "control-v1" if run["configuration_id"] == "baseline" else "candidate-v2"
        )
    renamed_results = BenchmarkResults.model_validate(results_payload)
    comparison = compare_speed(
        dataset,
        renamed_results,
        baseline_configuration="control-v1",
        candidate_configuration="candidate-v2",
        repetitions=3,
    )

    eligibility = evaluate_speed_candidate(
        comparison, _speed_policy(dataset, renamed_results)
    )

    assert eligibility["state"] == "passed"


def test_speed_candidate_fails_when_candidate_quality_policy_fails(benchmark_inputs):
    from qa_orchestrator.benchmark import evaluate_speed_candidate

    dataset, results = _speed_fixture(benchmark_inputs)
    comparison = compare_speed(
        dataset,
        results,
        baseline_configuration="baseline",
        candidate_configuration="candidate",
        repetitions=3,
    )
    comparison["quality_by_configuration"]["candidate"]["recall"] = 0.4
    policy = _speed_policy(dataset, results)

    result = evaluate_speed_candidate(comparison, policy)

    assert result["state"] == "failed"
    assert any("candidate" in reason and "recall" in reason for reason in result["reasons"])


def test_speed_candidate_is_blocked_without_order_or_minimum_samples(benchmark_inputs):
    from qa_orchestrator.benchmark import evaluate_speed_candidate

    dataset, unscheduled_results = _speed_fixture(benchmark_inputs, include_schedule=False)
    unscheduled = compare_speed(
        dataset,
        unscheduled_results,
        baseline_configuration="baseline",
        candidate_configuration="candidate",
        repetitions=3,
    )
    policy = _speed_policy(dataset, unscheduled_results)
    assert evaluate_speed_candidate(unscheduled, policy)["state"] == "blocked"

    dataset, scheduled_results = _speed_fixture(benchmark_inputs)
    scheduled = compare_speed(
        dataset,
        scheduled_results,
        baseline_configuration="baseline",
        candidate_configuration="candidate",
        repetitions=3,
    )
    insufficient_policy = _speed_policy(
        dataset, scheduled_results, min_speed_repetitions=4
    )
    assert evaluate_speed_candidate(scheduled, insufficient_policy)["state"] == "blocked"


def test_speed_candidate_is_blocked_when_baseline_fails_quality_policy(benchmark_inputs):
    from qa_orchestrator.benchmark import evaluate_speed_candidate

    dataset, results = _speed_fixture(benchmark_inputs)
    comparison = compare_speed(
        dataset,
        results,
        baseline_configuration="baseline",
        candidate_configuration="candidate",
        repetitions=3,
    )
    comparison["quality_by_configuration"]["baseline"]["precision"] = 0.2

    result = evaluate_speed_candidate(comparison, _speed_policy(dataset, results))

    assert result["state"] == "blocked"
    assert any("baseline" in reason for reason in result["reasons"])


def _exercise_speed_comparison_cli(tmp_path, benchmark_inputs):
    dataset_data, result_data = benchmark_inputs
    dataset_data["kind"] = "curated"
    runs = []
    for configuration, times in (("baseline", (100, 110)), ("candidate", (80, 90))):
        for attempt, elapsed_ms in enumerate(times, 1):
            run = deepcopy(result_data["runs"][0])
            run.update(
                run_id=f"{configuration}-{attempt}",
                configuration_id=configuration,
                attempt=attempt,
                elapsed_ms=elapsed_ms,
            )
            runs.append(run)
    dataset_path = tmp_path / "dataset.json"
    results_path = tmp_path / "results.json"
    output_path = tmp_path / "comparison.json"
    policy_path = tmp_path / "policy.json"
    dataset_path.write_text(json.dumps(dataset_data), encoding="utf-8")
    results_payload = {
        "schema_version": 3,
        "runs": runs,
        "schedule": {
            "schedule_id": "schedule-cli",
            "strategy": "interleaved",
            "run_order": [
                "baseline-1",
                "candidate-1",
                "baseline-2",
                "candidate-2",
            ],
        },
    }
    results_path.write_text(json.dumps(results_payload), encoding="utf-8")
    score = score_benchmark(
        BenchmarkDataset.model_validate(dataset_data),
        BenchmarkResults.model_validate(results_payload),
    )
    policy = BenchmarkQualityGatePolicy(
        policy_id="cli-speed-policy",
        dataset_kind=score["dataset_kind"],
        dataset_sha256=score["dataset_sha256"],
        min_cases=1,
        min_gold_findings=1,
        min_precision=0,
        min_recall=0,
        max_missed_high_impact=0,
        min_speed_repetitions=2,
    )
    policy_path.write_text(policy.model_dump_json(), encoding="utf-8")

    assert benchmark_main(
        [
            "compare",
            "--dataset",
            str(dataset_path),
            "--results",
            str(results_path),
            "--baseline",
            "baseline",
            "--candidate",
            "candidate",
            "--repetitions",
            "2",
            "--output",
            str(output_path),
        ]
    ) == 0
    comparison = json.loads(output_path.read_text(encoding="utf-8"))
    assert comparison["aggregate"]["median_improvement_percent"] == pytest.approx(19.0476190476)
    assert comparison["eligibility"] == "exploratory"

    assert benchmark_main(
        [
            "compare",
            "--dataset",
            str(dataset_path),
            "--results",
            str(results_path),
            "--baseline",
            "baseline",
            "--candidate",
            "candidate",
            "--repetitions",
            "2",
            "--quality-policy",
            str(policy_path),
            "--output",
            str(output_path),
        ]
    ) == 0
    gated = json.loads(output_path.read_text(encoding="utf-8"))
    assert gated["eligibility"]["state"] == "passed"


def test_speed_comparison_cli_applies_quality_policy_and_reports_exploratory_without_it(
    tmp_path, benchmark_inputs
):
    _exercise_speed_comparison_cli(tmp_path, benchmark_inputs)


def test_speed_comparison_cli_writes_artifact(tmp_path, benchmark_inputs):
    _exercise_speed_comparison_cli(tmp_path, benchmark_inputs)


def test_benchmark_usage_is_derived_from_complete_profile_attempt_ledger(benchmark_inputs):
    from datetime import UTC, datetime

    from qa_orchestrator.benchmark import benchmark_run_with_attempt_usage
    from qa_orchestrator.contracts import ProfileAttemptMetrics, ReviewAgent

    _, data = benchmark_inputs
    run = BenchmarkResults.model_validate(data).runs[0]
    now = datetime.now(UTC)
    def entry(attempt, outcome, input_tokens, output_tokens):
        return ProfileAttemptMetrics(
            profile=ReviewAgent.CODE_REVIEWER, attempt=attempt, outcome=outcome,
            review_head_sha=run.head_sha, review_diff_sha256=run.diff_sha256,
            started_at=now, completed_at=now, queue_seconds=0, executor_seconds=0,
            input_tokens=input_tokens, output_tokens=output_tokens,
        )
    failed = entry(1, "retryable_error", 40, 5)
    completed = entry(2, "completed", 100, 20)
    measured = benchmark_run_with_attempt_usage(
        run, (failed, completed), expected_profiles=(ReviewAgent.CODE_REVIEWER,),
    )
    assert (measured.input_tokens, measured.output_tokens, measured.token_usage_scope) == (
        140, 25, "includes_retries"
    )
    unknown = benchmark_run_with_attempt_usage(
        run, (entry(1, "retryable_error", None, 5), completed),
        expected_profiles=(ReviewAgent.CODE_REVIEWER,),
    )
    assert unknown.input_tokens is None and unknown.output_tokens == 25
    with pytest.raises(ValueError, match="contiguous"):
        benchmark_run_with_attempt_usage(run, (completed,), expected_profiles=(ReviewAgent.CODE_REVIEWER,))
    with pytest.raises(ValueError, match="cover every"):
        benchmark_run_with_attempt_usage(run, (), expected_profiles=(ReviewAgent.CODE_REVIEWER,))
