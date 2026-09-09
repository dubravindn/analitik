import io
import json
from unittest.mock import patch

from hermes.ai_analyst import _extract_json_text, _openclaw_completion


class _Response(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()


def test_extract_json_text_accepts_plain_and_fenced_json():
    assert _extract_json_text('{"ok":true}') == '{"ok":true}'
    assert _extract_json_text('```json\n{"ok":true}\n```') == '{"ok":true}'


def test_openclaw_completion_reads_compatible_response(tmp_path, monkeypatch):
    token = tmp_path / "token"
    token.write_text("secret", encoding="utf-8")
    monkeypatch.setenv("AI_OPENCLAW_TOKEN_FILE", str(token))
    response = _Response(json.dumps({
        "model": "openai/gpt-test",
        "choices": [{"message": {"content": "```json\n{\"ok\":true}\n```"}}],
    }).encode())
    with patch("urllib.request.urlopen", return_value=response):
        raw, model = _openclaw_completion("prompt", session_key="s", timeout=3)
    assert json.loads(raw) == {"ok": True}
    assert model == "openai/gpt-test"
