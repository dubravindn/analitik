"""Закрытая Telegram-группа: группа и руководитель проверяются отдельно."""

from hermes import bot


_CONFIGURED = "1914630610,798855496,-5228125278"


def _message(chat_id: int, user_id: int, text: str = "/menu@CBDanalitik_bot") -> dict:
    return {
        "chat": {"id": chat_id, "type": "supergroup"},
        "from": {"id": user_id},
        "text": text,
    }


def test_bound_group_allows_each_leader():
    for user_id in (1914630610, 798855496):
        allowed, chat_id, actor_id, is_group = bot._message_access(
            _message(-5228125278, user_id), _CONFIGURED,
        )
        assert allowed is True
        assert chat_id == "-5228125278"
        assert actor_id == str(user_id)
        assert is_group is True


def test_bound_group_rejects_other_member():
    allowed, *_ = bot._message_access(
        _message(-5228125278, 123456), _CONFIGURED,
    )
    assert allowed is False


def test_other_group_is_rejected():
    allowed, *_ = bot._message_access(
        _message(-100999999, 1914630610), _CONFIGURED,
    )
    assert allowed is False


def test_group_menu_names_both_bots():
    assert "📊 Аналитика" in bot._GROUP_MENU_TEXT
    assert "👥 Работа сотрудников" in bot._GROUP_MENU_TEXT


def test_addressed_command_is_normalized():
    assert bot._command_name("/ask@CBDanalitik_bot Кто просел?") == "ask"


def test_command_target_routes_only_to_addressed_bot():
    assert bot._command_target("/ask@CBDanalitik_bot Кто просел?") == "cbdanalitik_bot"
    assert bot._command_target("/menu@CBDOt4et_bot") == "cbdot4et_bot"
    assert bot._command_target("/menu") == ""


def test_reply_is_routed_only_to_analytics_bot():
    message = _message(-5228125278, 1914630610, "Почему просела выручка?")
    message["reply_to_message"] = {
        "from": {"username": "CBDanalitik_bot"},
    }
    assert bot._is_reply_to_bot(message, "CBDanalitik_bot") is True
    assert bot._is_reply_to_bot(message, "CBDOt4et_bot") is False
