from dataclasses import dataclass

from qa_router_mcp.contracts import ReviewAgent, ReviewRoute

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
    focus: str
    escalation_signals: tuple[str, ...]


REVIEW_PROFILES: dict[ReviewAgent, ReviewProfileDefinition] = {
    ReviewAgent.PR_TEST_ANALYZER: ReviewProfileDefinition(
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
        focus="Focus on the exact changed surface, concrete failure modes, nearby contracts, and evidence gaps.",
        escalation_signals=(
            "A changed caller or dependent contract is not represented in the evidence.",
            "The diff changes a failure path without an observable verification point.",
        ),
    ),
    ReviewAgent.SECURITY_REVIEWER: ReviewProfileDefinition(
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
        focus="Focus on the execution path, callers, dependencies, and architecture boundaries.",
        escalation_signals=(
            "The execution path leaves the inspected repository or indexed graph.",
            "A dependency or caller contract cannot be verified from the current evidence.",
        ),
    ),
    ReviewAgent.TYPESCRIPT_REVIEWER: ReviewProfileDefinition(
        focus="Focus on types, asynchronous and error contracts, narrowing, and unsafe casts.",
        escalation_signals=(
            "A type or async contract is enforced only at runtime.",
            "An unsafe cast or broad catch can hide an incompatible producer.",
        ),
    ),
    ReviewAgent.REACT_REVIEWER: ReviewProfileDefinition(
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


def build_review_route(agent_profile: ReviewAgent) -> ReviewRoute:
    definition = REVIEW_PROFILES[agent_profile]
    return ReviewRoute(
        profile=agent_profile,
        focus=definition.focus,
        required_sections=list(REQUIRED_REVIEW_SECTIONS),
        constraints=list(COMMON_CONSTRAINTS),
        escalation_signals=list(definition.escalation_signals),
        read_only=True,
        host_owns_decisions=True,
    )
