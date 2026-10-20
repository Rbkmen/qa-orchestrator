"""Host-side supervision of injected read-only profile executors."""

import asyncio
import os
import tempfile
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from time import perf_counter
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field

from qa_orchestrator.checkpoints import ProfileCheckpointStore, checkpoint_key
from qa_orchestrator.contracts import (
    FinalReviewReport,
    ProfileAttemptMetrics,
    ProfileAttemptObservation,
    ProfileExecutionMetrics,
    ProfileReviewReport,
    QaTaskOutcome,
    ReviewAgent,
    ReviewContext,
    ReviewNote,
    ReviewRoute,
)
from qa_orchestrator.evidence import validate_context_selection, validate_registered_locations
from qa_orchestrator.orchestration import (
    AdvanceQaOrchestrationRequest,
    DeepReviewSignals,
    OrchestrationError,
    OrchestrationStatus,
    OrchestrationStep,
    QaOrchestrationSession,
)
from qa_orchestrator.review_rules import ReviewRuleset
from qa_orchestrator.service import OrchestratorService


class HostExecutionPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    max_concurrency: int = Field(default=3, ge=1, le=32)
    profile_timeout_seconds: float = Field(default=300, gt=0, le=3600)
    run_timeout_seconds: float = Field(default=1800, gt=0, le=86400)
    max_retries: int = Field(default=1, ge=0, le=3)
    retry_delay_seconds: float = Field(default=0.5, ge=0, le=30)
    heartbeat_interval_seconds: float = Field(default=30, gt=0, le=600)


class RetryableProfileError(Exception):
    """An executor may explicitly mark a transient failure as safe to retry."""


@dataclass(frozen=True)
class HostProfileJob:
    session: QaOrchestrationSession
    profile: ReviewAgent
    route: ReviewRoute
    previous_reports: tuple[ProfileReviewReport, ...]
    attempt: int
    review_rules: ReviewRuleset | None = None
    observation: ProfileAttemptObservation | None = None


@dataclass(frozen=True)
class HostProfileFailure:
    profile: ReviewAgent | None
    reason: str


@dataclass(frozen=True)
class HostExecutionResult:
    session: QaOrchestrationSession
    unsaved_reports: tuple[ProfileReviewReport, ...]
    failures: tuple[HostProfileFailure, ...]
    started_at: datetime
    completed_at: datetime
    reused_profiles: tuple[ReviewAgent, ...] = ()
    execution_attempts: tuple[ProfileAttemptMetrics, ...] = ()


ProfileExecutor = Callable[[HostProfileJob], Awaitable[ProfileReviewReport]]
RiskAssessor = Callable[[tuple[ProfileReviewReport, ...]], DeepReviewSignals]


class HostReviewAdapter:
    """Execute fixed primary-review waves; the host still owns evidence and final decisions."""

    def __init__(
        self,
        service: OrchestratorService,
        policy: HostExecutionPolicy | None = None,
        *,
        evidence_verifier: Callable[[ReviewContext], object] | None = None,
        checkpoint_store: ProfileCheckpointStore | None = None,
        executor_identity: str | None = None,
    ):
        self.service = service
        self.policy = policy or HostExecutionPolicy()
        self.evidence_verifier = evidence_verifier
        if checkpoint_store is not None and (
            not executor_identity or not executor_identity.strip()
        ):
            raise ValueError("checkpoint reuse requires an explicit executor/instruction version")
        self.checkpoint_store = checkpoint_store
        self.executor_identity = executor_identity

    async def _execute_profile(
        self,
        session: QaOrchestrationSession,
        profile: ReviewAgent,
        executor: ProfileExecutor,
        semaphore: asyncio.Semaphore,
        reused: list[ReviewAgent],
        ledger: list[ProfileAttemptMetrics],
    ) -> ProfileReviewReport | HostProfileFailure:
        context = session.review_context
        if context is None:
            return HostProfileFailure(None, "missing_review_context")
        queued = perf_counter()
        async with semaphore:
            queue_seconds = perf_counter() - queued
            started_at = datetime.now(UTC)
            route = self.service.prepare_review_route(profile)
            key = None
            if self.checkpoint_store:
                key = checkpoint_key(
                    session.review_context,
                    route,
                    session.model_policy,
                    session.profile_reports,
                    self.executor_identity,
                )
                cached = self.checkpoint_store.load(key)
                if cached is not None:
                    cached = self._with_changed_path_coverage_gaps(
                        session.review_context, cached
                    )
                    self._validate_report(session.review_context, profile, cached)
                    reused.append(profile)
                    return cached
            attempts = []
            for attempt in range(1, self.policy.max_retries + 2):
                observation = ProfileAttemptObservation()
                job = HostProfileJob(
                    session.model_copy(deep=True), profile, route.model_copy(deep=True),
                    tuple(session.profile_reports), attempt,
                    session.review_context.review_rules.for_paths(
                        session.review_context.changed_files
                    ) if session.review_context.review_rules else None,
                    observation,
                )
                attempt_started = datetime.now(UTC)
                clock = perf_counter()
                outcome, failure = "failed", None
                try:
                    async with asyncio.timeout(self.policy.profile_timeout_seconds):
                        raw_report = await executor(job)
                    report = ProfileReviewReport.model_validate(raw_report)
                    selection = report.context_selection
                    if selection is not None:
                        selected_paths = tuple(
                            sorted(
                                {
                                    entry.path
                                    for entry in context.evidence_registry
                                    if entry.kind == "source"
                                    and entry.revision == context.head_sha
                                    and entry.source_text_available is True
                                    and entry.path in context.changed_files
                                    and entry.evidence_ref in selection.included_refs
                                }
                            )
                        )
                        if observation.selected_packet_sha256 is not None and (
                            observation.source_packet_sha256 != selection.source_packet_sha256
                            or observation.selected_packet_sha256
                            != selection.selected_packet_sha256
                            or observation.included_evidence_refs != selection.included_refs
                            or observation.omitted_evidence_refs != selection.omitted_refs
                            or observation.included_source_paths != selected_paths
                        ):
                            raise ValueError(
                                "executor input receipt does not match report context selection"
                            )
                        observation.source_packet_sha256 = selection.source_packet_sha256
                        observation.selected_packet_sha256 = selection.selected_packet_sha256
                        observation.included_evidence_refs = selection.included_refs
                        observation.omitted_evidence_refs = selection.omitted_refs
                        observation.included_source_paths = selected_paths
                    usage = report.execution_metrics
                    if usage:
                        for name in ("input_tokens", "output_tokens"):
                            if getattr(observation, name) is None:
                                setattr(observation, name, getattr(usage, name))
                    if report.profile != profile:
                        failure = HostProfileFailure(profile, "wrong_profile")
                    else:
                        report = self._with_changed_path_coverage_gaps(
                            session.review_context, report
                        )
                        outcome = "completed"
                except (TimeoutError, RetryableProfileError) as exc:
                    outcome = "timeout" if isinstance(exc, TimeoutError) else "retryable_error"
                    if attempt > self.policy.max_retries:
                        failure = HostProfileFailure(
                            profile, "profile_timeout" if outcome == "timeout" else "retry_exhausted"
                        )
                except asyncio.CancelledError:
                    outcome = "cancelled"
                    raise
                except Exception:  # noqa: BLE001 -- never retain private provider exception bodies
                    failure = HostProfileFailure(profile, "executor_error")
                finally:
                    record = ProfileAttemptMetrics(
                        **observation.model_dump(), profile=profile, attempt=attempt,
                        outcome=outcome, started_at=attempt_started, completed_at=datetime.now(UTC),
                        review_head_sha=context.head_sha,
                        review_diff_sha256=context.diff_sha256,
                        queue_seconds=queue_seconds if attempt == 1 else 0,
                        executor_seconds=perf_counter() - clock,
                    )
                    attempts.append(record)
                    ledger.append(record)
                if failure:
                    return failure
                if outcome != "completed":
                    await asyncio.sleep(self.policy.retry_delay_seconds)
                    continue
                def total(name):
                    values = [getattr(record, name) for record in attempts]
                    return sum(values) if all(value is not None for value in values) else None
                metrics = ProfileExecutionMetrics(
                    started_at=started_at, completed_at=datetime.now(UTC),
                    input_tokens=total("input_tokens"), output_tokens=total("output_tokens"),
                    attempt_count=attempt, attempts=tuple(attempts),
                )
                completed_report = ProfileReviewReport.model_validate({
                    **report.model_dump(), "execution_metrics": metrics.model_dump(),
                    "review_rules_sha256": session.review_context.review_rules.digest
                    if session.review_context.review_rules else None,
                })
                if self.checkpoint_store:
                    try:
                        self._validate_report(session.review_context, profile, completed_report)
                    except ValueError:
                        return completed_report
                    self.checkpoint_store.save(key, completed_report)
                return completed_report
        raise AssertionError("unreachable executor state")

    async def run_primary_review(
        self,
        run_id: str,
        executor: ProfileExecutor,
        assess_risk: RiskAssessor,
    ) -> HostExecutionResult:
        started_at = datetime.now(UTC)
        session = self.service.get_qa_orchestration(run_id)
        if (
            session.status is not OrchestrationStatus.ACTIVE
            or session.current_step is not OrchestrationStep.PRIMARY_REVIEW
            or session.review_context is None
        ):
            raise ValueError("host adapter requires an active review-bound primary review")
        if self.evidence_verifier is not None:
            self.evidence_verifier(session.review_context)
        if session.selected_bundle is not None:
            groups = self.service.prepare_review_bundle(session.selected_bundle).execution_groups
        else:
            if session.selected_profile is None:
                raise ValueError("host adapter requires a selected profile or bundle")
            groups = [[session.selected_profile]]
        semaphore = asyncio.Semaphore(self.policy.max_concurrency)
        unsaved: tuple[ProfileReviewReport, ...] = ()
        failures: tuple[HostProfileFailure, ...] = ()
        reused: list[ReviewAgent] = []
        ledger: list[ProfileAttemptMetrics] = []
        tasks: list[asyncio.Task] = []
        lease_seconds = (session.expires_at - datetime.now(UTC)).total_seconds()
        interval = min(self.policy.heartbeat_interval_seconds, max(lease_seconds / 3, 0.01))
        heartbeat = asyncio.create_task(self._renew_lease(run_id, interval))
        wave: asyncio.Future | None = None
        cancellation: asyncio.CancelledError | None = None
        try:
            async with asyncio.timeout(self.policy.run_timeout_seconds):
                offset = 0
                for group in groups:
                    completed = len(session.completed_profiles)
                    remaining = group[max(0, completed - offset) :]
                    offset += len(group)
                    if not remaining:
                        continue
                    tasks = [
                        asyncio.create_task(
                            self._execute_profile(session, profile, executor, semaphore, reused, ledger)
                        )
                        for profile in remaining
                    ]
                    wave = asyncio.gather(*tasks)
                    done, _ = await asyncio.wait(
                        (wave, heartbeat), return_when=asyncio.FIRST_COMPLETED
                    )
                    if heartbeat in done:
                        failures = (HostProfileFailure(None, "lease_error"),)
                        await heartbeat
                        break
                    outputs = await wave
                    unsaved = tuple(x for x in outputs if isinstance(x, ProfileReviewReport))
                    failures = tuple(x for x in outputs if isinstance(x, HostProfileFailure))
                    if failures:
                        break
                    is_last = len(session.completed_profiles) + len(remaining) == len(
                        session.review_profiles
                    )
                    signals = (
                        assess_risk(tuple(session.profile_reports) + unsaved) if is_last else None
                    )
                    session = self._submit_transition(
                        AdvanceQaOrchestrationRequest(
                            run_id=run_id,
                            completed_step="primary_review",
                            status="completed",
                            completed_profiles=remaining,
                            profile_reports=list(unsaved),
                            risk_signals=signals,
                            request_id=f"host-{uuid4().hex}",
                            expected_revision=session.revision,
                        )
                    )
                    unsaved = ()
        except TimeoutError:
            failures = (HostProfileFailure(None, "run_timeout"),)
        except asyncio.CancelledError as exc:
            cancellation = exc
            try:
                self._stop_partial(run_id, session.revision)
            except OrchestrationError:
                # Cancellation must not mutate a session advanced by another host.
                pass
            raise
        except Exception:  # noqa: BLE001 -- isolate host assessment and report submission failures
            if not failures:
                failures = (HostProfileFailure(None, "report_or_assessment_error"),)
        finally:
            heartbeat.cancel()
            if wave is not None and not wave.done():
                wave.cancel()
            for task in tasks:
                if not task.done():
                    task.cancel()
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)
            await asyncio.gather(heartbeat, return_exceptions=True)
            if wave is not None:
                await asyncio.gather(wave, return_exceptions=True)
            if cancellation is not None:
                cancellation.execution_attempts = tuple(ledger)
            if failures and not unsaved:
                unsaved = tuple(
                    task.result()
                    for task in tasks
                    if not task.cancelled()
                    and task.exception() is None
                    and isinstance(task.result(), ProfileReviewReport)
                )
        if failures:
            session = self._stop_partial(run_id, session.revision)
        return HostExecutionResult(
            session,
            unsaved,
            failures,
            started_at,
            datetime.now(UTC),
            tuple(p for p in session.review_profiles if p in reused),
            tuple(ledger),
        )

    def finish_review(
        self,
        run_id: str,
        *,
        outcome: QaTaskOutcome,
        final_review_report: FinalReviewReport | None = None,
        expected_revision: int | None = None,
        markdown_output: Path | None = None,
    ) -> QaOrchestrationSession:
        """Finalize host review and optionally write its bound Markdown report atomically."""
        final_report = (
            FinalReviewReport.model_validate(final_review_report)
            if final_review_report is not None
            else None
        )
        output_path = Path(markdown_output) if markdown_output is not None else None
        staged_path: str | None = None
        try:
            if output_path is not None:
                current = self.service.get_qa_orchestration(run_id)
                if final_report is None or current.review_context is None:
                    raise ValueError(
                        "Markdown export requires a final report and bound review context"
                    )
                from qa_orchestrator.report import (
                    render_final_report,
                    validate_final_report_context,
                )

                validate_final_report_context(final_report, current.review_context)
                rendered = render_final_report(final_report, context=current.review_context)
                if output_path.exists() and output_path.is_dir():
                    raise ValueError("Markdown output path must be a file")
                descriptor, staged_path = tempfile.mkstemp(
                    prefix=f".{output_path.name}.", suffix=".tmp", dir=output_path.parent
                )
                with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as staged:
                    staged.write(rendered)
                    staged.flush()
                    os.fsync(staged.fileno())
            session = self.service.finish_qa_orchestration(
                run_id=run_id,
                outcome=outcome,
                final_review_report=final_report,
                expected_revision=expected_revision,
            )
            if staged_path is not None and output_path is not None:
                os.replace(staged_path, output_path)
                staged_path = None
            return session
        finally:
            if staged_path is not None:
                try:
                    os.unlink(staged_path)
                except FileNotFoundError:
                    pass

    @staticmethod
    def _with_changed_path_coverage_gaps(
        context: ReviewContext,
        report: ProfileReviewReport,
    ) -> ProfileReviewReport:
        bound_paths = set(context.changed_files)
        reviewed_paths = set(report.reviewed_changed_paths)
        unknown_paths = reviewed_paths - bound_paths
        if unknown_paths:
            examples = ", ".join(sorted(unknown_paths)[:5])
            raise ValueError(
                "profile report claims review of paths outside the bound change: " + examples
            )
        unreported_paths = sorted(bound_paths - reviewed_paths)
        if not unreported_paths:
            return report
        examples = ", ".join(
            path if len(path) <= 160 else path[:157] + "..."
            for path in unreported_paths[:5]
        )
        gap = ReviewNote(
            text=(
                f"The profile did not explicitly attest inspection of {len(unreported_paths)} "
                f"changed path(s), including {examples}. Coverage for those paths is "
                "unverified; review them before treating this profile as complete."
            )
        )
        gaps = list(report.coverage_gaps)
        if gap in gaps:
            return report
        if len(gaps) >= 100:
            raise ValueError("profile report has no room for host-generated coverage gaps")
        gaps.append(gap)
        return ProfileReviewReport.model_validate(
            {**report.model_dump(mode="python"), "coverage_gaps": tuple(gaps)}
        )

    @staticmethod
    def _validate_report(context: ReviewContext, profile: ReviewAgent, report: ProfileReviewReport):
        if report.profile != profile:
            raise ValueError("checkpoint profile mismatch")
        refs = {ref for f in report.finding_candidates for ref in f.evidence_refs}
        refs.update(
            ref for n in (*report.coverage_gaps, *report.unverified) for ref in n.evidence_refs
        )
        refs.update(e.evidence_ref for e in report.evidence_map)
        if refs - set(context.evidence_refs):
            raise ValueError("profile report cites unknown evidence refs")
        expected = context.review_rules.digest if context.review_rules else None
        if report.review_rules_sha256 != expected:
            raise ValueError("profile report rules digest mismatch")
        if set(report.reviewed_changed_paths) - set(context.changed_files):
            raise ValueError("profile report claims review of paths outside the bound change")
        validate_registered_locations(context, report.finding_candidates)
        validate_context_selection(context, report)

    def _submit_transition(self, request: AdvanceQaOrchestrationRequest) -> QaOrchestrationSession:
        for attempt in range(self.policy.max_retries + 1):
            try:
                return self.service.advance_qa_orchestration(request)
            except (ConnectionError, TimeoutError):
                if attempt == self.policy.max_retries:
                    raise
        raise AssertionError("unreachable transition state")

    async def _renew_lease(self, run_id: str, interval: float) -> None:
        while True:
            await asyncio.sleep(interval)
            current = self.service.get_qa_orchestration(run_id)
            try:
                self.service.renew_qa_orchestration(run_id, current.revision)
            except OrchestrationError as exc:
                if str(exc) != "stale session revision":
                    raise

    def _stop_partial(self, run_id: str, expected_revision: int) -> QaOrchestrationSession:
        current = self.service.get_qa_orchestration(run_id)
        if current.status is OrchestrationStatus.ACTIVE:
            return self._submit_transition(
                AdvanceQaOrchestrationRequest(
                    run_id=run_id,
                    completed_step=current.current_step,
                    status="partial",
                    request_id=f"host-stop-{uuid4().hex}",
                    expected_revision=expected_revision,
                )
            )
        return current
