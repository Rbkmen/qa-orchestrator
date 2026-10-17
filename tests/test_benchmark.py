from copy import deepcopy

import pytest
from pydantic import ValidationError

from qa_orchestrator.benchmark import (
    BenchmarkDataset,
    BenchmarkResults,
    benchmark_main,
    compare_speed,
    import_reviewbench,
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
                        "category": "correctness",
                        "path": "src/api.py",
                        "start_line": i,
                        "end_line": i,
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


def test_scoring_uses_explicit_verdicts_and_counts_duplicate_noise(benchmark_inputs):
    dataset, results = benchmark_inputs
    score = score_benchmark(
        BenchmarkDataset.model_validate(dataset), BenchmarkResults.model_validate(results)
    )
    run = score["runs"][0]
    assert run["precision"] == 1 / 3
    assert run["recall"] == 0.5
    assert run["matched_gold"] == 1
    assert run["missed_high_impact"] == 0
    assert run["confidence"]["high"]["valid"] == 1
    assert run["confidence"]["medium"]["valid"] == 0
    assert score["configurations"]["baseline"]["input_tokens"] is None


def test_scores_reject_different_source_snapshot(benchmark_inputs):
    dataset, results = benchmark_inputs
    results["runs"][0]["head_sha"] = "5" * 40
    with pytest.raises(ValueError, match="pinned dataset case"):
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
                elapsed_ms=elapsed_ms,
            )
            runs.append(run)
    dataset = BenchmarkDataset.model_validate(dataset_data)
    results = BenchmarkResults.model_validate({"runs": runs})
    comparison = compare_speed(
        dataset,
        results,
        baseline_configuration="baseline",
        candidate_configuration="candidate",
        repetitions=3,
    )
    assert comparison["cases"][0]["baseline_median_ms"] == 600
    assert comparison["cases"][0]["candidate_median_ms"] == 500
    assert comparison["aggregate"]["median_speedup_ratio"] == 1.2
    assert comparison["quality_by_configuration"]["baseline"]["precision"] == 1 / 3

    changed = deepcopy(runs)
    for run in changed[-3:]:
        run["model"] = "other-model"
    with pytest.raises(ValueError, match="same model"):
        compare_speed(
            dataset,
            BenchmarkResults.model_validate({"runs": changed}),
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
            BenchmarkResults.model_validate({"runs": changed_packets}),
            baseline_configuration="baseline",
            candidate_configuration="candidate",
            repetitions=3,
        )


def test_speed_comparison_cli_writes_artifact(tmp_path, benchmark_inputs):
    import json

    dataset_data, result_data = benchmark_inputs
    runs = []
    for configuration, times in (("baseline", (100, 110)), ("candidate", (80, 90))):
        for attempt, elapsed_ms in enumerate(times, 1):
            run = deepcopy(result_data["runs"][0])
            run.update(
                run_id=f"{configuration}-{attempt}",
                configuration_id=configuration,
                elapsed_ms=elapsed_ms,
            )
            runs.append(run)
    dataset_path = tmp_path / "dataset.json"
    results_path = tmp_path / "results.json"
    output_path = tmp_path / "comparison.json"
    dataset_path.write_text(json.dumps(dataset_data), encoding="utf-8")
    results_path.write_text(json.dumps({"runs": runs}), encoding="utf-8")

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
