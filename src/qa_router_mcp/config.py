from dataclasses import dataclass
from os import environ
from pathlib import Path
from urllib.parse import urlsplit

from qa_router_mcp.contracts import DraftKind
from qa_router_mcp.validation import requested_case_count

INPUT_CHAR_BUDGETS = {
    DraftKind.TEST_CASES: 20_000,
    DraftKind.LOG_SUMMARY: 40_000,
    DraftKind.AUTOMATION_SKELETON: 20_000,
    DraftKind.TRANSLATION: 12_000,
    DraftKind.REWRITE: 12_000,
    DraftKind.SHORT_EXPLANATION: 6_000,
    DraftKind.TEXT_SUMMARY: 24_000,
    DraftKind.REVIEW_CHECKLIST: 24_000,
}

OUTPUT_TOKEN_BUDGETS = {
    DraftKind.TEST_CASES: 3_072,
    DraftKind.LOG_SUMMARY: 1_536,
    DraftKind.AUTOMATION_SKELETON: 3_072,
    DraftKind.TRANSLATION: 1_536,
    DraftKind.REWRITE: 1_536,
    DraftKind.SHORT_EXPLANATION: 512,
    DraftKind.TEXT_SUMMARY: 2_048,
    DraftKind.REVIEW_CHECKLIST: 2_048,
}

PINNED_MODELS = {
    "qwen/qwen3.5-9b",
}

METRICS_SOURCES = {"interactive", "benchmark", "smoke"}


@dataclass(frozen=True, slots=True)
class Settings:
    enabled: bool = True
    model: str = "qwen/qwen3.5-9b"
    lmstudio_url: str = "http://127.0.0.1:1234"
    context: int = 16_384
    max_output_tokens: int = 3_072
    max_input_chars: int = 40_000
    timeout_seconds: float = 90.0
    max_parallel: int = 1
    ttl_seconds: int = 300
    context_reserve_tokens: int = 512
    metrics_source: str = "interactive"
    metrics_retention_days: int = 30
    metrics_max_events: int = 10_000
    profile_version: str = "router-v11"
    data_dir: Path = Path.home() / ".qa-router"

    def input_limit(self, kind: DraftKind) -> int:
        return min(self.max_input_chars, INPUT_CHAR_BUDGETS[kind])

    def output_limit(self, kind: DraftKind, content: str) -> int:
        input_chars = len(content)
        if kind == DraftKind.TEST_CASES:
            case_count = requested_case_count(content)
            adaptive_limit = 1_024 if case_count <= 3 else 2_048
            if case_count >= 7:
                adaptive_limit = 3_072
        elif kind == DraftKind.AUTOMATION_SKELETON:
            adaptive_limit = 1_536 if input_chars <= 6_000 else 3_072
        elif kind == DraftKind.TEXT_SUMMARY:
            adaptive_limit = 768 if input_chars <= 6_000 else 2_048
        elif kind == DraftKind.LOG_SUMMARY:
            adaptive_limit = 768 if input_chars <= 8_000 else 1_024
            if input_chars > 24_000:
                adaptive_limit = 1_536
        elif kind in {DraftKind.TRANSLATION, DraftKind.REWRITE}:
            adaptive_limit = 512 if input_chars <= 1_000 else 1_024
            if input_chars > 6_000:
                adaptive_limit = 1_536
        else:
            adaptive_limit = OUTPUT_TOKEN_BUDGETS[kind]
        return min(self.max_output_tokens, OUTPUT_TOKEN_BUDGETS[kind], adaptive_limit)

    @property
    def metrics_path(self) -> Path:
        return self.data_dir / "metrics.jsonl"

    def __post_init__(self) -> None:
        if self.model not in PINNED_MODELS:
            raise ValueError("model must match a pinned model in the local MLX catalog")
        if self.context != 16_384 or self.max_parallel != 1:
            raise ValueError("context and parallelism must match the verified profile")
        parsed_url = urlsplit(self.lmstudio_url)
        try:
            port = parsed_url.port
        except ValueError as exc:
            raise ValueError("LM Studio URL must use loopback") from exc
        canonical_url = f"http://{parsed_url.hostname}:{port}"
        if (
            parsed_url.scheme != "http"
            or parsed_url.hostname not in {"127.0.0.1", "localhost"}
            or port is None
            or self.lmstudio_url != canonical_url
        ):
            raise ValueError("LM Studio URL must use loopback")
        if self.ttl_seconds < 0:
            raise ValueError("TTL must be non-negative")
        if self.max_output_tokens < 1 or self.max_input_chars < 1 or self.timeout_seconds <= 0:
            raise ValueError("input, output, and timeout limits must be positive")
        if not 0 < self.context_reserve_tokens < self.context:
            raise ValueError("context reserve must fit the verified context")
        if self.metrics_source not in METRICS_SOURCES:
            raise ValueError("metrics source must be interactive, benchmark, or smoke")
        if self.metrics_retention_days < 1 or self.metrics_max_events < 1:
            raise ValueError("metrics retention must be positive")

    @classmethod
    def from_env(cls) -> "Settings":
        defaults = cls()
        data_dir = Path(environ.get("QA_ROUTER_DATA_DIR", str(defaults.data_dir)))
        return cls(
            enabled=(
                environ.get("QA_ROUTER_ENABLED", "1") == "1"
                and not (data_dir / "disabled").exists()
            ),
            model=environ.get("QA_ROUTER_MODEL", defaults.model),
            lmstudio_url=environ.get("QA_ROUTER_LMSTUDIO_URL", defaults.lmstudio_url),
            context=int(environ.get("QA_ROUTER_CONTEXT", str(defaults.context))),
            max_output_tokens=int(
                environ.get("QA_ROUTER_MAX_OUTPUT", str(defaults.max_output_tokens))
            ),
            max_input_chars=int(
                environ.get("QA_ROUTER_MAX_INPUT_CHARS", str(defaults.max_input_chars))
            ),
            timeout_seconds=float(environ.get("QA_ROUTER_TIMEOUT", str(defaults.timeout_seconds))),
            max_parallel=1,
            ttl_seconds=int(environ.get("QA_ROUTER_TTL_SECONDS", str(defaults.ttl_seconds))),
            context_reserve_tokens=int(
                environ.get("QA_ROUTER_CONTEXT_RESERVE", str(defaults.context_reserve_tokens))
            ),
            metrics_source=environ.get("QA_ROUTER_METRICS_SOURCE", defaults.metrics_source),
            metrics_retention_days=int(
                environ.get(
                    "QA_ROUTER_METRICS_RETENTION_DAYS", str(defaults.metrics_retention_days)
                )
            ),
            metrics_max_events=int(
                environ.get("QA_ROUTER_METRICS_MAX_EVENTS", str(defaults.metrics_max_events))
            ),
            data_dir=data_dir,
        )
