from pathlib import Path

from qa_orchestrator.config import Settings
from qa_orchestrator.contracts import (
    QaOrchestrationCatalog,
    QaTaskOutcome,
    QaTaskType,
    ReviewAgent,
    ReviewRoute,
)
from qa_orchestrator.model_policy import ModelSelection, load_model_selection
from qa_orchestrator.orchestration import (
    AdvanceQaOrchestrationRequest,
    QaOrchestrationList,
    QaOrchestrationSession,
    QaOrchestrator,
)
from qa_orchestrator.review_profiles import build_review_catalog, build_review_route
from qa_orchestrator.session_store import SqliteSessionStore


class OrchestratorService:
    @classmethod
    def from_settings(cls, *, data_dir: Path | None = None) -> "OrchestratorService":
        settings = Settings(data_dir=data_dir) if data_dir is not None else Settings()
        return cls(settings)

    def __init__(self, settings: Settings) -> None:
        model_selection = load_model_selection(settings.model_policy_path)
        session_store = (
            SqliteSessionStore(settings.orchestration_session_store_path)
            if settings.orchestration_session_store_path is not None
            else None
        )
        self.orchestrator = QaOrchestrator(
            ttl_seconds=settings.orchestration_session_ttl_seconds,
            max_sessions=settings.orchestration_max_sessions,
            model_selection=model_selection,
            session_store=session_store,
        )

    def prepare_review_route(self, agent_profile: ReviewAgent | str) -> ReviewRoute:
        try:
            resolved_profile = ReviewAgent(agent_profile)
        except (TypeError, ValueError) as exc:
            raise ValueError("unknown review agent profile") from exc
        return build_review_route(resolved_profile)

    def get_qa_orchestration_model_policy(self) -> ModelSelection:
        return self.orchestrator.model_selection

    def get_qa_orchestration_catalog(self) -> QaOrchestrationCatalog:
        return build_review_catalog()

    def start_qa_orchestration(self, task_type: QaTaskType) -> QaOrchestrationSession:
        return self.orchestrator.start(task_type)

    def advance_qa_orchestration(
        self,
        request: AdvanceQaOrchestrationRequest,
    ) -> QaOrchestrationSession:
        return self.orchestrator.advance(**request.model_dump())

    def get_qa_orchestration(self, run_id: str) -> QaOrchestrationSession:
        return self.orchestrator.get(run_id)

    def list_qa_orchestrations(self) -> QaOrchestrationList:
        return self.orchestrator.list_sessions()

    def finish_qa_orchestration(
        self,
        *,
        run_id: str,
        outcome: QaTaskOutcome,
    ) -> QaOrchestrationSession:
        return self.orchestrator.finish(run_id=run_id, outcome=outcome)
