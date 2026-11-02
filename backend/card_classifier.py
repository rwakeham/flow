"""
Transaction typing and category precedence for card transactions.

Pure functions: the caller supplies the merchant cache, so the rules are
testable without a database.

Precedence for a merchant transaction (purchase, refund, benefit credit):

  1. The user's own category for this merchant (cache source 'user')  -> 'merchant'
  2. A confident local rule: named brand, strong keyword, or an issuer
     category that overrides a generic keyword                          -> 'rule'
  3. A cached AI answer for this merchant (cache source 'ai')            -> 'ai'
  4. A weak local guess, queued for the AI: issuer category only
     ('issuer'), weak keyword ('keyword'), or nothing ('unclassified')

Rules beat cached AI answers so improving a rule takes effect on the next
import without clearing the cache. Rule results are not cached: they are
recomputed every time and are visible on the transactions themselves.

Transaction-level sources: a row whose category the user set directly is
'user'; rows updated by a merchant-wide edit are 'merchant'. Neither is ever
overwritten by the AI.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Mapping

import card_rules
import card_taxonomy
from card_privacy import compact_key

MERCHANT_TYPES = {"purchase", "refund", "benefit_credit"}
CONFIDENT_RULE_SOURCES = {"merchant rule", "keyword rule", "issuer category (over keyword)"}
WEAK_SOURCE_MAP = {"issuer category": "issuer", "keyword": "keyword", "unclassified": "unclassified"}
WEAK_SOURCES = set(WEAK_SOURCE_MAP.values())
AI_UPDATABLE_SOURCES = WEAK_SOURCES | {"ai"}
USER_SOURCES = {"user", "merchant"}


@dataclass
class CacheEntry:
    canonical_merchant: str
    merchant_family: str
    cat_l1: str
    cat_l2: str
    cat_l3: str
    source: str                      # 'ai' | 'user'
    subscription_hint: bool | None = None
    confidence: float | None = None


PREFIX_FLOOR = 8     # compact characters: "AMAZON" (6) must never claim "AMAZONPRIME"
PREFIX_RATIO = 0.6   # the shared prefix must cover most of the longer name


class MerchantIndex:
    """
    The merchant cache, looked up by compact key with truncation tolerance.

    Exact compact-key hits come first. Otherwise a cached merchant matches when
    one compact key is a prefix of the other, the shorter is at least
    PREFIX_FLOOR characters, and it covers at least PREFIX_RATIO of the longer:
    issuers cut names at different widths ("SPRINGFIELDGRIZZL" from one,
    "SPRINGFIELDGRIZZLIES" elsewhere), the same truncation import_dedup
    tolerates for statement lines. "SPRINGFIELD" does not claim
    "SPRINGFIELDGRIZZL": too little of it is shared. Among several loose
    candidates the user's own entry wins, then the longest overlap.
    """

    def __init__(self, entries: Mapping[str, CacheEntry] | None = None):
        self._exact: dict[str, CacheEntry] = {}
        for k, v in (entries or {}).items():
            ck = compact_key(k)
            if ck and (ck not in self._exact or v.source == "user"):
                self._exact[ck] = v

    def __len__(self) -> int:
        return len(self._exact)

    def match(self, key: str) -> str | None:
        """The cache key that serves ``key``: itself, a loose match, or None."""
        k = compact_key(key)
        if not k:
            return None
        if k in self._exact:
            return k
        best, best_rank = None, None
        for cand, entry in self._exact.items():
            shorter, longer = sorted((cand, k), key=len)
            if (len(shorter) < PREFIX_FLOOR or len(shorter) < PREFIX_RATIO * len(longer)
                    or not longer.startswith(shorter)):
                continue
            rank = (entry.source == "user", len(shorter))
            if best_rank is None or rank > best_rank:
                best, best_rank = cand, rank
        return best

    def get(self, key: str, default=None) -> CacheEntry | None:
        m = self.match(key)
        return self._exact[m] if m else default


@dataclass
class TxnClass:
    txn_type: str
    txn_subtype: str
    is_spend: bool
    canonical_merchant: str
    merchant_family: str
    cat_l1: str
    cat_l2: str
    cat_l3: str
    category_source: str
    needs_ai: bool = False


def upper_desc(raw: str) -> str:
    return re.sub(r"\s+", " ", (raw or "").upper()).strip()


def classify_transaction(
    raw_description: str,
    amount: float,                       # Flow convention: charges negative
    issuer_type: str,
    issuer_category: str,
    card_name: str,
    merchant_key: str,
    merchant_label: str,
    cache: MerchantIndex | Mapping[str, CacheEntry],
) -> TxnClass:
    desc_u = upper_desc(raw_description)
    spend_amount = -float(amount)        # the rules expect purchases positive
    ttype, sub = card_rules.classify_txn_type(desc_u, spend_amount, issuer_type or "", issuer_category or "")
    is_spend = ttype in card_rules.SPEND_TYPES

    if ttype == "card_fee":
        l1, l2, l3 = card_taxonomy.current(*card_rules.FEE_CLASS.get(sub, ("Financial / Investment", "Fees", "Card fee")))
        return TxnClass(ttype, sub, is_spend, f"{card_name} fee", "Card fees", l1, l2, l3, "rule")

    if ttype not in MERCHANT_TYPES:
        title = ttype.replace("_", " ").title()
        return TxnClass(ttype, sub, is_spend, title, "Non-spend", card_taxonomy.EXCLUDED_L1, title,
                        sub or title, "rule")

    r = card_rules.classify_merchant(desc_u, issuer_category or "", fallback_name=merchant_label or None)
    entry = cache.get(merchant_key) if merchant_key else None

    if entry is not None and entry.source == "user":
        out = TxnClass(ttype, sub, is_spend, entry.canonical_merchant, entry.merchant_family,
                       entry.cat_l1, entry.cat_l2, entry.cat_l3, "merchant")
    elif r["class_source"] in CONFIDENT_RULE_SOURCES:
        out = TxnClass(ttype, sub, is_spend, r["canonical"], r["family"], *card_taxonomy.current(r["l1"], r["l2"], r["l3"]),
                       "rule")
    elif entry is not None and entry.source == "ai":
        out = TxnClass(ttype, sub, is_spend, entry.canonical_merchant, entry.merchant_family,
                       entry.cat_l1, entry.cat_l2, entry.cat_l3, "ai")
    else:
        out = TxnClass(ttype, sub, is_spend, r["canonical"], r["family"], *card_taxonomy.current(r["l1"], r["l2"], r["l3"]),
                       WEAK_SOURCE_MAP.get(r["class_source"], "unclassified"),
                       needs_ai=bool(merchant_key))

    if ttype == "benefit_credit":
        # Card benefit credits keep their own L1 so the annual-fee discussion can see them.
        out.cat_l1, out.cat_l2, out.cat_l3 = "Card Credits & Adjustments", "Card benefit credit", out.canonical_merchant
        out.category_source, out.needs_ai = "rule", False
    return out


def coerce_category(l1: str, l2: str, l3: str) -> tuple[str, str, str]:
    """Clamp an externally supplied category to the taxonomy (L1/L2 fixed, L3 free)."""
    l1, l2, l3 = (l1 or "").strip(), (l2 or "").strip(), (l3 or "").strip()
    if not card_taxonomy.is_valid(l1, l2):
        return card_taxonomy.UNCATEGORIZED
    return l1, l2, (l3[:80] or l2)
