"""
settings_store: the password hash, the encrypted API key, and where each value comes
from (.env while set there, else the store). Pure: no database.
"""

from __future__ import annotations

import os

os.environ.setdefault("DATABASE_URL", "postgresql+psycopg2://test@localhost/test")
os.environ.setdefault("SECRET_KEY", "test-" * 8)

import pytest  # noqa: E402

import settings_store as st  # noqa: E402


@pytest.fixture(autouse=True)
def clean(monkeypatch):
    for var in list(st.SETTING_ENV.values()) + list(st.SECRET_ENV.values()):
        monkeypatch.delenv(var, raising=False)
    st.testing_state()
    yield
    st.testing_state()


def test_password_hash_round_trip_and_shape():
    stored = st.hash_password("correct horse")
    assert stored.startswith("scrypt$16384$8$1$") and "correct horse" not in stored
    assert st.check_password("correct horse", stored)
    assert not st.check_password("correct hors", stored)
    assert not st.check_password("correct horse", "")
    assert not st.check_password("correct horse", "md5$x")
    assert not st.check_password("correct horse", "scrypt$16384$8$1$not-base64")
    assert st.hash_password("x") != st.hash_password("x")          # a fresh salt each time


def test_api_key_encrypts_under_secret_key_and_not_another():
    stored = st.encrypt("sk-ant-example-key", "k" * 40)
    assert stored.startswith("v1$") and "sk-ant" not in stored
    assert st.decrypt(stored, "k" * 40) == "sk-ant-example-key"
    assert st.decrypt(stored, "j" * 40) is None                     # rotated SECRET_KEY: unreadable, not wrong
    assert st.decrypt("v1$garbage", "k" * 40) is None
    assert st.decrypt("v2$a$b", "k" * 40) is None


def test_env_wins_while_set_then_the_store():
    st.testing_state({st.AI_MODEL: "claude-haiku-5-5", st.AI_REDACT: "Smith, Acme"},
                     {st.API_KEY: "sk-ant-stored", st.PASSWORD_HASH: st.hash_password("pw")})
    assert st.setting(st.AI_MODEL) == "claude-haiku-5-5" and st.setting_source(st.AI_MODEL) == "flow"
    assert st.setting(st.AI_MODEL_ASK) == "" and st.setting_source(st.AI_MODEL_ASK) is None
    assert st.redact_words() == ["Smith", "Acme"]
    assert st.api_key() == "sk-ant-stored" and st.api_key_source() == "flow"
    assert st.password_source() == "flow" and st.check_password("pw", st.password_hash())
    os.environ["FLOW_AI_MODEL"] = " claude-opus-5-5 "
    os.environ["ANTHROPIC_API_KEY"] = "sk-ant-env"
    os.environ["FLOW_PASSWORD"] = "other"
    assert st.setting(st.AI_MODEL) == "claude-opus-5-5" and st.setting_source(st.AI_MODEL) == "env"
    assert st.api_key() == "sk-ant-env" and st.api_key_source() == "env"
    assert st.password_source() == "env"


def test_ai_enabled_flag_reads_off_words(monkeypatch):
    assert st.ai_enabled_flag()
    for word in ("0", "false", "No", "OFF"):
        st.testing_state({st.AI_ENABLED: word})
        assert not st.ai_enabled_flag()
    st.testing_state({st.AI_ENABLED: "1"})
    assert st.ai_enabled_flag()
    monkeypatch.setenv("FLOW_AI_ENABLED", "off")
    assert not st.ai_enabled_flag()


def test_ai_area_flags_default_on_and_read_off_words(monkeypatch):
    assert all(st.ai_area_flag(a) for a in st.AI_AREAS)
    st.testing_state({st.AI_CASH: "false"})
    assert st.ai_area_flag("cards") and not st.ai_area_flag("cash") and st.ai_area_flag("balances")
    monkeypatch.setenv("FLOW_AI_BALANCES", "0")
    assert not st.ai_area_flag("balances") and st.setting_source(st.AI_BALANCES) == "env"
    assert set(st.AI_AREAS.values()) <= set(st.SETTING_ENV)          # stored, exported and env-lockable like the rest


class FakeDB:
    """Enough of a Session for save_setting / save_secret / load: rows by (model, key)."""

    def __init__(self):
        self.rows = {}

    def get(self, model, key):
        return self.rows.get((model.__name__, key))

    def add(self, obj):
        self.rows[(type(obj).__name__, obj.key)] = obj

    def delete(self, obj):
        self.rows.pop((type(obj).__name__, obj.key), None)

    def commit(self):
        pass

    def query(self, model):
        db = self

        class Q:
            def filter(self, *_):
                return self

            def all(self):
                return [r for (name, _), r in db.rows.items() if name == model.__name__]
        return Q()


def test_save_load_and_import_from_env():
    from models import AppSecret, AppSetting
    db = FakeDB()
    st.save_setting(db, st.AI_MODEL_CATEGORIZE, "claude-haiku-5-5")
    st.save_secret(db, st.API_KEY, "sk-ant-saved")
    st.set_password(db, "first")
    assert db.rows[("AppSetting", st.AI_MODEL_CATEGORIZE)].value == "claude-haiku-5-5"
    assert "sk-ant" not in db.rows[("AppSecret", st.API_KEY)].value              # encrypted at rest
    assert db.rows[("AppSecret", st.PASSWORD_HASH)].value.startswith("scrypt$")  # hashed, not encrypted
    st.testing_state()
    st.load(db)
    assert st.setting(st.AI_MODEL_CATEGORIZE) == "claude-haiku-5-5" and st.api_key() == "sk-ant-saved"
    assert st.check_password("first", st.password_hash()) and not st.api_key_unreadable()
    # .env values are copied in at startup, so removing them from .env later changes nothing.
    os.environ["FLOW_AI_MODEL"] = "claude-opus-5-5"
    os.environ["ANTHROPIC_API_KEY"] = "sk-ant-from-env"
    os.environ["FLOW_PASSWORD"] = "second"
    assert sorted(st.import_from_env(db)) == ["ANTHROPIC_API_KEY", "FLOW_AI_MODEL", "FLOW_PASSWORD"]
    assert st.import_from_env(db) == []                                           # already copied: nothing to do
    for var in ("FLOW_AI_MODEL", "ANTHROPIC_API_KEY", "FLOW_PASSWORD"):
        del os.environ[var]
    assert st.setting(st.AI_MODEL) == "claude-opus-5-5" and st.api_key() == "sk-ant-from-env"
    assert st.check_password("second", st.password_hash())
    # Removing values
    st.save_secret(db, st.API_KEY, "")
    st.save_setting(db, st.AI_MODEL, "")
    assert st.api_key() == "" and st.api_key_source() is None and st.setting(st.AI_MODEL) == ""
    assert ("AppSecret", st.API_KEY) not in db.rows and ("AppSetting", st.AI_MODEL) not in db.rows
    with pytest.raises(KeyError):
        st.save_setting(db, "alert_pct", "5")                                      # only Flow's own keys
    assert AppSetting and AppSecret


def test_a_key_saved_under_another_secret_key_is_reported_unreadable(monkeypatch):
    from models import AppSecret
    db = FakeDB()
    db.add(AppSecret(key=st.API_KEY, value=st.encrypt("sk-ant-old", "other-secret-key-" * 3)))
    st.load(db)
    assert st.api_key() == "" and st.api_key_unreadable()
    os.environ["ANTHROPIC_API_KEY"] = "sk-ant-env"
    assert not st.api_key_unreadable()                                            # .env supplies one
    del os.environ["ANTHROPIC_API_KEY"]
    st.save_secret(db, st.API_KEY, "sk-ant-new")                                  # re-entered: readable again
    assert st.api_key() == "sk-ant-new" and not st.api_key_unreadable()
