"""
Tests for how Anthropic API failures are classified and when Flow pauses AI calls.

An expired or revoked key (401/403) or an unbillable account must pause
automatic calls while the same key is configured, and clear as soon as the key
changes or any request succeeds. Uses the SDK's real exception classes; no
network. Runnable directly (``python3 test_card_ai_errors.py``) or via pytest.
"""

from __future__ import annotations

import os
import types
from datetime import datetime, timezone

os.environ.setdefault("DATABASE_URL", "postgresql+psycopg2://test@localhost/test")  # models import; never connects

import anthropic  # noqa: E402
import httpx2  # noqa: E402

import card_ai  # noqa: E402


def _status_error(cls, status, message):
    req = httpx2.Request("POST", "https://api.anthropic.com/v1/messages")
    return cls(message, response=httpx2.Response(status, request=req), body={"error": {"message": message}})


def test_classification():
    cases = [
        (_status_error(anthropic.AuthenticationError, 401, "invalid x-api-key"), "auth"),
        (_status_error(anthropic.PermissionDeniedError, 403, "key revoked"), "auth"),
        (_status_error(anthropic.BadRequestError, 400,
                       "Your credit balance is too low to access the Anthropic API."), "billing"),
        (_status_error(anthropic.BadRequestError, 400, "messages: field required"), "other"),
        (_status_error(anthropic.BadRequestError, 400, "The compiled grammar is too large, which would cause "
                       "performance issues. Simplify your tool schemas or reduce the number of strict tools."), "other"),
        (_status_error(anthropic.RateLimitError, 429, "rate limited"), "rate_limit"),
        (_status_error(anthropic.NotFoundError, 404, "model not found"), "model"),
        (anthropic.APIConnectionError(request=httpx2.Request("POST", "https://api.anthropic.com")), "network"),
        (card_ai.AiError("The model declined the request: policy"), "other"),
    ]
    for exc, kind in cases:
        message, fatal, got = card_ai._describe_error(exc)
        assert got == kind, (exc, got, kind)
        assert message
    assert "rejected" in card_ai._describe_error(cases[0][0])[0]
    assert card_ai._describe_error(cases[-1][0])[1] is False      # a refusal does not stop the run
    assert "Flow bug" in card_ai._describe_error(cases[4][0])[0]  # not blamed on the key or account


def _row(status, kind=None, fp="abc", when=None):
    return types.SimpleNamespace(status=status, error_kind=kind, key_fingerprint=fp, error="x",
                                 created_at=when or datetime(2026, 10, 24, tzinfo=timezone.utc))


def test_key_problem_holds_until_key_changes_or_a_call_succeeds():
    assert card_ai.problem_from_log(_row("error", "auth"), "abc")["kind"] == "auth"
    assert card_ai.problem_from_log(_row("error", "billing"), "abc")["kind"] == "billing"
    assert card_ai.problem_from_log(_row("error", "auth"), "new") is None        # key replaced
    assert card_ai.problem_from_log(_row("ok"), "abc") is None                    # latest call worked
    assert card_ai.problem_from_log(_row("error", "rate_limit"), "abc") is None   # transient: retry later
    assert card_ai.problem_from_log(_row("error", "network"), "abc") is None
    assert card_ai.problem_from_log(None, "abc") is None
    assert card_ai.problem_from_log(_row("error", "auth"), None) is None          # no key configured


def test_fingerprint_is_short_and_not_the_key():
    old = os.environ.get("ANTHROPIC_API_KEY")
    try:
        os.environ["ANTHROPIC_API_KEY"] = "sk-ant-secret-value"
        fp = card_ai.key_fingerprint()
        assert fp and len(fp) == 12 and "secret" not in fp
        os.environ["ANTHROPIC_API_KEY"] = "sk-ant-other-value"
        assert card_ai.key_fingerprint() != fp
        os.environ["ANTHROPIC_API_KEY"] = ""
        assert card_ai.key_fingerprint() is None
    finally:
        if old is None:
            os.environ.pop("ANTHROPIC_API_KEY", None)
        else:
            os.environ["ANTHROPIC_API_KEY"] = old


if __name__ == "__main__":
    import sys
    failed = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"PASS {name}")
            except Exception as exc:  # noqa: BLE001
                failed += 1
                print(f"FAIL {name}: {exc!r}")
    sys.exit(1 if failed else 0)
