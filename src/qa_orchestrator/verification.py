"""Provider-neutral verification of bounded run metadata supplied by a read-only adapter."""

import hashlib
import json
import re
from collections.abc import Mapping
from datetime import UTC, datetime

from qa_orchestrator.contracts import ReviewVerificationRecord, VerificationReceipt

_PROVIDER_RESPONSE_FIELDS = frozenset(
    {"run_id", "head_sha", "conclusion", "scope_sha256", "started_at", "completed_at"}
)
_RUN_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,255}$")


def _aware_datetime(value: object, *, field: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"provider {field} must be a timezone-aware timestamp")
    return value


def verify_provider_run(
    record: ReviewVerificationRecord,
    response: Mapping[str, object],
    *,
    verified_at: datetime,
) -> VerificationReceipt:
    """Validate normalized provider run metadata and return a hash-only receipt.

    This function does not fetch data. The caller is responsible for supplying a
    response from a configured read-only provider adapter.
    """
    if not isinstance(response, Mapping) or set(response) != _PROVIDER_RESPONSE_FIELDS:
        raise ValueError("provider response must contain only normalized run metadata")
    if record.source != "ci_provider":
        raise ValueError("provider run verification requires a ci_provider record")
    if not all(
        (
            record.run_id,
            record.scope_sha256,
            record.started_at,
            record.completed_at,
        )
    ):
        raise ValueError("verification record needs run_id, scope digest, and run timestamps")
    if not _RUN_ID.fullmatch(record.run_id):
        raise ValueError("verification record run_id is not a stable identifier")

    run_id = response["run_id"]
    head_sha = response["head_sha"]
    conclusion = response["conclusion"]
    scope_sha256 = response["scope_sha256"]
    if not all(isinstance(value, str) for value in (run_id, head_sha, conclusion, scope_sha256)):
        raise ValueError("provider run identifiers, conclusion, and scope digest must be text")
    if not _RUN_ID.fullmatch(run_id):
        raise ValueError("provider run_id is not a stable identifier")

    started_at = _aware_datetime(response["started_at"], field="started_at")
    completed_at = _aware_datetime(response["completed_at"], field="completed_at")
    verified_at = _aware_datetime(verified_at, field="verified_at")
    if completed_at < started_at:
        raise ValueError("provider run completed_at precedes started_at")
    if run_id != record.run_id:
        raise ValueError("provider run ID does not match the verification record")
    if head_sha.lower() != record.head_sha:
        raise ValueError("provider head SHA does not match the verification record")
    if conclusion != record.status:
        raise ValueError("provider conclusion does not match the verification record")
    if scope_sha256.lower() != record.scope_sha256:
        raise ValueError("provider scope digest does not match the verification record")
    if started_at != record.started_at or completed_at != record.completed_at:
        raise ValueError("provider timestamps do not match the verification record")
    if verified_at < completed_at:
        raise ValueError("verified_at must be at or after provider run completion")

    normalized_response = {
        "run_id": run_id,
        "head_sha": head_sha.lower(),
        "conclusion": conclusion,
        "scope_sha256": scope_sha256.lower(),
        "started_at": started_at.astimezone(UTC).isoformat(),
        "completed_at": completed_at.astimezone(UTC).isoformat(),
    }
    source_response_sha256 = hashlib.sha256(
        json.dumps(
            normalized_response,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")
    ).hexdigest()
    return VerificationReceipt(
        verifier_id="provider-neutral",
        run_id=run_id,
        head_sha=head_sha,
        conclusion=conclusion,
        scope_sha256=scope_sha256,
        source_response_sha256=source_response_sha256,
        verified_at=verified_at,
    )
