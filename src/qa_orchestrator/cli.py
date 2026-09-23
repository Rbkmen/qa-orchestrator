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
    ("triage_model", "Модель триажа"),
    ("primary_model", "Модель основного ревью"),
    ("deep_model", "Модель глубокой проверки"),
    ("synthesis_model", "Модель синтеза"),
)
REASONING_ROLE_LABELS = (
    ("triage_reasoning", "Reasoning для триажа"),
    ("primary_reasoning", "Reasoning для основного ревью"),
    ("deep_reasoning", "Reasoning для глубокой проверки"),
    ("synthesis_reasoning", "Reasoning для синтеза"),
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


def _provider_choice(current: ModelProvider | None) -> ModelProvider:
    print("\nШаг 1/3. Выбери провайдера:")
    for index, (provider, label) in enumerate(PROVIDER_OPTIONS, start=1):
        suffix = " (текущий)" if provider is current else ""
        print(f"  {index}. {_paint(label, _COLORS['provider'])}{suffix}")
    print(_paint("  b. Назад / выйти", _COLORS["muted"]))

    default_index = next(
        (index for index, (provider, _) in enumerate(PROVIDER_OPTIONS, start=1) if provider is current),
        1,
    )
    answer = input(f"Номер [{default_index}]: ").strip()
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
        raise ValueError("нужно выбрать номер из меню провайдеров") from exc


def _model_value(
    *,
    label: str,
    provider: ModelProvider,
    current: str | None,
    default: str | None,
    override: str | None,
) -> str:
    selected_default = current or default
    if override is not None:
        value = override.strip()
    else:
        print(f"\nШаг 2/3. {_paint(label, _COLORS['model'])}:")
        catalog = list(MODEL_CATALOGS.get(provider, ()))
        catalog_ids = {entry.model_id for entry in catalog}
        if selected_default and selected_default not in catalog_ids:
            catalog.insert(
                0,
                ModelCatalogEntry(
                    f"Текущая модель: {selected_default}",
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
                suffix = " (текущая/по умолчанию)" if entry.model_id == selected_default else ""
                color = _COLORS["selected"] if entry.model_id == selected_default else _COLORS["model"]
                print(
                    f"  {index}. {_paint(entry.label, color)}: "
                    f"{_paint(entry.model_id, color)}{suffix}"
                )
            print(_paint("  0. Ввести другой model ID", _COLORS["model"]))
            print(_paint("  b. Назад", _COLORS["muted"]))
            answer = input(f"Выбор [{default_index}]: ").strip()
            if _is_back(answer):
                raise _BackRequested
            if not answer:
                value = selected_default or catalog[default_index - 1].model_id
            elif answer == "0":
                value = input("Model ID (b — назад): ").strip()
                if _is_back(value):
                    raise _BackRequested
            else:
                try:
                    index = int(answer)
                    value = catalog[index - 1].model_id
                except (ValueError, IndexError) as exc:
                    raise ValueError(f"{label}: нужно выбрать номер из меню моделей") from exc
        else:
            print("  1. Ввести model ID")
            print(_paint("  b. Назад", _COLORS["muted"]))
            answer = input("Выбор [1]: ").strip()
            if _is_back(answer):
                raise _BackRequested
            if answer not in {"", "1"}:
                raise ValueError(f"{label}: нужно выбрать 1")
            value = input("Model ID (b — назад): ").strip()
            if _is_back(value):
                raise _BackRequested

        if provider in MODEL_CATALOGS:
            print("Подсказка: используй точный API model ID; для другого ID выбери 0.")
    if not value:
        raise ValueError(f"{label}: значение обязательно")
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
    label: str,
    provider: ModelProvider,
    model: str,
    current: ReasoningEffort | None,
    default: ReasoningEffort,
    override: ReasoningEffort | None,
) -> ReasoningEffort:
    options = reasoning_options_for(provider, model)
    selected_default = _selected_reasoning_default(options, current, default)
    if override is not None:
        if override not in options:
            allowed = ", ".join(options)
            raise ValueError(
                f"{label}: значение {override!r} не поддерживается моделью {model!r}; "
                f"доступно: {allowed}"
            )
        return override

    parameter_name = _reasoning_parameter_name(provider)
    print(f"\nШаг 3/3. {_paint(label, _COLORS['reasoning'])} ({parameter_name}):")
    default_index = next(
        (
            index
            for index, value in enumerate(options, start=1)
            if value == selected_default
        ),
        1,
    )
    for index, value in enumerate(options, start=1):
        description = REASONING_DESCRIPTIONS[value]
        suffix = " (текущий/по умолчанию)" if value == selected_default else ""
        color = _COLORS["selected"] if value == selected_default else _COLORS["reasoning"]
        print(f"  {index}. {_paint(value, color)} — {description}{suffix}")
    if label == "Reasoning для глубокой проверки" and "high" in options:
        print("Рекомендация: high для сложных и рискованных проверок.")
    if options == ("none",):
        print(f"Подсказка: {model} не поддерживает {parameter_name}; будет сохранено none.")
    elif provider is ModelProvider.ANTHROPIC:
        print("Подсказка: Anthropic effort задаёт глубину рассуждений для этой модели.")
    elif model == "gpt-6-astra":
        print("Подсказка: GPT-6 Astra не поддерживает none; начни с low или medium.")
    else:
        print("Подсказка: доступные значения зависят от конкретной модели OpenAI.")
    print(_paint("  b. Назад", _COLORS["muted"]))
    answer = input(f"Выбор [{default_index}]: ").strip()
    if _is_back(answer):
        raise _BackRequested
    if not answer:
        return selected_default
    try:
        return options[int(answer) - 1]
    except (ValueError, IndexError) as exc:
        raise ValueError(f"{label}: нужно выбрать уровень reasoning из меню") from exc


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

    settings = Settings.from_env()
    current: ModelSelection | None = None
    if settings.model_policy_path.exists():
        try:
            current = load_model_selection(settings.model_policy_path)
        except ValueError as exc:
            print(
                f"Текущая модельная политика не читается ({exc}); будет создана новая.",
                file=sys.stderr,
            )

    provider_is_interactive = args.provider is None
    reasoning_labels = dict(REASONING_ROLE_LABELS)
    all_model_flags_supplied = all(getattr(args, field) is not None for field, _ in ROLE_LABELS)
    no_reasoning_flags_supplied = all(
        getattr(args, field) is None for field, _ in REASONING_ROLE_LABELS
    )
    use_model_defaults_without_prompts = (
        args.provider is not None and all_model_flags_supplied and no_reasoning_flags_supplied
    )

    while True:
        try:
            provider = (
                ModelProvider(args.provider)
                if args.provider is not None
                else _provider_choice(current.provider if current is not None else None)
            )
        except _BackRequested:
            print("\nНастройка отменена.")
            return 130

        defaults = DEFAULT_MODEL_SELECTION if provider is ModelProvider.OPENAI else None
        model_values: dict[str, str] = {}
        reasoning_values: dict[str, ReasoningEffort] = {}
        steps: list[tuple[str, str, str]] = []
        for field, label in ROLE_LABELS:
            steps.append(("model", field, label))
            reasoning_field = f"{field.removesuffix('_model')}_reasoning"
            steps.append(("reasoning", reasoning_field, reasoning_labels[reasoning_field]))

        step_index = 0
        back_to_provider = False
        while step_index < len(steps):
            step_kind, field, label = steps[step_index]
            try:
                if step_kind == "model":
                    current_value = (
                        getattr(current, field)
                        if current is not None and current.provider is provider
                        else None
                    )
                    default_value = getattr(defaults, field) if defaults is not None else None
                    model_values[field] = _model_value(
                        label=label,
                        provider=provider,
                        current=current_value,
                        default=default_value,
                        override=getattr(args, field),
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
                            label=label,
                            provider=provider,
                            model=model_values[model_field],
                            current=current_reasoning,
                            default=getattr(DEFAULT_MODEL_SELECTION, field),
                            override=getattr(args, field),
                        )
            except _BackRequested:
                if step_index == 0:
                    if not provider_is_interactive:
                        raise ValueError("к выбору провайдера нельзя вернуться в этом режиме")
                    back_to_provider = True
                    break
                step_index -= 1
            else:
                step_index += 1

        if back_to_provider:
            continue
        break

    selection = ModelSelection(provider=provider, **model_values, **reasoning_values)
    save_model_selection(settings.model_policy_path, selection)
    print(f"\nМодельная политика сохранена: {settings.model_policy_path}")
    print(f"Провайдер: {_paint(selection.provider.value, _COLORS['provider'])}")
    for field, label in ROLE_LABELS:
        print(f"{label}: {_paint(getattr(selection, field), _COLORS['model'])}")
    for field, label in REASONING_ROLE_LABELS:
        print(f"{label}: {_paint(getattr(selection, field), _COLORS['reasoning'])}")
    print("Проверь политику командой: qa-orch reload")
    print("Если MCP-клиент уже подключён, перезапусти его соединение.")
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
    print("  qa-orch setup          провайдер -> модель -> reasoning/effort")
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
