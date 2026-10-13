from copy import deepcopy

import pytest
from pydantic import ValidationError

from qa_orchestrator.benchmark import (
    BenchmarkDataset,
    BenchmarkResults,
    benchmark_main,
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
