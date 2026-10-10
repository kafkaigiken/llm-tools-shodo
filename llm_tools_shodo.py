"""llm plugin: proofread Japanese text with the Shodo API (https://shodo.ink).

Two entry points share the same code:
  - `llm shodo`: a CLI command. Reads text, writes the corrected text. No model involved.
  - `shodo_proofread`: an llm tool, for use inside a model conversation.

API flow:
  1. POST {api_root}/lint/       body={"body": text, "type": "text"} -> {"lint_id": ...}
  2. GET  {api_root}/lint/{id}/  while "status" is "processing"
  3. Each message has index/index_to (character offsets), before, after and score.

Credentials. The root and the token always come from the same source, first match wins:
  1. SHODO_API_ROOT and SHODO_API_TOKEN environment variables, both set.
  2. SHODO_API_ROOT plus the token stored with `llm keys set shodo`.
  3. The "default" profile in ~/.config/shodo/credentials (or ~/.shodo/credentials when
     that file does not exist), the file `shodo login` writes.
"""

import json
import os
import sys
import time
from pathlib import Path

import click
import httpx2
import llm

ENV_ROOT = "SHODO_API_ROOT"
ENV_TOKEN = "SHODO_API_TOKEN"
KEY_ALIAS = "shodo"
POLL_TIMEOUT = 120  # seconds
HTTP_TIMEOUT = 30  # seconds
# The official `shodo lint` CLI hides messages below this score. Below it, a
# suggestion is reported but never applied.
MIN_SCORE = 0.5


class ShodoError(Exception):
    pass


def _credentials_file():
    config_home = Path(os.environ.get("XDG_CONFIG_HOME", "~/.config")).expanduser()
    path = config_home / "shodo/credentials"
    if not path.exists():
        path = Path("~/.shodo/credentials").expanduser()
        if not path.exists():
            return None, None
    try:
        profile = json.loads(path.read_text(encoding="utf-8"))["default"]
        return profile[ENV_ROOT], profile[ENV_TOKEN]
    except (OSError, ValueError, KeyError, TypeError):
        return None, None


def _credentials():
    root = os.environ.get(ENV_ROOT)
    pairs = [
        (root, os.environ.get(ENV_TOKEN)),
        (root, llm.get_key(alias=KEY_ALIAS)),  # stored key only, no env fallback
        _credentials_file(),
    ]
    for root, token in pairs:
        if root and token:
            return root, token
    raise ShodoError(
        f"Shodo credentials not found. Set {ENV_ROOT}, then set {ENV_TOKEN} or run "
        f"`llm keys set {KEY_ALIAS}`. A `shodo login` profile also works."
    )


def _client(root, token, transport=None):
    return httpx2.Client(
        base_url=root.rstrip("/") + "/",
        headers={"Authorization": f"Bearer {token}"},
        timeout=HTTP_TIMEOUT,
        transport=transport,
    )


def lint(text, is_html=False):
    """Run a Shodo lint and return the list of message dicts."""
    try:
        with _client(*_credentials()) as client:
            body = {"body": text, "type": "html" if is_html else "text"}
            lint_id = client.post("lint/", json=body).raise_for_status().json()["lint_id"]

            deadline = time.monotonic() + POLL_TIMEOUT
            pause = 0.25
            while True:
                result = client.get(f"lint/{lint_id}/").raise_for_status().json()
                status = result.get("status")
                if status == "done":
                    return result.get("messages") or []
                if status != "processing":
                    raise ShodoError(f"Shodo lint {lint_id} ended with status {status!r}: {result}")
                if time.monotonic() > deadline:
                    raise ShodoError(f"Shodo lint {lint_id} timed out after {POLL_TIMEOUT} s.")
                time.sleep(pause)
                pause = min(pause * 2, 4)
    except httpx2.HTTPStatusError as e:
        raise ShodoError(
            f"Shodo API error {e.response.status_code} for {e.request.url}: {e.response.text}"
        ) from e
    except httpx2.HTTPError as e:
        raise ShodoError(f"Shodo request failed: {e}") from e
    except (ValueError, KeyError, TypeError, AttributeError) as e:
        raise ShodoError(f"Unexpected response from Shodo: {e!r}") from e


def apply_fixes(text, messages):
    """Apply every message that has a suggested `after` value and a score of at least MIN_SCORE.

    Returns (corrected_text, applied, skipped). Exact duplicate suggestions count once.
    Fixes go from the end of the text to the start so earlier offsets stay valid.
    A fix is skipped when:
      - its offsets are outside the text or reversed,
      - text[index:index_to] does not match `before` (for example, offset differences with emoji),
      - it overlaps a fix already applied, or it is a second insertion at the same position.
    """
    seen, candidates = set(), []
    for m in messages:
        if m.get("after") is None or m.get("score", 1) < MIN_SCORE:
            continue
        key = _fix_key(m)
        if key not in seen:
            seen.add(key)
            candidates.append(m)
    candidates.sort(key=lambda m: (m["index"], m["index_to"]), reverse=True)

    applied, skipped = [], []
    limit = len(text)
    inserted_at = None  # position of the last insertion applied
    for m in candidates:
        start, end = m["index"], m["index_to"]
        before = m.get("before")
        if (
            not 0 <= start <= end <= limit
            or (start == end == inserted_at)
            or (before is not None and text[start:end] != before)
        ):
            skipped.append(m)
            continue
        text = text[:start] + m["after"] + text[end:]
        applied.append(m)
        limit = start
        inserted_at = start if start == end else None

    applied.reverse()
    skipped.reverse()
    return text, applied, skipped


def proofread(text, is_html=False):
    """Lint and fix. Returns (corrected_text, applied, advisory)."""
    if not text.strip():
        return text, [], []
    messages = lint(text, is_html)
    corrected, applied, _ = apply_fixes(text, messages)
    # Everything not applied is advisory, except duplicates of an applied fix.
    done = {_fix_key(m) for m in applied}
    advisory = [m for m in messages if _fix_key(m) not in done]
    return corrected, applied, advisory


def _fix_key(m):
    return (m.get("index"), m.get("index_to"), m.get("before"), m.get("after"))


def _describe(m):
    pos = m.get("from") or {}
    where = f"{pos.get('line', 0) + 1}:{pos.get('ch', 0) + 1}" if pos else "?"
    change = ""
    if m.get("after") is not None:
        change = f" 「{m.get('before') or ''}」→「{m['after']}」"
    return f"- {where} [{m.get('severity', '')}] {m.get('message', '')}{change}"


def _section(title, messages):
    return [f"{title} ({len(messages)}):"] + ([_describe(m) for m in messages] or ["- none"])


def _report(applied, advisory):
    return "\n".join(
        _section("APPLIED FIXES", applied) + [""] + _section("ISSUES WITHOUT AUTOMATIC FIX", advisory)
    )


def shodo_proofread(text: str) -> str:
    """
    Proofread Japanese text with the Shodo AI proofreading API and return the corrected text.

    Pass the full Japanese text exactly as the user gave it. The tool sends it to Shodo,
    applies every confident suggested fix automatically, and returns the corrected text plus
    a list of the changes and of the remaining issues that have no automatic fix.

    Args:
        text: The Japanese text to proofread, unchanged.

    Returns:
        A report with three sections: CORRECTED TEXT (the full text with fixes applied),
        APPLIED FIXES, and ISSUES WITHOUT AUTOMATIC FIX.
    """
    corrected, applied, advisory = proofread(text)
    return "\n".join(["CORRECTED TEXT:", corrected, "", _report(applied, advisory)])


@llm.hookimpl
def register_tools(register):
    register(shodo_proofread)


@llm.hookimpl
def register_commands(cli):
    @cli.command(name="shodo")
    @click.argument("file", type=click.File("rb"), default="-")
    @click.option("--html", is_flag=True, help="The input is HTML.")
    @click.option("-q", "--quiet", is_flag=True, help="Do not print the fix report to stderr.")
    def shodo_command(file, html, quiet):
        """Proofread Japanese text with Shodo and print the corrected text.

        Reads FILE, or stdin when FILE is omitted or "-". The input must be UTF-8.
        The corrected text goes to stdout with every other byte unchanged, including
        line endings. The applied fixes and the remaining issues go to stderr.

        Example: cat article.md | llm shodo > fixed.md
        """
        try:
            text = file.read().decode("utf-8")
        except UnicodeDecodeError as e:
            raise click.ClickException(f"Input is not UTF-8 ({e}). Convert it first, e.g. with iconv.")
        try:
            corrected, applied, advisory = proofread(text, html)
        except ShodoError as e:
            raise click.ClickException(str(e))
        stdout = click.get_binary_stream("stdout")
        stdout.write(corrected.encode("utf-8"))
        stdout.flush()
        if not quiet:
            if corrected and not corrected.endswith("\n"):
                click.echo(err=True)  # keep the report off the last line of the text
            click.echo(_report(applied, advisory), err=True)
