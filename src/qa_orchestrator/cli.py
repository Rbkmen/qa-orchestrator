"""Console entry point for the MCP server and local model setup wizard."""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Sequence

from qa_orchestrator.config import Settings
from qa_orchestrator.model_policy import (
    DEFAULT_MODEL_SELECTION,
    MODEL_CATALOGS,
    REASONING_EFFORTS,
    ModelCatalogEntry,
    ModelProvider,
    ModelSelection,
    ReasoningEffort,
    load_model_selection,
    reasoning_options_for,
    save_model_selection,
)
from qa_orchestrator.server import main as serve

PROVIDER_OPTIONS = (
    (ModelProvider.OPENAI, "OpenAI"),
    (ModelProvider.ANTHROPIC, "Anthropic"),
)
ROLE_LABELS = (
    ("triage_model", "triage"),
    ("primary_model", "primary"),
    ("deep_model", "deep"),
    ("synthesis_model", "synthesis"),
)
REASONING_ROLE_LABELS = (
    ("triage_reasoning", "triage"),
    ("primary_reasoning", "primary"),
    ("deep_reasoning", "deep"),
    ("synthesis_reasoning", "synthesis"),
)
REASONING_DESCRIPTIONS = {
    "none": "не передавать параметр reasoning/effort",
    "minimal": "минимальный уровень рассуждений",
    "low": "быстрее и дешевле, подходит для простых задач",
    "medium": "сбалансированный вариант по умолчанию",
    "high": "сложный код, отладка и глубокий анализ",
    "xhigh": "длинные и сложные агентные задачи",
    "max": "максимальный уровень reasoning",
}
REASONING_DESCRIPTIONS_EN = {
    "none": "do not send a reasoning/effort parameter",
    "minimal": "minimum reasoning effort",
    "low": "faster and cheaper; suitable for simple tasks",
    "medium": "balanced default",
    "high": "complex code, debugging, and deep analysis",
    "xhigh": "long and complex agentic tasks",
    "max": "maximum reasoning effort",
}
MODEL_LABELS_EN = {
    "gpt-6-astra": "GPT-6 Astra — highest capability",
    "gpt-6-sol": "GPT-6 Sol — complex code and agentic tasks",
    "gpt-6-luna": "GPT-6 Luna — faster and more economical",
    "gpt-5.6-sol": "GPT-5.6 Sol",
    "gpt-5.6-terra": "GPT-5.6 Terra — balanced quality and cost",
    "gpt-5.6-luna": "GPT-5.6 Luna — economical option",
    "gpt-4.1": "GPT-4.1 — reasoning is not supported",
    "claude-fable-5-1": "Claude Fable 5.1 — complex reasoning tasks",
    "claude-opus-5-5": "Claude Opus 5.5 — highest capability",
    "claude-opus-5": "Claude Opus 5",
    "claude-sonnet-5": "Claude Sonnet 5 — balanced quality and speed",
    "claude-haiku-4-5-20251001": "Claude Haiku 4.5 — fast, no effort control",
}
SETUP_COPY = {
    "ru": {
        "language_title": "\nЯзык настройки / Language:",
        "language_prompt": "Выбор [1]: ",
        "language_invalid": "Выбери 1 (RU) или 2 (EN) / Choose 1 (RU) or 2 (EN).",
        "language_options": ("Русский (RU)", "English (EN)"),
        "provider_title": "Шаг 1/3. Выбери провайдера:",
        "provider_prompt": "Номер [{default}]: ",
        "provider_current": " (текущий)",
        "back_exit": "  b. Назад / выйти",
        "back": "  b. Назад",
        "model_step": "Шаг 2/3. {label}",
        "reasoning_step": "Шаг 3/3. {label} ({parameter})",
        "model_labels": {
            "triage": "Модель триажа",
            "primary": "Модель основного ревью",
            "deep": "Модель глубокой проверки",
            "synthesis": "Модель синтеза",
        },
        "reasoning_labels": {
            "triage": "Reasoning для триажа",
            "primary": "Reasoning для основного ревью",
            "deep": "Reasoning для глубокой проверки",
            "synthesis": "Reasoning для синтеза",
        },
        "stage_descriptions": {
            "triage": "Первично оценивает задачу и выбирает набор или профиль ревью.",
            "primary": "Проверяет изменения по выбранным профилям ревью.",
            "deep": "Дополнительно анализирует сложные или эскалированные случаи.",
            "synthesis": "Объединяет результаты ревью в итоговый QA-вывод.",
        },
        "reasoning_descriptions": REASONING_DESCRIPTIONS,
        "current_model": "Текущая модель: {model}",
        "current_default": " (текущая/по умолчанию)",
        "custom_model": "  0. Ввести другой model ID",
        "custom_model_only": "  1. Ввести model ID",
        "choose_prompt": "Выбор [{default}]: ",
        "model_id_prompt": "Model ID (b — назад): ",
        "model_id_hint": "Каталог локальный: доступность модели и поддержку reasoning у провайдера инструмент не проверяет.",
        "deep_recommendation": "Рекомендация: high для сложных и рискованных проверок.",
        "unsupported_reasoning": "Подсказка: {model} не поддерживает {parameter}; будет сохранено none.",
        "anthropic_reasoning": "Подсказка: Anthropic effort задаёт глубину рассуждений для этой модели.",
        "astra_reasoning": "Подсказка: GPT-6 Astra не поддерживает none; начни с low или medium.",
        "openai_reasoning": "Подсказка: доступные значения зависят от конкретной модели OpenAI.",
        "cancelled": "Настройка отменена.",
        "unreadable_policy": "Текущая модельная политика не читается ({error}); будет создана новая.",
        "invalid_provider": "нужно выбрать номер из меню провайдеров",
        "invalid_model": "{label}: нужно выбрать номер из меню моделей",
        "invalid_custom_model": "{label}: нужно выбрать 1",
        "empty_model": "{label}: значение обязательно",
        "invalid_reasoning": "{label}: нужно выбрать уровень reasoning из меню",
        "unsupported_effort": "{label}: значение {value!r} не поддерживается моделью {model!r}; доступно: {allowed}",
        "back_unavailable": "к выбору провайдера нельзя вернуться в этом режиме",
        "setup_error": "Ошибка настройки",
        "saved": "Модельная политика сохранена: {path}",
        "provider_summary": "Провайдер: {provider}",
        "verify": "Проверь политику командой: qa-orch reload",
        "restart": "Если MCP-клиент уже подключён, перезапусти его соединение.",
    },
    "en": {
        "language_title": "\nSetup language / Язык:",
        "language_prompt": "Choose [1]: ",
        "language_invalid": "Choose 1 (RU) or 2 (EN) / Выбери 1 (RU) или 2 (EN).",
        "language_options": ("Русский (RU)", "English (EN)"),
        "provider_title": "Step 1/3. Choose a provider:",
        "provider_prompt": "Number [{default}]: ",
        "provider_current": " (current)",
        "back_exit": "  b. Back / exit",
        "back": "  b. Back",
        "model_step": "Step 2/3. {label}",
        "reasoning_step": "Step 3/3. {label} ({parameter})",
        "model_labels": {
            "triage": "Triage model",
            "primary": "Primary review model",
            "deep": "Deep review model",
            "synthesis": "Synthesis model",
        },
        "reasoning_labels": {
            "triage": "Triage reasoning",
            "primary": "Primary review reasoning",
            "deep": "Deep review reasoning",
            "synthesis": "Synthesis reasoning",
        },
        "stage_descriptions": {
            "triage": "Initially assesses the task and selects a review bundle or profile.",
            "primary": "Reviews the changes using the selected review profiles.",
            "deep": "Provides deeper analysis for complex or escalated cases.",
            "synthesis": "Combines review results into the final QA outcome.",
        },
        "reasoning_descriptions": REASONING_DESCRIPTIONS_EN,
        "current_model": "Current model: {model}",
        "current_default": " (current/default)",
        "custom_model": "  0. Enter another model ID",
        "custom_model_only": "  1. Enter a model ID",
        "choose_prompt": "Choose [{default}]: ",
        "model_id_prompt": "Model ID (b — back): ",
        "model_id_hint": "The catalog is local; provider availability and reasoning support are not checked here.",
        "deep_recommendation": "Recommendation: use high for complex and high-risk reviews.",
        "unsupported_reasoning": "Tip: {model} does not support {parameter}; none will be saved.",
        "anthropic_reasoning": "Tip: Anthropic effort controls reasoning depth for this model.",
        "astra_reasoning": "Tip: GPT-6 Astra does not support none; start with low or medium.",
        "openai_reasoning": "Tip: available values depend on the specific OpenAI model.",
        "cancelled": "Setup cancelled.",
        "unreadable_policy": "The current model policy cannot be read ({error}); a new one will be created.",
        "invalid_provider": "select a number from the provider menu",
        "invalid_model": "{label}: select a number from the model menu",
        "invalid_custom_model": "{label}: select 1",
        "empty_model": "{label}: a value is required",
        "invalid_reasoning": "{label}: select a reasoning value from the menu",
        "unsupported_effort": "{label}: value {value!r} is not supported by model {model!r}; available: {allowed}",
        "back_unavailable": "cannot go back to provider selection in this mode",
        "setup_error": "Setup error",
        "saved": "Model policy saved: {path}",
        "provider_summary": "Provider: {provider}",
        "verify": "Verify the policy with: qa-orch reload",
        "restart": "If your MCP client is already connected, restart its connection.",
    },
}
_BACK_INPUTS = frozenset({"b", "back", "назад"})
_COLORS = {
    "provider": "\033[36m",
    "model": "\033[34m",
    "reasoning": "\033[35m",
    "selected": "\033[32m",
    "muted": "\033[2m",
}
_RESET_COLOR = "\033[0m"


class _BackRequested(Exception):
    """User asked to return to the previous setup step."""


def _colors_enabled() -> bool:
    if os.environ.get("FORCE_COLOR") == "1":
        return True
    return os.environ.get("NO_COLOR") is None and sys.stdout.isatty()


def _paint(value: str, color: str) -> str:
    if not _colors_enabled():
        return value
    return f"{color}{value}{_RESET_COLOR}"


def _is_back(value: str) -> bool:
    return value.casefold() in _BACK_INPUTS


def _choose_language() -> str:
    copy = SETUP_COPY["ru"]
    print(copy["language_title"])
    for index, label in enumerate(copy["language_options"], start=1):
        print(f"  {index}. {label}")
    print(_paint(copy["back_exit"], _COLORS["muted"]))
    answer = input(copy["language_prompt"]).strip().casefold()
    if _is_back(answer):
        raise _BackRequested
    if answer in {"", "1", "ru"}:
        return "ru"
    if answer in {"2", "en"}:
        return "en"
    raise ValueError(copy["language_invalid"])


def _provider_choice(current: ModelProvider | None, language: str) -> ModelProvider:
    copy = SETUP_COPY[language]
    print(f"\n{copy['provider_title']}")
    for index, (provider, label) in enumerate(PROVIDER_OPTIONS, start=1):
        suffix = copy["provider_current"] if provider is current else ""
        print(f"  {index}. {_paint(label, _COLORS['provider'])}{suffix}")
    print(_paint(copy["back_exit"], _COLORS["muted"]))

    default_index = next(
        (index for index, (provider, _) in enumerate(PROVIDER_OPTIONS, start=1) if provider is current),
        1,
    )
    answer = input(copy["provider_prompt"].format(default=default_index)).strip()
    if _is_back(answer):
        raise _BackRequested
    if not answer:
        return PROVIDER_OPTIONS[default_index - 1][0]
    try:
        index = int(answer)
        if not 1 <= index <= len(PROVIDER_OPTIONS):
            raise ValueError
        return PROVIDER_OPTIONS[index - 1][0]
    except (ValueError, IndexError) as exc:
        raise ValueError(copy["invalid_provider"]) from exc


def _model_value(
    *,
    stage: str,
    provider: ModelProvider,
    current: str | None,
    default: str | None,
    override: str | None,
    language: str,
) -> str:
    copy = SETUP_COPY[language]
    label = copy["model_labels"][stage]
    selected_default = current or default
    if override is not None:
        value = override.strip()
    else:
        print(f"\n{copy['model_step'].format(label=_paint(label, _COLORS['model']))}:")
        print(f"  {copy['stage_descriptions'][stage]}")
        catalog = list(MODEL_CATALOGS.get(provider, ()))
        catalog_ids = {entry.model_id for entry in catalog}
        if selected_default and selected_default not in catalog_ids:
            catalog.insert(
                0,
                ModelCatalogEntry(
                    copy["current_model"].format(model=selected_default),
                    selected_default,
                    reasoning_options_for(provider, selected_default),
                ),
            )

        if catalog:
            default_index = next(
                (
                    index
                    for index, entry in enumerate(catalog, start=1)
                    if entry.model_id == selected_default
                ),
                1,
            )
            for index, entry in enumerate(catalog, start=1):
                suffix = copy["current_default"] if entry.model_id == selected_default else ""
                color = _COLORS["selected"] if entry.model_id == selected_default else _COLORS["model"]
                entry_label = (
                    MODEL_LABELS_EN.get(entry.model_id, entry.label)
                    if language == "en"
                    else entry.label
                )
                print(
                    f"  {index}. {_paint(entry_label, color)}: "
                    f"{_paint(entry.model_id, color)}{suffix}"
                )
            print(_paint(copy["custom_model"], _COLORS["model"]))
            print(_paint(copy["back"], _COLORS["muted"]))
            answer = input(copy["choose_prompt"].format(default=default_index)).strip()
            if _is_back(answer):
                raise _BackRequested
            if not answer:
                value = selected_default or catalog[default_index - 1].model_id
            elif answer == "0":
                value = input(copy["model_id_prompt"]).strip()
                if _is_back(value):
                    raise _BackRequested
            else:
                try:
                    index = int(answer)
                    value = catalog[index - 1].model_id
                except (ValueError, IndexError) as exc:
                    raise ValueError(copy["invalid_model"].format(label=label)) from exc
        else:
            print(copy["custom_model_only"])
            print(_paint(copy["back"], _COLORS["muted"]))
            answer = input(copy["choose_prompt"].format(default=1)).strip()
            if _is_back(answer):
                raise _BackRequested
            if answer not in {"", "1"}:
                raise ValueError(copy["invalid_custom_model"].format(label=label))
            value = input(copy["model_id_prompt"]).strip()
            if _is_back(value):
                raise _BackRequested

    if not value:
        raise ValueError(copy["empty_model"].format(label=label))
    return value


def _reasoning_parameter_name(provider: ModelProvider) -> str:
    if provider is ModelProvider.OPENAI:
        return "OpenAI reasoning.effort"
    return "Anthropic output_config.effort"


def _selected_reasoning_default(
    options: tuple[ReasoningEffort, ...],
    current: ReasoningEffort | None,
    default: ReasoningEffort,
) -> ReasoningEffort:
    if current in options:
        return current
    if default in options:
        return default
    if "medium" in options:
        return "medium"
    return options[-1]


def _reasoning_value(
    *,
    stage: str,
    provider: ModelProvider,
    model: str,
    current: ReasoningEffort | None,
    default: ReasoningEffort,
    override: ReasoningEffort | None,
    language: str,
) -> ReasoningEffort:
    copy = SETUP_COPY[language]
    label = copy["reasoning_labels"][stage]
    options = reasoning_options_for(provider, model)
    selected_default = _selected_reasoning_default(options, current, default)
    if override is not None:
        if override not in options:
            allowed = ", ".join(options)
            raise ValueError(
                copy["unsupported_effort"].format(
                    label=label,
                    value=override,
                    model=model,
                    allowed=allowed,
                )
            )
        return override

    parameter_name = _reasoning_parameter_name(provider)
    print(
        f"\n{copy['reasoning_step'].format(label=_paint(label, _COLORS['reasoning']), parameter=parameter_name)}:"
    )
    print(f"  {copy['stage_descriptions'][stage]}")
    default_index = next(
        (
            index
            for index, value in enumerate(options, start=1)
            if value == selected_default
        ),
        1,
    )
    for index, value in enumerate(options, start=1):
        description = copy["reasoning_descriptions"][value]
        suffix = copy["current_default"] if value == selected_default else ""
        color = _COLORS["selected"] if value == selected_default else _COLORS["reasoning"]
        print(f"  {index}. {_paint(value, color)} — {description}{suffix}")
    if stage == "deep" and "high" in options:
        print(copy["deep_recommendation"])
    if options == ("none",):
        print(copy["unsupported_reasoning"].format(model=model, parameter=parameter_name))
    elif provider is ModelProvider.ANTHROPIC:
        print(copy["anthropic_reasoning"])
    elif model == "gpt-6-astra":
        print(copy["astra_reasoning"])
    else:
        print(copy["openai_reasoning"])
    print(_paint(copy["back"], _COLORS["muted"]))
    answer = input(copy["choose_prompt"].format(default=default_index)).strip()
    if _is_back(answer):
        raise _BackRequested
    if not answer:
        return selected_default
    try:
        return options[int(answer) - 1]
    except (ValueError, IndexError) as exc:
        raise ValueError(copy["invalid_reasoning"].format(label=label)) from exc


def _setup(argv: Sequence[str]) -> int:
    parser = argparse.ArgumentParser(prog="qa-orch setup")
    parser.add_argument("--provider", choices=[provider.value for provider, _ in PROVIDER_OPTIONS])
    for field, _ in ROLE_LABELS:
        parser.add_argument(f"--{field.replace('_', '-')}")
    for field, _ in REASONING_ROLE_LABELS:
        parser.add_argument(
            f"--{field.replace('_', '-')}",
            choices=REASONING_EFFORTS,
        )
    args = parser.parse_args(argv)

    provider_is_interactive = args.provider is None
    all_model_flags_supplied = all(getattr(args, field) is not None for field, _ in ROLE_LABELS)
    no_reasoning_flags_supplied = all(
        getattr(args, field) is None for field, _ in REASONING_ROLE_LABELS
    )
    all_reasoning_flags_supplied = all(
        getattr(args, field) is not None for field, _ in REASONING_ROLE_LABELS
    )
    use_model_defaults_without_prompts = (
        args.provider is not None and all_model_flags_supplied and no_reasoning_flags_supplied
    )
    fully_noninteractive = (
        args.provider is not None
        and all_model_flags_supplied
        and (no_reasoning_flags_supplied or all_reasoning_flags_supplied)
    )
    try:
        language = "ru" if fully_noninteractive else _choose_language()
    except _BackRequested:
        print(f"\n{SETUP_COPY['ru']['cancelled']} / {SETUP_COPY['en']['cancelled']}")
        return 130
    copy = SETUP_COPY[language]

    settings = Settings.from_env()
    current: ModelSelection | None = None
    if settings.model_policy_path.exists():
        try:
            current = load_model_selection(settings.model_policy_path)
        except ValueError as exc:
            print(
                copy["unreadable_policy"].format(error=exc),
                file=sys.stderr,
            )

    while True:
        try:
            provider = (
                ModelProvider(args.provider)
                if args.provider is not None
                else _provider_choice(current.provider if current is not None else None, language)
            )
        except _BackRequested:
            print(f"\n{copy['cancelled']}")
            return 130

        print(copy["model_id_hint"])
        defaults = DEFAULT_MODEL_SELECTION if provider is ModelProvider.OPENAI else None
        model_values: dict[str, str] = {}
        reasoning_values: dict[str, ReasoningEffort] = {}
        steps: list[tuple[str, str, str]] = []
        for field, stage in ROLE_LABELS:
            steps.append(("model", field, stage))
            reasoning_field = f"{field.removesuffix('_model')}_reasoning"
            steps.append(("reasoning", reasoning_field, stage))

        step_index = 0
        back_to_provider = False
        while step_index < len(steps):
            step_kind, field, stage = steps[step_index]
            try:
                if step_kind == "model":
                    current_value = (
                        getattr(current, field)
                        if current is not None and current.provider is provider
                        else None
                    )
                    default_value = getattr(defaults, field) if defaults is not None else None
                    model_values[field] = _model_value(
                        stage=stage,
                        provider=provider,
                        current=current_value,
                        default=default_value,
                        override=getattr(args, field),
                        language=language,
                    )
                else:
                    model_field = f"{field.removesuffix('_reasoning')}_model"
                    current_reasoning = (
                        getattr(current, field)
                        if (
                            current is not None
                            and current.provider is provider
                            and getattr(current, model_field) == model_values[model_field]
                        )
                        else None
                    )
                    if use_model_defaults_without_prompts:
                        reasoning_values[field] = _selected_reasoning_default(
                            reasoning_options_for(provider, model_values[model_field]),
                            current_reasoning,
                            getattr(DEFAULT_MODEL_SELECTION, field),
                        )
                    else:
                        reasoning_values[field] = _reasoning_value(
                            stage=stage,
                            provider=provider,
                            model=model_values[model_field],
                            current=current_reasoning,
                            default=getattr(DEFAULT_MODEL_SELECTION, field),
                            override=getattr(args, field),
                            language=language,
                        )
            except _BackRequested:
                if step_index == 0:
                    if not provider_is_interactive:
                        raise ValueError(copy["back_unavailable"])
                    back_to_provider = True
                    break
                step_index -= 1
            else:
                step_index += 1

        if back_to_provider:
            continue
        break

    try:
        selection = ModelSelection(provider=provider, **model_values, **reasoning_values)
    except ValueError as exc:
        print(f"{copy['setup_error']}: {exc}", file=sys.stderr)
        return 2
    save_model_selection(settings.model_policy_path, selection)
    print(f"\n{copy['saved'].format(path=settings.model_policy_path)}")
    print(copy["provider_summary"].format(
        provider=_paint(selection.provider.value, _COLORS["provider"])
    ))
    for field, stage in ROLE_LABELS:
        label = copy["model_labels"][stage]
        print(f"{label}: {_paint(getattr(selection, field), _COLORS['model'])}")
    for field, stage in REASONING_ROLE_LABELS:
        label = copy["reasoning_labels"][stage]
        print(f"{label}: {_paint(getattr(selection, field), _COLORS['reasoning'])}")
    print(copy["verify"])
    print(copy["restart"])
    return 0


def _show_config() -> int:
    settings = Settings.from_env()
    selection = load_model_selection(settings.model_policy_path)
    payload = {
        "path": str(settings.model_policy_path),
        "configured": settings.model_policy_path.exists(),
        "selection": selection.model_dump(mode="json"),
    }
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


def _reload_config() -> int:
    settings = Settings.from_env()
    selection = load_model_selection(settings.model_policy_path)
    print(f"Модельная политика перечитана и проверена: {settings.model_policy_path}")
    print(json.dumps(selection.model_dump(mode="json"), ensure_ascii=False, indent=2))
    print("Для уже открытого MCP-клиента перезапусти соединение с сервером.")
    return 0


def _help() -> int:
    print("Использование:")
    print("  qa-orch setup          язык -> провайдер -> модель -> reasoning/effort")
    print("  qa-orch config show    показать текущую модельную политику")
    print("  qa-orch reload         перечитать и проверить модельную политику")
    print("  qa-orch                запустить MCP STDIO-сервер")
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    try:
        if not args:
            serve()
            return 0
        if args[0] == "setup":
            return _setup(args[1:])
        if args[:2] == ["config", "show"]:
            return _show_config()
        if args in (["reload"], ["config", "reload"]):
            return _reload_config()
        if args[0] in {"--help", "-h"}:
            return _help()
        raise ValueError(f"неизвестная команда: {args[0]}")
    except (EOFError, KeyboardInterrupt):
        print("\nНастройка отменена.", file=sys.stderr)
        return 130
    except ValueError as exc:
        print(f"Ошибка настройки: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
