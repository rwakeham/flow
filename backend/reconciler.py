"""
Fuzzy matching between bank-imported transactions and manual ledger entries.

Matching rules:
  1. EXACT match: identical date and amount → always suggest (regardless of description).
  2. CLOSE match: amount within max(|amount| * 10%, $5.00) and date within ±14 days.
     score = 0.40 * amount_score + 0.30 * date_score + 0.30 * description_score
     Returned only if score >= min_score (default 0.25).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date


@dataclass
class TxnInfo:
    id: int
    date: date
    amount: float          # positive=credit, negative=debit
    description: str       # manual description or bank_description


@dataclass
class MatchSuggestion:
    bank_id: int
    manual_id: int
    score: float


_STOPWORDS = {
    "the", "a", "an", "and", "or", "of", "to", "in", "for", "on", "at",
    "is", "it", "its", "be", "as", "by", "from", "with",
}


def _keywords(text: str) -> set[str]:
    """Extract meaningful words from a description string."""
    lowered = text.lower()
    cleaned = re.sub(r"[^a-z0-9 ]", " ", lowered)
    tokens = cleaned.split()
    return {t for t in tokens if len(t) >= 3 and not t.isdigit() and t not in _STOPWORDS}


def _description_score(bank_desc: str, manual_desc: str) -> float:
    """Word-overlap Jaccard-style similarity [0.0, 1.0]."""
    bank_kw = _keywords(bank_desc)
    manual_kw = _keywords(manual_desc)
    if not bank_kw or not manual_kw:
        return 0.0
    intersection = bank_kw & manual_kw
    union = bank_kw | manual_kw
    return len(intersection) / len(union)


def suggest_matches(
    bank_txns: list[TxnInfo],
    manual_txns: list[TxnInfo],
    min_score: float = 0.25,
) -> list[MatchSuggestion]:
    """
    For each bank transaction, find the best-scoring unmatched manual transaction.

    Returns one suggestion per bank transaction (highest score above threshold).
    A manual transaction may appear as the suggestion for multiple bank transactions
    (the UI lets the user confirm/reject).
    """
    suggestions: list[MatchSuggestion] = []

    for bank in bank_txns:
        best_score = -1.0
        best_manual_id: int | None = None
        best_is_exact = False

        amount_tolerance = max(abs(bank.amount) * 0.10, 5.00)
        date_window = 14

        for manual in manual_txns:
            amount_diff = abs(bank.amount - manual.amount)
            date_diff = abs((bank.date - manual.date).days)

            # Rule 1: identical date + identical amount → always suggest
            if amount_diff == 0 and date_diff == 0:
                desc_score = _description_score(bank.description, manual.description)
                # Perfect amount + date: weight heavily so it always wins
                score = 0.40 * 1.0 + 0.30 * 1.0 + 0.30 * desc_score
                if score > best_score:
                    best_score = score
                    best_manual_id = manual.id
                    best_is_exact = True
                continue

            # Rule 2: close match — amount within tolerance and date within window
            if amount_diff > amount_tolerance:
                continue
            if date_diff > date_window:
                continue

            amount_score = 1.0 - (amount_diff / amount_tolerance)
            date_score = 1.0 - (date_diff / date_window)
            desc_score = _description_score(bank.description, manual.description)
            score = 0.40 * amount_score + 0.30 * date_score + 0.30 * desc_score

            if score > best_score:
                best_score = score
                best_manual_id = manual.id
                best_is_exact = False

        if best_manual_id is not None:
            # Exact matches bypass min_score; fuzzy matches must meet threshold
            if best_is_exact or best_score >= min_score:
                suggestions.append(MatchSuggestion(
                    bank_id=bank.id,
                    manual_id=best_manual_id,
                    score=round(best_score, 4),
                ))

    return suggestions
