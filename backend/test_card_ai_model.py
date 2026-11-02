"""Which model each AI task uses: FLOW_AI_MODEL_<TASK>, then FLOW_AI_MODEL, then the default."""

import os
import sys
from types import SimpleNamespace as NS

os.environ.setdefault("DATABASE_URL", "postgresql+psycopg2://u:p@localhost/x")
os.environ.setdefault("SECRET_KEY", "test")
sys.path.insert(0, os.path.dirname(__file__))

import pytest  # noqa: E402

import card_ai  # noqa: E402

_VARS = ("FLOW_AI_MODEL", "FLOW_AI_MODEL_CATEGORIZE", "FLOW_AI_MODEL_ASK")


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for v in _VARS:
        monkeypatch.delenv(v, raising=False)


def test_default_is_sonnet_not_opus():
    assert card_ai.DEFAULT_MODEL == "claude-sonnet-5-5"
    assert card_ai.model_name() == "claude-sonnet-5-5"
    for task in card_ai.TASKS:
        assert card_ai.model_name(task) == "claude-sonnet-5-5"


def test_flow_ai_model_applies_to_every_task(monkeypatch):
    monkeypatch.setenv("FLOW_AI_MODEL", "claude-opus-5-5")
    assert card_ai.model_name() == "claude-opus-5-5"
    assert card_ai.model_name(card_ai.TASK_CATEGORIZE) == "claude-opus-5-5"
    assert card_ai.model_name(card_ai.TASK_ASK) == "claude-opus-5-5"


def test_task_override_wins_only_for_its_task(monkeypatch):
    monkeypatch.setenv("FLOW_AI_MODEL", "claude-opus-5-5")
    monkeypatch.setenv("FLOW_AI_MODEL_CATEGORIZE", "claude-haiku-5-5")
    assert card_ai.model_name(card_ai.TASK_CATEGORIZE) == "claude-haiku-5-5"
    assert card_ai.model_name(card_ai.TASK_ASK) == "claude-opus-5-5"
    assert card_ai.model_name() == "claude-opus-5-5"          # insights, balances


def test_blank_values_fall_through(monkeypatch):
    monkeypatch.setenv("FLOW_AI_MODEL", "  ")
    monkeypatch.setenv("FLOW_AI_MODEL_ASK", "")
    assert card_ai.model_name(card_ai.TASK_ASK) == card_ai.DEFAULT_MODEL


def test_unknown_task_ignores_stray_variables(monkeypatch):
    monkeypatch.setenv("FLOW_AI_MODEL_INSIGHTS", "claude-haiku-5-5")
    assert card_ai.model_name("insights") == card_ai.DEFAULT_MODEL


def test_haiku_gets_no_server_side_fallback():
    assert card_ai.fallback_kwargs("claude-haiku-5-5") == {}
    assert card_ai.fallback_kwargs("claude-sonnet-5-5")["fallbacks"] == "default"


def test_call_sends_the_task_model(monkeypatch):
    monkeypatch.setenv("FLOW_AI_MODEL_CATEGORIZE", "claude-haiku-5-5")
    sent = {}

    def parse(**kwargs):
        sent.update(kwargs)
        return NS(stop_reason="end_turn", parsed_output=object(), usage=None, model=kwargs["model"])

    client = NS(beta=NS(messages=NS(parse=parse)))
    _parsed, _usage, model = card_ai._call(client, "system", {}, object, effort="low", max_tokens=10,
                                           task=card_ai.TASK_CATEGORIZE)
    assert sent["model"] == model == "claude-haiku-5-5"
    assert "fallbacks" not in sent and "betas" not in sent

    sent.clear()
    card_ai._call(client, "system", {}, object, effort="low", max_tokens=10)
    assert sent["model"] == "claude-sonnet-5-5" and sent["fallbacks"] == "default"


def test_model_not_found_names_the_tasks_model(monkeypatch):
    import anthropic
    import httpx2 as httpx
    monkeypatch.setenv("FLOW_AI_MODEL_ASK", "claude-nope")
    req = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
    exc = anthropic.NotFoundError("not found", response=httpx.Response(404, request=req), body=None)
    message, fatal, kind = card_ai._describe_error(exc, card_ai.TASK_ASK)
    assert "claude-nope" in message and fatal and kind == "model"


def test_models_in_use_say_where_each_model_comes_from(monkeypatch):
    by = lambda: {m["feature"]: (m["model"], m["env_var"], m["source"]) for m in card_ai.models_in_use()}  # noqa: E731
    assert set(by().values()) == {(card_ai.DEFAULT_MODEL, None, None)}                # nothing set: Flow's default
    monkeypatch.setenv("FLOW_AI_MODEL", "claude-opus-5-5")
    monkeypatch.setenv("FLOW_AI_MODEL_CATEGORIZE", "claude-haiku-5-5")
    m = by()
    assert m["Card categorization"] == ("claude-haiku-5-5", "FLOW_AI_MODEL_CATEGORIZE", "env")
    assert m["Assistant"] == ("claude-opus-5-5", "FLOW_AI_MODEL", "env")
    assert m["Spending insights and reading pasted balances"] == ("claude-opus-5-5", "FLOW_AI_MODEL", "env")
    # Each row's model is the one the feature really uses.
    for feature, task in card_ai.FEATURES:
        assert m[feature][0] == card_ai.model_name(task)


def test_models_from_the_ai_dialog_are_used_when_env_is_unset(monkeypatch):
    import settings_store as st
    st.testing_state({st.AI_MODEL: "claude-opus-5-5", st.AI_MODEL_ASK: "claude-haiku-5-5"})
    try:
        assert card_ai.model_name(card_ai.TASK_ASK) == "claude-haiku-5-5"
        assert card_ai.model_name(card_ai.TASK_CATEGORIZE) == "claude-opus-5-5"
        rows = {m["feature"]: m for m in card_ai.models_in_use()}
        assert rows["Assistant"]["source"] == "flow" and rows["Assistant"]["setting"] == st.AI_MODEL_ASK
        monkeypatch.setenv("FLOW_AI_MODEL_ASK", "claude-sonnet-5-5")              # .env wins while set
        assert card_ai.model_name(card_ai.TASK_ASK) == "claude-sonnet-5-5"
    finally:
        st.testing_state()
