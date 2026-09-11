"""Synthetic tests of the actual copied worker, analyst, and queue functions."""
from __future__ import annotations

import importlib
import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).parent


def load_version(name: str):
    for module in tuple(sys.modules):
        if module == "hermes" or module.startswith("hermes."):
            del sys.modules[module]
    source_root = str(ROOT / name)
    sys.path[:] = [item for item in sys.path if item not in {str(ROOT / "full_source"), str(ROOT / "patched_full")}]
    sys.path.insert(0, source_root)
    return (importlib.import_module("hermes.ai_worker"), importlib.import_module("hermes.ai_analyst"),
            importlib.import_module("hermes.ai_queue"), importlib.import_module("hermes.telegram"))


def payload():
    return {"report_type": "question", "report_id": "DEMO-Q", "facts": [
        *[{"id": f"financial.{index}", "category": "financial"} for index in range(430)],
        *[{"id": f"stock.{index}", "category": "stock_product"} for index in range(35)],
    ], "live_source": {"requested": ["stock"], "refreshed": [],
        "errors": [{"domain": "stock", "error_type": "DemoTimeout"}],
        "refreshed_at": "2026-09-12T00:00:00+00:00"}}


def worker_result(worker, *, fail=False, thread_id="77"):
    worker.log.disabled = True  # Expected synthetic failure must not look like a live incident.
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory); jobs, processing, results = root / "jobs", root / "processing", root / "results"
        for path in (jobs, processing, results): path.mkdir()
        worker.ensure_spool = lambda: (jobs, processing, results)
        if fail:
            worker.run_codex_question = lambda _payload: (_ for _ in ()).throw(RuntimeError("synthetic failure"))
        else:
            worker.run_codex_question = lambda _payload: {"status": "validated", "validated": {"answer": "synthetic"}}
        job = {"run_id": "run-1", "chat_id": "-100demo", "thread_id": thread_id, "mode": "live", "payload": payload()}
        (jobs / "run-1.json").write_text(json.dumps(job), encoding="utf-8")
        assert worker.process_one() is True
        return json.loads((results / "run-1.json").read_text(encoding="utf-8"))


class Cursor:
    def __enter__(self): return self
    def __exit__(self, *_): return False
    def execute(self, *_args): pass


class Connection:
    def cursor(self): return Cursor()
    def commit(self): pass
    def close(self): pass


def test_full_path():
    old_worker, old_analyst, _old_queue, _old_tg = load_version("full_source")
    old_context = old_analyst._compact_question_payload(payload())
    assert not any(item["category"] == "stock_product" for item in old_context["facts"])
    assert "live_source" not in old_context
    assert "thread_id" not in worker_result(old_worker)

    worker, analyst, queue, telegram = load_version("patched_full")
    context = analyst._compact_question_payload(payload())
    selected_stock = [item for item in context["facts"] if item["category"] == "stock_product"]
    assert len(context["facts"]) == 430 and len(selected_stock) == 30
    assert sum(item["category"] == "financial" for item in context["facts"]) == 400
    assert context["live_source"]["errors"][0]["domain"] == "stock"
    assert context["context_limits"]["stock_product_omitted"] == 5

    result = worker_result(worker)
    assert result["thread_id"] == "77"
    assert result["source_refresh"]["errors"][0]["error_type"] == "DemoTimeout"
    assert "Данные могут быть несвежими" in analyst.render_telegram_answer(result)
    assert worker_result(worker, thread_id=None)["thread_id"] == ""
    failed = worker_result(worker, fail=True)
    assert failed["status"] == "failed" and failed["mode"] == "live"
    assert failed["thread_id"] == "77" and failed["source_refresh"]["errors"]

    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "result.json"
        path.write_text(json.dumps(result), encoding="utf-8")
        queue._consume_result(Connection, "synthetic-token-not-used", path)
        assert not path.exists()
    assert len(telegram.SENT) == 1
    _args, kwargs = telegram.SENT[0]
    assert kwargs["message_thread_id"] == "77"
    assert "Данные могут быть несвежими" in _args[2]


if __name__ == "__main__":
    test_full_path()
    print("PASS: full copied functions; synthetic data; network/database/model/Telegram calls: 0")
