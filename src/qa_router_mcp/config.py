from dataclasses import dataclass
from os import environ
from pathlib import Path


@dataclass(frozen=True, slots=True)
class Settings:
    metrics_retention_days: int = 30
    metrics_max_events: int = 10_000
    data_dir: Path = Path.home() / ".qa-router"

    @property
    def metrics_path(self) -> Path:
        return self.data_dir / "metrics.jsonl"

    def __post_init__(self) -> None:
        if self.metrics_retention_days < 1 or self.metrics_max_events < 1:
            raise ValueError("metrics retention must be positive")

    @classmethod
    def from_env(cls) -> "Settings":
        defaults = cls()
        data_dir = Path(environ.get("QA_ROUTER_DATA_DIR", str(defaults.data_dir)))
        return cls(
            metrics_retention_days=int(
                environ.get("QA_ROUTER_METRICS_RETENTION_DAYS", str(defaults.metrics_retention_days))
            ),
            metrics_max_events=int(
                environ.get("QA_ROUTER_METRICS_MAX_EVENTS", str(defaults.metrics_max_events))
            ),
            data_dir=data_dir,
        )
