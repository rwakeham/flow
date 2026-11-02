"""
Anthropic API integration for card analytics: merchant categorization and
aggregate-only spending insights.

Privacy contract (see card_privacy.py):
  * categorization sends only distinct sanitized merchant labels, the issuer's
    generic category label, and the payment-processor name, keyed by a
    throwaway integer id. No amounts, dates, counts, card or account details,
    names, or raw descriptions.
  * insights send only aggregate tables (card_analytics.insights_payload):
    totals, rollups, monthly figures and canonical merchant names, with cards
    aliased "Card A", "Card B".
  * every call is logged in ai_request_log, success or not: the data sent, the
    whole request body (system prompt, tools, messages, settings) and the whole
    response as received.

Each merchant is classified once: answers are cached in merchant_categories and
reused by every later import. Nothing is sent without an API key, or while AI
calls are switched off. The key, the models, the on/off switch and the extra
redact words come from settings_store (the AI dialog; .env while a variable is
set there).
"""

from __future__ import annotations

import hashlib
import json
import logging
import threading
from collections import Counter
from datetime import datetime, timezone
from typing import Callable

from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.orm import Session

import card_amount_rules
import card_classifier
import card_import
import card_privacy
import card_taxonomy
import settings_store
from models import AiRequestLog, CardAccount, CardTransaction, MerchantCategory

log = logging.getLogger("flow.card_ai")

CATEGORIZE_PROMPT_VERSION = "cat-v1"
INSIGHTS_PROMPT_VERSION = "ins-v3"
BATCH_SIZE = 60
DEFAULT_MODEL = "claude-sonnet-5-5"
# Models that accept server-side refusal fallback with fallbacks="default".
# Claude Haiku 5.5 has no server-side fallback, so it is deliberately absent.
_FALLBACK_MODELS = {"claude-opus-5-5", "claude-opus-5", "claude-fable-5-1", "claude-fable-5", "claude-sonnet-5-5"}
_FALLBACK_BETA = "server-side-fallback-2026-07-01"

# Tasks whose model can be set on its own; anything else uses the model for every feature.
TASK_CATEGORIZE = "categorize"
TASK_ASK = "ask"
TASKS = (TASK_CATEGORIZE, TASK_ASK)
TASK_SETTING = {TASK_CATEGORIZE: settings_store.AI_MODEL_CATEGORIZE, TASK_ASK: settings_store.AI_MODEL_ASK}


# ── Configuration ────────────────────────────────────────────────────────────

def model_name(task: str | None = None) -> str:
    """The model for ``task``: its own setting, else the model for every feature, else DEFAULT_MODEL."""
    return model_setting(task)[0]


# Every AI feature and the setting that picks its model (what the AI dialog shows).
FEATURES = (
    ("Card categorization", TASK_CATEGORIZE),
    ("Assistant", TASK_ASK),
    ("Spending insights and reading pasted balances", None),
)


def model_setting(task: str | None = None) -> tuple[str, str | None, str | None]:
    """(model, the setting that chose it, where that setting is set: "env" or "flow"); the setting
    is None for Flow's default."""
    for key in ([TASK_SETTING[task]] if task in TASKS else []) + [settings_store.AI_MODEL]:
        value = settings_store.setting(key)
        if value:
            return value, key, settings_store.setting_source(key)
    return DEFAULT_MODEL, None, None


def models_in_use() -> list[dict]:
    """Each feature's model and where it comes from (the AI dialog's table and form)."""
    out = []
    for feature, task in FEATURES:
        model, key, source = model_setting(task)
        out.append({"feature": feature, "task": task, "model": model, "setting": key, "source": source,
                    "env_var": settings_store.SETTING_ENV[key] if key else None,
                    "own_setting": TASK_SETTING.get(task)})
    return out


def enabled() -> bool:
    return settings_store.ai_enabled_flag() and bool(settings_store.api_key())


def disabled_reason() -> str | None:
    if not settings_store.ai_enabled_flag():
        return "AI calls are switched off."
    if settings_store.api_key_unreadable():
        return ("The saved API key can't be read: SECRET_KEY changed since it was saved. "
                "Enter the key again under Settings → AI.")
    if not settings_store.api_key():
        return "No Anthropic API key is set (Settings → AI in the top bar), so nothing is sent to the AI."
    return None


def key_fingerprint() -> str | None:
    """A short one-way hash of the configured key, so a stored failure can tell whether the key changed since."""
    key = settings_store.api_key()
    return hashlib.sha256(key.encode()).hexdigest()[:12] if key else None


def _default_client():
    import anthropic
    return anthropic.Anthropic(api_key=settings_store.api_key() or None, timeout=300.0, max_retries=2)


# Tests replace this with a fake client factory.
client_factory: Callable[[], object] = _default_client


class AiError(Exception):
    """A call failed in a way the user should see (auth, refusal, bad request)."""


# ── Schemas ──────────────────────────────────────────────────────────────────

class MerchantGuess(BaseModel):
    id: int
    canonical_name: str
    merchant_family: str
    cat_l1: str
    cat_l2: str
    cat_l3: str
    confidence: float
    subscription_hint: bool
    note: str


class MerchantBatch(BaseModel):
    merchants: list[MerchantGuess]


class InsightFinding(BaseModel):
    title: str
    detail: str
    numbers: list[str]


class InsightAction(BaseModel):
    action: str
    estimated_annual_value: float | None
    effort: str


class InsightReport(BaseModel):
    headline: str
    findings: list[InsightFinding]
    actions: list[InsightAction]
    review_items: list[str]
    assumptions: list[str]


# ── Prompts ──────────────────────────────────────────────────────────────────

def _taxonomy_json() -> str:
    compact = {
        t["l1"]: {l2["name"]: l2["l3"] for l2 in t["l2"]}
        for t in card_taxonomy.as_list(for_ai=True)
    }
    return json.dumps(compact, indent=1, sort_keys=False)


CATEGORIZE_SYSTEM = """You classify credit-card merchant names into a fixed spending taxonomy for a \
personal finance tool. You receive only sanitized merchant labels (store numbers, locations, names \
and other identifiers have been removed, so labels can be truncated) and, when available, the card \
issuer's own category label and the payment processor. You do not receive amounts, dates or any \
account information, and you must not infer or mention any.

Taxonomy as JSON: {L1: {L2: [known L3 values]}}. Choose cat_l1 and cat_l2 only from it. Choose \
cat_l3 from the listed values, or write a short new L3 when none fits.
<taxonomy>
%s
</taxonomy>

Guidance:
- canonical_name is the brand as a customer would say it ("Southwest Airlines", "Shake Shack"). \
merchant_family groups brands under a parent (Whole Foods -> "Amazon", Hampton Inn -> "Hilton"); \
use the canonical name when there is no parent.
- Payment processors (Square, Toast, Shopify, PayPal, Apple Pay) are not merchants; classify the \
business behind them.
- Big-box retailers (Target, Walmart, Costco) are Shopping; do not guess what was bought.
- A flight or hotel bought through an online travel agency is "Booking agency / OTA".
- Winery shipments through Commerce7, WineDirect or Vinoshipper are "Direct winery / wine club".
- The issuer category is a strong hint about the merchant type but can be generic or wrong.
- subscription_hint is true for merchants that normally bill on a fixed recurring schedule \
(streaming, software, memberships, gyms, utilities, insurance, phone), false for everyday merchants.
- confidence is how sure you are of cat_l1 and cat_l2, from 0 to 1. When the label is not \
recognisable, set confidence below 0.5 and use "Uncategorized", "Needs review", "Needs review".
- note is one short phrase on what the merchant is, or an empty string.
- Return exactly one object per input merchant, with its id unchanged."""


INSIGHTS_SYSTEM = """You are a careful personal-finance analyst writing a short report for one \
household from aggregate credit-card spending tables. You receive only totals, category and \
merchant rollups, monthly figures and a recurring-charge list; cards are labelled "Card A", \
"Card B" and so on. You do not receive transactions, dates finer than a month, or any personal or \
account details, and you must not invent any.

Write for the person whose money this is: specific numbers, plain language, no filler. Name \
cards exactly as given and one at a time ("Card C and Card D", not "Cards C and D"); the app \
shows the real card names. Keep facts \
and inference visibly separate; say "likely" or "consistent with" when inferring. Never call \
anything fraud, an error or a duplicate charge; a repeated same-dollar charge is "worth a look". \
Monthly equivalents and annualized figures are extrapolations, so say so when you use them. \
Months flagged partial are incomplete; do not interpret their dips. Do not give investment, tax \
or legal advice and do not recommend specific financial products.

Fill the response schema:
- headline: one sentence with net spend and the period.
- findings: 5 to 8 items ordered by how much money is involved. Cover the category mix, merchant \
concentration, recurring charges (price changes, possible overlaps across cards, overdue or ended), \
high-frequency small merchants, one-off-driven months, and travel or dining when material. Each \
item has a short title, a two or three sentence detail, and the key numbers as short strings.
- actions: 2 to 5 concrete actions. estimated_annual_value is a modeled dollar figure or null; \
effort is "low", "medium" or "high".
- review_items: short list of specific things worth checking, from the review section.
- assumptions: what was extrapolated or inferred, and which inputs would sharpen the analysis."""


# ── Low-level call ───────────────────────────────────────────────────────────

def _jsonable(obj):
    """A JSON-ready copy of a request or response: SDK models dumped, schema classes as their schema."""
    if isinstance(obj, type) and issubclass(obj, BaseModel):
        return {"output_schema": obj.__name__, "json_schema": obj.model_json_schema()}
    if hasattr(obj, "model_dump"):
        return _jsonable(obj.model_dump(mode="json", exclude_none=True))
    if isinstance(obj, dict):
        return {str(k): _jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_jsonable(v) for v in obj]
    if obj is None or isinstance(obj, (str, int, float, bool)):
        return obj
    if hasattr(obj, "__dict__"):                       # test fakes (SimpleNamespace)
        return {k: _jsonable(v) for k, v in vars(obj).items() if not k.startswith("_")}
    return str(obj)


def record_request(row: AiRequestLog | None, kwargs: dict) -> None:
    """Store the whole request body on its log row, before it is sent."""
    if row is not None:
        row.request_json = json.dumps(_jsonable(kwargs), sort_keys=True)


def record_response(row: AiRequestLog | None, resp) -> None:
    """Store the whole response on its log row, as received."""
    if row is not None and resp is not None:
        row.response_json = json.dumps(_jsonable(resp), sort_keys=True)


def fallback_kwargs(model: str) -> dict:
    return {"betas": [_FALLBACK_BETA], "fallbacks": "default"} if model in _FALLBACK_MODELS else {}


def _call(client, system: str, payload: dict, output_format, effort: str, max_tokens: int,
          log_row: AiRequestLog | None = None, task: str | None = None):
    """One structured-output request, recorded on ``log_row``. Returns (parsed, usage, model_used)."""
    model = model_name(task)
    kwargs = dict(
        model=model,
        max_tokens=max_tokens,
        system=[{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
        messages=[{"role": "user", "content": json.dumps(payload, sort_keys=True)}],
        output_format=output_format,
        output_config={"effort": effort},
        **fallback_kwargs(model),
    )
    record_request(log_row, kwargs)
    resp = client.beta.messages.parse(**kwargs)
    record_response(log_row, resp)
    if resp.stop_reason == "refusal":
        detail = getattr(getattr(resp, "stop_details", None), "explanation", None) or "request declined"
        raise AiError(f"The model declined the request: {detail}")
    if resp.stop_reason == "max_tokens" or resp.parsed_output is None:
        raise AiError("The response was cut off or did not match the schema.")
    return resp.parsed_output, resp.usage, getattr(resp, "model", model)


def _log(db: Session, purpose: str, version: str, payload: dict, count: int,
         task: str | None = None) -> AiRequestLog:
    row = AiRequestLog(
        purpose=purpose, model=model_name(task), prompt_version=version,
        payload_json=json.dumps(payload, sort_keys=True), item_count=count, status="error",
        key_fingerprint=key_fingerprint(),
    )
    db.add(row)
    db.flush()
    return row


def _finish_log(row: AiRequestLog, usage=None, status: str = "ok", error: str | None = None,
                kind: str | None = None) -> None:
    row.status = status
    row.error = error
    row.error_kind = kind if status != "ok" else None
    if usage is not None:
        row.input_tokens = getattr(usage, "input_tokens", None)
        row.output_tokens = getattr(usage, "output_tokens", None)
        row.cache_read_tokens = getattr(usage, "cache_read_input_tokens", None)


KEY_TEST_PROMPT_VERSION = "key-v1"


def test_key(db: Session, client=None) -> tuple[bool, str]:
    """
    "Test the key" in the AI dialog: the smallest possible request (one token back) with the
    configured key and the model for every feature, logged like every other call (purpose
    ``key_test``; it carries no data of yours). Returns (ok, message). A success also clears a
    stored key problem, since key_problem reads the newest log row.
    """
    row = _log(db, "key_test", KEY_TEST_PROMPT_VERSION, {"check": "key"}, 0)
    model = model_name()
    kwargs = dict(model=model, max_tokens=1, messages=[{"role": "user", "content": "Reply with OK."}])
    record_request(row, kwargs)
    try:
        resp = (client or client_factory()).messages.create(**kwargs)
    except Exception as exc:   # noqa: BLE001 - every failure is reported to the user, by kind
        message, _fatal, kind = _describe_error(exc)
        _finish_log(row, status="error", error=message, kind=kind)
        db.commit()
        return False, message
    record_response(row, resp)
    _finish_log(row, usage=getattr(resp, "usage", None))
    db.commit()
    return True, f"The key works: {getattr(resp, 'model', model)} answered."


# Failures that will repeat until the user changes the key or the account: Flow
# stops calling automatically while the same key is configured.
KEY_PROBLEM_KINDS = {"auth", "billing"}
KEY_PROBLEM_MESSAGES = {
    "auth": "The Anthropic API key was rejected (expired, revoked or mistyped).",
    "billing": "The Anthropic account cannot be billed (credit balance too low or billing not set up).",
}
_BILLING_HINTS = ("credit balance", "billing", "insufficient", "payment")


def _describe_error(exc: Exception, task: str | None = None) -> tuple[str, bool, str]:
    """(message, fatal, kind). Fatal errors stop the run; the rest may succeed later."""
    import anthropic
    if isinstance(exc, AiError):
        return str(exc), False, "other"
    if isinstance(exc, (anthropic.AuthenticationError, anthropic.PermissionDeniedError)):
        return KEY_PROBLEM_MESSAGES["auth"], True, "auth"
    if isinstance(exc, anthropic.NotFoundError):
        return f"Model {model_name(task)!r} was not found.", True, "model"
    if isinstance(exc, anthropic.BadRequestError):
        message = str(getattr(exc, "message", exc))
        if any(h in message.lower() for h in _BILLING_HINTS):
            return f"{KEY_PROBLEM_MESSAGES['billing']} ({message})", True, "billing"
        if any(h in message.lower() for h in ("grammar", "too complex", "strict tools")):
            return ("The API rejected Flow's tool definitions as too complex. This is a Flow bug, not your "
                    f"key or account: please report it. ({message})"), True, "other"
        return f"The request was rejected: {message}", True, "other"
    if isinstance(exc, anthropic.RateLimitError):
        return "Rate limited by the Anthropic API; try again shortly.", True, "rate_limit"
    if isinstance(exc, anthropic.APIStatusError):
        return f"Anthropic API error {exc.status_code}; try again later.", True, "other"
    if isinstance(exc, anthropic.APIConnectionError):
        return "Could not reach the Anthropic API.", True, "network"
    return f"Unexpected error: {exc}", True, "other"


def key_problem(db: Session) -> dict | None:
    """
    A failure that will repeat with the configured key: the most recent AI request
    failed for auth or billing reasons, and the key has not changed since.
    Persistent (read from ai_request_log), so it survives restarts. Any later
    successful request, or a new key, clears it.
    """
    fp = key_fingerprint()
    if fp is None:
        return None
    return problem_from_log(db.query(AiRequestLog).order_by(AiRequestLog.id.desc()).first(), fp)


def problem_from_log(last, fingerprint: str | None) -> dict | None:
    """The key_problem decision, given the newest log row and the current key's fingerprint."""
    if last is None or fingerprint is None or last.status == "ok" or last.error_kind not in KEY_PROBLEM_KINDS:
        return None
    if last.key_fingerprint != fingerprint:
        return None
    return {"kind": last.error_kind, "message": KEY_PROBLEM_MESSAGES[last.error_kind],
            "detail": last.error, "at": last.created_at.isoformat() if last.created_at else None}


# ── Categorization ───────────────────────────────────────────────────────────

_run_lock = threading.Lock()
_rerun = threading.Event()  # a run was asked for while one was going (see run_categorization)
_status: dict = {"running": False, "last_run": None, "runs": 0}


def status() -> dict:
    return {**_status}


def _cardholder_names(db: Session) -> list[str]:
    names: list[str] = []
    for (raw,) in db.query(CardAccount.cardholder_names).all():
        names.extend(n.strip() for n in (raw or "").splitlines() if n.strip())
    names.extend(
        r[0] for r in db.execute(text(
            "SELECT DISTINCT cardholder FROM card_transactions WHERE cardholder IS NOT NULL AND cardholder <> ''"
        )).fetchall()
    )
    names.extend(settings_store.redact_words())
    return names


def split_pending(rows, index: card_classifier.MerchantIndex, stale_keys: set[str]) -> tuple[list[dict], dict[str, str]]:
    """
    Decide, per merchant key, whether the AI must be asked or the cache can answer.

    ``rows`` are (merchant_key, label, issuer_category, processor, category_source)
    for transactions that only have a weak local guess or an AI answer.
    Returns (to_send, reusable):
      to_send   [{merchant_key, label, issuer_category, processor}], sorted by key:
                keys no cache entry serves, plus keys whose own AI entry is from an
                older prompt (``stale_keys``);
      reusable  {merchant_key: cache key}: weak rows a cache entry already serves,
                exactly or as a truncated / extended spelling, so no call is needed.
    """
    by_key: dict[str, dict] = {}
    cats: dict[str, Counter] = {}
    weak: set[str] = set()
    for key, label, issuer_cat, processor, source in rows:
        entry = by_key.setdefault(key, {"merchant_key": key, "label": label, "processor": processor or ""})
        if issuer_cat:
            cats.setdefault(key, Counter())[issuer_cat] += 1
        if processor and not entry["processor"]:
            entry["processor"] = processor
        if source in card_classifier.WEAK_SOURCES:
            weak.add(key)

    to_send, reusable = [], {}
    for key in sorted(by_key):
        served_by = index.match(key)
        if served_by is None or (served_by == key and key in stale_keys):
            entry = by_key[key]
            entry["issuer_category"] = cats[key].most_common(1)[0][0] if key in cats else ""
            to_send.append(entry)
        elif key in weak:
            reusable[key] = served_by
    return to_send, reusable


def _pending_rows(db: Session):
    return db.execute(text("""
        SELECT merchant_key, merchant_label, issuer_category, processor, category_source
          FROM card_transactions
         WHERE merchant_key <> ''
           AND category_source IN ('issuer', 'keyword', 'unclassified', 'ai')
           AND txn_type IN ('purchase', 'refund')
    """)).fetchall()


def _stale_keys(db: Session) -> set[str]:
    return {k for (k,) in db.execute(text(
        "SELECT merchant_key FROM merchant_categories WHERE source = 'ai' AND prompt_version IS DISTINCT FROM :ver"
    ), {"ver": CATEGORIZE_PROMPT_VERSION}).fetchall()}


def pending_merchants(db: Session, include_stale: bool = True) -> list[dict]:
    """Distinct merchants the AI should classify (see split_pending). Labels that may
    never be sent (is_safe_label) are left out: they wait for the user, not the AI."""
    to_send, _ = split_pending(_pending_rows(db), card_import.load_cache(db),
                               _stale_keys(db) if include_stale else set())
    names = _cardholder_names(db)
    return [it for it in to_send if card_privacy.is_safe_label(it["label"], names)]


def resolve_from_cache(db: Session) -> int:
    """
    Give weak rows the category of the cached merchant that already serves them
    (a different spelling or truncation of a merchant the AI or the user has
    categorized). No API call. Returns the number of transactions updated.
    """
    index = card_import.load_cache(db)
    _, reusable = split_pending(_pending_rows(db), index, set())
    updated = 0
    for key, served_by in reusable.items():
        e = index.get(served_by)
        updated += db.query(CardTransaction).filter(
            CardTransaction.merchant_key == key,
            CardTransaction.category_source.in_(card_classifier.WEAK_SOURCES),
            CardTransaction.txn_type.in_(["purchase", "refund"]),
        ).update({
            "canonical_merchant": e.canonical_merchant, "merchant_family": e.merchant_family,
            "cat_l1": e.cat_l1, "cat_l2": e.cat_l2, "cat_l3": e.cat_l3,
            "category_source": "merchant" if e.source == "user" else "ai",
        }, synchronize_session=False)
    if updated:
        db.commit()
    return updated


def build_categorize_payload(items: list[dict]) -> dict:
    """The entire user message for one batch. Ids are positions in this batch only."""
    merchants = []
    for i, it in enumerate(items):
        m = {"id": i, "label": it["label"]}
        if it.get("issuer_category"):
            m["issuer_category"] = it["issuer_category"]
        if it.get("processor"):
            m["processor"] = it["processor"]
        merchants.append(m)
    return {"merchants": merchants}


def _apply_guess(db: Session, item: dict, g: MerchantGuess, model: str) -> int:
    key = item["merchant_key"]
    existing = db.get(MerchantCategory, key)
    if existing is not None and existing.source == "user":
        return 0
    l1, l2, l3 = card_classifier.coerce_category(g.cat_l1, g.cat_l2, g.cat_l3)
    confidence = max(0.0, min(1.0, float(g.confidence)))
    if (l1, l2, l3) == card_taxonomy.UNCATEGORIZED:
        confidence = min(confidence, 0.49)
    canonical = (g.canonical_name or item["label"]).strip()[:120] or item["label"]
    family = (g.merchant_family or canonical).strip()[:120] or canonical
    if existing is None:
        existing = MerchantCategory(merchant_key=key)
        db.add(existing)
    existing.canonical_merchant = canonical
    existing.merchant_family = family
    existing.cat_l1, existing.cat_l2, existing.cat_l3 = l1, l2, l3
    existing.source = "ai"
    existing.subscription_hint = bool(g.subscription_hint)
    existing.confidence = confidence
    existing.model = model
    existing.prompt_version = CATEGORIZE_PROMPT_VERSION
    existing.sanitized_label = item["label"]

    return db.query(CardTransaction).filter(
        CardTransaction.merchant_key == key,
        CardTransaction.category_source.in_(card_classifier.AI_UPDATABLE_SOURCES),
        CardTransaction.txn_type.in_(["purchase", "refund"]),
    ).update({
        "canonical_merchant": canonical, "merchant_family": family,
        "cat_l1": l1, "cat_l2": l2, "cat_l3": l3, "category_source": "ai",
    }, synchronize_session=False)


def categorize_pending(db: Session, client=None, force: bool = False) -> dict:
    """
    Classify every pending merchant, in batches. Commits after each batch so a
    failure part-way keeps the batches that succeeded.

    With a known key problem (see key_problem) nothing is sent unless ``force``:
    automatic runs after imports would only repeat the same failure. The manual
    "Re-run AI" action forces a try, which is how a fixed account is confirmed.
    """
    result = {"sent": 0, "classified": 0, "transactions_updated": 0, "skipped_unsafe": 0,
              "batches": 0, "reused": 0, "unanswered": 0, "error": None}
    # Spellings of merchants already categorized need no call; this is local.
    result["reused"] = resolve_from_cache(db)
    reason = disabled_reason()
    if reason:
        result["error"] = reason
        return result
    problem = None if force else key_problem(db)
    if problem:
        result["error"] = problem["message"]
        result["paused"] = True
        return result

    names = _cardholder_names(db)
    items = []
    for it in pending_merchants(db):
        if card_privacy.is_safe_label(it["label"], names):
            items.append(it)
        else:
            result["skipped_unsafe"] += 1
    if not items:
        return result

    client = client or client_factory()
    for start in range(0, len(items), BATCH_SIZE):
        batch = items[start:start + BATCH_SIZE]
        payload = build_categorize_payload(batch)
        log_row = _log(db, "categorize", CATEGORIZE_PROMPT_VERSION, payload, len(batch), task=TASK_CATEGORIZE)
        db.commit()
        try:
            parsed, usage, model = _call(client, CATEGORIZE_SYSTEM % _taxonomy_json(), payload,
                                         MerchantBatch, effort="low", max_tokens=16000, log_row=log_row,
                                         task=TASK_CATEGORIZE)
        except Exception as exc:  # noqa: BLE001 - every failure is logged and surfaced
            message, fatal, kind = _describe_error(exc, TASK_CATEGORIZE)
            _finish_log(log_row, status="refusal" if "declined" in message else "error", error=message, kind=kind)
            db.commit()
            log.warning("categorization batch failed: %s", message)
            result["error"] = message
            if fatal:
                break
            continue

        result["sent"] += len(batch)
        result["batches"] += 1
        answered = set()
        for g in parsed.merchants:
            if 0 <= g.id < len(batch) and g.id not in answered:
                answered.add(g.id)
                result["transactions_updated"] += _apply_guess(db, batch[g.id], g, model)
                result["classified"] += 1
        # A merchant the reply left out is stored as "needs review", not left pending: otherwise
        # every Run would send it again and nothing would visibly change.
        for i, item in enumerate(batch):
            if i not in answered:
                l1, l2, l3 = card_taxonomy.UNCATEGORIZED
                _apply_guess(db, item, MerchantGuess(id=i, canonical_name=item["label"], merchant_family=item["label"],
                                                     cat_l1=l1, cat_l2=l2, cat_l3=l3, confidence=0.0,
                                                     subscription_hint=False, note="no answer from the AI"), model)
                result["unanswered"] += 1
        _finish_log(log_row, usage=usage)
        db.commit()
    # Answers just cached may also serve other spellings in this run's data.
    result["reused"] += resolve_from_cache(db)
    card_amount_rules.apply(db)          # renamed or newly categorized charges follow amount rules
    db.commit()
    return result


def run_categorization(session_factory, force: bool = False) -> dict | None:
    """
    Background entry point: one run at a time; records the outcome for /ai/status.
    A request that arrives mid-run (the next file of a bulk import) runs once more
    afterwards: the running pass read its merchant list before those rows existed.
    """
    outcome = None
    while True:
        if not _run_lock.acquire(blocking=False):
            _rerun.set()
            # Still held: the holder checks the flag after it releases. Released in
            # between: nobody will, so take the run here.
            if not _run_lock.acquire(blocking=False):
                return outcome
        _rerun.clear()
        _status["running"] = True
        db = session_factory()
        try:
            outcome = categorize_pending(db, force=force)
            if outcome.get("classified"):
                import change_log
                change_log.record(db, "card", "ai_run", f"AI categorized {outcome['classified']} merchants ({outcome.get('transactions_updated', 0)} charges)",
                                  {k: v for k, v in outcome.items() if isinstance(v, int)}, via="schedule")
                db.commit()
        except Exception as exc:  # noqa: BLE001
            db.rollback()
            log.exception("categorization run failed")
            outcome = {"sent": 0, "classified": 0, "transactions_updated": 0, "error": str(exc)}
        finally:
            db.close()
            _status["running"] = False
            _run_lock.release()
        _status["last_run"] = {"at": datetime.now(timezone.utc).isoformat(), **outcome}
        _status["runs"] += 1
        if not _rerun.is_set():
            return outcome
        force = False  # a forced try is the user's; the queued one follows the normal rules


# ── Insights ─────────────────────────────────────────────────────────────────

def generate_insights(db: Session, payload: dict, client=None) -> tuple[InsightReport, AiRequestLog]:
    reason = disabled_reason()
    if reason:
        raise AiError(reason)
    problem = key_problem(db)
    if problem:
        raise AiError(problem["message"] + " Update ANTHROPIC_API_KEY in .env and redeploy.")
    log_row = _log(db, "insights", INSIGHTS_PROMPT_VERSION, payload, 1)
    db.commit()
    client = client or client_factory()
    try:
        report, usage, _model = _call(client, INSIGHTS_SYSTEM, payload, InsightReport,
                                      effort="medium", max_tokens=16000, log_row=log_row)
    except Exception as exc:  # noqa: BLE001
        message, _fatal, kind = _describe_error(exc)
        _finish_log(log_row, status="refusal" if "declined" in message else "error", error=message, kind=kind)
        db.commit()
        raise AiError(message) from exc
    _finish_log(log_row, usage=usage)
    db.commit()
    return report, log_row
