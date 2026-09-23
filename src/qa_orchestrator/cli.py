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
    REASONING_EFFORTS,
    ModelProvider,
    ModelSelection,
    ReasoningEffort,
    load_model_selection,
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
MODEL_CATALOGS: dict[ModelProvider, tuple[tuple[str, str], ...]] = {
    ModelProvider.OPENAI: (
        ("GPT-6 Astra — максимальное качество", "gpt-6-astra"),
        ("GPT-6 Sol — сложный код и агентные задачи", "gpt-6-sol"),
        ("GPT-6 Luna — быстрее и экономичнее", "gpt-6-luna"),
        ("GPT-5.6 Sol", "gpt-5.6-sol"),
        ("GPT-5.6 Terra — баланс качества и цены", "gpt-5.6-terra"),
        ("GPT-5.6 Luna — экономичный вариант", "gpt-5.6-luna"),
        ("GPT-5.5", "gpt-5.5"),
        ("GPT-5.4", "gpt-5.4"),
        ("GPT-4.1 — без reasoning", "gpt-4.1"),
    ),
    ModelProvider.ANTHROPIC: (
        ("Claude Opus 5.5 — максимальное качество", "claude-opus-5-5"),
        ("Claude Opus 5", "claude-opus-5"),
        ("Claude Opus 4.8", "claude-opus-4-8"),
        ("Claude Opus 4.7", "claude-opus-4-7"),
        ("Claude Opus 4.6", "claude-opus-4-6"),
        ("Claude Sonnet 5 — баланс качества и скорости", "claude-sonnet-5"),
        ("Claude Sonnet 4.6", "claude-sonnet-4-6"),
        ("Claude Sonnet 4.5 — snapshot", "claude-sonnet-4-5-20250929"),
        ("Claude Haiku 4.5 — быстрый и экономичный", "claude-haiku-4-5-20251001"),
    ),
}
REASONING_OPTIONS: tuple[tuple[ReasoningEffort, str], ...] = (
    ("none", "без дополнительного reasoning, минимальная задержка"),
    ("minimal", "минимальный уровень рассуждений"),
    ("low", "быстрее и дешевле, подходит для простых задач"),
    ("medium", "сбалансированный вариант по умолчанию"),
    ("high", "сложный код, отладка и глубокий анализ"),
    ("xhigh", "длинные и сложные агентные задачи"),
    ("max", "максимальный уровень reasoning"),
)
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
        catalog_ids = {model_id for _, model_id in catalog}
        if selected_default and selected_default not in catalog_ids:
            catalog.insert(0, (f"Текущая модель: {selected_default}", selected_default))

        if catalog:
            default_index = next(
                (
                    index
                    for index, (_, model_id) in enumerate(catalog, start=1)
                    if model_id == selected_default
                ),
                1,
            )
            for index, (model_label, model_id) in enumerate(catalog, start=1):
                suffix = " (текущая/по умолчанию)" if model_id == selected_default else ""
                color = _COLORS["selected"] if model_id == selected_default else _COLORS["model"]
                print(
                    f"  {index}. {_paint(model_label, color)}: "
                    f"{_paint(model_id, color)}{suffix}"
                )
            print(_paint("  0. Ввести другой model ID", _COLORS["model"]))
            print(_paint("  b. Назад", _COLORS["muted"]))
            answer = input(f"Выбор [{default_index}]: ").strip()
            if _is_back(answer):
                raise _BackRequested
            if not answer:
                value = selected_default or catalog[default_index - 1][1]
            elif answer == "0":
                value = input("Model ID (b — назад): ").strip()
                if _is_back(value):
                    raise _BackRequested
            else:
                try:
                    index = int(answer)
                    value = catalog[index - 1][1]
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


def _reasoning_value(
    *,
    label: str,
    model: str,
    current: ReasoningEffort | None,
    default: ReasoningEffort,
    override: ReasoningEffort | None,
) -> ReasoningEffort:
    selected_default = "none" if model == "gpt-4.1" else current or default
    if override is not None:
        return override

    print(f"\nШаг 3/3. {_paint(label, _COLORS['reasoning'])} (OpenAI reasoning.effort):")
    default_index = next(
        (
            index
            for index, (value, _) in enumerate(REASONING_OPTIONS, start=1)
            if value == selected_default
        ),
        1,
    )
    for index, (value, description) in enumerate(REASONING_OPTIONS, start=1):
        suffix = " (текущий/по умолчанию)" if value == selected_default else ""
        color = _COLORS["selected"] if value == selected_default else _COLORS["reasoning"]
        print(f"  {index}. {_paint(value, color)} — {description}{suffix}")
    if label == "Reasoning для глубокой проверки":
        print("Рекомендация: high для сложных и рискованных проверок.")
    elif model == "gpt-4.1":
        print("Подсказка: gpt-4.1 — non-reasoning модель; используй none.")
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
        return REASONING_OPTIONS[int(answer) - 1][0]
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

    reasoning_overrides = [getattr(args, field) for field, _ in REASONING_ROLE_LABELS]
    provider_is_interactive = args.provider is None
    reasoning_labels = dict(REASONING_ROLE_LABELS)

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

        if provider is not ModelProvider.OPENAI and any(
            value is not None for value in reasoning_overrides
        ):
            raise ValueError("параметры reasoning сейчас доступны только для OpenAI")

        defaults = DEFAULT_MODEL_SELECTION if provider is ModelProvider.OPENAI else None
        model_values: dict[str, str] = {}
        reasoning_values: dict[str, ReasoningEffort] = {}
        steps: list[tuple[str, str, str]] = []
        for field, label in ROLE_LABELS:
            steps.append(("model", field, label))
            if provider is ModelProvider.OPENAI:
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
                    reasoning_values[field] = _reasoning_value(
                        label=label,
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
    if provider is ModelProvider.OPENAI:
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
    print("  qa-orch setup          провайдер -> модель -> reasoning")
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
