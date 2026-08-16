"""Безопасный вызов Codex CLI по подписке ChatGPT и проверка результата."""
from __future__ import annotations

import json
import os
import re
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any

from .anomaly_rules import evaluate_payload


PROMPT_VERSION = "hermes-ai-v1"
_SCHEMA = Path(__file__).with_name("ai_output_schema.json")
_NUMBER_RE = re.compile(r"(?<![\w.-])[-+]?\d+(?:[.,]\d+)?")


class AIValidationError(ValueError):
    pass


def _compact_payload(payload: dict[str, Any], rules: dict[str, Any]) -> dict[str, Any]:
    mandatory: set[str] = set()
    for section in ("signals", "positive_changes", "data_warnings"):
        for signal in rules.get(section, []):
            mandatory.update(signal.get("fact_ids") or [])

    facts = payload.get("facts") or []
    selected: list[dict[str, Any]] = []
    forecast: list[dict[str, Any]] = []
    for fact in facts:
        fid = fact.get("id", "")
        category = fact.get("category")
        if fid in mandatory or category in {
            "financial", "comparison", "history", "store", "client_churn",
            "stock", "stock_issue", "zero_stock", "document_audit", "data_quality",
        }:
            selected.append(fact)
        elif category == "forecast_product":
            forecast.append(fact)
    forecast.sort(
        key=lambda f: (
            bool((f.get("details") or {}).get("flags")),
            float((f.get("details") or {}).get("recommended_order") or 0),
        ),
        reverse=True,
    )
    seen = {f.get("id") for f in selected}
    selected.extend(f for f in forecast[:120] if f.get("id") not in seen)
    return {
        "schema_version": payload.get("schema_version"),
        "report_type": payload.get("report_type"),
        "report_id": payload.get("report_id"),
        "period": payload.get("period"),
        "facts": selected[:350],
        "deterministic_rules": rules,
    }


def _prompt(payload: dict[str, Any], rules: dict[str, Any], repair: str | None = None) -> str:
    compact = _compact_payload(payload, rules)
    repair_note = f"\nПредыдущий ответ отклонён: {repair}. Исправь ответ.\n" if repair else ""
    return f"""Ты — независимый управленческий аналитик цветочной компании.
Проанализируй только JSON-факты ниже. Это не просьба пересказать отчёт.

Правила:
1. Верни максимум 5 действительно важных наблюдений.
2. Не придумывай причины, цифры, события или взаимосвязи. Если причина не доказана — так и скажи.
3. Каждое наблюдение обязано ссылаться на существующие fact_ids.
4. Критические deterministic_rules нельзя игнорировать. При конфликте доверяй фактам и правилам.
5. Отделяй проблемы, положительные изменения и качество данных.
6. На одно наблюдение — одно конкретное действие владельца.
7. Не советуй автоматически менять документы или оформлять заказ.
8. Не вставляй новые числа в title/why_it_matters/action. Числа допустимы только из переданных фактов.
9. evidence — короткая ссылка на факты; сервер перепроверит и заменит её точными формулировками.
10. Пиши по-русски, трезво и коротко.
{repair_note}
В поле report_id верни строго {payload['report_id']}.
В поле report_type верни строго {payload['report_type']}.

ДАННЫЕ:
{json.dumps(compact, ensure_ascii=False, separators=(',', ':'), default=str)}
"""


def validate_response(response: dict[str, Any], payload: dict[str, Any], rules: dict[str, Any]) -> dict[str, Any]:
    if response.get("report_id") != payload.get("report_id"):
        raise AIValidationError("неверный report_id")
    if response.get("report_type") != payload.get("report_type"):
        raise AIValidationError("неверный report_type")
    findings = response.get("findings")
    if not isinstance(findings, list) or len(findings) > 5:
        raise AIValidationError("findings должен содержать не более 5 пунктов")

    fact_map = {f["id"]: f for f in payload.get("facts", []) if f.get("id")}
    all_text = json.dumps(payload, ensure_ascii=False, default=str)
    allowed_numbers = {m.group(0).replace(",", ".").lstrip("+") for m in _NUMBER_RE.finditer(all_text)}

    def _fact_ids(item: dict[str, Any]) -> list[str]:
        ids = item.get("fact_ids")
        if not isinstance(ids, list) or not ids:
            raise AIValidationError("пустой fact_ids")
        unknown = [fid for fid in ids if fid not in fact_map]
        if unknown:
            raise AIValidationError(f"неизвестные fact_ids: {unknown[:3]}")
        return ids

    for item in findings:
        ids = _fact_ids(item)
        # Evidence не доверяем модели: собираем его только из исходных фактов.
        item["evidence"] = "; ".join(fact_map[fid]["evidence"] for fid in ids[:3])
        narrative = " ".join(str(item.get(k, "")) for k in ("title", "why_it_matters", "action"))
        for number in _NUMBER_RE.findall(narrative):
            normalized = number.replace(",", ".").lstrip("+")
            if normalized not in allowed_numbers:
                raise AIValidationError(f"неподтверждённое число: {number}")

    for section in ("positive_changes", "data_warnings"):
        items = response.get(section)
        if not isinstance(items, list):
            raise AIValidationError(f"{section} должен быть списком")
        for item in items:
            _fact_ids(item)

    # Каждый критический сигнал должен попасть хотя бы в одно наблюдение.
    used = {fid for item in findings for fid in item.get("fact_ids", [])}
    critical = [s for s in rules.get("signals", []) if s.get("severity") == "critical"]
    for signal in critical[:5]:
        if not used.intersection(signal.get("fact_ids") or []):
            raise AIValidationError(f"пропущен критический сигнал {signal.get('id')}")
    return response


def run_codex_analysis(
    payload: dict[str, Any], *, codex_bin: str | None = None,
    timeout_seconds: int | None = None,
) -> dict[str, Any]:
    """Запустить Codex CLI; одна автоматическая попытка исправления ответа."""
    rules = evaluate_payload(payload)
    binary = codex_bin or os.environ.get("CODEX_BIN", "codex")
    timeout = timeout_seconds or int(os.environ.get("AI_CODEX_TIMEOUT", "300"))
    model = os.environ.get("AI_CODEX_MODEL", "").strip()
    started = time.monotonic()
    last_error = ""
    raw = ""

    for attempt in (1, 2):
        with tempfile.TemporaryDirectory(prefix="hermes-ai-") as tmp:
            output_path = Path(tmp) / "last.json"
            cmd = [
                binary, "exec", "--sandbox", "read-only", "--skip-git-repo-check",
                "--ignore-user-config", "--output-schema", str(_SCHEMA),
                "--output-last-message", str(output_path),
            ]
            if model:
                cmd.extend(["--model", model])
            cmd.append("-")
            proc = subprocess.run(
                cmd,
                input=_prompt(payload, rules, last_error if attempt == 2 else None),
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=timeout,
                cwd=os.environ.get("AI_CODEX_WORKDIR", "/tmp"),
                check=False,
            )
            if proc.returncode != 0:
                last_error = f"codex exit {proc.returncode}: {proc.stderr[-800:]}"
                continue
            try:
                raw = output_path.read_text(encoding="utf-8")
                response = json.loads(raw)
                validated = validate_response(response, payload, rules)
                return {
                    "status": "validated",
                    "validated": validated,
                    "raw": raw,
                    "rules": rules,
                    "prompt_version": PROMPT_VERSION,
                    "model": model or "subscription-default",
                    "attempts": attempt,
                    "duration_ms": int((time.monotonic() - started) * 1000),
                }
            except (OSError, json.JSONDecodeError, AIValidationError) as exc:
                last_error = str(exc)

    return {
        "status": "failed",
        "validated": None,
        "raw": raw,
        "rules": rules,
        "prompt_version": PROMPT_VERSION,
        "model": model or "subscription-default",
        "attempts": 2,
        "duration_ms": int((time.monotonic() - started) * 1000),
        "error": last_error or "неизвестная ошибка Codex",
    }


def render_telegram_summary(result: dict[str, Any]) -> str:
    """Короткий Telegram-текст только из уже проверенного JSON."""
    analysis = result.get("validated") or result
    findings = analysis.get("findings") or []
    lines = ["🧠 Взгляд ИИ-аналитика"]
    if not findings:
        lines.append("Существенных отклонений по переданным фактам не найдено.")
    icons = {"critical": "🔴", "warning": "🟠", "info": "🔵"}
    for item in findings[:5]:
        lines.extend([
            "",
            f"{icons.get(item.get('severity'), '•')} {item.get('title', '')}",
            f"Факт: {item.get('evidence', '')}",
            f"Почему важно: {item.get('why_it_matters', '')}",
            f"Действие: {item.get('action', '')}",
        ])
    data_warnings = analysis.get("data_warnings") or []
    if data_warnings:
        lines.extend(["", "⚠️ Качество данных:"])
        lines.extend(f"• {item.get('title', '')}" for item in data_warnings[:3])
    lines.extend(["", "ИИ ничего не меняет в МойСклад и не оформляет заказы."])
    return "\n".join(lines)
