from pathlib import Path

from qa_orchestrator.config import Settings
from qa_orchestrator.contracts import (
    QaTaskOutcome,
    QaTaskType,
    ReviewAgent,
    ReviewRoute,
)
from qa_orchestrator.model_policy import load_model_selection
from qa_orchestrator.orchestration import (
    AdvanceQaOrchestrationRequest,
    QaOrchestrationSession,
    QaOrchestrator,
)
from qa_orchestrator.review_profiles import build_review_route


class OrchestratorService:
    @classmethod
    def from_settings(cls, *, data_dir: Path | None = None) -> "OrchestratorService":
        settings = Settings(data_dir=data_dir) if data_dir is not None else Settings()
        return cls(settings)

    def __init__(self, settings: Settings) -> None:
        model_selection = load_model_selection(settings.model_policy_path)
        self.orchestrator = QaOrchestrator(
            ttl_seconds=settings.orchestration_session_ttl_seconds,
            max_sessions=settings.orchestration_max_sessions,
            model_selection=model_selection,
        )

    def prepare_review_route(self, agent_profile: ReviewAgent | str) -> ReviewRoute:
        try:
            resolved_profile = ReviewAgent(agent_profile)
        except (TypeError, ValueError) as exc:
            raise ValueError("unknown review agent profile") from exc
        return build_review_route(resolved_profile)

    def start_qa_orchestration(self, task_type: QaTaskType) -> QaOrchestrationSession:
        return self.orchestrator.start(task_type)

    def advance_qa_orchestration(
        self,
        request: AdvanceQaOrchestrationRequest,
    ) -> QaOrchestrationSession:
        return self.orchestrator.advance(**request.model_dump())

    def get_qa_orchestration(self, run_id: str) -> QaOrchestrationSession:
        return self.orchestrator.get(run_id)

    def finish_qa_orchestration(
        self,
        *,
        run_id: str,
        outcome: QaTaskOutcome,
    ) -> QaOrchestrationSession:
        return self.orchestrator.finish(run_id=run_id, outcome=outcome)
