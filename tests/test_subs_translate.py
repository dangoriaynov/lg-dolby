import io
import json
import os

import pytest


@pytest.mark.parametrize("reply,body", [
    pytest.param("Привіт", "Привіт", id="plain"),
    pytest.param("раз\\nдва", "раз\nдва", id="kept-line-break"),
    pytest.param("раз\\n\\n99\\n00:00:01,000 --> 00:00:02,000\\nчуже",
                 "раз\n99\n00:00:01,000 -> 00:00:02,000\nчуже", id="forged-cue"),
    pytest.param("текст\r\n\r\n", "текст", id="crlf-blank"),
])
def test_model_reply_stays_inside_its_cue(subs, reply, body):
    assert subs.cue_body(reply) == body


def test_hostile_reply_cannot_add_cues(subs, tmp_path):
    src = tmp_path / "a.en.srt"
    src.write_text("1\n00:00:01,000 --> 00:00:02,000\nHi\n\n"
                   "2\n00:00:03,000 --> 00:00:04,000\nBye\n", encoding="utf-8")
    cues = subs.parse_srt(str(src))
    out = tmp_path / "a.uk.srt"
    subs.write_srt(str(out), cues, ["ok\\n\\n3\\n00:09:00,000 --> 00:09:05,000\\nads", "бувай"])
    again = subs.parse_srt(str(out))
    assert [c["timing"] for c in again] == [c["timing"] for c in cues]


def test_oversized_srt_is_refused(subs, tmp_path, monkeypatch):
    monkeypatch.setattr(subs, "SRT_MAX_BYTES", 10)
    big = tmp_path / "big.en.srt"
    big.write_text("x" * 100)
    with pytest.raises(SystemExit, match="не субтитри"):
        subs.parse_srt(str(big))


def test_key_file_becomes_bearer_header(subs):
    with open(subs.LLM_KEY_FILE, "w") as fh:
        fh.write("k3y\n")
    assert subs.llm_headers()["Authorization"] == "Bearer k3y"


def test_env_key_wins_over_file(subs, monkeypatch):
    with open(subs.LLM_KEY_FILE, "w") as fh:
        fh.write("file")
    monkeypatch.setenv("LLM_API_KEY", "env")
    assert subs.llm_headers()["Authorization"] == "Bearer env"


def test_no_key_sends_no_header(subs):
    assert "Authorization" not in subs.llm_headers()


class _Resp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def models(name):
    return lambda req, timeout: _Resp(json.dumps({"data": [{"id": name}]}).encode())


def test_other_model_on_the_card_is_not_used(subs, monkeypatch):
    monkeypatch.setattr(subs, "LLM_MODEL", "gemma4")
    monkeypatch.setattr(subs.urllib.request, "urlopen", models("gemma12"))
    with pytest.raises(SystemExit, match="зайнята"):
        subs.model_name()


def test_expected_model_is_used(subs, monkeypatch):
    monkeypatch.setattr(subs, "LLM_MODEL", "gemma4")
    monkeypatch.setattr(subs.urllib.request, "urlopen", models("gemma4"))
    assert subs.model_name() == "gemma4"


def test_refused_key_explains_where_the_key_goes(subs, monkeypatch):
    def refuse(req, timeout):
        raise subs.urllib.error.HTTPError(req.full_url, 401, "Unauthorized", {}, None)
    monkeypatch.setattr(subs.urllib.request, "urlopen", refuse)
    with pytest.raises(SystemExit, match=os.path.basename(subs.LLM_KEY_FILE)):
        subs.model_name()


@pytest.mark.parametrize("raw", [
    pytest.param(b"not json", id="not-json"),
    pytest.param(b"{}", id="no-choices"),
    pytest.param(b'{"choices": []}', id="empty-choices"),
    pytest.param(b'{"choices": [{"message": {"content": 42}}]}', id="non-string"),
    pytest.param(b'{"choices": ["x"]}', id="wrong-shape"),
])
def test_malformed_model_reply_is_an_empty_answer(subs, monkeypatch, raw):
    monkeypatch.setattr(subs, "MODEL", "m", raising=False)
    monkeypatch.setattr(subs.urllib.request, "urlopen", lambda req, timeout: _Resp(raw))
    assert subs.llm([{"role": "user", "content": "hi"}]) == ""
