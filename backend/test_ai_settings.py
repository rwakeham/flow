"""
The AI dialog's settings and key routes (routers/ai): validation, the ".env wins" lock,
and the key test's logging. Pure: fake sessions and a fake client, no network.
"""

from __future__ import annotations

import os
from types import SimpleNamespace as NS

os.environ.setdefault("DATABASE_URL", "postgresql+psycopg2://test@localhost/test")
os.environ.setdefault("SECRET_KEY", "test-" * 8)

import pytest  # noqa: E402
from fastapi import HTTPException  # noqa: E402

import card_ai  # noqa: E402
import settings_store as st  # noqa: E402
from routers import ai as R  # noqa: E402


@pytest.fixture(autouse=True)
def clean(monkeypatch):
    for var in list(st.SETTING_ENV.values()) + list(st.SECRET_ENV.values()):
        monkeypatch.delenv(var, raising=False)
    st.testing_state()
    yield
    st.testing_state()


def test_checked_settings_validates_and_leaves_absent_keys_alone():
    body = R.SettingsBody(ai_model=" claude-haiku-5-5 ", ai_enabled=False, ai_redact=" Smith ,Acme,, ")
    assert R.checked_settings(body) == {st.AI_MODEL: "claude-haiku-5-5", st.AI_ENABLED: "0", st.AI_REDACT: "Smith, Acme"}
    assert R.checked_settings(R.SettingsBody(ai_model="")) == {st.AI_MODEL: ""}          # blank: back to the default
    assert R.checked_settings(R.SettingsBody()) == {}
    with pytest.raises(HTTPException) as e:
        R.checked_settings(R.SettingsBody(ai_model_ask="claude sonnet"))
    assert e.value.status_code == 422
    with pytest.raises(HTTPException):
        R.checked_settings(R.SettingsBody(ai_redact="<script>"))


def test_settings_payload_says_where_each_value_comes_from(monkeypatch):
    st.testing_state({st.AI_MODEL_ASK: "claude-haiku-5-5"}, {st.API_KEY: "sk-ant-stored-1234"})
    p = R.settings_payload()
    assert p["ai_model_ask"] == {"value": "claude-haiku-5-5", "source": "flow", "env_var": "FLOW_AI_MODEL_ASK"}
    assert p["ai_model"]["source"] is None and p["api_key"]["last4"] == "1234" and p["api_key"]["source"] == "flow"
    assert "sk-ant" not in str(p)                                                      # never the key itself
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-env-key-5678")
    assert R.settings_payload()["api_key"] == {"set": True, "last4": "5678", "source": "env",
                                               "env_var": "ANTHROPIC_API_KEY", "unreadable": False}


class FakeDB:
    def __init__(self):
        self.rows, self.committed = [], 0

    def add(self, obj):
        self.rows.append(obj)

    def flush(self):
        pass

    def commit(self):
        self.committed += 1


def test_key_test_logs_the_call_and_reports_the_outcome():
    st.testing_state(secrets_={st.API_KEY: "sk-ant-x"})
    db = FakeDB()
    good = NS(messages=NS(create=lambda **kw: NS(model=kw["model"], usage=NS(input_tokens=9, output_tokens=1),
                                                  content=[], stop_reason="end_turn")))
    ok, message = card_ai.test_key(db, client=good)
    row = db.rows[0]
    assert ok and "works" in message and row.purpose == "key_test" and row.status == "ok" and row.input_tokens == 9
    assert "sk-ant" not in (row.request_json or "") and row.key_fingerprint == card_ai.key_fingerprint()
    import anthropic
    import httpx2
    req = httpx2.Request("POST", "https://api.anthropic.com/v1/messages")
    exc = anthropic.AuthenticationError("invalid x-api-key", response=httpx2.Response(401, request=req),
                                        body={"error": {"message": "invalid x-api-key"}})
    bad = NS(messages=NS(create=lambda **kw: (_ for _ in ()).throw(exc)))
    ok, message = card_ai.test_key(FakeDB2 := FakeDB(), client=bad)
    assert not ok and "rejected" in message and FakeDB2.rows[0].error_kind == "auth"


def test_env_locked_values_are_refused_with_409(monkeypatch):
    monkeypatch.setenv("FLOW_AI_MODEL", "claude-opus-5-5")
    with pytest.raises(HTTPException) as e:
        R.put_settings(R.SettingsBody(ai_model="claude-haiku-5-5"), db=FakeDB())
    assert e.value.status_code == 409 and "FLOW_AI_MODEL" in e.value.detail
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-env")
    with pytest.raises(HTTPException) as e:
        R.put_key(R.KeyBody(key="sk-ant-new-key"), db=FakeDB())
    assert e.value.status_code == 409
    monkeypatch.delenv("ANTHROPIC_API_KEY")
    with pytest.raises(HTTPException) as e:
        R.put_key(R.KeyBody(key="not a key"), db=FakeDB())
    assert e.value.status_code == 422
