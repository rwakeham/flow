"""
Fuzzy matching between bank-imported transactions and manual ledger entries.

Matching rules:
  1. EXACT match: identical date and amount → always suggest (regardless of description).
  2. CLOSE match: amount within max(|amount| * 10%, $5.00) and date within ±14 days.

Both score as:
     0.30 * amount_score + 0.20 * date_score + 0.50 * sqrt(description_score)

The description carries half the confidence: date and amount agreeing is a
prerequisite, not proof — amount collisions on a busy day are routine. A pair
sharing no words tops out at 50%, so it renders amber for review instead of a
confident green. The square root compensates for how harsh word-overlap Jaccard
is on legitimately different vocabularies (bank wording vs the user's ledger
names): a genuine pairing sharing a third of its words scores ~0.79, not ~0.67.

Exact matches are always suggested; close matches must clear min_score (default
0.25). Every suggestion also carries ``description_score`` raw, so callers can
label a date-and-amount-only pairing explicitly.

``suggest_matches`` scores each bank transaction independently and may propose the
same manual transaction to several of them.  ``assign_matches`` builds on it to
produce a one-to-one assignment, which is what callers want whenever a set of
repeated identical transactions is in play.
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
    description_score: float = 0.0   # word overlap alone, surfaced for review


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


def _combined_score(amount_score: float, date_score: float, desc_score: float) -> float:
    """
    Blend the three signals; see the module docstring for the weighting rationale.
    sqrt lifts the harsh Jaccard overlap so genuinely-related-but-differently-worded
    pairs are not scored like collisions.
    """
    return 0.30 * amount_score + 0.20 * date_score + 0.50 * (desc_score ** 0.5)


def suggest_matches(
    bank_txns: list[TxnInfo],
    manual_txns: list[TxnInfo],
    min_score: float = 0.25,
) -> list[MatchSuggestion]:
    """
    For each bank transaction, find the best-scoring unmatched manual transaction.

    Returns one suggestion per bank transaction (highest score above threshold).
    A manual transaction may appear as the suggestion for multiple bank transactions;
    use ``assign_matches`` when each manual transaction may be claimed only once.
    """
    suggestions: list[MatchSuggestion] = []

    for bank in bank_txns:
        best_score = -1.0
        best_manual_id: int | None = None
        best_desc_score = 0.0
        best_is_exact = False

        amount_tolerance = max(abs(bank.amount) * 0.10, 5.00)
        date_window = 14

        for manual in manual_txns:
            amount_diff = abs(bank.amount - manual.amount)
            date_diff = abs((bank.date - manual.date).days)

            # Rule 1: identical date + identical amount → always suggest
            if amount_diff == 0 and date_diff == 0:
                desc_score = _description_score(bank.description, manual.description)
                score = _combined_score(1.0, 1.0, desc_score)
                if score > best_score:
                    best_score = score
                    best_manual_id = manual.id
                    best_desc_score = desc_score
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
            score = _combined_score(amount_score, date_score, desc_score)

            if score > best_score:
                best_score = score
                best_manual_id = manual.id
                best_desc_score = desc_score
                best_is_exact = False

        if best_manual_id is not None:
            # Exact matches bypass min_score; fuzzy matches must meet threshold
            if best_is_exact or best_score >= min_score:
                suggestions.append(MatchSuggestion(
                    bank_id=bank.id,
                    manual_id=best_manual_id,
                    score=round(best_score, 4),
                    description_score=round(best_desc_score, 4),
                ))

    return suggestions


def assign_matches(
    bank_txns: list[TxnInfo],
    manual_txns: list[TxnInfo],
    min_score: float = 0.25,
) -> list[MatchSuggestion]:
    """
    Pair bank and manual transactions one-to-one: every manual transaction is
    claimed by at most one bank transaction, and vice versa.

    Repeated identical transactions (two $4.99 fee reimbursements on the same day)
    would otherwise all point at the same manual entry, and every bank row but one
    would be left unmatched even though an equally good manual entry was free.  So
    assignment runs in rounds: take the highest-scoring suggestion for each still-free
    manual entry, then re-score the bank transactions that lost a tie against the
    manual entries that remain.  A bank transaction with no suggestion at all is
    dropped — removing manual candidates can never give it one later.

    Returns the accepted pairings, highest score first.
    """
    pending_bank = list(bank_txns)
    available_manual = {m.id: m for m in manual_txns}
    assigned: list[MatchSuggestion] = []

    while pending_bank and available_manual:
        suggestions = suggest_matches(pending_bank, list(available_manual.values()), min_score)
        if not suggestions:
            break

        suggested_bank_ids = {s.bank_id for s in suggestions}
        claimed_bank_ids: set[int] = set()

        for suggestion in sorted(suggestions, key=lambda s: -s.score):
            if suggestion.manual_id not in available_manual:
                continue  # a higher-scoring bank txn took it this round
            del available_manual[suggestion.manual_id]
            claimed_bank_ids.add(suggestion.bank_id)
            assigned.append(suggestion)

        # Retry only the bank txns that had a suggestion but lost it to a better match.
        pending_bank = [
            b for b in pending_bank
            if b.id in suggested_bank_ids and b.id not in claimed_bank_ids
        ]

    assigned.sort(key=lambda s: -s.score)
    return assigned


def match_annotations(rows, today: date) -> dict[int, dict]:
    """
    Confidence annotations for a ledger view, keyed by transaction id.

    ``rows`` are ledger records — anything exposing id, date, amount, description,
    source, bank_description, matched_to_id and is_reconciled. Returns:

      * ``match_score`` / ``match_description_score`` on both halves of a pending
        pair — matched but not yet confirmed. Such a pair is a proposal awaiting
        review, so the view should show how good it is rather than a bare tick.
      * ``suggestion`` on each unmatched bank row, from the same one-to-one
        assignment the reconcile screen runs over the same candidates, so the two
        screens never propose different partners for the same row.

    Candidates exclude future-dated manual entries: a posted transaction should
    not be paired with something the user has only planned.
    """
    by_id = {r.id: r for r in rows}
    out: dict[int, dict] = {}

    def _info(r, is_bank: bool) -> TxnInfo:
        return TxnInfo(
            id=r.id,
            date=r.date,
            amount=float(r.amount),
            description=(r.bank_description or "") if is_bank else r.description,
        )

    for row in rows:
        if row.source != "bank" or row.matched_to_id is None or row.is_reconciled:
            continue
        partner = by_id.get(row.matched_to_id)
        if partner is None:
            continue
        scored = suggest_matches([_info(row, True)], [_info(partner, False)], min_score=0.0)
        if not scored:
            continue
        for txn_id in (row.id, partner.id):
            out.setdefault(txn_id, {}).update(
                match_score=scored[0].score,
                match_description_score=scored[0].description_score,
            )

    unmatched_bank = [
        r for r in rows
        if r.source == "bank" and r.matched_to_id is None and not r.is_reconciled
    ]
    unmatched_manual = [
        r for r in rows
        if r.source == "manual" and r.matched_to_id is None
        and not r.is_reconciled and r.date <= today
    ]
    if not unmatched_bank or not unmatched_manual:
        return out

    for s in assign_matches(
        [_info(r, True) for r in unmatched_bank],
        [_info(r, False) for r in unmatched_manual],
    ):
        out.setdefault(s.bank_id, {})["suggestion"] = {
            "manual_id": s.manual_id,
            "score": s.score,
            "description_score": s.description_score,
        }

    return out
