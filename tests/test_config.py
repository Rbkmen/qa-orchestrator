import pytest

from qa_router_mcp.config import PINNED_MODELS, Settings
from qa_router_mcp.contracts import DraftKind


def test_settings_use_pinned_safe_defaults(monkeypatch):
    for name in ("QA_ROUTER_ENABLED", "QA_ROUTER_MODEL", "QA_ROUTER_CONTEXT"):
        monkeypatch.delenv(name, raising=False)

    settings = Settings.from_env()

    assert settings.enabled is True
    assert settings.model == "qwen/qwen3.5-9b"
    assert settings.context == 16_384
    assert settings.ttl_seconds == 300
    assert settings.timeout_seconds == 90
    assert settings.max_input_chars == 40_000
    assert settings.max_output_tokens == 3_072
    assert settings.max_parallel == 1
    assert settings.context_reserve_tokens == 512
    assert settings.metrics_source == "interactive"
    assert settings.metrics_retention_days == 30
    assert settings.metrics_max_events == 10_000
    assert settings.profile_version == "router-v11"
    assert settings.input_limit(DraftKind.SHORT_EXPLANATION) == 6_000
    assert settings.input_limit(DraftKind.LOG_SUMMARY) == 40_000


def test_review_checklist_has_a_bounded_budget():
    settings = Settings()

    assert settings.input_limit(DraftKind.REVIEW_CHECKLIST) == 24_000
    assert settings.output_limit(DraftKind.REVIEW_CHECKLIST, "evidence") == 2_048


@pytest.mark.parametrize(
    ("kind", "content", "expected"),
    [
        (DraftKind.TEST_CASES, "Draft 3 test cases", 1_024),
        (DraftKind.TEST_CASES, "Draft 6 test cases", 2_048),
        (DraftKind.TEST_CASES, "Draft 12 test cases", 3_072),
        (DraftKind.AUTOMATION_SKELETON, "x" * 1_000, 1_536),
        (DraftKind.AUTOMATION_SKELETON, "x" * 7_000, 3_072),
        (DraftKind.TEXT_SUMMARY, "x" * 1_000, 768),
        (DraftKind.TEXT_SUMMARY, "x" * 7_000, 2_048),
        (DraftKind.LOG_SUMMARY, "x" * 1_000, 768),
        (DraftKind.LOG_SUMMARY, "x" * 10_000, 1_024),
        (DraftKind.LOG_SUMMARY, "x" * 30_000, 1_536),
        (DraftKind.TRANSLATION, "x" * 500, 512),
        (DraftKind.TRANSLATION, "x" * 2_000, 1_024),
        (DraftKind.TRANSLATION, "x" * 8_000, 1_536),
        (DraftKind.REWRITE, "x" * 500, 512),
        (DraftKind.SHORT_EXPLANATION, "x" * 1_000, 512),
    ],
)
def test_settings_apply_adaptive_output_budgets(kind, content, expected):
    settings = Settings()

    assert settings.output_limit(kind, content) == expected


def test_adaptive_output_budget_respects_global_cap():
    settings = Settings(max_output_tokens=700)

    assert settings.output_limit(DraftKind.LOG_SUMMARY, "x" * 30_000) == 700


def test_zero_disables_router(monkeypatch):
    monkeypatch.setenv("QA_ROUTER_ENABLED", "0")

    assert Settings.from_env().enabled is False


def test_disabled_marker_turns_off_router(monkeypatch, tmp_path):
    monkeypatch.setenv("QA_ROUTER_ENABLED", "1")
    monkeypatch.setenv("QA_ROUTER_DATA_DIR", str(tmp_path))
    (tmp_path / "disabled").touch()

    assert Settings.from_env().enabled is False


@pytest.mark.parametrize(
    "url",
    [
        "https://remote.example.test",
        "https://127.0.0.1:1234",
        "http://localhost:1234@evil.example:80",
        "http://user:password@127.0.0.1:1234",
        "http://127.0.0.1",
        "http://localhost:not-a-port",
        "http://127.0.0.1:1234/path",
        "http://127.0.0.1:1234?",
        "http://127.0.0.1:1234?target=remote",
        "http://127.0.0.1:1234#",
    ],
)
def test_non_loopback_or_ambiguous_lmstudio_url_is_rejected(url):
    with pytest.raises(ValueError, match="loopback"):
        Settings(lmstudio_url=url)


@pytest.mark.parametrize(
    "url",
    ["http://127.0.0.1:1234", "http://localhost:1234"],
)
def test_explicit_loopback_lmstudio_url_is_allowed(url):
    assert Settings(lmstudio_url=url).lmstudio_url == url


def test_unpinned_model_or_unknown_metrics_source_is_rejected():
    with pytest.raises(ValueError, match="pinned model"):
        Settings(model="another-model")
    with pytest.raises(ValueError, match="metrics source"):
        Settings(metrics_source="unknown")
    with pytest.raises(ValueError, match="metrics retention"):
        Settings(metrics_retention_days=0)
    with pytest.raises(ValueError, match="metrics retention"):
        Settings(metrics_max_events=0)


def test_qwen_is_the_only_allowed_local_model():
    assert PINNED_MODELS == {"qwen/qwen3.5-9b"}


@pytest.mark.parametrize(
    "override",
    [
        {"max_output_tokens": 0},
        {"max_input_chars": 0},
        {"timeout_seconds": 0},
    ],
)
def test_runtime_limits_must_be_positive(override):
    with pytest.raises(ValueError, match="limits must be positive"):
        Settings(**override)
