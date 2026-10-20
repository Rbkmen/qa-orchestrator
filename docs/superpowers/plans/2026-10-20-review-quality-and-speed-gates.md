# Review Quality and Speed Gates Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox syntax for tracking.

**Goal:** Make review-quality gates reproducible and compare orchestration speed with explicit scheduling, tail-latency, queue, token, and quality evidence.

**Architecture:** Add a named policy with a required dataset digest and explicit quality thresholds while preserving the current ad-hoc experimental gate. Extend benchmark runs with optional scheduling and queue metadata; compare p95 and median metrics, and block speed eligibility unless order and quality evidence meet the policy.

**Tech Stack:** Python 3.12, Pydantic 2, pytest; no new runtime dependencies.

**Spec:** docs/superpowers/specs/2026-10-20-review-quality-and-speed-gates-design.md

## Global Constraints

- Never invent or activate an approved corpus digest; no active production policy ships until the corpus is reviewed and approved.
- Keep the existing explicit-threshold quality gate available as experimental mode.
- Require exact dataset kind and digest for a named quality policy.
- Missing scheduling or queue data is unknown, never zero or randomized.
- Do not claim stable p95 from too few repetitions; the policy sets its sample minimum.
- A speed candidate must pass the same pinned quality policy as its baseline.
- Benchmark execution remains opt-in and does not contact an external model service by default.
- Use CodeGraph before changing these modules and after each stage to inspect affected callers and serialization paths.

## Review Focus

- Correct metrics on incomplete or partially adjudicated data: gate is blocked, not passed.
- Dataset digest or kind mismatch: gate is blocked with the exact mismatch reason.
- Missing samples or one failed run: quality/speed status cannot pass.
- Paired runs with different prompts, model, packets, or adjudicator: comparison rejects them.
- Missing or malformed run order cannot satisfy an order-controlled speed gate; missing queue wait stays unknown and is reported with its sample count.

---

### Task 1: Add a digest-pinned quality policy

**Files:**
- Modify: src/qa_orchestrator/benchmark.py
- Test: tests/test_benchmark.py

**Interfaces:**
- Add BenchmarkQualityGatePolicy with policy_id, dataset_kind, dataset_sha256, min_cases, min_gold_findings, min_precision, min_recall, max_missed_high_impact, optional evidence/location/stratum floors, and optional min_speed_repetitions (quality-only policies do not need a speed sample minimum).
- Implement evaluate_quality_policy(score: Mapping[str, object], policy: BenchmarkQualityGatePolicy, *, configuration_id: str) -> dict with state passed, failed, or blocked.
- A policy requires exact dataset kind and SHA plus minimum case and gold-finding coverage; it does not require a checked-in approved digest by itself.
- Add aggregate gold_count to scored configuration output so the policy can verify adjudicated gold coverage without inferring it from recall.

- [ ] **Step 1: Write failing policy and state tests**

~~~python
def test_quality_policy_blocks_missing_or_mismatched_dataset_digest(score):
    policy = quality_policy(dataset_sha256="a" * 64)
    result = evaluate_quality_policy(score_with_digest("b" * 64), policy, configuration_id="candidate")
    assert result["state"] == "blocked"
    assert "dataset" in " ".join(result["reasons"])

def test_quality_policy_separates_threshold_failure_from_blocked(score):
    result = evaluate_quality_policy(score_below_recall(score), valid_policy(), configuration_id="candidate")
    assert result["state"] == "failed"

def test_quality_policy_blocks_incomplete_coverage(score):
    result = evaluate_quality_policy(score_with_missing_gold(score), valid_policy(), configuration_id="candidate")
    assert result["state"] == "blocked"

def test_quality_policy_blocks_any_failed_attempt(score):
    result = evaluate_quality_policy(score_with_failed_attempt(score), valid_policy(), configuration_id="candidate")
    assert result["state"] == "blocked"

def test_quality_policy_reports_each_threshold_measurement(score):
    result = evaluate_quality_policy(score, valid_policy(), configuration_id="candidate")
    assert result["observed"]["recall"] == score["configurations"]["candidate"]["recall"]
    assert result["thresholds"]["min_gold_findings"] == valid_policy().min_gold_findings
~~~

The examples use small builders added inside `tests/test_benchmark.py` for a valid policy, scored payload, digest mismatch, threshold miss, incomplete coverage, and one failed attempt. Build them from the existing `benchmark_inputs` fixture; do not add shared production fixtures.

- [ ] **Step 2: Run the focused tests and confirm they fail**

Run: uv run pytest tests/test_benchmark.py::test_quality_policy_blocks_missing_or_mismatched_dataset_digest tests/test_benchmark.py::test_quality_policy_separates_threshold_failure_from_blocked tests/test_benchmark.py::test_quality_policy_blocks_incomplete_coverage tests/test_benchmark.py::test_quality_policy_blocks_any_failed_attempt tests/test_benchmark.py::test_quality_policy_reports_each_threshold_measurement -q

Expected: named policies and three-state gate results are not defined.

- [ ] **Step 3: Implement policy validation and evaluation**

Add gold_count to score_benchmark configuration aggregates. Reuse evaluate_quality_gate for measured thresholds. Classify missing identity, insufficient case/gold coverage, incomplete data, or missing required metrics as blocked and measured threshold misses as failed. Return policy ID, dataset identity, each threshold, each observed value, and reasons. Keep explicit-threshold mode unchanged and mark its output experimental.

- [ ] **Step 4: Re-run focused and existing quality-gate tests**

Run: uv run pytest tests/test_benchmark.py::test_quality_policy_blocks_missing_or_mismatched_dataset_digest tests/test_benchmark.py::test_quality_policy_separates_threshold_failure_from_blocked tests/test_benchmark.py::test_quality_policy_blocks_incomplete_coverage tests/test_benchmark.py::test_quality_policy_blocks_any_failed_attempt tests/test_benchmark.py::test_quality_policy_reports_each_threshold_measurement tests/test_benchmark.py::test_quality_gate_can_pin_an_approved_adjudicated_dataset tests/test_benchmark.py::test_quality_gate_fails_incomplete_or_unresolved_runs -q

Expected: new policy behavior passes and legacy gate behavior is preserved.

### Task 2: Load named quality policies from the CLI

**Files:**
- Modify: src/qa_orchestrator/benchmark.py
- Test: tests/test_benchmark.py

**Interfaces:**
- Add --policy PATH to benchmark quality-gate. Keep --configuration required in both modes; policy mode and explicit-threshold mode are mutually exclusive. Make threshold arguments optional at argparse construction, then require them only in experimental mode and reject mixing them with --policy.
- No default production policy file is added without an approved dataset digest.

- [ ] **Step 1: Write the CLI policy test**

~~~python
def test_quality_gate_cli_loads_digest_pinned_policy(tmp_path, benchmark_inputs):
    dataset_data, results_data = benchmark_inputs
    score = score_benchmark(BenchmarkDataset.model_validate(dataset_data), BenchmarkResults.model_validate(results_data))
    score_file = tmp_path / "score.json"
    policy_file = tmp_path / "policy.json"
    output = tmp_path / "gate.json"
    score_file.write_text(json.dumps(score), encoding="utf-8")
    policy = BenchmarkQualityGatePolicy(policy_id="test", dataset_kind=score["dataset_kind"],
                                        dataset_sha256=score["dataset_sha256"], min_cases=1,
                                        min_gold_findings=1, min_precision=0, min_recall=0,
                                        max_missed_high_impact=0)
    policy_file.write_text(policy.model_dump_json(), encoding="utf-8")
    result = benchmark_main(["quality-gate", "--score", str(score_file), "--configuration", "baseline",
                             "--policy", str(policy_file), "--output", str(output)])
    assert json.loads(output.read_text())["state"] == "passed"
    assert result == 0

def test_quality_gate_cli_rejects_mixed_policy_and_experimental_thresholds(tmp_path):
    with pytest.raises(SystemExit):
        benchmark_main(["quality-gate", "--score", str(tmp_path / "score.json"),
                        "--configuration", "candidate", "--policy", str(tmp_path / "policy.json"),
                        "--min-recall", "0.9"])
~~~

- [ ] **Step 2: Run the focused test and confirm it fails**

Run: uv run pytest tests/test_benchmark.py::test_quality_gate_cli_loads_digest_pinned_policy -q

Expected: --policy is not a recognized argument.

- [ ] **Step 3: Implement policy-file parsing and exit behavior**

Validate policy JSON with Pydantic. Return zero only for passed; use non-zero results for failed and blocked and preserve a machine-readable reason in output.

- [ ] **Step 4: Re-run CLI policy and existing CLI gate tests**

Run: uv run pytest tests/test_benchmark.py::test_quality_gate_cli_loads_digest_pinned_policy tests/test_benchmark.py::test_quality_gate_cli_rejects_mixed_policy_and_experimental_thresholds tests/test_benchmark.py::test_quality_gate_cli_accepts_required_dataset_identity tests/test_benchmark.py::test_quality_gate_cli_returns_nonzero_when_thresholds_fail -q

Expected: both policy and experimental CLI paths work.

### Task 3: Record schedule and queue measurements

**Files:**
- Modify: src/qa_orchestrator/benchmark.py
- Test: tests/test_benchmark.py

**Interfaces:**
- Add optional BenchmarkRun.queue_wait_ms.
- Add BenchmarkSchedule with schedule_id, strategy (interleaved or randomized), and run_order containing every successful BenchmarkRun.run_id exactly once; failed attempts remain in the failure ledger and are not scheduled runs. Preserve omission of absent optional fields when serializing legacy artifacts.
- BenchmarkResults schema version 3 is required when schedule or queue metadata is supplied; versions 1 and 2 remain readable.

- [ ] **Step 1: Write failing measurement-validation tests**

~~~python
def test_benchmark_schedule_requires_unique_complete_run_order():
    with pytest.raises(ValidationError):
        BenchmarkResults.model_validate(results_with_duplicate_schedule_run_id())

def test_benchmark_run_accepts_nonnegative_queue_wait():
    assert benchmark_run(queue_wait_ms=120).queue_wait_ms == 120

def test_legacy_results_wire_format_omits_absent_schedule_and_queue_fields():
    payload = legacy_results().model_dump(mode="json")
    assert "schedule" not in payload
    assert "queue_wait_ms" not in payload["runs"][0]
~~~

- [ ] **Step 2: Run the focused tests and confirm they fail**

Run: uv run pytest tests/test_benchmark.py::test_benchmark_schedule_requires_unique_complete_run_order tests/test_benchmark.py::test_benchmark_run_accepts_nonnegative_queue_wait tests/test_benchmark.py::test_legacy_results_wire_format_omits_absent_schedule_and_queue_fields -q

Expected: schedule and queue fields are not defined.

- [ ] **Step 3: Implement schedule and queue contracts**

Validate schema-version compatibility, nonnegative queue time, unique run IDs in run_order, and exact coverage of all successful result run IDs. Interleaving is checked for the selected configuration pair by compare_speed, where its meaning is unambiguous.

- [ ] **Step 4: Re-run focused and legacy-wire tests**

Run: uv run pytest tests/test_benchmark.py::test_benchmark_schedule_requires_unique_complete_run_order tests/test_benchmark.py::test_benchmark_run_accepts_nonnegative_queue_wait tests/test_benchmark.py::test_legacy_results_wire_format_omits_task_context_hash tests/test_benchmark.py::test_legacy_results_wire_format_omits_absent_schedule_and_queue_fields -q

Expected: schedule validation passes and legacy serialization remains stable.

### Task 4: Report tail latency and gate speed eligibility

**Files:**
- Modify: src/qa_orchestrator/benchmark.py
- Test: tests/test_benchmark.py

**Interfaces:**
- Extend compare_speed output with per-case and aggregate median/p95 elapsed_ms, queue-wait distributions with observed/missing counts, sample counts, and a schedule assessment (`interleaved_validated`, `randomized_host_declared`, `unknown`, or `invalid`). Do not label host-declared randomization as independently verified.
- Implement evaluate_speed_candidate(comparison: Mapping[str, object], policy: BenchmarkQualityGatePolicy) -> dict.
- Add optional --quality-policy PATH to benchmark compare; without it the result remains exploratory.

- [ ] **Step 1: Write failing speed comparison and eligibility tests**

~~~python
def test_speed_comparison_reports_p95_queue_and_schedule(benchmark_inputs):
    comparison = compare_speed(dataset, scheduled_results, baseline_configuration="base",
                               candidate_configuration="candidate", repetitions=3)
    assert comparison["schedule_assessment"] == "interleaved_validated"
    assert comparison["aggregate"]["candidate_p95_elapsed_ms"] == expected_p95
    assert comparison["aggregate"]["candidate_queue_wait_ms"]["p95"] == expected_queue_p95
    assert comparison["aggregate"]["candidate_queue_wait_ms"]["missing_count"] == expected_missing_queue_samples

def test_compare_speed_marks_non_alternating_interleaved_order_invalid(benchmark_inputs):
    comparison = compare_speed(dataset, non_alternating_results, baseline_configuration="base",
                               candidate_configuration="candidate", repetitions=3)
    assert comparison["schedule_assessment"] == "invalid"

def test_speed_candidate_fails_when_candidate_quality_policy_fails(benchmark_inputs):
    result = evaluate_speed_candidate(comparison_with_candidate_quality_regression, policy)
    assert result["state"] == "failed"

def test_speed_candidate_is_blocked_without_order_or_minimum_samples(benchmark_inputs):
    result = evaluate_speed_candidate(comparison_without_controlled_schedule, policy)
    assert result["state"] == "blocked"

def test_speed_candidate_is_blocked_when_baseline_fails_quality_policy(benchmark_inputs):
    result = evaluate_speed_candidate(comparison_with_baseline_quality_failure, policy)
    assert result["state"] == "blocked"

def test_speed_comparison_cli_applies_quality_policy_and_reports_exploratory_without_it(benchmark_inputs, tmp_path):
    exploratory = run_compare_cli(benchmark_inputs, tmp_path / "exploratory.json")
    assert exploratory["eligibility"] == "exploratory"
    gated = run_compare_cli(benchmark_inputs, tmp_path / "gated.json", quality_policy=valid_policy())
    assert gated["eligibility"]["state"] in {"passed", "failed", "blocked"}
~~~

- [ ] **Step 2: Run the focused tests and confirm they fail**

Run: uv run pytest tests/test_benchmark.py::test_speed_comparison_reports_p95_queue_and_schedule tests/test_benchmark.py::test_compare_speed_marks_non_alternating_interleaved_order_invalid tests/test_benchmark.py::test_speed_candidate_fails_when_candidate_quality_policy_fails tests/test_benchmark.py::test_speed_candidate_is_blocked_without_order_or_minimum_samples tests/test_benchmark.py::test_speed_candidate_is_blocked_when_baseline_fails_quality_policy -q

Expected: comparison lacks schedule, queue, p95, and policy eligibility fields.

- [ ] **Step 3: Implement comparison statistics and policy checks**

Use the benchmark's existing p95 convention. Do not treat missing queue values as zero; report observed and missing counts, with null statistics when no queue values exist. Require a complete recorded schedule and the policy's min_speed_repetitions for controlled eligibility; validate that interleaved order alternates configurations, while randomized order remains explicitly host-declared. Missing queue measurements are reported as unavailable but do not by themselves block a latency comparison. Evaluate both baseline and candidate against the same dataset policy, including every configured quality metric. A failed candidate is failed; an unqualified baseline or missing/incomplete evidence is blocked.

- [ ] **Step 4: Run focused and existing speed-comparison tests**

Run: uv run pytest tests/test_benchmark.py::test_speed_comparison_reports_p95_queue_and_schedule tests/test_benchmark.py::test_compare_speed_marks_non_alternating_interleaved_order_invalid tests/test_benchmark.py::test_speed_candidate_fails_when_candidate_quality_policy_fails tests/test_benchmark.py::test_speed_candidate_is_blocked_without_order_or_minimum_samples tests/test_benchmark.py::test_speed_candidate_is_blocked_when_baseline_fails_quality_policy tests/test_benchmark.py::test_speed_comparison_cli_applies_quality_policy_and_reports_exploratory_without_it tests/test_benchmark.py::test_speed_comparison_requires_same_model_packet_and_complete_repetitions tests/test_benchmark.py::test_speed_comparison_cli_writes_artifact -q

Expected: same-input safeguards remain intact; uncontrolled or low-sample comparisons cannot pass the speed gate.

### Task 5: Document experimental and digest-pinned gate usage

**Files:**
- Modify: README.md

- [ ] **Step 1: Document how to pin a reviewed dataset and interpret speed output**

Explain experimental explicit-threshold mode, named digest-pinned policy mode, blocked versus failed, sample counts, and host-reported scheduling/queue limitations. Do not add an unapproved dataset digest.

- [ ] **Step 2: Run documentation whitespace and benchmark checks**

Run: git diff --check -- README.md
Run: uv run pytest tests/test_benchmark.py -q

Expected: README introduces no whitespace errors and all benchmark tests pass.

- [ ] **Step 3: Run the full repository suite after all three plans are implemented**

Run: uv run pytest -q

Expected: all existing and new tests pass; report unavailable external or environment-dependent checks separately.
