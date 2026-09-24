from dataclasses import dataclass
from os import environ
from pathlib import Path


@dataclass(frozen=True, slots=True)
class Settings:
    orchestration_session_ttl_seconds: int = 1_800
    orchestration_max_sessions: int = 100
    data_dir: Path = Path.home() / ".qa-orchestrator"

    @property
    def model_policy_path(self) -> Path:
        return Path(
            environ.get(
                "QA_ORCHESTRATOR_MODEL_POLICY_PATH",
                str(self.data_dir / "model-policy.json"),
            )
        )

    def __post_init__(self) -> None:
        if self.orchestration_session_ttl_seconds < 1 or self.orchestration_max_sessions < 1:
            raise ValueError("orchestration limits must be positive")

    @classmethod
    def from_env(cls) -> "Settings":
        defaults = cls()
        data_dir = Path(environ.get("QA_ORCHESTRATOR_DATA_DIR", str(defaults.data_dir)))
        return cls(
            orchestration_session_ttl_seconds=int(
                environ.get(
                    "QA_ORCHESTRATOR_ORCHESTRATION_TTL_SECONDS",
                    str(defaults.orchestration_session_ttl_seconds),
                )
            ),
            orchestration_max_sessions=int(
                environ.get(
                    "QA_ORCHESTRATOR_ORCHESTRATION_MAX_SESSIONS",
                    str(defaults.orchestration_max_sessions),
                )
            ),
            data_dir=data_dir,
        )
