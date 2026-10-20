import hashlib
from datetime import UTC, datetime, timedelta

import pytest

from qa_orchestrator.contracts import ReviewVerificationRecord
from qa_orchestrator.verification import verify_provider_run

_STARTED_AT = datetime(2026, 10, 20, 12, 0, tzinfo=UTC)
_COMPLETED_AT = datetime(2026, 10, 20, 12, 5, tzinfo=UTC)
_SCOPE = "pytest tests/test_service.py"
_SCOPE_SHA256 = hashlib.sha256(_SCOPE.encode("utf-8")).hexdigest()


@pytest.fixture
def record():
    return ReviewVerificationRecord(
        verification_id="V-01",
        check_type="ci",
        scenario="unit test suite",
        status="passed",
        head_sha="1" * 40,
        environment="Linux, Python 3.13",
        source="ci_provider",
        source_ref="workflow 123 / job 456",
        scope=_SCOPE,
        scope_sha256=_SCOPE_SHA256,
        run_id="github-actions:123:456",
        started_at=_STARTED_AT,
        completed_at=_COMPLETED_AT,
    )


def provider_response(record, **overrides):
    response = {
        "run_id": record.run_id,
        "head_sha": record.head_sha,
        "conclusion": record.status,
        "scope_sha256": record.scope_sha256,
        "started_at": record.started_at,
        "completed_at": record.completed_at,
    }
    return {**response, **overrides}


def test_verify_provider_run_rejects_wrong_sha(record):
    with pytest.raises(ValueError, match="head SHA"):
        verify_provider_run(
            record,
            provider_response(record, head_sha="f" * 40),
            verified_at=_COMPLETED_AT + timedelta(seconds=1),
        )


@pytest.mark.parametrize(
    "changes",
    [
        {"scope_sha256": "0" * 64},
        {"started_at": _STARTED_AT + timedelta(seconds=1)},
    ],
)
def test_verify_provider_run_rejects_scope_or_timestamp_mismatch(record, changes):
    with pytest.raises(ValueError):
        verify_provider_run(
            record,
            provider_response(record, **changes),
            verified_at=_COMPLETED_AT + timedelta(seconds=1),
        )


def test_verify_provider_run_rejects_completion_after_verified_at(record):
    with pytest.raises(ValueError, match="verified_at"):
        verify_provider_run(
            record,
            provider_response(record),
            verified_at=_COMPLETED_AT - timedelta(seconds=1),
        )


def test_verify_provider_run_returns_receipt_for_matching_run(record):
    verified_at = _COMPLETED_AT + timedelta(seconds=1)

    receipt = verify_provider_run(
        record,
        provider_response(record),
        verified_at=verified_at,
    )

    assert receipt.head_sha == record.head_sha
    assert receipt.run_id == record.run_id
    assert receipt.conclusion == record.status
    assert receipt.scope_sha256 == record.scope_sha256
    assert receipt.verified_at == verified_at
    assert len(receipt.source_response_sha256) == 64


def test_verify_provider_run_rejects_unexpected_response_fields(record):
    with pytest.raises(ValueError, match="only normalized run metadata"):
        verify_provider_run(
            record,
            provider_response(record, access_token="do-not-persist"),
            verified_at=_COMPLETED_AT + timedelta(seconds=1),
        )
