"""Host-side supervision of injected read-only profile executors."""

import asyncio
import hashlib
import json
import os
import tempfile
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from time import perf_counter
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator

from qa_orchestrator.checkpoints import ProfileCheckpointStore, checkpoint_key
from qa_orchestrator.contracts import (
    _MAX_PROFILE_REVIEW_REPORT_BYTES,
    FinalReviewReport,
    ProfileAttemptMetrics,
    ProfileAttemptObservation,
    ProfileContextSelection,
    ProfileExecutionMetrics,
    ProfileReviewReport,
    QaTaskOutcome,
    QaTaskType,
    ReviewAgent,
    ReviewBundle,
    ReviewContext,
    ReviewNote,
    ReviewRoute,
)
from qa_orchestrator.evidence import validate_context_selection, validate_registered_locations
from qa_orchestrator.orchestration import (
    AdvanceQaOrchestrationRequest,
    DeepReviewAssessment,
    DeepReviewSignals,
    OrchestrationError,
    OrchestrationReason,
    OrchestrationStatus,
    OrchestrationStep,
    QaOrchestrationSession,
    StageModelPolicy,
    validate_final_report_profile_links,
    validate_final_report_verification_records,
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


class ProfileOutputError(ValueError):
    """An execution failure with an optional private, bounded output retained in memory."""

    def __init__(
        self, message: str, *, retained_output: bytes | None = None,
        context_selection: ProfileContextSelection | None = None,
        missing_source_paths: tuple[str, ...] = (),
        review_head_sha: str | None = None,
        review_diff_sha256: str | None = None,
        source_packet_sha256: str | None = None,
    ):
        if retained_output is not None and len(retained_output) > _MAX_PROFILE_REVIEW_REPORT_BYTES:
            raise ValueError("retained output exceeds the profile report limit")
        super().__init__(message)
        self.retained_output = retained_output
        self.context_selection = context_selection
        self.missing_source_paths = missing_source_paths
        self.review_head_sha = review_head_sha
        self.review_diff_sha256 = review_diff_sha256
        self.source_packet_sha256 = source_packet_sha256


@dataclass(frozen=True)
class RetainedProfileOutput:
    profile: ReviewAgent
    attempt: int
    raw_output: bytes = field(repr=False)
    context_selection: ProfileContextSelection | None = None
    missing_source_paths: tuple[str, ...] = ()
    review_head_sha: str | None = None
    review_diff_sha256: str | None = None
    source_packet_sha256: str | None = None
    previous_reports: tuple[ProfileReviewReport, ...] | None = field(default=None, repr=False)


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
    retained_outputs: tuple[RetainedProfileOutput, ...] = field(default=(), repr=False)


class SynthesisReplayInputs(BaseModel):
    """Fixed upstream review results for an in-memory synthesis prompt experiment."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    task_type: QaTaskType
    review_context: ReviewContext
    review_profiles: tuple[ReviewAgent, ...]
    completed_profiles: tuple[ReviewAgent, ...]
    selected_bundle: ReviewBundle | None = None
    selected_profile: ReviewAgent | None = None
    profile_reports: tuple[ProfileReviewReport, ...]
    risk_signals: DeepReviewSignals | None = None
    deep_assessment: DeepReviewAssessment | None = None
    deep_reason_code: OrchestrationReason | None = None
    model_policy: StageModelPolicy | None = None


class SynthesisReplayJob(BaseModel):
    """One host-executed synthesis call over fixed upstream results."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    run_id: str
    prompt_id: str = Field(min_length=1, max_length=128)
    prompt: str = Field(min_length=1, max_length=256_000, repr=False)
    prompt_sha256: str
    inputs_sha256: str
    inputs: SynthesisReplayInputs

    @field_validator("prompt_id")
    @classmethod
    def normalize_prompt_id(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("prompt_id must not be blank")
        return value


class SynthesisReplayResult(BaseModel):
    """Ephemeral, hash-bound result; replay does not update the stored session."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    run_id: str
    prompt_id: str
    prompt_sha256: str
    inputs_sha256: str
    output_sha256: str
    final_review_report: FinalReviewReport


ProfileExecutor = Callable[[HostProfileJob], Awaitable[ProfileReviewReport]]
SynthesisReplayExecutor = Callable[[SynthesisReplayJob], Awaitable[FinalReviewReport]]
RiskAssessor = Callable[[tuple[ProfileReviewReport, ...]], DeepReviewSignals]


def _canonical_json_sha256(value: object) -> str:
    payload = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


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
        retained_outputs: list[RetainedProfileOutput],
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
                    previous_report_projection=getattr(
                        executor, "checkpoint_previous_report_projection", None
                    ),
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
                        receipt_present = any(
                            (
                                observation.submitted_prompt_sha256 is not None,
                                observation.prompt_write_status is not None,
                                observation.source_packet_sha256 is not None,
                                observation.selected_packet_sha256 is not None,
                                bool(observation.included_evidence_refs),
                                bool(observation.omitted_evidence_refs),
                                bool(observation.included_source_paths),
                            )
                        )
                        if receipt_present and (
                            observation.source_packet_sha256 != selection.source_packet_sha256
                            or observation.selected_packet_sha256
                            != selection.selected_packet_sha256
                            or observation.included_evidence_refs != selection.included_refs
                            or observation.omitted_evidence_refs != selection.omitted_refs
                            or observation.included_source_paths != selected_paths
                            or observation.prompt_write_status != "written"
                            or observation.submitted_prompt_sha256 is None
                        ):
                            raise ValueError(
                                "executor input receipt does not match report context selection"
                            )
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
                    if isinstance(exc, ProfileOutputError) and exc.retained_output is not None:
                        retained_outputs.append(RetainedProfileOutput(
                            profile, attempt, exc.retained_output, exc.context_selection,
                            exc.missing_source_paths, exc.review_head_sha,
                            exc.review_diff_sha256, exc.source_packet_sha256,
                            tuple(session.profile_reports),
                        ))
                    outcome = "timeout" if isinstance(exc, TimeoutError) else "retryable_error"
                    if isinstance(exc, TimeoutError) and observation.failure_kind is None:
                        observation.failure_kind = "timeout"
                    if attempt > self.policy.max_retries:
                        failure = HostProfileFailure(
                            profile, "profile_timeout" if outcome == "timeout" else "retry_exhausted"
                        )
                except asyncio.CancelledError:
                    observation.failure_kind = observation.failure_kind or "cancelled"
                    outcome = "cancelled"
                    raise
                except Exception as exc:  # noqa: BLE001 -- never retain private provider exception bodies
                    if isinstance(exc, ProfileOutputError) and exc.retained_output is not None:
                        retained_outputs.append(RetainedProfileOutput(
                            profile, attempt, exc.retained_output, exc.context_selection,
                            exc.missing_source_paths, exc.review_head_sha,
                            exc.review_diff_sha256, exc.source_packet_sha256,
                            tuple(session.profile_reports),
                        ))
                    observation.failure_kind = observation.failure_kind or "executor_error"
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
            if session.review_profiles[-1:] == [ReviewAgent.OMISSION_AUDITOR]:
                groups.append([ReviewAgent.OMISSION_AUDITOR])
        else:
            if session.selected_profile is None or not session.review_profiles:
                raise ValueError("host adapter requires a scheduled profile route or bundle")
            groups = [[profile] for profile in session.review_profiles]
        semaphore = asyncio.Semaphore(self.policy.max_concurrency)
        unsaved: tuple[ProfileReviewReport, ...] = ()
        failures: tuple[HostProfileFailure, ...] = ()
        reused: list[ReviewAgent] = []
        ledger: list[ProfileAttemptMetrics] = []
        retained_outputs: list[RetainedProfileOutput] = []
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
                            self._execute_profile(session, profile, executor, semaphore, reused, ledger, retained_outputs)
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
                    reports_for_assessment = tuple(session.profile_reports) + unsaved
                    signals = (
                        assess_risk(reports_for_assessment)
                        if is_last
                        else None
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
                    if (
                        session.current_step is OrchestrationStep.PRIMARY_REVIEW
                        and session.current_profile is not None
                        and all(session.current_profile not in existing for existing in groups)
                    ):
                        groups.append([session.current_profile])
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
            tuple(retained_outputs),
        )

    async def replay_synthesis(
        self,
        run_id: str,
        *,
        prompt_id: str,
        prompt: str,
        executor: SynthesisReplayExecutor,
    ) -> SynthesisReplayResult:
        """Re-run only host synthesis over the saved profile reports without persisting it."""
        session = self.service.get_qa_orchestration(run_id)
        if (
            session.current_step not in {
                OrchestrationStep.SYNTHESIS,
                OrchestrationStep.AWAITING_HOST_OUTCOME,
            }
            or session.review_context is None
            or tuple(session.completed_profiles) != tuple(session.review_profiles)
            or tuple(report.profile for report in session.profile_reports)
            != tuple(session.review_profiles)
        ):
            raise ValueError(
                "synthesis replay requires a bound session with all upstream profile reports"
            )
        inputs = SynthesisReplayInputs(
            task_type=session.task_type,
            review_context=session.review_context,
            review_profiles=tuple(session.review_profiles),
            completed_profiles=tuple(session.completed_profiles),
            selected_bundle=session.selected_bundle,
            selected_profile=session.selected_profile,
            profile_reports=tuple(session.profile_reports),
            risk_signals=session.risk_signals,
            deep_assessment=session.deep_assessment,
            deep_reason_code=session.deep_reason_code,
            model_policy=session.model_policy,
        )
        inputs_sha256 = _canonical_json_sha256(inputs.model_dump(mode="json"))
        prompt_sha256 = hashlib.sha256(prompt.encode("utf-8")).hexdigest()
        job = SynthesisReplayJob(
            run_id=run_id,
            prompt_id=prompt_id,
            prompt=prompt,
            prompt_sha256=prompt_sha256,
            inputs_sha256=inputs_sha256,
            inputs=inputs,
        )
        final_report = FinalReviewReport.model_validate(await executor(job))
        if final_report.profiles_reviewed != inputs.completed_profiles:
            raise ValueError("synthesis replay output does not match the fixed profile reports")
        expected_rules = (
            inputs.review_context.review_rules.digest
            if inputs.review_context.review_rules
            else None
        )
        if final_report.review_rules_sha256 != expected_rules:
            raise ValueError("synthesis replay output rules digest mismatch")
        from qa_orchestrator.report import validate_final_report_context

        validate_final_report_context(final_report, inputs.review_context)
        validate_final_report_verification_records(inputs.review_context, final_report)
        validate_final_report_profile_links(
            inputs.review_context,
            inputs.profile_reports,
            final_report,
        )
        output_sha256 = _canonical_json_sha256(final_report.model_dump(mode="json"))
        return SynthesisReplayResult(
            run_id=run_id,
            prompt_id=job.prompt_id,
            prompt_sha256=job.prompt_sha256,
            inputs_sha256=job.inputs_sha256,
            output_sha256=output_sha256,
            final_review_report=final_report,
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
                from qa_orchestrator.orchestration import _build_review_coverage_ledger
                from qa_orchestrator.report import (
                    render_final_report,
                    validate_final_report_context,
                )

                validate_final_report_context(final_report, current.review_context)
                render_report = final_report.model_copy(update={
                    "coverage_ledger": _build_review_coverage_ledger(
                        current.review_context, current.profile_reports, current.completed_profiles
                    ),
                })
                rendered = render_final_report(
                    render_report, context=current.review_context, outcome=outcome
                )
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
        reviewed_hunks = set(report.reviewed_diff_hunk_indexes)
        unreported_hunks = (
            sorted(set(range(len(context.diff_hunks))) - reviewed_hunks)
            if context.diff_hunk_inventory_status == "complete"
            else []
        )
        if not unreported_paths and not unreported_hunks:
            return report
        gap_details = []
        if unreported_paths:
            path_examples = ", ".join(
                path if len(path) <= 160 else path[:157] + "..."
                for path in unreported_paths[:5]
            )
            gap_details.append(
                f"The profile did not explicitly attest inspection of {len(unreported_paths)} "
                f"changed path(s), including {path_examples}. Coverage for those paths is "
                "unverified."
            )
        if unreported_hunks:
            hunk_examples = ", ".join(str(index) for index in unreported_hunks[:10])
            if len(unreported_hunks) > 10:
                hunk_examples += ", ..."
            gap_details.append(
                f"The profile did not explicitly attest inspection of {len(unreported_hunks)} "
                f"diff hunk(s), including indexes {hunk_examples}. Coverage for those hunks is "
                "unverified."
            )
        gap = ReviewNote(
            text=(
                " ".join(gap_details)
                + " Review the omitted material before treating this profile as complete."
            )
        )
        gaps = list(report.coverage_gaps)
        if gap not in gaps:
            if len(gaps) >= 100:
                raise ValueError("profile report has no room for host-generated coverage gaps")
            gaps.append(gap)
        if len(gaps) == len(report.coverage_gaps):
            return report
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
        if any(
            index >= len(context.diff_hunks)
            for index in report.reviewed_diff_hunk_indexes
        ):
            raise ValueError("profile report claims review of a hunk outside the bound diff")
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
