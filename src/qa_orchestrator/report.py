"""Human-readable rendering for structured final review reports."""

import argparse
import json
import re
from pathlib import Path

from qa_orchestrator.contracts import FinalReviewReport, ReviewContext
from qa_orchestrator.evidence import validate_registered_locations

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
    "pre_existing": "существовал до изменения",
    "unknown": "не установлено",
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


def render_final_report(
    report: FinalReviewReport, *, context: ReviewContext | None = None
) -> str:
    """Render findings and non-finding outcomes without inventing evidence."""
    lines = ["# QA review", "", _text(report.summary), ""]
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
        lines.append("")

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
    if report_refs - set(context.evidence_refs):
        raise ValueError("report cites refs outside the bound context")
    validate_registered_locations(context, report.finding_candidates)


def report_main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="qa-orch report")
    subparsers = parser.add_subparsers(dest="command", required=True)
    render = subparsers.add_parser("render", help="Render a final review report as Markdown")
    render.add_argument("--input", required=True, type=Path)
    render.add_argument("--output", type=Path, help="Write Markdown to this file instead of stdout")
    render.add_argument("--context", type=Path, help="Optional bound context for evidence locators")
    args = parser.parse_args(argv)
    report = FinalReviewReport.model_validate_json(args.input.read_bytes())
    context = (
        ReviewContext.model_validate_json(args.context.read_bytes()) if args.context else None
    )
    if context is not None:
        validate_final_report_context(report, context)
    output = render_final_report(report, context=context)
    if args.output:
        args.output.write_text(output, encoding="utf-8")
    else:
        print(output, end="")
    return 0
