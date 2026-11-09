"""Human-readable rendering for structured final review reports."""

import argparse
import json
import re
from pathlib import Path
from typing import Literal

from qa_orchestrator.contracts import (
    FinalReviewReport,
    QaTaskOutcome,
    ReviewContext,
    ReviewVerificationRecord,
    review_changed_paths_sha256,
)
from qa_orchestrator.evidence import (
    complete_criterion_coverage,
    impact_coverage_status,
    input_manifest_completeness,
    review_completion_gaps,
    validate_impact_coverage_context,
    validate_registered_locations,
)

_SEVERITY_ORDER = {"blocker": 0, "high": 1, "medium": 2, "low": 3, None: 4}
_SEVERITY_LABELS = {
    "blocker": "Blocker",
    "high": "High",
    "medium": "Medium",
    "low": "Low",
    None: "Не указана",
}
_ORIGIN_LABELS = {
    "introduced": "внесён этим изменением",
    "exacerbated_by_pr": "существовал до PR; PR усилил дефект",
    "pre_existing": "существовал до изменения",
    "unknown": "не установлено",
}
_CRITERION_OUTCOME_LABELS = {
    "confirmed": "подтверждено",
    "contradicted": "противоречит реализации",
    "unverified": "не проверено",
    "not_applicable_to_diff": "не относится к diff",
}
_MARKDOWN_SPECIAL = re.compile(r"([\\`*_{}\[\]()#+.!|>~-])")


def _text(value: str) -> str:
    normalized = " ".join(value.split())
    normalized = normalized.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    return _MARKDOWN_SPECIAL.sub(r"\\\1", normalized)


def _code(value: str) -> str:
    content = json.dumps(value, ensure_ascii=False)
    runs = re.findall(r"`+", content)
    delimiter = "`" * (max((len(run) for run in runs), default=0) + 1)
    return f"{delimiter}{content}{delimiter}"


def _locator(context: ReviewContext, evidence_ref: str) -> str | None:
    entry = next(
        (record for record in context.evidence_registry if record.evidence_ref == evidence_ref),
        None,
    )
    if entry is None:
        return None
    if entry.kind == "source":
        location = entry.path or "source"
        if entry.start_line is not None:
            end_line = entry.end_line or entry.start_line
            location += f":{entry.start_line}" + (f"-{end_line}" if end_line != entry.start_line else "")
        if entry.revision:
            location += f"@{entry.revision}"
        return location
    if entry.kind == "diff":
        return f"diff@{entry.revision}" if entry.revision else "diff"
    return entry.locator or entry.kind


def _refs(refs: tuple[str, ...], context: ReviewContext | None = None) -> str:
    rendered = []
    for ref in refs:
        item = _text(ref)
        locator = _locator(context, ref) if context is not None else None
        if locator:
            item += f" — {_code(locator)}"
        rendered.append(item)
    return ", ".join(rendered) or "нет"


def _coverage_path_label(
    index: int,
    *,
    ledger_path_count: int,
    context: ReviewContext | None,
) -> str:
    if context is not None and len(context.changed_files) == ledger_path_count:
        return _code(context.changed_files[index])
    return f"index {index}"


def _render_coverage_ledger(
    report: FinalReviewReport,
    context: ReviewContext | None,
) -> list[str]:
    lines = ["## Changed-path coverage", ""]
    ledger = report.coverage_ledger
    if context is not None and context.diff_hunk_inventory_status == "partial":
        if context.diff_hunk_inventory_count_at_least is None:
            lines.append(
                "Diff hunk inventory: partial; hunk count is unavailable and no indexed hunks are bound."
            )
        else:
            lines.append(
                "Diff hunk inventory: partial; at least "
                f"{context.diff_hunk_inventory_count_at_least} total, "
                f"{len(context.diff_hunks)} indexed."
            )
    if (
        context is not None
        and context.neighbor_discovery_candidate_results_omitted_by_limit
    ):
        omitted = context.neighbor_discovery_candidate_results_omitted_by_limit
        label = "candidate" if omitted == 1 else "candidates"
        lines.append(
            f"Neighbor discovery omitted {omitted} eligible {label} at its result limit; "
            "blast-radius coverage is incomplete."
        )
    if ledger is None:
        lines.extend((
            "Host-generated coverage ledger is unavailable; this report may predate coverage tracking.",
            "",
        ))
        return lines

    if context is None or context.diff_hunk_inventory_sha256 is None:
        lines.append("Diff hunk coverage: unknown; no complete bound hunk inventory was supplied.")
    elif (
        ledger.diff_hunk_inventory_sha256 != context.diff_hunk_inventory_sha256
        or ledger.diff_hunk_count != len(context.diff_hunks)
    ):
        lines.append("Diff hunk coverage: unknown; ledger does not match the bound hunk inventory.")
    else:
        reviewed_count = ledger.diff_hunk_count - len(
            ledger.unreviewed_diff_hunk_indexes
        )
        lines.append(
            f"Diff hunk coverage: {reviewed_count}/{ledger.diff_hunk_count} hunk indexes "
            "attested by a non-explorer reviewer. This records an attestation, not proof of reasoning."
        )
        if ledger.unreviewed_diff_hunk_indexes:
            lines.append(
                "Unreviewed diff hunk indexes: "
                + ", ".join(map(str, ledger.unreviewed_diff_hunk_indexes[:20]))
                + (", ..." if len(ledger.unreviewed_diff_hunk_indexes) > 20 else "")
            )

    mapping_valid = (
        context is not None
        and len(context.changed_files) == ledger.changed_path_count
        and review_changed_paths_sha256(context.changed_files) == ledger.changed_paths_sha256
    )
    if mapping_valid:
        lines.append("Path indexes are mapped to the matching bound changed-path list.")
    elif context is None:
        lines.append(
            f"Path mapping requires the bound context ({ledger.changed_path_count} paths; "
            f"SHA-256 {ledger.changed_paths_sha256})."
        )
    else:
        lines.append(
            "The supplied context does not match this ledger; path names are suppressed and indexes are shown."
        )
    lines.append(
        "This records source excerpts delivered by the host and inspection paths attested by the executor; "
        "it does not prove model reasoning."
    )

    available = set(ledger.head_source_excerpt_path_indexes)
    registered = set(ledger.registered_head_source_path_indexes)
    unavailable = set(ledger.head_source_text_unavailable_path_indexes)
    all_indexes = set(range(ledger.changed_path_count))
    missing_registration = sorted(all_indexes - registered)
    unavailable_text = sorted(unavailable)
    unknown_text_status = sorted(registered - available - unavailable)
    lines.append(
        f"Paths with registered head-source evidence: {len(registered)}/{ledger.changed_path_count}; "
        f"paths with verified text excerpts: {len(available)}/{ledger.changed_path_count}."
    )
    if missing_registration:
        labels = ", ".join(
            _coverage_path_label(
                index,
                ledger_path_count=ledger.changed_path_count,
                context=context if mapping_valid else None,
            )
            for index in missing_registration
        )
        lines.append(f"- No registered head-source record: {labels}")
    if unavailable_text:
        labels = ", ".join(
            _coverage_path_label(
                index,
                ledger_path_count=ledger.changed_path_count,
                context=context if mapping_valid else None,
            )
            for index in unavailable_text
        )
        lines.append(f"- Source text explicitly marked unavailable: {labels}")
    if unknown_text_status:
        labels = ", ".join(
            _coverage_path_label(
                index,
                ledger_path_count=ledger.changed_path_count,
                context=context if mapping_valid else None,
            )
            for index in unknown_text_status
        )
        lines.append(f"- Source text availability not verified: {labels}")

    for profile in ledger.profiles:
        attested = set(profile.reviewer_attested_path_indexes)
        included = set(profile.source_excerpt_included_path_indexes)
        lines.append("")
        lines.append(
            f"- **{_text(profile.profile.value)}:** context selection "
            f"{'recorded' if profile.context_selection_recorded else 'not recorded'}; "
            f"executor input receipt "
            f"{'verified' if profile.executor_input_receipt_verified else 'unverified'}; "
            f"reviewer-attested {len(attested)}/{ledger.changed_path_count} paths."
        )
        if attested:
            labels = ", ".join(
                _coverage_path_label(
                    index,
                    ledger_path_count=ledger.changed_path_count,
                    context=context if mapping_valid else None,
                )
                for index in sorted(attested)[:20]
            )
            suffix = f" (+{len(attested) - 20} more)" if len(attested) > 20 else ""
            lines.append(f"  - Executor-attested as reviewed: {labels}{suffix}")
        if not profile.context_selection_recorded:
            if available:
                lines.append(
                    "  - Source-excerpt delivery cannot be confirmed because no selection receipt was recorded."
                )
        elif not profile.executor_input_receipt_verified:
            if available:
                lines.append(
                    "  - Source-excerpt delivery cannot be confirmed because the executor input receipt is missing or does not match the selection."
                )
        else:
            lines.append(
                f"  - Paths with at least one verified excerpt in the selection: "
                f"{len(included)}/{len(available)}."
            )
            if included:
                labels = ", ".join(
                    _coverage_path_label(
                        index,
                        ledger_path_count=ledger.changed_path_count,
                        context=context if mapping_valid else None,
                    )
                    for index in sorted(included)[:20]
                )
                suffix = f" (+{len(included) - 20} more)" if len(included) > 20 else ""
                lines.append(f"  - Paths with included text excerpts: {labels}{suffix}")
            omitted_source = sorted(available - included)
            if omitted_source:
                labels = ", ".join(
                    _coverage_path_label(
                        index,
                        ledger_path_count=ledger.changed_path_count,
                        context=context if mapping_valid else None,
                    )
                    for index in omitted_source
                )
                lines.append(f"  - Available excerpts not recorded in selection: {labels}")
        unattested = sorted(all_indexes - attested)
        if unattested:
            labels = ", ".join(
                _coverage_path_label(
                    index,
                    ledger_path_count=ledger.changed_path_count,
                    context=context if mapping_valid else None,
                )
                for index in unattested
            )
            lines.append(f"  - Not explicitly attested as reviewed: {labels}")
    lines.append("")
    return lines


def _render_impact_coverage(
    report: FinalReviewReport,
    context: ReviewContext | None,
) -> list[str]:
    lines = ["## Impact closure", ""]
    ledger = report.impact_coverage
    bound_candidate_omissions = (
        context.neighbor_discovery_candidate_results_omitted_by_limit
        if context is not None
        else None
    )
    if ledger is None:
        if bound_candidate_omissions:
            label = "candidate" if bound_candidate_omissions == 1 else "candidates"
            lines.extend(
                (
                    "Impact closure: partial",
                    f"- **Reason:** bound neighbor discovery omitted {bound_candidate_omissions} eligible {label} at its result limit; no impact-coverage ledger was supplied.",
                    "- **Scope:** impact closure only describes the recorded bounded discovery; it is not exhaustive repository coverage.",
                    "",
                )
            )
            return lines
        lines.extend(
            (
                "Impact closure: unknown",
                "- **Reason:** no impact-coverage ledger was supplied.",
                "- **Scope:** impact closure only describes the recorded bounded discovery; it is not exhaustive repository coverage.",
                "",
            )
        )
        return lines

    status = impact_coverage_status(ledger)
    if bound_candidate_omissions:
        status = "partial"
    lines.extend(
        (
            f"Impact closure: {status}",
            "This ledger covers the recorded bounded discovery only; it does not claim exhaustive repository coverage.",
            (
                f"- **Discovery:** {_text(ledger.discovery_method)} v{ledger.discovery_version}; "
                f"state {_text(ledger.discovery_state)}."
            ),
            f"- **Bound head:** {_code(ledger.head_sha)}",
            f"- **Changed-path SHA-256:** {_code(ledger.changed_paths_sha256)}",
            f"- **Discovery SHA-256:** {_code(ledger.discovery_sha256)}",
            f"- **Candidates:** {len(ledger.candidates)}",
        )
    )
    if bound_candidate_omissions:
        label = "candidate" if bound_candidate_omissions == 1 else "candidates"
        lines.append(
            f"- **Bound discovery omissions:** {bound_candidate_omissions} eligible {label} were excluded by the result limit."
        )

    disposition_counts = {
        disposition: sum(
            candidate.disposition == disposition for candidate in ledger.candidates
        )
        for disposition in (
            "reviewed",
            "no_relevant_relationship",
            "unresolved",
            "not_checked",
        )
    }
    lines.append(
        "- **Dispositions:** "
        + "; ".join(
            f"{_text(disposition)} {count}"
            for disposition, count in disposition_counts.items()
        )
    )

    limit_details = []
    for limit in ledger.limits:
        state = []
        if limit.reached:
            state.append("reached")
        if limit.uninspected:
            state.append("uninspected")
        suffix = f" ({', '.join(state)})" if state else ""
        limit_details.append(f"{_text(limit.name)} = {limit.value}{suffix}")
    lines.append("- **Configured scan limits:** " + "; ".join(limit_details))

    total_omissions = sum(omission.count for omission in ledger.omissions)
    omission_label = "omission" if total_omissions == 1 else "omissions"
    lines.append(
        f"- **Omissions:** {total_omissions} {omission_label} across "
        f"{len(ledger.omissions)} categories."
    )
    for omission in ledger.omissions:
        details = [f"{omission.count} omitted"]
        if omission.limit is not None:
            details.append(f"limit {omission.limit}")
        if omission.examples:
            examples = ", ".join(_text(example) for example in omission.examples)
            details.append(f"examples: {examples}")
        lines.append(f"  - **{_text(omission.kind)}:** " + "; ".join(details))

    if status == "partial" and (
        ledger.omissions
        or any(limit.reached or limit.uninspected for limit in ledger.limits)
    ):
        lines.append("- Discovery was capped or left items uninspected; closure remains partial.")

    if ledger.candidates:
        lines.append("- **Candidate dispositions:**")
        for candidate in ledger.candidates:
            details = [
                f"**{_text(candidate.disposition)}**",
                _text(candidate.rationale),
                f"locator: {_text(candidate.locator)}",
            ]
            if candidate.evidence_refs:
                details.append(f"evidence: {_refs(candidate.evidence_refs, context)}")
            lines.append(
                f"  - **{_text(candidate.candidate_id)} · {_text(candidate.relation)}:** "
                + "; ".join(details)
            )
    else:
        lines.append("- **Candidate dispositions:** none discovered in this bounded scan.")

    lines.append("")
    return lines


def _render_profile_execution(report: FinalReviewReport) -> list[str]:
    ledger = report.coverage_ledger
    if ledger is None:
        return []

    profiles = [
        profile for profile in ledger.profiles if profile.execution_metrics is not None
    ]
    if not profiles:
        return []

    lines = [
        "## Profile execution",
        "",
        "The adapter records callback outcomes. Prompt hashes and input selections are executor observations; a completed stdin write does not prove provider receipt or model reasoning. Reviewed paths remain executor attestations.",
        "",
    ]
    for profile in profiles:
        metrics = profile.execution_metrics
        assert metrics is not None
        elapsed = (metrics.completed_at - metrics.started_at).total_seconds()
        summary = [
            f"elapsed {elapsed:.2f}s",
            f"attempts {metrics.attempt_count}",
        ]
        if metrics.input_tokens is not None:
            summary.append(f"input tokens {metrics.input_tokens}")
        if metrics.output_tokens is not None:
            summary.append(f"output tokens {metrics.output_tokens}")
        lines.append(f"- **{_text(profile.profile.value)}:** " + "; ".join(summary))

        for attempt in metrics.attempts:
            attempt_details = [
                f"queue {attempt.queue_seconds:.2f}s",
                f"executor {attempt.executor_seconds:.2f}s",
                f"head {attempt.review_head_sha or 'not recorded'}",
            ]
            if attempt.review_diff_sha256 is not None:
                attempt_details.append(f"diff {attempt.review_diff_sha256}")
            stdin_delivery = {
                "prepared": "prepared; write not observed",
                "written": "write completed (host observed)",
            }.get(attempt.prompt_write_status, "not recorded")
            attempt_details.append(f"CLI stdin delivery {stdin_delivery}")
            if attempt.submitted_prompt_sha256 is not None:
                attempt_details.append(f"prompt sha256 {attempt.submitted_prompt_sha256}")
            if attempt.source_packet_sha256 is not None:
                attempt_details.append(f"source packet sha256 {attempt.source_packet_sha256}")
            if attempt.selected_packet_sha256 is not None:
                attempt_details.append(f"selected packet sha256 {attempt.selected_packet_sha256}")
            if attempt.included_evidence_refs:
                refs = ", ".join(attempt.included_evidence_refs[:20])
                suffix = f" (+{len(attempt.included_evidence_refs) - 20} more)" if len(attempt.included_evidence_refs) > 20 else ""
                attempt_details.append(f"included refs {refs}{suffix}")
            if attempt.omitted_evidence_refs:
                refs = ", ".join(attempt.omitted_evidence_refs[:20])
                suffix = f" (+{len(attempt.omitted_evidence_refs) - 20} more)" if len(attempt.omitted_evidence_refs) > 20 else ""
                attempt_details.append(f"omitted refs {refs}{suffix}")
            if attempt.included_source_paths:
                paths = ", ".join(_code(path) for path in attempt.included_source_paths[:20])
                suffix = f" (+{len(attempt.included_source_paths) - 20} more)" if len(attempt.included_source_paths) > 20 else ""
                attempt_details.append(f"source paths {paths}{suffix}")
            for label, value in (
                ("preparation", attempt.preparation_seconds),
                ("model", attempt.model_seconds),
                ("validation", attempt.validation_seconds),
            ):
                if value is not None:
                    attempt_details.append(f"{label} {value:.2f}s")
            if attempt.failure_kind is not None:
                attempt_details.append(f"failure kind {_text(attempt.failure_kind)}")
            if attempt.input_tokens is not None:
                attempt_details.append(f"input tokens {attempt.input_tokens}")
            if attempt.output_tokens is not None:
                attempt_details.append(f"output tokens {attempt.output_tokens}")
            lines.append(
                f"  - Attempt {attempt.attempt} ({_text(attempt.outcome)}): "
                + "; ".join(attempt_details)
            )
    lines.append("")
    return lines


def verification_binding(
    record: ReviewVerificationRecord,
    context: ReviewContext,
) -> Literal["current", "stale", "unknown"]:
    """Classify a recorded run by whether it used the exact reviewed head."""
    if context.head_sha is None or record.head_sha is None:
        return "unknown"
    return "current" if record.head_sha == context.head_sha else "stale"


def _render_verification_records(
    report: FinalReviewReport,
    context: ReviewContext | None,
) -> list[str]:
    lines = [
        "## Tests and CI verification",
        "",
        "These are host-supplied records; status, head binding and provenance are shown separately. A diff-only review cannot establish head binding. The orchestrator does not independently authenticate host-reported results.",
        "",
    ]
    if not report.verification_records:
        lines.extend(("No test, CI, or runtime verification records were supplied.", ""))
        return lines
    for record in report.verification_records:
        binding = verification_binding(record, context) if context is not None else "unknown"
        status = record.status
        if record.status == "passed" and binding == "stale":
            status = "passed, stale, not a current pass"
        elif record.status == "passed" and binding == "unknown":
            status = "passed, head binding unknown"
        elif record.status == "not_run":
            status = "not run"
        provenance = {
            "host_reported": "host reported, unverified",
            "local_reproduced": "locally reproduced",
            "provider_verified": "provider verified",
        }[record.provenance]
        details = [
            f"status: {_text(status)}",
            f"head binding: {_text(binding)}",
            f"provenance: {_text(provenance)}",
            f"scenario: {_text(record.scenario)}",
            f"head: {_code(record.head_sha) if record.head_sha else 'unknown'}",
            f"environment: {_text(record.environment)}",
            f"source: {_text(record.source)} / {_code(record.source_ref)}",
        ]
        if record.scope:
            details.append(f"scope: {_text(record.scope)}")
        if record.scope_sha256:
            details.append(f"scope sha256: {_code(record.scope_sha256)}")
        if record.run_id:
            details.append(f"run ID: {_code(record.run_id)}")
        if record.artifact_sha256:
            details.append(f"artifact sha256: {_code(record.artifact_sha256)}")
        if record.receipt:
            details.append(f"verifier: {_text(record.receipt.verifier_id)}")
            details.append(f"verified at: {_text(record.receipt.verified_at.isoformat())}")
            details.append(
                f"provider response sha256: {_code(record.receipt.source_response_sha256)}"
            )
        if record.run_url:
            details.append(f"run: <{record.run_url}>")
        if record.reason:
            details.append(f"reason: {_text(record.reason)}")
        if record.evidence_refs:
            details.append(f"evidence: {_refs(record.evidence_refs, context)}")
        lines.append(f"- **{_text(record.verification_id)} · {_text(record.check_type)}:** " + "; ".join(details))
    lines.append("")
    return lines


def render_final_report(
    report: FinalReviewReport, *, context: ReviewContext | None = None,
    outcome: QaTaskOutcome | None = None,
    stop_reason: str | None = None,
    review_execution_blocker: str | None = None,
) -> str:
    """Render findings and non-finding outcomes without inventing evidence."""
    if context is not None:
        report = complete_criterion_coverage(context, report)
    lines = ["# QA review", "", _text(report.summary), ""]
    input_summary = input_manifest_completeness(context)
    manifest = context.input_manifest if context is not None else None
    sources = manifest.sources if manifest is not None else ()
    available = [source.source_id for source in sources if source.retrieval_status == "complete"]
    incomplete = [item["source_id"] for item in input_summary["incomplete_sources"]]
    limitations = [note.text for note in (*report.coverage_gaps, *report.unverified)]
    completion_gaps = list(review_completion_gaps(context, report)) if context is not None else []
    if report.coverage_ledger is not None:
        missing_receipts = [
            profile.profile.value for profile in report.coverage_ledger.profiles
            if not profile.executor_input_receipt_verified
        ]
        if missing_receipts:
            completion_gaps.append("executor input receipt unverified: " + ", ".join(missing_receipts))
    lines.extend((
        "## Review boundaries",
        "",
        "- **Outcome:** " + (_text(outcome) if outcome else "not provided"),
        "- **Reported profiles:** " + (", ".join(_code(p.value) for p in report.profiles_reviewed) or "none recorded"),
        "- **Sources retrieved:** " + (", ".join(_code(s) for s in available) or "none recorded"),
        "- **Incomplete sources:** " + (", ".join(_code(s) for s in incomplete) or ("none declared" if manifest is not None else "unknown; manifest unavailable")),
        "- **Reported limitations:** " + ("; ".join(_text(s) for s in limitations) or "none reported"),
    ))
    if context is not None and context.input_kind == "diff":
        lines.append(
            "- **Diff-only scope:** Git revisions, repository provenance and surrounding "
            "source are not verified by this input."
        )
    if outcome in {"partial", "blocked"}:
        lines.append(
            "- **Stop reason (host-reported):** "
            + (_text(stop_reason) if stop_reason else "not supplied")
        )
        if review_execution_blocker:
            lines.append("- **Review execution blocker (host-reported):** " + _text(review_execution_blocker))
        lines.append(
            "- **Known completion gaps (host-selected outcome):** "
            + ("; ".join(_text(gap) for gap in completion_gaps) if completion_gaps else
               "no structured completion gaps recorded; see reported limitations")
        )
    lines.append("")
    source_counts = input_summary["source_counts"]
    scope_categories = input_summary["scope_categories"]
    required_scope_kinds = input_summary["scope_required_source_kinds"]
    missing_scope_kinds = input_summary["missing_scope_source_kinds"]
    lines.extend(
        (
            "## Input completeness",
            "",
            f"Input completeness: {_text(input_summary['status'])}",
            f"- **Status basis:** {_text(input_summary['status_reason'])}",
            (
                f"- **Path-derived scope:** policy v{input_summary['scope_policy_version']}; "
                + (", ".join(_code(category) for category in scope_categories) or "none")
            ),
            (
                "- **Scope-required source kinds:** "
                + (", ".join(_code(kind) for kind in required_scope_kinds) or "none")
            ),
            (
                "- **Missing scope-required source kinds:** "
                + (", ".join(_code(kind) for kind in missing_scope_kinds) or "none")
            ),
            (
                f"- **Sources:** {source_counts['declared']} declared "
                f"({source_counts['required']} required, {source_counts['optional']} optional); "
                f"{source_counts['complete_required']} required complete, "
                f"{source_counts['incomplete_required']} required incomplete, "
                f"{source_counts['incomplete_optional']} optional incomplete."
            ),
            "- **Source inventory assessment:** "
            + _text(input_summary["source_inventory_status"] or "not recorded (legacy manifest)"),
            "- **Description conflict assessment:** "
            + _text(input_summary["source_conflict_status"] or "not recorded (legacy manifest)"),
        )
    )
    if input_summary["incomplete_sources"]:
        lines.append("- **Incomplete sources:**")
        for source in input_summary["incomplete_sources"]:
            source_kind = "required" if source["required"] else "optional"
            reasons = ", ".join(source["reasons"])
            lines.append(
                f"  - {_code(source['source_id'])} ({source_kind}): {_text(reasons)}"
            )
    else:
        lines.append("- **Incomplete sources:** none")
    if input_summary["source_conflicts"]:
        lines.append("- **Description conflicts:**")
        for conflict in input_summary["source_conflicts"]:
            source_refs = ", ".join(_code(source_id) for source_id in conflict["source_ids"])
            criterion_refs = ", ".join(_text(ref) for ref in conflict["criterion_refs"])
            details = [
                _text(conflict["disposition"]),
                f"sources: {source_refs}",
                _text(conflict["summary"]),
            ]
            if criterion_refs:
                details.append(f"criteria: {criterion_refs}")
            if conflict["resolution_source_id"]:
                details.append(f"resolved by: {_code(conflict['resolution_source_id'])}")
            if conflict["resolution_summary"]:
                details.append(_text(conflict["resolution_summary"]))
            lines.append(f"  - {_code(conflict['conflict_id'])}: " + "; ".join(details))
    if input_summary["missing_criterion_refs"]:
        missing_refs = ", ".join(
            _text(reference) for reference in input_summary["missing_criterion_refs"]
        )
        lines.append(f"- **Unregistered declared criterion refs:** {missing_refs}")
    else:
        lines.append("- **Unregistered declared criterion refs:** none")
    if input_summary["unlisted_criterion_refs"]:
        unlisted_refs = ", ".join(
            _text(reference) for reference in input_summary["unlisted_criterion_refs"]
        )
        lines.append(f"- **Unlisted registered criterion refs:** {unlisted_refs}")
    else:
        lines.append("- **Unlisted registered criterion refs:** none")
    lines.append("")
    lines.extend(("## Findings", ""))
    findings = sorted(
        report.finding_candidates,
        key=lambda finding: (_SEVERITY_ORDER[finding.severity], finding.candidate_id),
    )
    if not findings:
        lines.extend(("Подтверждённых findings нет в рассмотренном объёме.", ""))
    for finding in findings:
        severity = _SEVERITY_LABELS[finding.severity]
        lines.extend(
            (
                f"### {_text(finding.candidate_id)} · {_text(severity)} · {_text(finding.title)}",
                "",
                f"- **Affected area:** {_text(finding.affected_area) if finding.affected_area else 'не указана'}",
                f"- **Change origin:** {_ORIGIN_LABELS[finding.change_origin]}",
                f"- **Problem:** {_text(finding.description)}",
                f"- **Expected behavior:** {_text(finding.expected_behavior) if finding.expected_behavior else 'not stated'}",
                f"- **Source-predicted actual behavior:** {_text(finding.actual_behavior) if finding.actual_behavior else 'not stated'}",
                f"- **Failure condition:** {_text(finding.failure_condition) if finding.failure_condition else 'не указано'}",
                f"- **Counterevidence that would change the conclusion:** {_text(finding.flip_condition) if finding.flip_condition else 'не указано'}",
                f"- **Evidence refs:** {_refs(finding.evidence_refs, context)}",
            )
        )
        if finding.source_locations:
            locations = ", ".join(
                _code(
                    f"{location.path}:{location.start_line}"
                    + (f"-{location.end_line}" if location.end_line else "")
                )
                for location in finding.source_locations
            )
            lines.append(f"- **Source locations:** {locations}")
        if finding.impact:
            lines.append(f"- **Impact:** {_text(finding.impact)}")
        if finding.next_step:
            lines.append(f"- **Next step:** {_text(finding.next_step)}")
        lines.extend(
            (
                f"- **Evidence confidence:** {_text(finding.confidence)}",
                f"- **Verification gap:** {_text(finding.verification_gap)}",
                "",
            )
        )

    if report.criterion_coverage:
        lines.extend(("## Requirements and MR claims", ""))
        for item in sorted(report.criterion_coverage, key=lambda entry: entry.criterion_ref):
            criterion_type = (
                "Requirement" if item.criterion_kind == "requirement" else "MR claim"
            )
            expected = (
                _text(item.expected_behavior)
                if item.expected_behavior is not None
                else "не сформулировано"
            )
            lines.extend(
                (
                    f"### {criterion_type} · {_refs((item.criterion_ref,), context)}",
                    "",
                    f"- **Expected behavior:** {expected}",
                    f"- **Outcome:** {_CRITERION_OUTCOME_LABELS[item.outcome]}",
                    f"- **Implementation evidence:** {_refs(item.implementation_refs, context)}",
                    f"- **Test/CI evidence:** {_refs(item.test_refs, context)}",
                    f"- **Assessment:** {_text(item.rationale)}",
                    "",
                )
            )

    if report.scenario_coverage:
        lines.extend(("## Scenario disposition matrix", ""))
        for index, item in enumerate(report.scenario_coverage, start=1):
            lines.extend(
                (
                    f"### Scenario {index}: {_text(item.scenario)}",
                    "",
                    f"- **Disposition:** {_text(item.disposition)}",
                    f"- **Evidence refs:** {_refs(item.evidence_refs, context)}",
                    f"- **Assessment:** {_text(item.rationale)}",
                    "",
                )
            )

    if report.candidate_decisions:
        lines.extend(("## Candidate decisions", ""))
        for decision in report.candidate_decisions:
            final_link = (
                f" → {_text(decision.final_finding_id)}"
                if decision.final_finding_id
                else ""
            )
            lines.append(
                f"- {_text(decision.source.profile.value)} / "
                f"{_text(decision.source.candidate_id)}: **{_text(decision.verdict)}**"
                f"{final_link} — {_text(decision.rationale)} "
                f"(refs: {_refs(decision.evidence_refs, context)})"
            )
            if decision.refutation is not None:
                lines.append(
                    f"  - Refutation: **{_text(decision.refutation.status)}** — "
                    f"{_text(decision.refutation.rationale)} "
                    f"(refs: {_refs(decision.refutation.evidence_refs, context)})"
                )
        lines.append("")

    unconfirmed = [
        decision
        for decision in report.candidate_decisions
        if decision.verdict in {"rejected", "unverified"}
    ]
    if unconfirmed:
        lines.extend(("## Possible bugs not confirmed", ""))
        for decision in unconfirmed:
            candidate = decision.candidate_snapshot
            title = candidate.title if candidate is not None else decision.source.candidate_id
            lines.extend(
                (
                    (f"### {_text(decision.source.candidate_id)} · "
                     f"{_text(decision.verdict)} · {_text(title)}"),
                    "",
                )
            )
            if candidate is not None:
                lines.extend(
                    (
                        f"- **Original description:** {_text(candidate.description)}",
                        f"- **Expected behavior:** {_text(candidate.expected_behavior) if candidate.expected_behavior else 'not stated'}",
                        f"- **Source-predicted actual behavior:** {_text(candidate.actual_behavior) if candidate.actual_behavior else 'not stated'}",
                        f"- **Failure condition:** {_text(candidate.failure_condition) if candidate.failure_condition else 'не указано'}",
                        f"- **Counterevidence that would change the conclusion:** {_text(candidate.flip_condition) if candidate.flip_condition else 'не указано'}",
                        f"- **Original evidence refs:** {_refs(candidate.evidence_refs, context)}",
                    )
                )
                if candidate.source_locations:
                    locations = ", ".join(
                        _code(
                            f"{location.path}:{location.start_line}"
                            + (f"-{location.end_line}" if location.end_line else "")
                        )
                        for location in candidate.source_locations
                    )
                    lines.append(f"- **Original source locations:** {locations}")
                if candidate.verification_gap:
                    lines.append(f"- **Verification gap:** {_text(candidate.verification_gap)}")
            else:
                lines.append("- **Original description:** не сохранено в legacy report")
            lines.append(f"- **Host assessment:** {_text(decision.rationale)}")
            if decision.refutation is not None:
                lines.extend(
                    (
                        f"- **Refutation status:** {_text(decision.refutation.status)}",
                        f"- **Refutation rationale:** {_text(decision.refutation.rationale)}",
                        f"- **Refutation evidence:** {_refs(decision.refutation.evidence_refs, context)}",
                    )
                )
            else:
                lines.append("- **Refutation status:** проверка не сохранена")
            lines.append("")

    lines.extend(_render_manual_checks(report, context))
    lines.extend(_render_coverage_ledger(report, context))
    lines.extend(_render_impact_coverage(report, context))
    lines.extend(_render_profile_execution(report))
    lines.extend(_render_verification_records(report, context))

    for heading, notes in (
        ("Coverage gaps", report.coverage_gaps),
        ("Unverified", report.unverified),
    ):
        if notes:
            lines.extend((f"## {heading}", ""))
            lines.extend(
                f"- {_text(note.text)} (refs: {_refs(note.evidence_refs, context)})"
                for note in notes
            )
            lines.append("")

    return "\n".join(lines).rstrip() + "\n"


def validate_final_report_context(report: FinalReviewReport, context: ReviewContext) -> None:
    report = complete_criterion_coverage(context, report)
    validate_impact_coverage_context(report, context)
    ledger = report.coverage_ledger
    if ledger is not None:
        expected_hunk_count = (
            len(context.diff_hunks)
            if context.diff_hunk_inventory_sha256 is not None
            else None
        )
        if (
            ledger.diff_hunk_inventory_sha256 != context.diff_hunk_inventory_sha256
            or ledger.diff_hunk_count != expected_hunk_count
        ):
            raise ValueError("coverage ledger does not match the bound diff hunk inventory")
        if (
            ledger.changed_path_count != len(context.changed_files)
            or ledger.changed_paths_sha256 != review_changed_paths_sha256(context.changed_files)
        ):
            raise ValueError("coverage ledger does not match the bound changed paths")
        if tuple(profile.profile for profile in ledger.profiles) != report.profiles_reviewed:
            raise ValueError("coverage ledger profiles do not match the final report")
        paths = set(context.changed_files)
        registered_paths = {
            entry.path
            for entry in context.evidence_registry
            if entry.kind == "source"
            and entry.revision == context.head_sha
            and entry.path in paths
        }
        available_paths = {
            entry.path
            for entry in context.evidence_registry
            if entry.kind == "source"
            and entry.revision == context.head_sha
            and entry.source_text_available is True
            and entry.path in paths
        }
        explicitly_unavailable_paths = {
            entry.path
            for entry in context.evidence_registry
            if entry.kind == "source"
            and entry.revision == context.head_sha
            and entry.source_text_available is False
            and entry.path in paths
        } - available_paths
        expected_registered = tuple(
            index for index, path in enumerate(context.changed_files) if path in registered_paths
        )
        expected_available = tuple(
            index for index, path in enumerate(context.changed_files) if path in available_paths
        )
        expected_unavailable = tuple(
            index
            for index, path in enumerate(context.changed_files)
            if path in explicitly_unavailable_paths
        )
        if (
            ledger.registered_head_source_path_indexes != expected_registered
            or ledger.head_source_excerpt_path_indexes != expected_available
            or ledger.head_source_text_unavailable_path_indexes != expected_unavailable
        ):
            raise ValueError("coverage ledger source status does not match the bound evidence registry")
    report_refs = {
        ref for finding in report.finding_candidates for ref in finding.evidence_refs
    }
    report_refs.update(
        ref
        for note in (*report.coverage_gaps, *report.unverified)
        for ref in note.evidence_refs
    )
    report_refs.update(
        ref for decision in report.candidate_decisions for ref in decision.evidence_refs
    )
    report_refs.update(
        ref
        for decision in report.candidate_decisions
        if decision.candidate_snapshot is not None
        for ref in decision.candidate_snapshot.evidence_refs
    )
    report_refs.update(
        ref
        for decision in report.candidate_decisions
        if decision.refutation is not None
        for ref in decision.refutation.evidence_refs
    )
    report_refs.update(
        ref
        for item in report.criterion_coverage
        for ref in (
            item.criterion_ref,
            *item.implementation_refs,
            *item.test_refs,
        )
    )
    report_refs.update(
        ref for item in report.scenario_coverage for ref in item.evidence_refs
    )
    report_refs.update(
        ref for item in report.verification_records for ref in item.evidence_refs
    )
    report_refs.update(ref for item in report.manual_checks for ref in item.evidence_refs)
    registry = {entry.evidence_ref: entry for entry in context.evidence_registry}
    for item in report.verification_records:
        for ref in item.evidence_refs:
            entry = registry.get(ref)
            if entry is not None and (
                entry.kind != "runtime" or entry.revision != item.head_sha
            ):
                raise ValueError(
                    "runtime evidence must match the verification record head and be a runtime record"
                )
    if report_refs - set(context.evidence_refs):
        raise ValueError("report cites refs outside the bound context")
    validate_registered_locations(context, report.finding_candidates)
    validate_registered_locations(
        context,
        tuple(
            decision.candidate_snapshot
            for decision in report.candidate_decisions
            if decision.candidate_snapshot is not None
        ),
    )


def _render_manual_checks(report: FinalReviewReport, context: ReviewContext | None) -> list[str]:
    if not report.manual_checks:
        return []
    lines = ["## Manual checks (proposed, not executed)", ""]
    for check in report.manual_checks:
        lines.append(f"- {_text(check.action)} → {_text(check.expected_result)} (evidence: {_refs(check.evidence_refs, context)})")
    return [*lines, ""]


def render_session_status(session) -> str:
    """Summarize recorded state without asserting that reported work occurred."""
    context = session.review_context
    lines = ["# Review status", "", f"- Run: {_code(session.run_id)}",
             f"- Status: {_text(session.status.value)}; stage: {_text(session.current_step.value)}",
             f"- Next: {_text(session.next_action)}",
             "- Completed profiles (reported): " + (", ".join(_code(p.value) for p in session.completed_profiles) or "none")]
    if session.status.value in {"partial", "blocked"}:
        reason = _text(session.stop_reason) if session.stop_reason else "not recorded"
        lines.append(f"- Stop reason (host-reported): {reason}")
        if session.review_execution_blocker:
            lines.append("- Review execution blocker (host-reported): " + _text(session.review_execution_blocker))
    if context is None:
        lines.append("- Review input: not recorded")
    else:
        lines.extend((f"- Repository: {_code(context.repository)}",
                      f"- Diff SHA-256: {_code(context.diff_sha256)}",
                      f"- Base: {_code(context.base_sha) if context.base_sha else 'unknown'}; head: {_code(context.head_sha) if context.head_sha else 'unknown'}",
                      "- Changed paths: " + ", ".join(_code(path) for path in context.changed_files)))
        manifest = context.input_manifest
        if manifest is None:
            lines.append("- Sources: unknown; manifest unavailable")
        else:
            for source in manifest.sources:
                lines.append(f"- Source {_code(source.source_id)} ({_text(source.kind)}, {_code(source.locator)}): retrieval {_text(source.retrieval_status)}, pagination {_text(source.pagination_status)}, extraction {_text(source.extraction_status)}, criteria {_text(source.criteria_status)}")
            completeness = input_manifest_completeness(context)
            lines.append(f"- Input completeness (host-reported): {_text(completeness['status'])} — {_text(completeness['status_reason'])}")
    return "\n".join(lines) + "\n"


def render_brief_report(report: FinalReviewReport, *, context: ReviewContext | None = None,
                        outcome: QaTaskOutcome | None = None,
                        stop_reason: str | None = None,
                        review_execution_blocker: str | None = None) -> str:
    """Render a concise view of recorded findings and limitations."""
    if context is not None:
        report = complete_criterion_coverage(context, report)
    lines = ["# QA review", "", _text(report.summary), "",
             f"Outcome: {_text(outcome) if outcome else 'not provided'}",
             "Reported profiles: " + ", ".join(_code(profile.value) for profile in report.profiles_reviewed)]
    if outcome in {"partial", "blocked"}:
        lines.append("Stop reason (host-reported): " + (_text(stop_reason) if stop_reason else "not supplied"))
        if review_execution_blocker:
            lines.append("Review execution blocker (host-reported): " + _text(review_execution_blocker))
    lines.extend(("", "## Confirmed findings", ""))
    if not report.finding_candidates:
        lines.append("No confirmed findings in the reviewed scope.")
    for finding in sorted(report.finding_candidates, key=lambda f: (_SEVERITY_ORDER[f.severity], f.candidate_id)):
        locations = ", ".join(_code(f"{loc.path}:{loc.start_line}") for loc in finding.source_locations)
        lines.append(f"- **{_text(finding.severity)} · {_text(finding.title)}:** {_text(finding.description)} (evidence: {_refs(finding.evidence_refs, context)}{'; ' + locations if locations else ''})")
        if finding.next_step:
            lines.append(f"  - Next: {_text(finding.next_step)}")
    lines.extend(("", *_render_manual_checks(report, context)))
    lines.extend(("", "## Coverage gaps and unverified", ""))
    gaps_start = len(lines)
    notes = (*report.coverage_gaps, *report.unverified)
    lines.extend(f"- {_text(note.text)} (evidence: {_refs(note.evidence_refs, context)})" for note in notes)
    for item in report.scenario_coverage:
        if item.disposition in {"unverified", "candidate"}:
            lines.append(f"- Scenario {_text(item.scenario)} ({_text(item.disposition)}): {_text(item.rationale)} (evidence: {_refs(item.evidence_refs, context)})")
    for decision in report.candidate_decisions:
        if decision.verdict == "unverified":
            lines.append(f"- Unverified candidate {_text(decision.source.candidate_id)}: {_text(decision.rationale)} (evidence: {_refs(decision.evidence_refs, context)})")
            if decision.candidate_snapshot is not None:
                candidate = decision.candidate_snapshot
                lines.append(f"  - {_text(candidate.title)}: {_text(candidate.description)}")
                if candidate.verification_gap:
                    lines.append(f"  - Verification gap: {_text(candidate.verification_gap)}")
    unresolved_criteria = [item for item in report.criterion_coverage if item.outcome in {"unverified", "contradicted"}]
    if unresolved_criteria:
        lines.append("- Requirements and MR claims requiring attention:")
        for item in unresolved_criteria:
            lines.append(f"  - {_refs((item.criterion_ref,), context)}: {_text(item.outcome)} — {_text(item.rationale)}")
    if context is None or context.input_manifest is None:
        lines.append("- Source completeness unknown; input manifest unavailable.")
    else:
        completeness = input_manifest_completeness(context)
        if completeness["status"] != "complete":
            lines.append(f"- Input completeness: {_text(completeness['status'])} — {_text(completeness['status_reason'])}")
    if context is not None and context.input_kind == "diff":
        lines.append("- Diff-only input does not verify Git revisions or surrounding source.")
    if context is not None and outcome in {"partial", "blocked"}:
        for gap in review_completion_gaps(context, report):
            lines.append(f"- Completion gap: {_text(gap)}")
    if not report.verification_records:
        lines.append("- no verification records supplied; tests, CI and runtime are not established.")
    if len(lines) == gaps_start:
        lines.append("None reported.")
    if report.verification_records:
        lines.extend(("", *_render_verification_records(report, context)))
    return "\n".join(lines) + "\n"


def report_main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="qa-orch report")
    subparsers = parser.add_subparsers(dest="command", required=True)
    render = subparsers.add_parser("render", help="Render a final review report as Markdown")
    render.add_argument("--input", required=True, type=Path)
    render.add_argument("--output", type=Path, help="Write Markdown to this file instead of stdout")
    render.add_argument("--context", type=Path, help="Optional bound context for evidence locators")
    render.add_argument("--session", type=Path, help="Saved full session containing the final report")
    render.add_argument("--outcome", choices=("completed", "partial", "blocked"), help="Recorded session outcome")
    render.add_argument("--brief", action="store_true", help="Render a concise user-facing report")
    status = subparsers.add_parser("status", help="Summarize a saved session JSON")
    status.add_argument("--input", required=True, type=Path)
    status.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    if args.command == "status":
        from qa_orchestrator.orchestration import QaOrchestrationSession

        session = QaOrchestrationSession.model_validate_json(args.input.read_bytes())
        output = render_session_status(session)
        if args.output:
            args.output.write_text(output, encoding="utf-8")
        else:
            print(output, end="")
        return 0
    report = FinalReviewReport.model_validate_json(args.input.read_bytes())
    context = (
        ReviewContext.model_validate_json(args.context.read_bytes()) if args.context else None
    )
    outcome = args.outcome
    stop_reason = None
    review_execution_blocker = None
    if args.session:
        from qa_orchestrator.orchestration import QaOrchestrationSession

        session = QaOrchestrationSession.model_validate_json(args.session.read_bytes())
        if session.final_review_report is None or report != session.final_review_report:
            raise ValueError("session final report does not match input report")
        if session.review_context is None or (context is not None and context != session.review_context):
            raise ValueError("session review context does not match input context")
        if session.status.value not in {"completed", "partial", "blocked"}:
            raise ValueError("session outcome is not final")
        if outcome is not None and outcome != session.status.value:
            raise ValueError("session outcome does not match requested outcome")
        context = session.review_context
        outcome = session.status.value
        stop_reason = session.stop_reason or "not recorded"
        review_execution_blocker = session.review_execution_blocker
    if context is not None:
        validate_final_report_context(report, context)
    renderer = render_brief_report if args.brief else render_final_report
    output = renderer(report, context=context, outcome=outcome, stop_reason=stop_reason,
                      review_execution_blocker=review_execution_blocker)
    if args.output:
        args.output.write_text(output, encoding="utf-8")
    else:
        print(output, end="")
    return 0
