import hashlib
import json
import subprocess
from copy import deepcopy

import pytest
from pydantic import ValidationError

from qa_orchestrator.benchmark import (
    BenchmarkDataset,
    BenchmarkFailure,
    BenchmarkResults,
    benchmark_main,
    check_diff_budget,
    compare_speed,
    evaluate_quality_gate,
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
                elapsed_ms=elapsed_ms,
                input_tokens=1000 if configuration == "baseline" else 800,
                output_tokens=100 if configuration == "baseline" else 120,
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
    assert comparison["cases"][0]["baseline_median_input_tokens"] == 1000
    assert comparison["cases"][0]["candidate_median_input_tokens"] == 800
    assert comparison["cases"][0]["input_token_savings_percent"] == 20
    assert comparison["aggregate"]["median_speedup_ratio"] == 1.2
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
