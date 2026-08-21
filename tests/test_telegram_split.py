"""Тесты разбиения длинных сообщений Telegram по границам строк (пункт 1.4)."""
from hermes.telegram import (
    _split,
    analytics_group_keyboard,
    main_reply_keyboard,
    question_force_reply,
    set_my_commands,
    shared_group_keyboard,
)


def test_short_text_single_chunk():
    assert _split("abc", 100) == ["abc"]


def test_split_on_line_boundaries_roundtrip():
    text = "\n".join(f"строка {i} с текстом" for i in range(60))
    parts = _split(text, 100)
    assert all(len(p) <= 100 for p in parts)
    # склейка через \n восстанавливает исходный текст (строки не разрезаны)
    assert "\n".join(parts) == text
    # каждая часть состоит из целых строк исходного текста
    source_lines = set(text.split("\n"))
    for p in parts:
        for line in p.split("\n"):
            assert line in source_lines


def test_oversized_single_line_hard_cut():
    parts = _split("X" * 250, 100)
    assert [len(p) for p in parts] == [100, 100, 50]
    assert "".join(parts) == "X" * 250


def test_mixed_normal_and_oversized():
    text = "aaa\n" + "Y" * 250 + "\nbbb"
    parts = _split(text, 100)
    assert all(len(p) <= 100 for p in parts)


def test_main_keyboard_exposes_ai_conversation():
    labels = [button["text"] for row in main_reply_keyboard()["keyboard"] for button in row]
    assert "🧠 Спросить ИИ" in labels


def test_two_level_group_keyboards():
    root = [
        button["text"]
        for row in shared_group_keyboard()["keyboard"]
        for button in row
    ]
    analytics = [
        button["text"]
        for row in analytics_group_keyboard()["keyboard"]
        for button in row
    ]
    assert root[:2] == ["📊 Аналитика", "👥 Работа сотрудников"]
    assert "📊 Отчёт за период" in analytics
    assert "🧠 Задать вопрос аналитику" in analytics
    assert "⬅️ Общее меню" in analytics


def test_ai_question_uses_selective_force_reply():
    markup = question_force_reply()
    assert markup["force_reply"] is True
    assert markup["selective"] is True
    assert "аналитику" in markup["input_field_placeholder"]


def test_set_my_commands_serializes_scope(monkeypatch):
    captured = {}

    def fake_post(_token, method, payload):
        captured.update({"method": method, "payload": payload})
        return {"ok": True}

    monkeypatch.setattr("hermes.telegram._post", fake_post)
    set_my_commands(
        "token", [("menu", "Общее меню")], {"type": "all_group_chats"},
    )

    assert captured["method"] == "setMyCommands"
    assert captured["payload"]["commands"] == [
        {"command": "menu", "description": "Общее меню"},
    ]
    assert captured["payload"]["scope"] == {"type": "all_group_chats"}
