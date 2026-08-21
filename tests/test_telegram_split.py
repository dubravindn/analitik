"""Тесты разбиения длинных сообщений Telegram по границам строк (пункт 1.4)."""
from hermes.telegram import _split, main_reply_keyboard, set_my_commands


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
