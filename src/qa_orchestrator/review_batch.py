"""Host-owned helpers for preparing and accounting for multiple review inputs."""

from __future__ import annotations

import json
from pathlib import Path


def _inputs(path: Path) -> list[dict]:
    data = json.loads(path.read_text(encoding="utf-8"))
    inputs = data.get("inputs")
    if not isinstance(inputs, list) or not inputs:
        raise ValueError("manifest requires a non-empty inputs list")
    ids = [item.get("id") for item in inputs if isinstance(item, dict)]
    if len(ids) != len(inputs) or any(not isinstance(i, str) or not i.strip() for i in ids):
        raise ValueError("each input requires a non-empty id")
    if len(set(ids)) != len(ids):
        raise ValueError("input ids must be unique")
    return inputs


def prepare_review_batch(manifest: Path) -> dict:
    from qa_orchestrator.evidence import evidence_main
    from qa_orchestrator.git_evidence import resolve_git_input

    inputs = _inputs(manifest)
    root = manifest.resolve().parent
    protected = {manifest.resolve()}
    for item in inputs:
        for key in ("context", "diff", "content"):
            if item.get("kind") == "diff" and isinstance(item.get(key), str):
                protected.add((root / item[key]).resolve())
    outputs = []
    for item in inputs:
        for key in ("packet", "bound_context"):
            if item.get("kind") == "diff" and isinstance(item.get(key), str):
                outputs.append((root / item[key]).resolve())
    if len(set(outputs)) != len(outputs) or protected.intersection(outputs):
        raise ValueError("batch outputs must be distinct from all inputs and each other")
    if any(path.exists() for path in outputs):
        raise ValueError("batch outputs must not already exist")
    results = []
    for item in inputs:
        identity = item["id"]
        kind = item.get("kind")
        step = "resolve-git" if kind == "git" else "pack-diff" if kind == "diff" else "input"
        try:
            if kind == "git":
                resolved = resolve_git_input(root / item["repository"], item["base"], item["head"])
                results.append({"id": identity, "status": "resolved", "input": resolved})
            elif kind == "diff":
                options = []
                for key, flag in (
                    ("context", "--context"),
                    ("diff", "--diff"),
                    ("content", "--content"),
                    ("packet", "--packet"),
                    ("bound_context", "--bound-context"),
                ):
                    options.extend((flag, str(root / item[key])))
                evidence_main(["pack-diff", *options])
                results.append(
                    {
                        "id": identity,
                        "status": "ready",
                        "bound_context": str(root / item["bound_context"]),
                        "packet": str(root / item["packet"]),
                    }
                )
            else:
                raise ValueError("kind must be git or diff")
        except (ValueError, KeyError, OSError) as exc:
            results.append({"id": identity, "status": "failed", "step": step, "reason": str(exc)})
    return {"results": results}


def check_review_batch(manifest: Path, results_path: Path) -> dict:
    inputs = _inputs(manifest)
    records = json.loads(results_path.read_text(encoding="utf-8")).get("results")
    if not isinstance(records, list):
        raise TypeError("results must be a list")
    by_id = {}
    expected = {item["id"] for item in inputs}
    for record in records:
        if (
            not isinstance(record, dict)
            or record.get("id") not in expected
            or record["id"] in by_id
        ):
            raise ValueError("results contain an unknown or duplicate input id")
        by_id[record["id"]] = record
    unresolved = []
    for item in inputs:
        record = by_id.get(item["id"], {})
        finalized = (
            record.get("status") == "finalized"
            and bool(record.get("run_id"))
            and record.get("outcome") in {"completed", "partial", "blocked"}
        )
        failed = (
            record.get("status") == "failed"
            and isinstance(record.get("step"), str)
            and bool(record["step"].strip())
            and isinstance(record.get("reason"), str)
            and bool(record["reason"].strip())
        )
        if not (finalized or failed):
            unresolved.append(item["id"])
    return {
        "unresolved": unresolved,
        "note": "Host-supplied records are not independently verified.",
    }


def check_agent_trace(manifest: Path, trace: Path) -> dict:
    """Evaluate normalized host observations, not raw or authenticated transcripts."""
    expected = {item["id"] for item in _inputs(manifest)}
    events = json.loads(trace.read_text(encoding="utf-8")).get("events")
    if not isinstance(events, list):
        raise TypeError("events must be a list")
    started = {}
    accounted = set()
    final_seen = False
    for event in events:
        if not isinstance(event, dict):
            raise TypeError("events must be objects")
        if event.get("type") == "final":
            final_seen = True
            break
        identity = event.get("input_id")
        if identity not in expected:
            continue
        if event.get("type") == "start" and event.get("run_id"):
            started[identity] = event["run_id"]
        elif (
            event.get("type") == "finish"
            and event.get("run_id") == started.get(identity)
            and identity in started
            and event.get("outcome") in {"completed", "partial", "blocked"}
        ) or (
            event.get("type") == "failure"
            and event.get("step") in {"preflight", "start", "review", "finish"}
            and isinstance(event.get("reason"), str)
            and event["reason"].strip()
            and event.get("observed") is True
        ):
            accounted.add(identity)
    return {
        "final_seen": final_seen,
        "unresolved": sorted(expected - accounted),
        "note": "Normalized host observations are not independently authenticated or a final-answer content check.",
    }
