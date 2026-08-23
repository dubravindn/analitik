from __future__ import annotations

from datetime import datetime, timezone

from hermes.ai_analyst import _compact_payload, render_telegram_summary
from hermes.ai_queue import (
    _feedback_markup,
    _with_owner_guidance,
    is_dialogue_guidance,
    learn_from_dialogue,
    store_guidance_reply,
)


class _Cursor:
    def __init__(self, conn):
        self.conn = conn
        self.result = None

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def execute(self, sql, params=()):
        self.conn.queries.append((" ".join(sql.split()), params))
        if "FROM ai_guidance" in sql:
            self.result = self.conn.guidance_rows
        elif "FROM ai_delivery" in sql:
            self.result = [("run-1",)]
        elif "FROM ai_analysis_run" in sql:
            self.result = [(1,)]
        elif "INSERT INTO ai_guidance" in sql:
            self.conn.inserted = params

    def fetchall(self):
        return list(self.result or [])

    def fetchone(self):
        return (self.result or [None])[0]


class _Conn:
    def __init__(self, guidance_rows=()):
        self.guidance_rows = list(guidance_rows)
        self.queries = []
        self.inserted = None
        self.commits = 0

    def cursor(self):
        return _Cursor(self)

    def commit(self):
        self.commits += 1


def test_guidance_is_attached_oldest_to_newest_and_rehashes_payload():
    newer = datetime(2026, 8, 23, 12, tzinfo=timezone.utc)
    older = datetime(2026, 8, 22, 12, tzinfo=timezone.utc)
    conn = _Conn([
        ("Сначала проверяй списания", newer, "run-new"),
        ("Отделяй факты от гипотез", older, "run-old"),
    ])
    payload = {"report_id": "report-1", "payload_hash": "old", "facts": []}

    enriched = _with_owner_guidance(conn, payload)

    assert [item["text"] for item in enriched["owner_guidance"]] == [
        "Отделяй факты от гипотез", "Сначала проверяй списания",
    ]
    assert enriched["payload_hash"] != "old"
    assert payload["payload_hash"] == "old"


def test_reply_is_linked_to_delivered_analysis_before_guidance_is_saved():
    conn = _Conn()

    saved = store_guidance_reply(
        conn, chat_id="-1001", user_id="42", reply_message_id=777,
        guidance="  Неверно понял.  Сначала проверяй документы задним числом. ",
    )

    assert saved is True
    assert conn.inserted == (
        "run-1", "-1001", "42",
        "Неверно понял. Сначала проверяй документы задним числом.",
    )
    assert conn.commits == 1


def test_guidance_reaches_model_context_and_feedback_has_text_button():
    payload = {
        "report_id": "report-1", "report_type": "period", "facts": [],
        "owner_guidance": [{"text": "Проверяй старые отгрузки"}],
    }
    compact = _compact_payload(payload, {"signals": []})
    markup = _feedback_markup("run-1")

    assert compact["owner_guidance"] == [{"text": "Проверяй старые отгрузки"}]
    assert markup["inline_keyboard"][1][0]["callback_data"] == "ai_guidance:run-1"


def test_report_summary_tells_owner_how_to_correct_analysis():
    text = render_telegram_summary({"validated": {
        "findings": [], "positive_changes": [], "data_warnings": [],
    }})
    assert "Ответьте на это сообщение" in text


def test_dialogue_learning_distinguishes_rules_from_follow_up_questions():
    assert is_dialogue_guidance("Запомни: Фабрика не участвует в прибыли")
    assert is_dialogue_guidance("Это неверно, сначала учитывай списания БАЗЫ")
    assert is_dialogue_guidance("Для нас важно проверять старые отгрузки")
    assert not is_dialogue_guidance("Почему прибыль БАЗЫ снизилась?")
    assert not is_dialogue_guidance("Покажи это по складам")


def test_explicit_rule_from_dialogue_is_saved_for_future_runs():
    conn = _Conn()
    learned = learn_from_dialogue(
        conn, "run-1", "-1001", "42",
        "В дальнейшем учитывай изменения старых отгрузок",
    )
    assert learned is True
    assert conn.inserted == (
        "run-1", "-1001", "42",
        "В дальнейшем учитывай изменения старых отгрузок",
    )
