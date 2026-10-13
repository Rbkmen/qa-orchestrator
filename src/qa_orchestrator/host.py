"""Host-side supervision of injected read-only profile executors."""

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime

from pydantic import BaseModel, ConfigDict, Field

from qa_orchestrator.contracts import (
    ProfileExecutionMetrics,
    ProfileReviewReport,
    ReviewAgent,
    ReviewRoute,
)
from qa_orchestrator.orchestration import (
    AdvanceQaOrchestrationRequest,
    DeepReviewSignals,
    OrchestrationStatus,
    OrchestrationStep,
    QaOrchestrationSession,
)
from qa_orchestrator.service import OrchestratorService


class HostExecutionPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    max_concurrency: int = Field(default=3, ge=1, le=32)
    profile_timeout_seconds: float = Field(default=300, gt=0, le=3600)
    run_timeout_seconds: float = Field(default=1800, gt=0, le=86400)
    max_retries: int = Field(default=1, ge=0, le=3)
    retry_delay_seconds: float = Field(default=0.5, ge=0, le=30)


class RetryableProfileError(Exception):
    """An executor may explicitly mark a transient failure as safe to retry."""


@dataclass(frozen=True)
class HostProfileJob:
    session: QaOrchestrationSession
    profile: ReviewAgent
    route: ReviewRoute
    previous_reports: tuple[ProfileReviewReport, ...]
    attempt: int


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


ProfileExecutor = Callable[[HostProfileJob], Awaitable[ProfileReviewReport]]
RiskAssessor = Callable[[tuple[ProfileReviewReport, ...]], DeepReviewSignals]


class HostReviewAdapter:
    """Execute fixed primary-review waves; the host still owns evidence and final decisions."""

    def __init__(self, service: OrchestratorService, policy: HostExecutionPolicy | None = None):
        self.service = service
        self.policy = policy or HostExecutionPolicy()

    async def _execute_profile(
        self,
        session: QaOrchestrationSession,
        profile: ReviewAgent,
        executor: ProfileExecutor,
        semaphore: asyncio.Semaphore,
    ) -> ProfileReviewReport | HostProfileFailure:
        async with semaphore:
            started_at = datetime.now(UTC)
            route = self.service.prepare_review_route(profile)
            for attempt in range(1, self.policy.max_retries + 2):
                job = HostProfileJob(
                    session.model_copy(deep=True),
                    profile,
                    route,
                    tuple(session.profile_reports),
                    attempt,
                )
                try:
                    async with asyncio.timeout(self.policy.profile_timeout_seconds):
                        raw_report = await executor(job)
                    report = ProfileReviewReport.model_validate(raw_report)
                    if report.profile != profile:
                        return HostProfileFailure(profile, "wrong_profile")
                    usage = report.execution_metrics
                    metrics = ProfileExecutionMetrics(
                        started_at=started_at,
                        completed_at=datetime.now(UTC),
                        input_tokens=usage.input_tokens if usage and attempt == 1 else None,
                        output_tokens=usage.output_tokens if usage and attempt == 1 else None,
                        attempt_count=attempt,
                    )
                    return ProfileReviewReport.model_validate(
                        {
                            **report.model_dump(),
                            "execution_metrics": metrics.model_dump(),
                        }
                    )
                except (TimeoutError, RetryableProfileError) as exc:
                    if attempt > self.policy.max_retries:
                        reason = (
                            "profile_timeout"
                            if isinstance(exc, TimeoutError)
                            else "retry_exhausted"
                        )
                        return HostProfileFailure(profile, reason)
                    await asyncio.sleep(self.policy.retry_delay_seconds)
                except Exception:  # noqa: BLE001 -- isolate arbitrary injected executor failures
                    # Provider exception bodies may contain credentials or raw prompts.
                    return HostProfileFailure(profile, "executor_error")
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
        if session.selected_bundle is not None:
            groups = self.service.prepare_review_bundle(session.selected_bundle).execution_groups
        else:
            if session.selected_profile is None:
                raise ValueError("host adapter requires a selected profile or bundle")
            groups = [[session.selected_profile]]
        semaphore = asyncio.Semaphore(self.policy.max_concurrency)
        unsaved: tuple[ProfileReviewReport, ...] = ()
        failures: tuple[HostProfileFailure, ...] = ()
        tasks: list[asyncio.Task] = []
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
                            self._execute_profile(session, profile, executor, semaphore)
                        )
                        for profile in remaining
                    ]
                    outputs = await asyncio.gather(*tasks)
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
                    session = self.service.advance_qa_orchestration(
                        AdvanceQaOrchestrationRequest(
                            run_id=run_id,
                            completed_step="primary_review",
                            status="completed",
                            completed_profiles=remaining,
                            profile_reports=list(unsaved),
                            risk_signals=signals,
                        )
                    )
                    unsaved = ()
        except TimeoutError:
            failures = (HostProfileFailure(None, "run_timeout"),)
        except asyncio.CancelledError:
            self._stop_partial(run_id)
            raise
        except Exception:  # noqa: BLE001 -- isolate host assessment and report submission failures
            failures = (HostProfileFailure(None, "report_or_assessment_error"),)
        finally:
            for task in tasks:
                if not task.done():
                    task.cancel()
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)
            if failures and not unsaved:
                unsaved = tuple(
                    task.result()
                    for task in tasks
                    if not task.cancelled()
                    and task.exception() is None
                    and isinstance(task.result(), ProfileReviewReport)
                )
        if failures:
            session = self._stop_partial(run_id)
        return HostExecutionResult(session, unsaved, failures, started_at, datetime.now(UTC))

    def _stop_partial(self, run_id: str) -> QaOrchestrationSession:
        current = self.service.get_qa_orchestration(run_id)
        if current.status is OrchestrationStatus.ACTIVE:
            return self.service.advance_qa_orchestration(
                AdvanceQaOrchestrationRequest(
                    run_id=run_id,
                    completed_step=current.current_step,
                    status="partial",
                )
            )
        return current
