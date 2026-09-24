from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType

from qa_orchestrator.contracts import ReviewAgent, ReviewBundle, ReviewRoute

COMMON_CONSTRAINTS = (
    "Use only the evidence gathered by the host agent.",
    "Cite stable Evidence Packet references for every finding candidate.",
    "Keep possible defects separate from coverage gaps; leave confirmation and final decisions to the host.",
    "Do not decide severity, priority, root cause, release readiness, or merge readiness.",
    "Do not perform external writes.",
)


@dataclass(frozen=True, slots=True)
class ReviewProfileDefinition:
    display_name: str
    focus: str
    required_sections: tuple[str, ...]
    escalation_signals: tuple[str, ...]


REVIEW_PROFILES: Mapping[ReviewAgent, ReviewProfileDefinition] = MappingProxyType(
    {
        ReviewAgent.PR_TEST_ANALYZER: ReviewProfileDefinition(
            display_name="Test Analyzer",
            focus=(
                "Compare changed behavior with happy-path, negative, edge, integration, "
                "and meaningful assertion coverage. Report test and assertion gaps only; "
                "leave unrelated implementation defects to the code reviewer."
            ),
            required_sections=("Scope", "Coverage Gaps", "Unverified"),
            escalation_signals=(
                "Changed behavior crosses an unverified integration boundary.",
                "Assertions do not distinguish success from a silently wrong result.",
            ),
        ),
        ReviewAgent.CODE_REVIEWER: ReviewProfileDefinition(
            display_name="Code Reviewer",
            focus=(
                "Inspect the changed surface for concrete failure modes and contract breaks. "
                "Use the Evidence Map as context; add caller or dependency detail only when "
                "needed to support a candidate, and do not repeat the map."
            ),
            required_sections=("Scope", "Finding Candidates", "Coverage Gaps", "Unverified"),
            escalation_signals=(
                "A changed caller or dependent contract is not represented in the evidence.",
                "The diff changes a failure path without an observable verification point.",
            ),
        ),
        ReviewAgent.SECURITY_REVIEWER: ReviewProfileDefinition(
            display_name="Security Reviewer",
            focus=(
                "Focus on authentication, authorization, input validation, secrets, sensitive-data "
                "exposure, dependencies, payment, webhook, privacy, and access-control checks."
            ),
            required_sections=("Scope", "Finding Candidates", "Coverage Gaps", "Unverified"),
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
            required_sections=("Scope", "Finding Candidates", "Coverage Gaps", "Unverified"),
            escalation_signals=(
                "An error can be converted into a successful-looking response.",
                "Timeout, rollback, retry, or alerting behavior is not observable in the evidence.",
            ),
        ),
        ReviewAgent.CODE_EXPLORER: ReviewProfileDefinition(
            display_name="Faraday — Evidence Investigator",
            focus=(
                "Map the execution path, callers, dependencies, data flow, and architecture "
                "boundaries. Return evidence references and unresolved links only; do not "
                "diagnose defects or restate the full diff."
            ),
            required_sections=("Scope", "Evidence Map", "Unverified"),
            escalation_signals=(
                "The execution path leaves the inspected repository or indexed graph.",
                "A dependency or caller contract cannot be verified from the current evidence.",
            ),
        ),
        ReviewAgent.TYPESCRIPT_REVIEWER: ReviewProfileDefinition(
            display_name="TypeScript Reviewer",
            focus="Focus on types, asynchronous and error contracts, narrowing, and unsafe casts.",
            required_sections=("Scope", "Finding Candidates", "Coverage Gaps", "Unverified"),
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
            required_sections=("Scope", "Finding Candidates", "Coverage Gaps", "Unverified"),
            escalation_signals=(
                "A state or effect transition is not covered by an observable user path.",
                "Accessibility or rendered behavior depends on an unverified browser/runtime condition.",
            ),
        ),
        ReviewAgent.RUBY_REVIEWER: ReviewProfileDefinition(
            display_name="Ruby Reviewer",
            focus=(
                "Review changed Ruby behavior, not style preferences. Check nil/false truthiness, "
                "block and Enumerator return contracts, mutating APIs whose return value may be "
                "nil when unchanged, exception propagation, rescue/ensure, resource cleanup, and "
                "shared mutable state when concurrency is involved. Identify the actual persistence "
                "library from repository evidence. For ActiveRecord, inspect changed query composition, "
                "eager loading, and transaction/callback ordering. For Sequel, check whether immutable "
                "Dataset results are retained/materialized and how changed queries interact with "
                "transaction boundaries; do not infer ActiveRecord from Rails alone. When Sidekiq is "
                "present, inspect only relevant argument serialization, retry/idempotency, queue, "
                "uniqueness, and concurrency behavior. Follow the repository's RSpec or Minitest "
                "conventions; do not prescribe a test framework."
            ),
            required_sections=("Scope", "Finding Candidates", "Coverage Gaps", "Unverified"),
            escalation_signals=(
                "A changed Ruby truthiness or nil-return path can silently skip or corrupt behavior.",
                "A changed ActiveRecord/Sequel query or transaction path depends on an unverified contract.",
                "A Sidekiq retry or concurrency path can repeat or lose an external side effect.",
            ),
        ),
        ReviewAgent.PYTHON_REVIEWER: ReviewProfileDefinition(
            display_name="Python Reviewer",
            focus=(
                "Review changed Python behavior, not style preferences. Check None/truthiness and "
                "exception boundaries, async cancellation/timeouts and resource cleanup, mutable "
                "defaults and shared state, plus type/model validation and serialization. When "
                "FastMCP/MCP is present, inspect changed tool input/output schemas, error mapping, "
                "credential boundaries, and retry/idempotency contracts only where relevant. Follow "
                "the repository's Python and test conventions; do not prescribe a framework."
            ),
            required_sections=("Scope", "Finding Candidates", "Coverage Gaps", "Unverified"),
            escalation_signals=(
                "A changed async cancellation, timeout, or cleanup path can leak work or resources.",
                "A changed MCP schema, error, or credential boundary depends on an unverified contract.",
                "Retry or concurrency can duplicate a side effect or return success after tool failure.",
            ),
        ),
        ReviewAgent.MOBILE_REVIEWER: ReviewProfileDefinition(
            display_name="Mobile Reviewer",
            focus=(
                "Review changed React Native and native iOS/Android behavior. Check app lifecycle, "
                "navigation and deep links, permissions, bridge/WebView/native-module contracts, "
                "background/resume transitions, storage and notifications, and platform-specific "
                "build/config changes where touched. Check visible state and platform parity only "
                "for affected paths; do not infer device/runtime success from TypeScript or Jest."
            ),
            required_sections=("Scope", "Finding Candidates", "Coverage Gaps", "Unverified"),
            escalation_signals=(
                "A native permission, module, or build change has only JavaScript/unit evidence.",
                "A changed iOS/Android branch can diverge without a device-visible verification point.",
                "A lifecycle, background, or deep-link change depends on an unverified OS contract.",
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
        ReviewBundle.WIDGET_JS: (
            ReviewAgent.CODE_EXPLORER,
            ReviewAgent.CODE_REVIEWER,
            ReviewAgent.REACT_REVIEWER,
            ReviewAgent.PR_TEST_ANALYZER,
        ),
        ReviewBundle.RUBY_BACKEND: (
            ReviewAgent.CODE_EXPLORER,
            ReviewAgent.RUBY_REVIEWER,
            ReviewAgent.PR_TEST_ANALYZER,
        ),
        ReviewBundle.PYTHON_BACKEND: (
            ReviewAgent.CODE_EXPLORER,
            ReviewAgent.PYTHON_REVIEWER,
            ReviewAgent.PR_TEST_ANALYZER,
        ),
        ReviewBundle.MOBILE: (
            ReviewAgent.CODE_EXPLORER,
            ReviewAgent.MOBILE_REVIEWER,
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
        required_sections=list(definition.required_sections),
        constraints=list(COMMON_CONSTRAINTS),
        escalation_signals=list(definition.escalation_signals),
        read_only=True,
        host_owns_decisions=True,
    )
