"""Offline regression check for the exact /opt renderer, not a live integration."""
from __future__ import annotations

import ast
import hashlib
import importlib.util
import json
import tempfile
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent
BASE_SHA = "9f837ec597b109329d8caf9477881a01e6cec791eef8130354950588385c3242"


def load_renderer(folder):
    path = ROOT / folder / "hermes/ai_analyst.py"
    raw = path.read_bytes()
    tree = ast.parse(raw)
    if folder == "baseline":
        assert hashlib.sha256(raw).hexdigest() == BASE_SHA
    selected = [node for node in tree.body if isinstance(node, ast.FunctionDef)
                and node.name == "render_telegram_answer"]
    assert len(selected) == 1
    namespace = {"Any": Any}
    exec(compile(ast.Module(body=selected, type_ignores=[]), str(path), "exec"), namespace)
    unchanged = ast.Module(body=[node for node in tree.body if node not in selected], type_ignores=[])
    return namespace["render_telegram_answer"], ast.dump(unchanged)


def main():
    old, old_rest = load_renderer("baseline")
    new, new_rest = load_renderer("patched")
    assert old_rest == new_rest, "Only the renderer may change"
    ordinary = {"validated": {"answer": "Демонстрационный ответ", "evidence": ["Факт"],
                              "caveats": ["Ограничение"], "follow_up": "Уточнение"}}
    for freshness in [None, {}, {"errors": []}]:
        case = {**ordinary, "source_refresh": freshness}
        assert new(case) == old(case), "No-error text must stay byte-for-byte identical"
    case = {**ordinary, "source_refresh": {"errors": [{"domain": "stock"}]}}
    assert "Данные могут быть несвежими" not in old(case)
    assert "Данные могут быть несвежими" in new(case)
    assert "Не удалось обновить: stock" in new(case)
    assert new(case).count("⚠️ Актуальность данных:") == 1
    assert "Данные могут быть несвежими" in new({**ordinary, "source_refresh": {"errors": ["unknown"]}})

    helper_path = ROOT.parent / "b04/test_full_path.py"
    spec = importlib.util.spec_from_file_location("synthetic_b04_helpers", helper_path)
    helper = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(helper)
    worker, _analyst, queue, telegram = helper.load_version("patched_full")
    # Use the actual separate /opt renderer with the previously tested worker/queue.
    queue.render_telegram_answer = new
    for fail in (False, True):
        for thread in (None, "77"):
            telegram.SENT.clear()
            result = helper.worker_result(worker, fail=fail, thread_id=thread)
            result["chat_id"] = "123456789" if thread is None else "-100demo"
            with tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "result.json"
                path.write_text(json.dumps(result), encoding="utf-8")
                queue._consume_result(helper.Connection, "synthetic-token-not-used", path)
                assert not path.exists()
            assert len(telegram.SENT) == 1
            args, kwargs = telegram.SENT[0]
            assert args[1] == result["chat_id"]
            assert kwargs.get("message_thread_id") in ((None, "") if thread is None else (thread,))
            if fail:
                assert "Демонстрационный ответ" not in args[2]
                assert "synthetic" not in args[2]
            else:
                assert "Данные могут быть несвежими" in args[2]
    print("PASS: exact /opt renderer; unchanged no-error text; separate worker -> queue -> renderer -> fake delivery; private/topic and success/failure; external calls: 0")


if __name__ == "__main__":
    main()
