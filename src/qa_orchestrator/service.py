from pathlib import Path

from pydantic import Field, PrivateAttr, model_serializer

from qa_orchestrator.config import Settings
from qa_orchestrator.contracts import (
    FinalReviewReport,
    QaOrchestrationCatalog,
    QaTaskOutcome,
    QaTaskType,
    ReviewAgent,
    ReviewBundle,
    ReviewContext,
    ReviewRoute,
    ReviewRouteBatch,
    ReviewRouteSelection,
)
from qa_orchestrator.model_policy import ModelPolicySelection, load_model_selection
from qa_orchestrator.orchestration import (
    AdvanceQaOrchestrationRequest,
    OrchestrationStep,
    QaOrchestrationDeletion,
    QaOrchestrationList,
    QaOrchestrationSession,
    QaOrchestrator,
)
from qa_orchestrator.review_profiles import (
    build_review_catalog,
    build_review_route,
    bundle_execution_groups,
    bundle_profiles,
)
from qa_orchestrator.session_store import SqliteSessionStore


class QaOrchestrationAdvanceResult(QaOrchestrationSession):
    _compact_response: bool = PrivateAttr(default=False)

    @model_serializer(mode="wrap")
    def serialize_response(self, handler):
        payload = handler(self)
        if self._compact_response:
            for name in ("review_context", "profile_reports", "final_review_report",
                         "audit_events", "transition_receipts"):
                payload.pop(name, None)
        return payload

    route_warning: str | None = Field(
        default=None,
        description="Advisory scope limitation for a selected single profile; not a finding.",
    )
    selected_review_routes: ReviewRouteSelection | None = Field(
        default=None,
        description=(
            "After a successful triage selection, the exact checklists and execution groups for "
            "the selected route. Use this instead of a separate prepare call. This response-only "
            "field is not stored in the session."
        ),
    )


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

    def prepare_review_bundle(self, bundle: ReviewBundle | str) -> ReviewRouteBatch:
        try:
            resolved_bundle = ReviewBundle(bundle)
        except (TypeError, ValueError) as exc:
            raise ValueError("unknown review bundle") from exc
        return ReviewRouteBatch(
            bundle=resolved_bundle,
            routes=[build_review_route(profile) for profile in bundle_profiles(resolved_bundle)],
            execution_groups=[list(group) for group in bundle_execution_groups(resolved_bundle)],
        )

    def get_qa_orchestration_model_policy(self) -> ModelPolicySelection:
        return self.orchestrator.model_selection

    def get_qa_orchestration_catalog(self) -> QaOrchestrationCatalog:
        return build_review_catalog()

    def start_qa_orchestration(
        self,
        task_type: QaTaskType,
        review_context: ReviewContext | None = None,
    ) -> QaOrchestrationSession:
        return self.orchestrator.start(task_type, review_context)

    def advance_qa_orchestration(
        self,
        request: AdvanceQaOrchestrationRequest,
    ) -> QaOrchestrationAdvanceResult:
        limited_profile = (
            request.completed_step is OrchestrationStep.TRIAGE
            and request.status == "completed"
            and request.selected_profile in {ReviewAgent.PR_TEST_ANALYZER, ReviewAgent.CODE_EXPLORER}
        )
        warning = None
        if limited_profile:
            warning = (
                "This profile cannot report implementation findings. For an implementation review, "
                "choose an implementation reviewer or bundle, or explicitly acknowledge the limitation."
            )
        selected_review_routes = None
        if request.completed_step is OrchestrationStep.TRIAGE and request.status == "completed":
            if request.selected_bundle is not None:
                batch = self.prepare_review_bundle(request.selected_bundle)
                selected_review_routes = ReviewRouteSelection(
                    routes=batch.routes,
                    execution_groups=batch.execution_groups,
                )
            else:
                assert request.selected_profile is not None
                route = self.prepare_review_route(request.selected_profile)
                selected_review_routes = ReviewRouteSelection(
                    routes=[route],
                    execution_groups=[[route.profile]],
                )
        session = self.orchestrator.advance(**request.model_dump())
        return QaOrchestrationAdvanceResult.model_validate(
            {
                **session.model_dump(),
                "selected_review_routes": selected_review_routes,
                "route_warning": warning,
            }
        )

    def get_qa_orchestration(self, run_id: str) -> QaOrchestrationSession:
        return self.orchestrator.get(run_id)

    def renew_qa_orchestration(
        self, run_id: str, expected_revision: int | None = None,
    ) -> QaOrchestrationSession:
        return self.orchestrator.renew(run_id, expected_revision)

    def list_qa_orchestrations(self) -> QaOrchestrationList:
        return self.orchestrator.list_sessions()

    def delete_qa_orchestration(self, run_id: str) -> QaOrchestrationDeletion:
        return self.orchestrator.delete(run_id)

    def finish_qa_orchestration(
        self,
        *,
        run_id: str,
        outcome: QaTaskOutcome,
        final_review_report: FinalReviewReport | None = None,
        expected_revision: int | None = None,
    ) -> QaOrchestrationSession:
        return self.orchestrator.finish(
            run_id=run_id,
            outcome=outcome,
            final_review_report=final_review_report,
            expected_revision=expected_revision,
        )
