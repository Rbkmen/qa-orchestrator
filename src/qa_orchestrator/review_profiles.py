from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType

from qa_orchestrator.contracts import ReviewAgent, ReviewBundle, ReviewRoute

REQUIRED_REVIEW_SECTIONS = (
    "Scope",
    "Checklist",
    "Candidate Coverage Gaps",
    "Positive Observations",
    "Unverified",
)

COMMON_CONSTRAINTS = (
    "Use only the evidence gathered by the host agent.",
    "Separate candidate coverage gaps from confirmed findings.",
    "Do not decide severity, priority, root cause, release readiness, or merge readiness.",
    "Do not perform external writes.",
)


@dataclass(frozen=True, slots=True)
class ReviewProfileDefinition:
    display_name: str
    focus: str
    escalation_signals: tuple[str, ...]


REVIEW_PROFILES: Mapping[ReviewAgent, ReviewProfileDefinition] = MappingProxyType(
    {
        ReviewAgent.PR_TEST_ANALYZER: ReviewProfileDefinition(
            display_name="Test Analyzer",
            focus=(
                "Focus on changed behavior, happy-path, negative, edge, integration, "
                "and meaningful assertion coverage."
            ),
            escalation_signals=(
                "Changed behavior crosses an unverified integration boundary.",
                "Assertions do not distinguish success from a silently wrong result.",
            ),
        ),
        ReviewAgent.CODE_REVIEWER: ReviewProfileDefinition(
            display_name="Code Reviewer",
            focus="Focus on the exact changed surface, concrete failure modes, nearby contracts, and evidence gaps.",
            escalation_signals=(
                "A changed caller or dependent contract is not represented in the evidence.",
                "The diff changes a failure path without an observable verification point.",
            ),
        ),
        ReviewAgent.SECURITY_REVIEWER: ReviewProfileDefinition(
            display_name="Security Reviewer",
            focus=(
                "Focus on authentication, authorization, input validation, secrets, dependency, "
                "payment, webhook, and access-control checks."
            ),
            escalation_signals=(
                "The change touches identity, payment, webhook, or access-control boundaries.",
                "A security invariant depends on runtime or infrastructure evidence.",
            ),
        ),
        ReviewAgent.SILENT_FAILURE_HUNTER: ReviewProfileDefinition(
            display_name="Silent Failure Hunter",
            focus=(
                "Focus on swallowed errors, dangerous fallbacks, lost error propagation, timeout, "
                "rollback, and observability checks."
            ),
            escalation_signals=(
                "An error can be converted into a successful-looking response.",
                "Timeout, rollback, retry, or alerting behavior is not observable in the evidence.",
            ),
        ),
        ReviewAgent.CODE_EXPLORER: ReviewProfileDefinition(
            display_name="Faraday — Evidence Investigator",
            focus="Focus on the execution path, callers, dependencies, and architecture boundaries.",
            escalation_signals=(
                "The execution path leaves the inspected repository or indexed graph.",
                "A dependency or caller contract cannot be verified from the current evidence.",
            ),
        ),
        ReviewAgent.TYPESCRIPT_REVIEWER: ReviewProfileDefinition(
            display_name="TypeScript Reviewer",
            focus="Focus on types, asynchronous and error contracts, narrowing, and unsafe casts.",
            escalation_signals=(
                "A type or async contract is enforced only at runtime.",
                "An unsafe cast or broad catch can hide an incompatible producer.",
            ),
        ),
        ReviewAgent.REACT_REVIEWER: ReviewProfileDefinition(
            display_name="React Reviewer",
            focus=(
                "Focus on component state, rendering branches, effects, accessibility, and "
                "user-visible behavior."
            ),
            escalation_signals=(
                "A state or effect transition is not covered by an observable user path.",
                "Accessibility or rendered behavior depends on an unverified browser/runtime condition.",
            ),
        ),
    }
)


REVIEW_BUNDLES: Mapping[ReviewBundle, tuple[ReviewAgent, ...]] = MappingProxyType(
    {
        ReviewBundle.ORDINARY_MR: (
            ReviewAgent.CODE_EXPLORER,
            ReviewAgent.CODE_REVIEWER,
            ReviewAgent.PR_TEST_ANALYZER,
        ),
        ReviewBundle.WIDGET: (
            ReviewAgent.CODE_EXPLORER,
            ReviewAgent.REACT_REVIEWER,
            ReviewAgent.TYPESCRIPT_REVIEWER,
            ReviewAgent.PR_TEST_ANALYZER,
        ),
        ReviewBundle.SECURITY: (
            ReviewAgent.CODE_EXPLORER,
            ReviewAgent.SECURITY_REVIEWER,
            ReviewAgent.SILENT_FAILURE_HUNTER,
        ),
        ReviewBundle.AUTOTEST: (
            ReviewAgent.CODE_REVIEWER,
            ReviewAgent.PR_TEST_ANALYZER,
            ReviewAgent.TYPESCRIPT_REVIEWER,
        ),
        ReviewBundle.REQUIREMENTS: (
            ReviewAgent.CODE_EXPLORER,
            ReviewAgent.CODE_REVIEWER,
        ),
    }
)


def bundle_profiles(bundle: ReviewBundle) -> tuple[ReviewAgent, ...]:
    return REVIEW_BUNDLES[bundle]


def build_review_route(agent_profile: ReviewAgent) -> ReviewRoute:
    definition = REVIEW_PROFILES[agent_profile]
    return ReviewRoute(
        profile=agent_profile,
        display_name=definition.display_name,
        focus=definition.focus,
        required_sections=list(REQUIRED_REVIEW_SECTIONS),
        constraints=list(COMMON_CONSTRAINTS),
        escalation_signals=list(definition.escalation_signals),
        read_only=True,
        host_owns_decisions=True,
    )
