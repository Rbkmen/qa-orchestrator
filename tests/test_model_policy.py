import json
import os
import stat

import pytest

from qa_orchestrator.cli import PROVIDER_OPTIONS, main
from qa_orchestrator.model_policy import (
    MODEL_CATALOGS,
    ModelProvider,
    ModelSelection,
    load_model_selection,
    reasoning_options_for,
    save_model_selection,
)
from qa_orchestrator.orchestration import OrchestrationStep, build_model_policies
from qa_orchestrator.service import OrchestratorService


def test_model_selection_round_trips_without_credentials(tmp_path):
    path = tmp_path / "model-policy.json"
    selection = ModelSelection(
        provider=ModelProvider.ANTHROPIC,
        triage_model="claude-fast",
        primary_model="claude-balanced",
        deep_model="claude-deep",
        synthesis_model="claude-balanced",
    )

    save_model_selection(path, selection)

    assert load_model_selection(path) == selection
    assert "api_key" not in json.loads(path.read_text(encoding="utf-8"))


@pytest.mark.skipif(os.name == "nt", reason="POSIX permission modes are not available")
def test_saving_policy_preserves_existing_parent_permissions(tmp_path):
    parent = tmp_path / "shared"
    parent.mkdir()
    parent.chmod(0o755)
    path = parent / "model-policy.json"

    save_model_selection(
        path,
        ModelSelection(
            provider=ModelProvider.OPENAI,
            triage_model="triage",
            primary_model="primary",
            deep_model="deep",
            synthesis_model="synthesis",
        ),
    )

    assert stat.S_IMODE(parent.stat().st_mode) == 0o755
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


@pytest.mark.skipif(os.name == "nt", reason="POSIX permission modes are not available")
def test_saving_policy_creates_a_private_parent_directory(tmp_path):
    parent = tmp_path / "private"
    path = parent / "model-policy.json"

    save_model_selection(
        path,
        ModelSelection(
            provider=ModelProvider.OPENAI,
            triage_model="triage",
            primary_model="primary",
            deep_model="deep",
            synthesis_model="synthesis",
        ),
    )

    assert stat.S_IMODE(parent.stat().st_mode) == 0o700
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_service_uses_selected_models(tmp_path):
    save_model_selection(
        tmp_path / "model-policy.json",
        ModelSelection(
            provider=ModelProvider.ANTHROPIC,
            triage_model="claude-triage",
            primary_model="claude-review",
            deep_model="claude-deep",
            synthesis_model="claude-synthesis",
        ),
    )
    service = OrchestratorService.from_settings(data_dir=tmp_path)

    session = service.start_qa_orchestration("ordinary_review")

    assert session.model_policy.model == "claude-triage"
    assert session.model_policy.provider is ModelProvider.ANTHROPIC


def test_setup_offers_only_openai_and_anthropic():
    assert tuple(provider for provider, _ in PROVIDER_OPTIONS) == (
        ModelProvider.OPENAI,
        ModelProvider.ANTHROPIC,
    )


def test_model_catalog_contains_current_recommended_models():
    assert [entry.model_id for entry in MODEL_CATALOGS[ModelProvider.OPENAI]] == [
        "gpt-6-astra",
        "gpt-6-sol",
        "gpt-6-luna",
        "gpt-5.6-sol",
        "gpt-5.6-terra",
        "gpt-5.6-luna",
        "gpt-4.1",
    ]
    assert [entry.model_id for entry in MODEL_CATALOGS[ModelProvider.ANTHROPIC]] == [
        "claude-fable-5-1",
        "claude-opus-5-5",
        "claude-opus-5",
        "claude-sonnet-5",
        "claude-haiku-4-5-20251001",
    ]
    assert reasoning_options_for(ModelProvider.OPENAI, "gpt-6-astra") == (
        "low",
        "medium",
        "high",
        "xhigh",
        "max",
    )
    assert reasoning_options_for(ModelProvider.OPENAI, "gpt-6-sol") == (
        "none",
        "low",
        "medium",
        "high",
        "xhigh",
        "max",
    )
    assert reasoning_options_for(ModelProvider.OPENAI, "gpt-6-luna") == (
        "none",
        "low",
        "medium",
        "high",
        "xhigh",
        "max",
    )
    assert reasoning_options_for(ModelProvider.ANTHROPIC, "claude-haiku-4-5-20251001") == (
        "none",
    )


@pytest.mark.parametrize(
    ("provider", "model", "reasoning"),
    [
        (ModelProvider.OPENAI, "gpt-6-astra", "none"),
        (ModelProvider.OPENAI, "gpt-5.4", "max"),
        (ModelProvider.OPENAI, "gpt-4.1", "high"),
        (ModelProvider.ANTHROPIC, "claude-haiku-4-5-20251001", "high"),
    ],
)
def test_model_selection_rejects_unsupported_reasoning(provider, model, reasoning):
    with pytest.raises(ValueError, match="triage_reasoning"):
        ModelSelection(
            provider=provider,
            triage_model=model,
            primary_model=("gpt-6-sol" if provider is ModelProvider.OPENAI else "claude-sonnet-5"),
            deep_model=("gpt-6-sol" if provider is ModelProvider.OPENAI else "claude-sonnet-5"),
            synthesis_model=("gpt-6-sol" if provider is ModelProvider.OPENAI else "claude-sonnet-5"),
            triage_reasoning=reasoning,
        )


@pytest.mark.parametrize(
    ("provider", "model"),
    [
        (ModelProvider.OPENAI, "claude-sonnet-5"),
        (ModelProvider.ANTHROPIC, "gpt-6-sol"),
    ],
)
def test_model_selection_rejects_known_cross_provider_model(provider, model):
    with pytest.raises(ValueError, match="does not belong"):
        ModelSelection(
            provider=provider,
            triage_model=model,
            primary_model="review",
            deep_model="deep",
            synthesis_model="synthesis",
        )


def test_openai_reasoning_reaches_each_configured_stage():
    selection = ModelSelection(
        provider=ModelProvider.OPENAI,
        triage_model="gpt-5.5",
        primary_model="gpt-6-sol",
        deep_model="gpt-6-sol",
        synthesis_model="gpt-5.5",
        triage_reasoning="low",
        primary_reasoning="high",
        deep_reasoning="xhigh",
        synthesis_reasoning="xhigh",
    )

    policies = build_model_policies(selection)

    assert policies[OrchestrationStep.TRIAGE].reasoning == "low"
    assert policies[OrchestrationStep.PRIMARY_REVIEW].reasoning == "high"
    assert policies[OrchestrationStep.DEEP_REVIEW].reasoning == "xhigh"
    assert policies[OrchestrationStep.SYNTHESIS].reasoning == "xhigh"


def test_setup_command_writes_selected_policy(tmp_path, monkeypatch):
    monkeypatch.setenv("QA_ORCHESTRATOR_DATA_DIR", str(tmp_path))

    assert (
        main(
            [
                "setup",
                "--provider",
                "anthropic",
                "--triage-model",
                "claude-fast",
                "--primary-model",
                "claude-balanced",
                "--deep-model",
                "claude-deep",
                "--synthesis-model",
                "claude-balanced",
            ]
        )
        == 0
    )

    selection = load_model_selection(tmp_path / "model-policy.json")
    assert selection.provider is ModelProvider.ANTHROPIC
    assert selection.primary_model == "claude-balanced"


def test_setup_command_writes_openai_reasoning(tmp_path, monkeypatch):
    monkeypatch.setenv("QA_ORCHESTRATOR_DATA_DIR", str(tmp_path))

    assert (
        main(
            [
                "setup",
                "--provider",
                "openai",
                "--triage-model",
                "gpt-5.5",
                "--primary-model",
                "gpt-6-sol",
                "--deep-model",
                "gpt-6-sol",
                "--synthesis-model",
                "gpt-5.5",
                "--triage-reasoning",
                "low",
                "--primary-reasoning",
                "high",
                "--deep-reasoning",
                "xhigh",
                "--synthesis-reasoning",
                "xhigh",
            ]
        )
        == 0
    )

    selection = load_model_selection(tmp_path / "model-policy.json")
    assert selection.triage_reasoning == "low"
    assert selection.primary_reasoning == "high"
    assert selection.deep_reasoning == "xhigh"
    assert selection.synthesis_reasoning == "xhigh"


def test_setup_menu_can_keep_defaults_and_override_one_model(tmp_path, monkeypatch):
    monkeypatch.setenv("QA_ORCHESTRATOR_DATA_DIR", str(tmp_path))
    answers = iter(("1", "1", "", "", "0", "custom-primary", "", "", "", "", ""))
    monkeypatch.setattr("builtins.input", lambda _prompt: next(answers))

    assert main(["setup"]) == 0

    selection = load_model_selection(tmp_path / "model-policy.json")
    assert selection.triage_model == "gpt-6-luna"
    assert selection.primary_model == "custom-primary"
    assert selection.deep_model == "gpt-6-sol"


def test_setup_menu_orders_each_model_before_its_reasoning(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("QA_ORCHESTRATOR_DATA_DIR", str(tmp_path))
    answers = iter(("", "1", "", "", "", "", "", "", "", ""))
    prompts: list[str] = []

    def answer(prompt: str) -> str:
        prompts.append(prompt)
        return next(answers)

    monkeypatch.setattr("builtins.input", answer)

    assert main(["setup"]) == 0
    assert prompts == [
        "Выбор [1]: ",
        "Номер [1]: ",
        "Выбор [3]: ",
        "Выбор [6]: ",
        "Выбор [2]: ",
        "Выбор [3]: ",
        "Выбор [2]: ",
        "Выбор [4]: ",
        "Выбор [2]: ",
        "Выбор [3]: ",
    ]
    output = capsys.readouterr().out
    assert "1. Русский (RU)" in output
    assert "2. English (EN)" in output
    assert "Reasoning для глубокой проверки" in output
    assert "Первично оценивает задачу и выбирает набор или профиль ревью." in output
    assert "Объединяет результаты ревью в итоговый QA-вывод." in output
    assert "high" in output


def test_setup_menu_supports_english_and_explains_workflow_stages(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("QA_ORCHESTRATOR_DATA_DIR", str(tmp_path))
    answers = iter(("2", "1", "", "", "", "", "", "", "", ""))
    monkeypatch.setattr("builtins.input", lambda _prompt: next(answers))

    assert main(["setup"]) == 0

    output = capsys.readouterr().out
    assert "Step 1/3. Choose a provider" in output
    assert "Triage model" in output
    assert "Initially assesses the task and selects a review bundle or profile." in output
    assert "Primary review model" in output
    assert "Reviews the changes using the selected review profiles." in output
    assert "Deep review model" in output
    assert "Provides deeper analysis for complex or escalated cases." in output
    assert "Synthesis model" in output
    assert "Combines review results into the final QA outcome." in output
    policy = json.loads((tmp_path / "model-policy.json").read_text(encoding="utf-8"))
    assert "language" not in policy


def test_setup_menu_can_go_back_to_provider(tmp_path, monkeypatch):
    monkeypatch.setenv("QA_ORCHESTRATOR_DATA_DIR", str(tmp_path))
    answers = iter(("1", "1", "b", "2", "1", "1", "1", "1", "1", "1", "1", "1"))
    monkeypatch.setattr("builtins.input", lambda _prompt: next(answers))

    assert main(["setup"]) == 0

    selection = load_model_selection(tmp_path / "model-policy.json")
    assert selection.provider is ModelProvider.ANTHROPIC


def test_setup_uses_colors_when_forced(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("QA_ORCHESTRATOR_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("FORCE_COLOR", "1")
    answers = iter(("1", "1", "", "", "", "", "", "", "", ""))
    monkeypatch.setattr("builtins.input", lambda _prompt: next(answers))

    assert main(["setup"]) == 0

    output = capsys.readouterr().out
    assert "\033[36mOpenAI\033[0m" in output
    assert "\033[34mGPT-6 Astra" in output
    assert "\033[35mlow\033[0m" in output


def test_reload_command_reads_current_policy(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("QA_ORCHESTRATOR_DATA_DIR", str(tmp_path))
    selection = ModelSelection(
        provider=ModelProvider.OPENAI,
        triage_model="gpt-6-luna",
        primary_model="gpt-6-sol",
        deep_model="gpt-6-sol",
        synthesis_model="gpt-6-sol",
        deep_reasoning="low",
    )
    save_model_selection(tmp_path / "model-policy.json", selection)

    assert main(["reload"]) == 0

    output = capsys.readouterr().out
    assert "перечитана и проверена" in output
    assert '"deep_reasoning": "low"' in output


def test_setup_menu_offers_anthropic_model_ids(tmp_path, monkeypatch):
    monkeypatch.setenv("QA_ORCHESTRATOR_DATA_DIR", str(tmp_path))
    answers = iter(("1", "2", "4", "3", "3", "2", "5", "1", "2", "2"))
    monkeypatch.setattr("builtins.input", lambda _prompt: next(answers))

    assert main(["setup"]) == 0

    selection = load_model_selection(tmp_path / "model-policy.json")
    assert selection.provider is ModelProvider.ANTHROPIC
    assert selection.triage_model == "claude-sonnet-5"
    assert selection.primary_model == "claude-opus-5"
    assert selection.deep_model == "claude-haiku-4-5-20251001"
    assert selection.synthesis_model == "claude-opus-5-5"
    assert selection.triage_reasoning == "high"
    assert selection.primary_reasoning == "medium"
    assert selection.deep_reasoning == "none"
    assert selection.synthesis_reasoning == "medium"


@pytest.mark.parametrize("model", ["secret/path", "model with spaces", "../model"])
def test_model_selection_rejects_path_like_or_free_form_values(model):
    with pytest.raises(ValueError):
        ModelSelection(
            provider=ModelProvider.OPENAI,
            triage_model=model,
            primary_model="review",
            deep_model="deep",
            synthesis_model="synthesis",
        )
