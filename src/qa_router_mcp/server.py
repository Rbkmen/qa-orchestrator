from fastmcp import FastMCP

from qa_router_mcp.backends import LMStudioDraftBackend
from qa_router_mcp.config import Settings
from qa_router_mcp.contracts import (
    CanaryFeedbackReceipt,
    CanaryReason,
    CanaryVerdict,
    CoverageItem,
    DraftEnvelope,
    DraftKind,
    QaTaskOutcome,
    QaTaskOutcomeReceipt,
    QaTaskType,
    ReviewAgent,
)
from qa_router_mcp.events import JsonEventSink, read_metrics_lines
from qa_router_mcp.report import summarize_events
from qa_router_mcp.service import RouterService


def build_server(service: RouterService) -> FastMCP:
    mcp = FastMCP(name="qa-router-mcp")

    @mcp.tool
    async def draft_test_cases(
        requirement: str,
        coverage_map: list[CoverageItem | str],
        examples: str = "",
    ) -> DraftEnvelope:
        """Expand 1-12 approved coverage items with stable COV-* IDs into test-case drafts."""
        if not 1 <= len(coverage_map) <= 12:
            raise ValueError("coverage_map must contain 1-12 non-empty items")
        normalized = [
            item
            if isinstance(item, CoverageItem)
            else CoverageItem(
                coverage_id=f"COV-{index:02d}",
                purpose=item,
                source="legacy supplied coverage item",
                state="legacy supplied coverage item",
                expected_invariant=item,
            )
            for index, item in enumerate(coverage_map, 1)
        ]
        coverage_ids = [item.coverage_id for item in normalized]
        if len(coverage_ids) != len(set(coverage_ids)):
            raise ValueError("coverage IDs must be unique")
        coverage = "\n\n".join(
            "\n".join(
                (
                    f"Coverage ID: {item.coverage_id}",
                    f"Purpose: {item.purpose}",
                    f"Source: {item.source}",
                    f"State: {item.state}",
                    f"Expected invariant: {item.expected_invariant}",
                )
            )
            for item in normalized
        )
        content = (
            f"Draft exactly {len(coverage_map)} test cases.\n"
            f"REQUIREMENT:\n{requirement}\nAPPROVED_COVERAGE_MAP:\n{coverage}"
        )
        if examples:
            content += f"\nEXAMPLES:\n{examples}"
        return await service.draft(
            DraftKind.TEST_CASES,
            content,
            expected_coverage_ids=tuple(coverage_ids),
        )

    @mcp.tool
    async def draft_review_checklist(
        agent_profile: ReviewAgent,
        evidence_packet: str,
        project_pattern: str = "",
    ) -> DraftEnvelope:
        """Draft a bounded read-only review checklist from a sanitized Evidence Packet."""
        return await service.draft(
            DraftKind.REVIEW_CHECKLIST,
            evidence_packet,
            project_pattern or None,
            review_agent=agent_profile,
        )

    @mcp.tool
    async def summarize_logs(logs: str) -> DraftEnvelope:
        """Group sanitized log fragments by visible signature without root-cause decisions."""
        return await service.draft(DraftKind.LOG_SUMMARY, logs)

    @mcp.tool
    async def draft_automation_skeleton(
        scenario: str,
        pattern: str,
    ) -> DraftEnvelope:
        """Draft a non-writing automation skeleton from an explicit supplied pattern."""
        return await service.draft(DraftKind.AUTOMATION_SKELETON, scenario, pattern)

    @mcp.tool
    async def translate_text(
        text: str,
        target_language: str,
        preserve_terms: str = "",
    ) -> DraftEnvelope:
        """Translate bounded sanitized text while preserving explicitly supplied terms."""
        content = f"TARGET_LANGUAGE:\n{target_language}\nTEXT:\n{text}"
        if preserve_terms:
            content += f"\nPRESERVE_TERMS:\n{preserve_terms}"
        terms = tuple(term.strip() for term in preserve_terms.splitlines() if term.strip())
        return await service.draft(
            DraftKind.TRANSLATION,
            content,
            preserve_terms=terms,
        )

    @mcp.tool
    async def rewrite_text(text: str, instruction: str) -> DraftEnvelope:
        """Rewrite bounded sanitized text without adding facts."""
        content = f"INSTRUCTION:\n{instruction}\nTEXT:\n{text}"
        return await service.draft(DraftKind.REWRITE, content)

    @mcp.tool
    async def explain_short(
        topic: str,
        audience: str = "QA engineer",
    ) -> DraftEnvelope:
        """Draft a short non-researched explanation for the requested audience."""
        content = f"AUDIENCE:\n{audience}\nTOPIC:\n{topic}"
        return await service.draft(DraftKind.SHORT_EXPLANATION, content)

    @mcp.tool
    async def summarize_text(text: str, focus: str = "") -> DraftEnvelope:
        """Summarize only bounded sanitized supplied text without adding facts."""
        content = f"TEXT:\n{text}"
        if focus:
            content = f"FOCUS:\n{focus}\n{content}"
        return await service.draft(DraftKind.TEXT_SUMMARY, content)

    @mcp.tool
    async def record_canary_feedback(
        draft_id: str,
        verdict: CanaryVerdict,
        reason: CanaryReason,
    ) -> CanaryFeedbackReceipt:
        """Record content-free review feedback when a local draft requests it."""
        return service.record_canary_feedback(draft_id, verdict, reason)

    @mcp.tool
    async def record_qa_task_outcome(
        task_type: QaTaskType,
        outcome: QaTaskOutcome,
        codegraph_calls: int,
        source_mcp_calls: int,
        qwen_used: bool,
        findings_identified: int,
        findings_confirmed: int,
        findings_rejected: int,
        qwen_edits: int,
        repeated_source_reads: int,
        deep_analysis_used: bool = False,
        deep_model: str | None = None,
        deep_reasoning: str | None = None,
        deep_duration_ms: int | None = None,
        deep_input_tokens: int | None = None,
        deep_output_tokens: int | None = None,
        sol_used: bool | None = None,
        codegraph_response_tokens: int | None = None,
        source_mcp_response_tokens: int | None = None,
        avoided_source_read_tokens: int | None = None,
    ) -> QaTaskOutcomeReceipt:
        """Record one content-free outcome after a completed or stopped QA task."""
        return service.record_qa_task_outcome(
            task_type=task_type,
            outcome=outcome,
            codegraph_calls=codegraph_calls,
            source_mcp_calls=source_mcp_calls,
            qwen_used=qwen_used,
            deep_analysis_used=deep_analysis_used,
            deep_model=deep_model,
            deep_reasoning=deep_reasoning,
            deep_duration_ms=deep_duration_ms,
            deep_input_tokens=deep_input_tokens,
            deep_output_tokens=deep_output_tokens,
            sol_used=sol_used,
            findings_identified=findings_identified,
            findings_confirmed=findings_confirmed,
            findings_rejected=findings_rejected,
            qwen_edits=qwen_edits,
            repeated_source_reads=repeated_source_reads,
            codegraph_response_tokens=codegraph_response_tokens,
            source_mcp_response_tokens=source_mcp_response_tokens,
            avoided_source_read_tokens=avoided_source_read_tokens,
        )

    @mcp.tool
    def get_metrics_report(days: int = 7) -> dict[str, object]:
        """Read the content-free QA Router metrics report for a positive time window."""
        if days < 1:
            raise ValueError("days must be positive")
        path = service.settings.metrics_path
        try:
            lines = read_metrics_lines(path)
        except OSError as exc:
            raise RuntimeError("metrics_unavailable") from exc
        return summarize_events(lines, days=days)

    return mcp


def main() -> None:
    settings = Settings.from_env()
    service = RouterService(
        settings,
        LMStudioDraftBackend(settings),
        JsonEventSink(
            settings.metrics_path,
            settings.metrics_retention_days,
            settings.metrics_max_events,
        ),
    )
    build_server(service).run()
