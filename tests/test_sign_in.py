"""Signing Claude Code in through the browser, with no terminal window, and checking the sign-in."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest

from depop_seller import settings

# Behaves like `claude auth login` does when run without a terminal (probed against the real
# CLI): says it is opening the browser, prints a fallback link, then reads a pasted code from
# stdin while it waits for the browser. `auth status --json` reports whatever the login left.
STUB = textwrap.dedent("""
    import json, os, sys
    here = os.path.dirname(os.path.abspath(__file__))
    mark = os.path.join(here, 'signed-in')
    args = sys.argv[1:]
    if args[:2] == ['auth', 'status']:
        signed = os.path.exists(mark)
        print(json.dumps({'loggedIn': signed, 'authMethod': 'claude.ai' if signed else 'none',
                          'subscriptionType': 'max' if signed else None}))
        sys.exit(0 if signed else 1)
    assert args == ['auth', 'login', '--claudeai'], args
    open(os.path.join(here, 'login-env.json'), 'w').write(json.dumps(sorted(os.environ)))
    print('Opening browser to sign in' + chr(8230), flush=True)
    print("If the browser didn't open, visit: https://claude.example/oauth/authorize?code=true&state=abc", flush=True)
    sys.stdout.write('Paste code here if prompted > ')
    sys.stdout.flush()
    code = sys.stdin.readline().strip()
    if code == 'good-code#abc':
        open(mark, 'w').write('yes')
        print('Login successful.')
        sys.exit(0)
    print('Login failed: Request failed with status code 400')
    sys.exit(1)
""")


def _stub_cli(tmp_path: Path) -> Path:
    (tmp_path / "stub.py").write_text(STUB, encoding="utf-8")
    crlf = chr(13) + chr(10)
    cmd = tmp_path / "claude.cmd"
    cmd.write_text("@echo off" + crlf + f'"{sys.executable}" "{tmp_path / "stub.py"}" %*' + crlf, encoding="utf-8")
    return cmd


def _wait(sign_in: settings.SignIn, until, seconds: float = 20) -> dict:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        state = sign_in.status()
        if until(state):
            return state
        time.sleep(0.1)
    raise AssertionError(f"timed out: {sign_in.status()}")


@pytest.mark.skipif(os.name != "nt", reason="the stub CLI is a .cmd")
def test_signing_in_happens_in_the_browser_with_no_window_and_no_api_key(tmp_path: Path, monkeypatch):
    cli = _stub_cli(tmp_path)
    monkeypatch.setattr(settings, "find_claude_cli", lambda: cli)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test-must-not-reach-the-sign-in")
    started = []
    real_popen = subprocess.Popen

    def recording_popen(argv, **kw):
        started.append((argv, kw))
        return real_popen(argv, **kw)

    monkeypatch.setattr(settings.subprocess, "Popen", recording_popen)
    sign_in = settings.SignIn()

    with pytest.raises(RuntimeError, match="not waiting"):
        sign_in.send_code("good-code#abc")                   # nothing started yet

    first = sign_in.start()
    assert first["running"] is True
    state = _wait(sign_in, lambda s: s["url"])
    assert state["url"].startswith("https://claude.example/oauth/authorize")    # the "no page opened?" link
    argv, kw = started[0]
    assert argv[1:] == ["auth", "login", "--claudeai"]
    assert kw.get("creationflags", 0) & 0x08000000                              # no console window
    assert not any(k.upper().startswith("ANTHROPIC_") for k in kw["env"])      # the plan, never the API
    assert sign_in.start()["running"] is True and len(started) == 1            # a second press joins the first

    with pytest.raises(ValueError):
        sign_in.send_code("   ")
    sign_in.send_code("wrong-code#abc")
    state = _wait(sign_in, lambda s: not s["running"])
    assert state["ok"] is False and "Login failed" in state["detail"]

    sign_in.start()
    _wait(sign_in, lambda s: s["url"])
    sign_in.send_code("  good-code#abc ")                                      # pasted with spaces around
    state = _wait(sign_in, lambda s: not s["running"])
    assert state["ok"] is True and state["detail"] == "Signed in with your Claude Max plan."
    login_env = json.loads((tmp_path / "login-env.json").read_text())
    assert not any(k.upper().startswith("ANTHROPIC_") for k in login_env)


def test_sign_in_check_asks_the_cli_and_spends_nothing(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(settings, "find_claude_cli", lambda: tmp_path / "claude.exe")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test-must-not-reach-the-check")
    seen = {}
    answers = iter([
        {"loggedIn": True, "authMethod": "claude.ai", "subscriptionType": "pro"},
        {"loggedIn": False, "authMethod": "none"},
        {"loggedIn": True, "authMethod": "console"},                 # an API account: billed separately
        {"loggedIn": True, "authMethod": "claude.ai"},
    ])

    def fake_run(argv, **kw):
        seen.update(argv=argv, env=kw["env"], flags=kw.get("creationflags", 0))
        return subprocess.CompletedProcess(argv, 0, stdout=json.dumps(next(answers)), stderr="")

    monkeypatch.setattr(settings.subprocess, "run", fake_run)
    assert settings.test_claude_login() == {"ok": True, "plan": "Pro", "detail": "Signed in with your Claude Pro plan."}
    assert seen["argv"][1:] == ["auth", "status", "--json"]                   # no prompt, so no usage
    assert not any(k.upper().startswith("ANTHROPIC_") for k in seen["env"])
    if os.name == "nt":
        assert seen["flags"] & 0x08000000
    assert settings.test_claude_login()["ok"] is False
    out = settings.test_claude_login()
    assert out["ok"] is False and "API account" in out["detail"]
    assert settings.test_claude_login()["detail"] == "Signed in with your Claude plan."

    monkeypatch.setattr(settings, "find_claude_cli", lambda: None)
    assert settings.test_claude_login()["ok"] is False                        # no Claude app at all
