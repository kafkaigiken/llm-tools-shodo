import json

import httpx2
import llm
import pytest
from click.testing import CliRunner
from llm.cli import cli

import llm_tools_shodo
from llm_tools_shodo import apply_fixes, proofread, shodo_proofread

TEXT = "これはテストの文章でです。ら抜き言葉を見れる。"
FIXED = "これはテストの文章です。ら抜き言葉を見られる。"
ROOT = "https://api.shodo.ink/@org/proj/"


def msg(index, index_to, before, after, message="誤字", severity="error", score=1.0):
    return {
        "code": "x",
        "message": message,
        "severity": severity,
        "from": {"line": 0, "ch": index},
        "to": {"line": 0, "ch": index_to},
        "index": index,
        "index_to": index_to,
        "score": score,
        "before": before,
        "after": after,
    }


MESSAGES = [
    msg(9, 12, "でです", "です"),
    msg(19, 22, "見れる", "見られる", message="ら抜き言葉"),
    msg(0, 2, "これ", None, message="指示語", severity="warning"),
]


@pytest.fixture(autouse=True)
def isolated(monkeypatch, tmp_path):
    """Empty llm key store, no Shodo environment, no credentials file."""
    monkeypatch.setenv("LLM_USER_PATH", str(tmp_path / "llm"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("SHODO_API_ROOT", raising=False)
    monkeypatch.delenv("SHODO_API_TOKEN", raising=False)
    monkeypatch.setattr(llm_tools_shodo.time, "sleep", lambda s: None)
    return tmp_path


def mock_api(monkeypatch, handler):
    """Route every Shodo request to handler. Passing transport= also disables env proxies."""
    monkeypatch.setenv("SHODO_API_ROOT", ROOT)
    monkeypatch.setenv("SHODO_API_TOKEN", "tok")
    real_client = llm_tools_shodo._client
    monkeypatch.setattr(
        llm_tools_shodo,
        "_client",
        lambda root, token: real_client(root, token, transport=httpx2.MockTransport(handler)),
    )


@pytest.fixture
def fake_api(monkeypatch):
    requests = []

    def handler(request):
        requests.append(request)
        if request.method == "POST":
            return httpx2.Response(200, json={"lint_id": "abc"})
        if len(requests) == 2:
            return httpx2.Response(200, json={"status": "processing", "messages": []})
        return httpx2.Response(200, json={"status": "done", "messages": MESSAGES})

    mock_api(monkeypatch, handler)
    return requests


def write_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data))


# --- apply_fixes ---------------------------------------------------------------


def test_apply_fixes():
    fixed, applied, skipped = apply_fixes(TEXT, MESSAGES)
    assert fixed == FIXED
    assert len(applied) == 2
    assert skipped == []


def test_apply_fixes_skips_mismatch_and_overlap():
    bad = [msg(9, 12, "ちがう", "です"), msg(9, 12, "でです", "です"), msg(10, 12, "です", "")]
    fixed, applied, skipped = apply_fixes(TEXT, bad)
    assert len(applied) == 1
    assert len(skipped) == 2


def test_apply_fixes_skips_low_score():
    fixed, applied, skipped = apply_fixes("abcdef", [msg(1, 2, "b", "B", score=0.3)])
    assert fixed == "abcdef"
    assert applied == skipped == []


def test_apply_fixes_one_insertion_per_position():
    same = [msg(3, 3, "", "、"), msg(3, 3, "", "、")]
    assert apply_fixes("abcdef", same)[0] == "abc、def"
    different = [msg(3, 3, "", "X"), msg(3, 3, "", "Y")]
    fixed, applied, skipped = apply_fixes("abcdef", different)
    assert fixed in ("abcXdef", "abcYdef")
    assert len(applied) == len(skipped) == 1


def test_apply_fixes_insertion_next_to_replacement():
    fixed, _, _ = apply_fixes("abcdef", [msg(2, 4, "cd", "ZZ"), msg(2, 2, "", "I")])
    assert fixed == "abIZZef"


def test_apply_fixes_rejects_bad_offsets():
    bad = [msg(4, 2, None, "X"), msg(-2, 6, None, "X"), msg(5, 7, None, "X")]
    fixed, applied, skipped = apply_fixes("abcdef", bad)
    assert fixed == "abcdef"
    assert len(skipped) == 3


def test_proofread_report_lists_low_score_not_duplicates(monkeypatch):
    dup = msg(1, 2, "b", "B")
    low = msg(4, 5, "e", "E", score=0.2)
    monkeypatch.setattr(llm_tools_shodo, "lint", lambda text, is_html=False: [dup, dict(dup), low])
    fixed, applied, advisory = proofread("abcdef")
    assert fixed == "aBcdef"
    assert applied == [dup]
    assert advisory == [low]


def test_describe_null_before():
    assert "「」→「を」" in llm_tools_shodo._describe(msg(2, 2, None, "を"))


# --- lint and the tool ---------------------------------------------------------


def test_shodo_proofread(fake_api):
    out = shodo_proofread(TEXT)
    assert FIXED in out
    assert "APPLIED FIXES (2)" in out
    assert "ISSUES WITHOUT AUTOMATIC FIX (1)" in out
    post = fake_api[0]
    assert (post.method, str(post.url)) == ("POST", ROOT + "lint/")
    assert post.headers["Authorization"] == "Bearer tok"
    assert json.loads(post.content) == {"body": TEXT, "type": "text"}
    assert str(fake_api[-1].url) == ROOT + "lint/abc/"
    assert len(fake_api) == 3  # POST, one "processing" poll, one "done" poll


def test_empty_input_skips_api(fake_api):
    assert proofread("  \n") == ("  \n", [], [])
    assert fake_api == []


def test_tool_via_echo_model(fake_api):
    model = llm.get_model("echo")
    chain = model.chain(
        json.dumps({"tool_calls": [{"name": "shodo_proofread", "arguments": {"text": TEXT}}]}),
        tools=[shodo_proofread],
    )
    responses = list(chain.responses())
    output = json.loads(responses[-1].text())["tool_results"][0]["output"]
    assert "見られる" in output


@pytest.mark.parametrize(
    "response, match",
    [
        (httpx2.Response(401, text="bad token"), "401"),
        (httpx2.Response(200, text="<html>maintenance</html>"), "Unexpected response"),
        (httpx2.Response(200, json={"detail": "x"}), "Unexpected response"),
    ],
)
def test_bad_responses_raise_shodo_error(monkeypatch, response, match):
    mock_api(monkeypatch, lambda r: response)
    with pytest.raises(llm_tools_shodo.ShodoError, match=match):
        llm_tools_shodo.lint(TEXT)


def test_network_error_raises_shodo_error(monkeypatch):
    def handler(request):
        raise httpx2.ConnectError("refused", request=request)

    mock_api(monkeypatch, handler)
    with pytest.raises(llm_tools_shodo.ShodoError, match="request failed"):
        llm_tools_shodo.lint(TEXT)


def test_unknown_status_stops_polling(monkeypatch):
    calls = []

    def handler(request):
        calls.append(request)
        if request.method == "POST":
            return httpx2.Response(200, json={"lint_id": "abc"})
        return httpx2.Response(200, json={"status": "queued_forever"})

    mock_api(monkeypatch, handler)
    with pytest.raises(llm_tools_shodo.ShodoError, match="queued_forever"):
        llm_tools_shodo.lint(TEXT)
    assert len(calls) == 2


def test_mock_survives_proxy_env(monkeypatch, fake_api):
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:9")
    monkeypatch.setenv("ALL_PROXY", "http://127.0.0.1:9")
    assert FIXED in shodo_proofread(TEXT)


# --- CLI -----------------------------------------------------------------------


def test_cli_command(fake_api):
    result = CliRunner().invoke(cli, ["shodo"], input=TEXT + "\n")
    assert result.exit_code == 0, result.output
    assert result.stdout == FIXED + "\n"
    assert result.stderr.startswith("APPLIED FIXES (2)")


def test_cli_command_quiet(fake_api):
    result = CliRunner().invoke(cli, ["shodo", "-q"], input=TEXT)
    assert result.exit_code == 0
    assert result.stdout == FIXED
    assert result.stderr == ""


def test_cli_report_starts_on_new_line(fake_api):
    result = CliRunner().invoke(cli, ["shodo"], input=TEXT)
    assert result.stderr.startswith("\nAPPLIED FIXES")


def test_cli_keeps_crlf_and_control_bytes(monkeypatch):
    mock_api(
        monkeypatch,
        lambda r: httpx2.Response(
            200,
            json={"lint_id": "x"} if r.method == "POST" else {"status": "done", "messages": []},
        ),
    )
    raw = "あ\r\nい\rZ\x1b[31mR\x1b[0m\n".encode("utf-8")
    result = CliRunner().invoke(cli, ["shodo", "-q"], input=raw)
    assert result.exit_code == 0
    assert result.stdout_bytes == raw


def test_cli_rejects_non_utf8(fake_api):
    result = CliRunner().invoke(cli, ["shodo"], input="テスト".encode("shift_jis"))
    assert result.exit_code == 1
    assert "not UTF-8" in result.output
    assert result.exception is None or isinstance(result.exception, SystemExit)


def test_cli_network_error_is_clean(monkeypatch):
    def handler(request):
        raise httpx2.ConnectError("refused", request=request)

    mock_api(monkeypatch, handler)
    result = CliRunner().invoke(cli, ["shodo"], input=TEXT)
    assert result.exit_code == 1
    assert "Error: Shodo request failed" in result.output
    assert isinstance(result.exception, SystemExit)


# --- credentials ---------------------------------------------------------------


def test_env_pair_beats_stored_key(monkeypatch, isolated):
    write_json(isolated / "llm" / "keys.json", {"shodo": "stored-tok"})
    monkeypatch.setenv("SHODO_API_ROOT", ROOT)
    monkeypatch.setenv("SHODO_API_TOKEN", "env-tok")
    assert llm_tools_shodo._credentials() == (ROOT, "env-tok")


def test_env_root_with_stored_key(monkeypatch, isolated):
    write_json(isolated / "llm" / "keys.json", {"shodo": "stored-tok"})
    monkeypatch.setenv("SHODO_API_ROOT", ROOT)
    assert llm_tools_shodo._credentials() == (ROOT, "stored-tok")


def test_credentials_file_used_as_a_pair(monkeypatch, isolated):
    other = "https://api.shodo.ink/@org/other/"
    write_json(
        isolated / "shodo" / "credentials",
        {"default": {"SHODO_API_ROOT": other, "SHODO_API_TOKEN": "file-tok"}},
    )
    assert llm_tools_shodo._credentials() == (other, "file-tok")
    # A lone env root must not be paired with the file's token.
    monkeypatch.setenv("SHODO_API_ROOT", ROOT)
    assert llm_tools_shodo._credentials() == (other, "file-tok")


@pytest.mark.parametrize("content", ["{bad", '{"default": "x"}', "[1, 2]", "{}"])
def test_broken_credentials_file(isolated, content):
    path = isolated / "shodo" / "credentials"
    path.parent.mkdir()
    path.write_text(content)
    with pytest.raises(llm_tools_shodo.ShodoError, match="credentials not found"):
        llm_tools_shodo._credentials()


def test_missing_credentials():
    with pytest.raises(llm_tools_shodo.ShodoError):
        llm_tools_shodo._credentials()
