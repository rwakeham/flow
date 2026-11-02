"""
Flow's own settings and secrets, kept in the database rather than in .env.

Two kinds of value:

* **Settings** (``flow_settings`` rows; they travel in backups): the AI models, whether AI
  calls are on, the extra words stripped from merchant names (changed in the AI dialog), and
  whether the daily update check runs (Settings → Updates).
* **Secrets** (``flow_secrets`` rows; never in a backup): the dashboard password as a
  scrypt hash (nothing ever needs it back, only to check it) and the Anthropic API key,
  encrypted with AES-GCM under a key derived from SECRET_KEY. SECRET_KEY stays in .env
  because it is the root: a database dump holds the ciphertext and nothing to open it with.
  A SECRET_KEY that changed since the key was saved can't decrypt it (the cipher's tag
  fails); ``api_key_unreadable()`` reports that and the AI dialog asks for the key again.

Why move them out of the environment: an environment variable is printed by
``docker inspect`` and ``docker compose config`` to anyone who can reach Docker, and is
visible to every process in the container; a hash and a ciphertext are not worth reading.

**.env still wins while a variable is set there.** ``setup.sh`` writes FLOW_PASSWORD and
ANTHROPIC_API_KEY for the first start, and setting FLOW_PASSWORD is how a forgotten
password is reset. At every startup (``import_from_env``) a variable that is set is copied
into the store, so removing it from .env afterwards changes nothing; while it is set the
AI dialog and the password change say so and refuse to change the value underneath it.

One uvicorn worker, so the in-memory copy (loaded at startup, written through on every
change) is the only one.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import os
import secrets
import time

from sqlalchemy.orm import Session

# ── Keys ─────────────────────────────────────────────────────────────────────

AI_MODEL = "ai_model"
AI_MODEL_CATEGORIZE = "ai_model_categorize"
AI_MODEL_ASK = "ai_model_ask"
AI_ENABLED = "ai_enabled"
AI_REDACT = "ai_redact"
UPDATE_CHECK = "update_check"
BACKUP_DAILY = "backup_daily"        # the daily backup (backup_service.loop): on unless switched off
BACKUP_KEEP = "backup_keep"          # automatic backups kept (newest N; 0 keeps all)
BACKUP_HOUR = "backup_hour"          # UTC hour the daily backup is taken at or after
SETTING_ENV = {
    AI_MODEL: "FLOW_AI_MODEL",
    AI_MODEL_CATEGORIZE: "FLOW_AI_MODEL_CATEGORIZE",
    AI_MODEL_ASK: "FLOW_AI_MODEL_ASK",
    AI_ENABLED: "FLOW_AI_ENABLED",
    AI_REDACT: "FLOW_AI_REDACT",
    UPDATE_CHECK: "FLOW_UPDATE_CHECK",
    BACKUP_DAILY: "FLOW_BACKUP_DAILY",
    BACKUP_KEEP: "FLOW_BACKUP_KEEP",
    BACKUP_HOUR: "FLOW_BACKUP_HOUR",
}
BACKUP_KEEP_DEFAULT, BACKUP_KEEP_MAX = 30, 3650
BACKUP_HOUR_DEFAULT = 3
MODEL_SETTINGS = (AI_MODEL, AI_MODEL_CATEGORIZE, AI_MODEL_ASK)

PASSWORD_HASH = "password_hash"
API_KEY = "anthropic_api_key"
SECRET_ENV = {PASSWORD_HASH: "FLOW_PASSWORD", API_KEY: "ANTHROPIC_API_KEY"}

OFF_WORDS = ("0", "false", "no", "off")

# ── In-memory copy ───────────────────────────────────────────────────────────

_settings: dict[str, str] = {}
_secrets: dict[str, str] = {}      # decrypted
_unreadable: set[str] = set()      # secrets saved under another SECRET_KEY


def env_value(var: str) -> str:
    return os.environ.get(var, "").strip()


def setting(key: str) -> str:
    """The setting's value: .env while set there, else the store, else ""."""
    return env_value(SETTING_ENV[key]) or _settings.get(key, "")


def setting_source(key: str) -> str | None:
    """Where the value comes from: "env", "flow", or None when nothing sets it."""
    if env_value(SETTING_ENV[key]):
        return "env"
    return "flow" if _settings.get(key, "") else None


def ai_enabled_flag() -> bool:
    v = setting(AI_ENABLED).lower()
    return v not in OFF_WORDS


def update_check_flag() -> bool:
    """Whether the daily release check runs: on unless switched off (update_check.py)."""
    return setting(UPDATE_CHECK).lower() not in OFF_WORDS


def backup_daily_flag() -> bool:
    """Whether the daily backup runs (backup_service.loop): on unless switched off."""
    return setting(BACKUP_DAILY).lower() not in OFF_WORDS


def _int_setting(key: str, default: int, lo: int, hi: int) -> int:
    try:
        value = int(setting(key))
    except ValueError:
        return default
    return min(max(value, lo), hi)


def backup_keep() -> int:
    """How many automatic backups are kept (the newest); 0 keeps every one."""
    return _int_setting(BACKUP_KEEP, BACKUP_KEEP_DEFAULT, 0, BACKUP_KEEP_MAX)


def backup_hour() -> int:
    """The UTC hour at or after which the day's backup is taken."""
    return _int_setting(BACKUP_HOUR, BACKUP_HOUR_DEFAULT, 0, 23)


def env_locked_message(var: str) -> str:
    """Why a setting can't be changed in the app: its .env variable wins while it is set."""
    return (f"{var} is set in .env and wins while it is there. Remove it from .env (./setup.sh offers to) "
            "and run ./deploy.sh, then change it here.")


def redact_words() -> list[str]:
    return [w.strip() for w in setting(AI_REDACT).split(",") if w.strip()]


def api_key() -> str:
    return env_value("ANTHROPIC_API_KEY") or _secrets.get(API_KEY, "")


def api_key_source() -> str | None:
    if env_value("ANTHROPIC_API_KEY"):
        return "env"
    return "flow" if _secrets.get(API_KEY) else None


def api_key_unreadable() -> bool:
    """A key is saved but was encrypted under another SECRET_KEY (and .env doesn't supply one)."""
    return API_KEY in _unreadable and not env_value("ANTHROPIC_API_KEY")


def password_hash() -> str:
    return _secrets.get(PASSWORD_HASH, "")


def password_source() -> str | None:
    if env_value("FLOW_PASSWORD"):
        return "env"
    return "flow" if _secrets.get(PASSWORD_HASH) else None


# ── Password hashing (scrypt, standard library) ──────────────────────────────

SCRYPT_N, SCRYPT_R, SCRYPT_P = 2 ** 14, 8, 1


def hash_password(password: str, salt: bytes | None = None) -> str:
    salt = salt or secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=SCRYPT_N, r=SCRYPT_R, p=SCRYPT_P, dklen=32)
    return "$".join(["scrypt", str(SCRYPT_N), str(SCRYPT_R), str(SCRYPT_P),
                     base64.b64encode(salt).decode(), base64.b64encode(digest).decode()])


def check_password(password: str, stored: str) -> bool:
    """Constant-time check of ``password`` against a ``hash_password`` string; False for anything else."""
    try:
        algo, n, r, p, salt, digest = stored.split("$")
        if algo != "scrypt":
            return False
        want = base64.b64decode(digest)
        got = hashlib.scrypt(password.encode(), salt=base64.b64decode(salt), n=int(n), r=int(r), p=int(p),
                             dklen=len(want))
    except (ValueError, TypeError, AttributeError):
        return False
    return hmac.compare_digest(got, want)


# ── Secret encryption (AES-GCM under a key derived from SECRET_KEY) ──────────

def _cipher_key(secret_key: str) -> bytes:
    return hashlib.sha256(b"flow-secrets:" + secret_key.encode()).digest()


def encrypt(plaintext: str, secret_key: str) -> str:
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    nonce = secrets.token_bytes(12)
    ct = AESGCM(_cipher_key(secret_key)).encrypt(nonce, plaintext.encode(), b"flow-secret-v1")
    return "v1$" + base64.b64encode(nonce).decode() + "$" + base64.b64encode(ct).decode()


def decrypt(stored: str, secret_key: str) -> str | None:
    """The plaintext, or None when the value is damaged or was encrypted under another SECRET_KEY."""
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    from cryptography.exceptions import InvalidTag
    try:
        version, nonce, ct = stored.split("$")
        if version != "v1":
            return None
        return AESGCM(_cipher_key(secret_key)).decrypt(base64.b64decode(nonce), base64.b64decode(ct),
                                                       b"flow-secret-v1").decode()
    except (ValueError, TypeError, InvalidTag):
        return None


# ── Persistence ──────────────────────────────────────────────────────────────

def _secret_key() -> str:
    import auth
    return auth.SECRET_KEY


def load(db: Session) -> None:
    """Read every setting and secret into memory (startup, and after a backup restore)."""
    from models import AppSecret, AppSetting
    global _settings, _secrets, _unreadable
    _settings = {row.key: row.value for row in db.query(AppSetting).filter(AppSetting.key.in_(SETTING_ENV)).all()
                 if row.value}
    _secrets, _unreadable = {}, set()
    for row in db.query(AppSecret).all():
        if row.key == PASSWORD_HASH:
            _secrets[row.key] = row.value or ""
            continue
        value = decrypt(row.value or "", _secret_key())
        if value is None:
            _unreadable.add(row.key)
        elif value:
            _secrets[row.key] = value
    _secrets = {k: v for k, v in _secrets.items() if v}


def save_setting(db: Session, key: str, value: str) -> None:
    from models import AppSetting
    if key not in SETTING_ENV:
        raise KeyError(key)
    value = (value or "").strip()
    row = db.get(AppSetting, key)
    if value:
        row = row or AppSetting(key=key)
        row.value = value
        db.add(row)
        _settings[key] = value
    elif row is not None:
        db.delete(row)
        _settings.pop(key, None)
    db.commit()


def save_secret(db: Session, key: str, plaintext: str | None, *, hashed: bool = False) -> None:
    """Store a secret (encrypted; ``hashed`` values such as the password hash are stored as they are),
    or remove it when ``plaintext`` is empty."""
    from models import AppSecret
    if key not in SECRET_ENV:
        raise KeyError(key)
    row = db.get(AppSecret, key)
    if plaintext:
        row = row or AppSecret(key=key)
        row.value = plaintext if hashed else encrypt(plaintext, _secret_key())
        row.updated_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        db.add(row)
        _secrets[key] = plaintext
    elif row is not None:
        db.delete(row)
        _secrets.pop(key, None)
    _unreadable.discard(key)
    db.commit()


def set_password(db: Session, password: str) -> str:
    """Hash and store a new dashboard password; returns the hash."""
    stored = hash_password(password)
    save_secret(db, PASSWORD_HASH, stored, hashed=True)
    return stored


def import_from_env(db: Session) -> list[str]:
    """
    Copy every value set in .env into the store (startup). Returns the variables that were
    copied. The password is re-hashed only when the stored hash doesn't match it.
    """
    copied: list[str] = []
    for key, var in SETTING_ENV.items():
        value = env_value(var)
        if value and _settings.get(key) != value:
            save_setting(db, key, value)
            copied.append(var)
    env_key = env_value("ANTHROPIC_API_KEY")
    if env_key and _secrets.get(API_KEY) != env_key:
        save_secret(db, API_KEY, env_key)
        copied.append("ANTHROPIC_API_KEY")
    env_password = env_value("FLOW_PASSWORD")
    if env_password and not check_password(env_password, password_hash()):
        set_password(db, env_password)
        copied.append("FLOW_PASSWORD")
    return copied


def testing_state(settings: dict | None = None, secrets_: dict | None = None) -> None:
    """Tests: set the in-memory copy without a database."""
    global _settings, _secrets, _unreadable
    _settings, _secrets, _unreadable = dict(settings or {}), dict(secrets_ or {}), set()
