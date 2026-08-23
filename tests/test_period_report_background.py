"""Тяжёлый PDF не должен блокировать Telegram long-polling."""
from datetime import date

from hermes import bot


def test_period_report_starts_in_background_and_rejects_duplicate(monkeypatch):
    started = []
    messages = []

    class _Thread:
        def __init__(self, target, name, daemon):
            self.target = target
            self.name = name
            self.daemon = daemon

        def start(self):
            started.append(self)

    monkeypatch.setattr(bot.threading, "Thread", _Thread)
    monkeypatch.setattr(
        bot.tg, "send_message",
        lambda _token, _chat, text, *args: messages.append(text),
    )
    monkeypatch.setattr(bot, "_main_keyboard", lambda _chat: {})
    bot._PERIOD_REPORT_JOBS.clear()

    bot._run_period_pdf_only(
        lambda: (_ for _ in ()).throw(AssertionError("не должен запускаться синхронно")),
        lambda: None,
        date(2026, 8, 1), date(2026, 8, 23), "token", 123,
    )

    assert len(started) == 1
    assert "в фоне" in messages[0]
    assert "123" in bot._PERIOD_REPORT_JOBS

    bot._run_period_pdf_only(
        lambda: None, lambda: None,
        date(2026, 8, 1), date(2026, 8, 23), "token", 123,
    )
    assert len(started) == 1
    assert "уже формируется" in messages[-1]
    bot._PERIOD_REPORT_JOBS.clear()
