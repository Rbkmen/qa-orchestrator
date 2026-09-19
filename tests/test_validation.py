import pytest

from qa_router_mcp.contracts import DraftEnvelope, DraftKind
from qa_router_mcp.validation import (
    normalize_test_case_draft,
    repair_instruction,
    requested_case_count,
    validate_generated_draft,
)


def test_nested_json_test_cases_are_normalized_to_plain_text():
    draft = '[{"Coverage ID":"COV-A","Title":"A","Preconditions":["Ready"],"Steps":["Act","Verify"],"Expected Result":"Success"}]'

    normalized = normalize_test_case_draft(draft)

    assert normalized == (
        "Coverage ID: COV-A\nTitle: A\nPreconditions: Ready\n"
        "Steps: 1. Act 2. Verify\nExpected Result: Success"
    )


def test_unknown_nested_test_case_shape_is_not_normalized():
    draft = '[{"title":"Missing required fields"}]'

    assert normalize_test_case_draft(draft) == draft


def test_requested_case_count_supports_english_and_russian():
    assert requested_case_count("Draft three test cases") == 3
    assert requested_case_count("Сделай 4 кейса") == 4
    assert requested_case_count("Сделай три тест-кейса") == 3
    assert requested_case_count("Draft three focused smoke test cases") == 3
    assert requested_case_count("Draft 12 focused test cases") == 12
    assert requested_case_count("Draft a focused case") == 1


@pytest.mark.parametrize("field", ["title", "preconditions", "steps", "expected_result"])
def test_empty_case_field_is_rejected(field):
    labels = {
        "title": "Title",
        "preconditions": "Preconditions",
        "steps": "Steps",
        "expected_result": "Expected Result",
    }
    draft = "\n".join(
        f"{label}: " + ("" if key == field else "Content") for key, label in labels.items()
    )
    assert validate_generated_draft(
        DraftKind.TEST_CASES, "Draft one case", DraftEnvelope(draft=draft)
    ) == [f"test_cases_empty_{field}"]


def test_multiline_case_fields_are_valid():
    draft = "Title: Case\nPreconditions:\nReady\nSteps:\n1. Act\nExpected Result:\nSuccess"
    assert (
        validate_generated_draft(DraftKind.TEST_CASES, "Draft one case", DraftEnvelope(draft=draft))
        == []
    )


def test_short_explanation_over_120_words_is_rejected():
    result = DraftEnvelope(draft="word " * 121, unverified=[])

    assert validate_generated_draft(
        DraftKind.SHORT_EXPLANATION,
        "Explain retries",
        result,
    ) == ["short_explanation_too_long"]


def test_source_bound_log_summary_may_have_no_unverified_claims():
    result = DraftEnvelope(draft="Visible signatures: timeout (2)", unverified=[])

    assert (
        validate_generated_draft(
            DraftKind.LOG_SUMMARY,
            "ERROR timeout\nERROR timeout",
            result,
        )
        == []
    )


def test_translation_requires_every_preserved_term():
    result = DraftEnvelope(draft="Кнопка оформления отключена.", unverified=[])

    assert validate_generated_draft(
        DraftKind.TRANSLATION,
        "TARGET_LANGUAGE: Russian\nTEXT: The checkout button is disabled.",
        result,
        preserve_terms=("checkout",),
    ) == ["translation_missing_preserve_term"]


def test_review_checklist_requires_all_contract_sections():
    result = DraftEnvelope(
        draft="Scope: changed checkout behavior\nChecklist: inspect it",
        unverified=[],
    )

    assert validate_generated_draft(
        DraftKind.REVIEW_CHECKLIST,
        "Evidence packet",
        result,
    ) == [
        "review_missing_candidate_coverage_gaps",
        "review_missing_positive_observations",
        "review_missing_unverified",
    ]


def test_review_checklist_rejects_external_write():
    result = DraftEnvelope(
        draft=(
            "Scope: changed behavior\nChecklist: inspect\nCandidate Coverage Gaps: none\n"
            "Positive Observations: none\nUnverified: git push is not allowed"
        ),
        unverified=[],
    )

    assert validate_generated_draft(DraftKind.REVIEW_CHECKLIST, "Evidence", result) == [
        "review_external_write"
    ]


def test_review_checklist_rejects_decision_claim():
    result = DraftEnvelope(
        draft=(
            "Scope: changed behavior\nChecklist: inspect\nCandidate Coverage Gaps: none\n"
            "Positive Observations: none\nSeverity: high\nUnverified: none"
        ),
        unverified=[],
    )

    assert validate_generated_draft(DraftKind.REVIEW_CHECKLIST, "Evidence", result) == [
        "review_unsupported_decision"
    ]


def test_numbered_test_case_heading_counts_as_title():
    result = DraftEnvelope(
        draft=(
            "Test Case 1: Saved card\nPreconditions: Ready\nSteps: 1. Act\n"
            "Expected Result: Success\n\n"
            "Test Case 2: New card\nPreconditions: Ready\nSteps: 1. Act\n"
            "Expected Result: Success"
        ),
        unverified=[],
    )

    assert (
        validate_generated_draft(
            DraftKind.TEST_CASES,
            "Draft two test cases",
            result,
        )
        == []
    )


def test_every_test_case_block_requires_all_fields():
    result = DraftEnvelope(
        draft=(
            "Title: Complete case\nPreconditions: First\nPreconditions: Duplicate\n"
            "Steps: 1. Act\nSteps: 2. Act\nExpected Result: First\n"
            "Expected Result: Duplicate\n\nTitle: Incomplete case"
        ),
        unverified=[],
    )

    assert validate_generated_draft(
        DraftKind.TEST_CASES,
        "Draft two test cases",
        result,
    ) == [
        "test_cases_missing_preconditions",
        "test_cases_missing_steps",
        "test_cases_missing_expected_result",
    ]


def test_numbered_heading_and_title_field_do_not_count_as_two_cases():
    result = DraftEnvelope(
        draft=(
            "Test Case 1: Checkout\n\nTitle: Valid guest checkout\n"
            "Preconditions: Ready\nSteps: 1. Submit\nExpected Result: Success"
        ),
        unverified=[],
    )

    assert validate_generated_draft(
        DraftKind.TEST_CASES,
        "Draft two test cases",
        result,
    ) == ["test_cases_missing_title"]


def test_extra_test_case_is_rejected():
    result = DraftEnvelope(
        draft="\n\n".join(
            f"Title: Case {number}\nPreconditions: Ready\nSteps: 1. Act\n"
            f"Expected Result: Result {number}"
            for number in range(1, 4)
        ),
        unverified=[],
    )

    assert validate_generated_draft(
        DraftKind.TEST_CASES,
        "Draft exactly 2 test cases",
        result,
    ) == ["test_cases_wrong_count"]


def test_test_cases_require_exact_coverage_id_bijection():
    result = DraftEnvelope(
        draft=(
            "Coverage ID: COV-A\nTitle: A\nPreconditions: Ready\nSteps: 1. Act\n"
            "Expected Result: A\n\nCoverage ID: COV-A\nTitle: B\nPreconditions: Ready\n"
            "Steps: 1. Act\nExpected Result: B"
        ),
        unverified=[],
    )
    request = (
        "Draft exactly 2 test cases.\nAPPROVED_COVERAGE_MAP:\n"
        "Coverage ID: COV-A\nPurpose: A\n\nCoverage ID: COV-B\nPurpose: B"
    )

    assert validate_generated_draft(DraftKind.TEST_CASES, request, result) == [
        "test_cases_duplicate_coverage_id",
        "test_cases_missing_coverage_id",
    ]


def test_markdown_test_case_heading_without_colon_counts_as_title():
    result = DraftEnvelope(
        draft=("## Test Case 1\nPreconditions: Ready\nSteps: 1. Act\nExpected Result: Success"),
        unverified=[],
    )

    assert validate_generated_draft(DraftKind.TEST_CASES, "Draft one case", result) == []


def test_repair_instruction_uses_actionable_language():
    instruction = repair_instruction(["test_cases_missing_steps"])

    assert "include Steps in every test case" in instruction
    assert "test_cases_missing_steps" not in instruction


def test_automation_skeleton_cannot_contain_external_write():
    result = DraftEnvelope(
        draft="Run checks and then git push origin branch",
        unverified=["Review"],
    )

    assert validate_generated_draft(
        DraftKind.AUTOMATION_SKELETON,
        "Draft a skeleton",
        result,
    ) == ["automation_external_write"]


def test_automation_skeleton_cannot_contain_git_commit():
    result = DraftEnvelope(
        draft="git commit -m 'generated change'",
        unverified=["Review"],
    )

    assert validate_generated_draft(
        DraftKind.AUTOMATION_SKELETON,
        "Draft a skeleton",
        result,
    ) == ["automation_external_write"]


@pytest.mark.parametrize(
    "draft",
    [
        "curl -X POST https://example.test",
        "glab mr create --title generated",
        "kubectl apply -f deployment.yaml",
        "rm -rf /tmp/generated",
        "Path('result').write_text(content)",
        "Path('result').unlink()",
        "Path('result').touch()",
        "Path(path)\\\n.touch()",
        "path.touch()",
        "Path(path).open(mode)",
        "pathlib.Path(path).open(mode)",
        "path.open(mode)",
        "shutil.rmtree('generated')",
        "os.remove('result.txt')",
        "os.system(command)",
        "os.popen(command)",
        "os.execv(path, args)",
        "subprocess.run(['git', 'push', 'origin', 'main'])",
        "client.post(url, json=payload)",
        "client.request(url, method='DELETE')",
        "requests.request(method_name, url)",
        "httpx.Client().post(url)",
        "requests.Session().post(url)",
        "open('result.txt', 'w')",
        "fetch(url, {method: 'POST'})",
        "requests.patch(url, json=payload)",
        "printf '%s' result > output.txt",
        "command | tee output.txt",
    ],
)
def test_automation_skeleton_rejects_common_external_and_file_writes(draft):
    result = DraftEnvelope(draft=draft, unverified=["Review"])

    assert validate_generated_draft(
        DraftKind.AUTOMATION_SKELETON,
        "Draft a skeleton",
        result,
    ) == ["automation_external_write"]


def test_automation_skeleton_allows_non_mutating_string_methods():
    result = DraftEnvelope(draft="normalized = name.replace('-', '_')", unverified=[])

    assert (
        validate_generated_draft(
            DraftKind.AUTOMATION_SKELETON,
            "Draft a skeleton",
            result,
        )
        == []
    )


@pytest.mark.parametrize(
    "draft",
    [
        "Path('result').open('r')",
        "file.open('r')",
        "unittest.mock.patch('target')",
        "items.remove(value)",
    ],
)
def test_automation_skeleton_allows_safe_python_calls(draft):
    result = DraftEnvelope(draft=draft, unverified=[])

    assert (
        validate_generated_draft(
            DraftKind.AUTOMATION_SKELETON,
            "Draft a skeleton",
            result,
        )
        == []
    )


def test_coverage_ids_can_be_supplied_separately_from_examples():
    result = DraftEnvelope(
        draft=(
            "Coverage ID: COV-REAL\nTitle: Case\nPreconditions: Ready\n"
            "Steps: 1. Act\nExpected Result: Success"
        ),
        unverified=[],
    )

    assert (
        validate_generated_draft(
            DraftKind.TEST_CASES,
            "Draft one test case\nEXAMPLES:\nCoverage ID: COV-EXAMPLE",
            result,
            expected_coverage_ids=("COV-REAL",),
        )
        == []
    )


@pytest.mark.parametrize("value", [None, 12, {"nested": "value"}, [None]])
def test_nested_test_case_invalid_values_are_not_normalized(value):
    import json

    draft = json.dumps(
        {
            "Coverage ID": "COV-A",
            "Title": "Case",
            "Preconditions": value,
            "Steps": ["Act"],
            "Expected Result": "Success",
        }
    )
    assert normalize_test_case_draft(draft) == draft
