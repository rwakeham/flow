"""
De-duplication of imported transactions against what the ledger already holds.

The statement is the source of truth for *how many* transactions occurred. So
de-dup compares counts, not existence: for each (date, amount) the statement
reports N occurrences, the ledger already covers M of them, and the import adds
the surplus N - M. Asking "does a row like this exist?" per row — as this used to
— collapses every repeat group to a single transaction, because the first insert
is already visible to the next row's lookup.

What counts as coverage
-----------------------
A posted transaction is covered when the ledger already represents it:

  * a bank row, matched or not — it came from a statement, so it is a real posting;
  * a manual entry marked reconciled that has no bank partner — the user
    reconciled it by hand, or reconciled it against a bank row they later deleted.
    Skipping on its behalf is what keeps such a deletion permanent.

A confirmed reconciliation is stored as a *pair*: the bank row and the manual
entry, linked by matched_to_id and both flagged reconciled. Counting both would
double-count one real transaction, so linked manual entries are left out — their
bank partner already carries the count.

Unreconciled, unmatched manual entries are deliberately *not* coverage. Those are
the user's own planned or not-yet-cleared entries, and the right response to a
statement row landing on one is to match them, not to suppress the import.

Three tiers, matched in order
-----------------------------
Statement lines are de-duped against *other statement lines* — same bank — so
their text is comparable. But it is not *stable*: a bank may truncate the
description column differently between exports ("VENMO PAYMENT 260809
100000000000" in one download, "VENMO PAYMENT 260809 1000000000001 JANE
EXAMPLE" in the next), so an exact-text-only rule re-imports any posting whose
stored copy was cut differently. Text narrows the match where it can; counts
decide where it cannot:

  1. Exact — same date, amount, and normalized description.
  2. Truncation-tolerant — same date and amount, and one description is a prefix
     of the other (with a length floor, so short generic strings don't match
     everything). Catches the same posting cut at different widths.
  3. Count-based — same date and amount, full stop. Whatever bank rows tiers 1-2
     did not consume degrade to generic (date, amount) coverage here, joined by
     hand-reconciled ledger entries (whose descriptions are the user's own names
     and can never be text-compared to bank wording). The statement says N
     postings happened at this date and amount, the ledger covers M, the import
     adds N - M.

Running the precise tiers first, each to completion over the whole file, keeps a
rewritten line from consuming coverage that an exactly-matching line needed.

The tier-3 trade-off: an export that covers only ONE of two same-date same-amount
postings cannot tell which one it holds, and will skip it. Full-range exports —
the normal case — always list both, so the counts come out right.

Comparing ledger descriptions against bank descriptions is *not* de-dup's job —
they legitimately differ, and deciding whether they refer to the same transaction
is the fuzzy matcher's problem, downstream, where the user can review the pairing.

Erring toward importing
-----------------------
When the two passes cannot decide, the row is imported. A surplus import lands in
the reconcile view as an unmatched bank row: visible, scored, one click to remove.
A wrong skip is invisible — the transaction is silently absent and the only symptom
is a balance that does not tie out. Given the choice, leave the error where the
user can see it.

The planners are pure functions over pre-computed counts, so the rules are
testable without a database; the caller supplies the counts.
"""

from __future__ import annotations

from collections import Counter


def amount_key(value) -> str:
    """Normalize an amount to a stable cents-precision key (DB Numeric vs float)."""
    key = f"{float(value):.2f}"
    return "0.00" if key == "-0.00" else key


def normalize_description(text) -> str:
    """
    Fold a bank description to a comparable form: collapse whitespace runs, upcase.

    Some banks pad their columns ("VENMO            PAYMENT    260809"), and a bank's
    CSV and text exports can preserve that padding differently, so the same
    transaction could be stored under two different strings. Normalizing at
    comparison time makes both forms compare equal, whichever was stored.
    """
    return " ".join(str(text or "").split()).upper()


def coverage_key(txn_date, amount) -> tuple:
    """Generic coverage: a posting at this date and amount, whatever it was."""
    return (txn_date, amount_key(amount))


def line_key(txn_date, amount, bank_description) -> tuple:
    """A specific statement line, identified across exports by its own text."""
    return (txn_date, amount_key(amount), normalize_description(bank_description))


def manual_key(txn_date, amount, description) -> tuple:
    """
    Identity of a ledger row for ledger-file de-dup.

    Unlike bank descriptions, the descriptions in the user's own tracking
    spreadsheet are stable across exports and distinguish same-day, same-amount
    entries, so they belong in the key here.
    """
    return (txn_date, amount_key(amount), (description or "").strip())


def date_bounds(rows) -> tuple:
    dates = [r.date for r in rows]
    return (min(dates), max(dates))


# Below this, a prefix match is off: strings like "CHECK" are too generic for a
# truncation claim, and an empty description is a prefix of everything.
_PREFIX_FLOOR = 12


def _truncation_candidate(bucket: Counter, desc: str) -> str | None:
    """
    Find a stored description that reads as the same line cut at a different
    width: one string is a prefix of the other, and the shorter is long enough to
    be meaningful. Prefers the candidate sharing the most text.
    """
    best = None
    best_overlap = -1
    for stored, n in bucket.items():
        if n <= 0:
            continue
        shorter, longer = sorted((stored, desc), key=len)
        if len(shorter) < _PREFIX_FLOOR or not longer.startswith(shorter):
            continue
        if len(shorter) > best_overlap:
            best_overlap = len(shorter)
            best = stored
    return best


def plan_bank_import(rows, line_coverage: Counter, generic_coverage: Counter) -> tuple[list, int]:
    """
    Decide which statement rows to import.

    ``line_coverage``    maps line_key     -> bank rows already on file.
    ``generic_coverage`` maps coverage_key -> hand-reconciled entries with no bank row.

    Returns (rows_to_import, skipped_count).  Both counters are copied, so callers
    can reuse them.
    """
    line_remaining = Counter(line_coverage)
    skipped = 0

    # Tier 1 — this exact statement line is already on file.
    undecided = []
    for row in rows:
        key = line_key(row.date, row.amount, row.bank_description)
        if line_remaining[key] > 0:
            line_remaining[key] -= 1
            skipped += 1
            continue
        undecided.append(row)

    # Tier 2 — same date and amount, description truncated at a different width.
    buckets: dict[tuple, Counter] = {}
    for (d, a, desc), n in line_remaining.items():
        if n > 0:
            buckets.setdefault((d, a), Counter())[desc] += n

    still_undecided = []
    for row in undecided:
        d, a, desc = line_key(row.date, row.amount, row.bank_description)
        bucket = buckets.get((d, a))
        candidate = _truncation_candidate(bucket, desc) if bucket else None
        if candidate is not None:
            bucket[candidate] -= 1
            skipped += 1
            continue
        still_undecided.append(row)

    # Tier 3 — counts alone. Bank rows the text tiers could not claim still assert
    # "a posting happened at this date and amount"; so do hand-reconciled entries.
    generic_remaining = Counter(generic_coverage)
    for (d, a), bucket in buckets.items():
        remaining = sum(n for n in bucket.values() if n > 0)
        if remaining:
            generic_remaining[(d, a)] += remaining

    to_import = []
    for row in still_undecided:
        key = coverage_key(row.date, row.amount)
        if generic_remaining[key] > 0:
            generic_remaining[key] -= 1
            skipped += 1
            continue
        to_import.append(row)

    return to_import, skipped


def plan_ledger_import(rows, manual_counts: Counter) -> tuple[list, int]:
    """
    Decide which rows of the user's tracking spreadsheet to import as manual entries,
    consuming existing-row counts as they match.
    Returns (rows_to_import, skipped_count).
    """
    remaining = Counter(manual_counts)

    to_import = []
    skipped = 0

    for row in rows:
        key = manual_key(row.date, row.amount, row.description)
        if remaining[key] > 0:
            remaining[key] -= 1
            skipped += 1
            continue
        to_import.append(row)

    return to_import, skipped


# ── Wrong-account check ──────────────────────────────────────────────────────
#
# Many exports carry no account number, so the account picked in the import
# dialog is the only attribution, and de-duplication only compares against that
# account. A file uploaded to the wrong account therefore imports in full and
# double-counts. Before importing, compare the file with every other account of
# the same kind: if most of its rows are already there, and fewer are on the
# chosen one, the file most likely belongs there. Amounts compare signed, so a
# transfer between two of the user's accounts (-500 here, +500 there) is no match.

MATCH_PREFIX = 12          # description characters compared; exports truncate differently
OTHER_ACCOUNT_MIN_SHARE = 0.6
OTHER_ACCOUNT_MIN_ROWS = 3  # fewer rows than this must all match


def match_key(d, amount, desc) -> tuple:
    return (d, amount_key(amount), normalize_description(desc)[:MATCH_PREFIX])


def overlap(file_rows: list[tuple], existing: list[tuple], window: tuple | None) -> tuple[int, int]:
    """
    (matched, compared): how many of the file's (date, amount, description) rows
    inside ``window`` (the dates the other account's data covers) already exist
    there, counted one-to-one on date, amount and the start of the description.
    Rows outside the window can't be compared: the other account has no data then.
    """
    if not window:
        return 0, 0
    lo, hi = window
    compared = [r for r in file_rows if lo <= r[0] <= hi]
    remaining = Counter(match_key(*e) for e in existing)
    matched = 0
    for r in compared:
        k = match_key(*r)
        if remaining[k] > 0:
            remaining[k] -= 1
            matched += 1
    return matched, len(compared)


def is_likely_match(matched: int, compared: int) -> bool:
    if compared == 0 or matched == 0:
        return False
    return matched >= min(OTHER_ACCOUNT_MIN_ROWS, compared) and matched / compared >= OTHER_ACCOUNT_MIN_SHARE


def best_other_account(file_rows: list[tuple], chosen_id: int, names: dict[int, str],
                       existing: dict[int, list[tuple]], coverage: dict[int, tuple]) -> dict | None:
    """The other account the file most likely belongs to: {account_id, account, matched, compared, matched_on_chosen}."""
    if not file_rows:
        return None
    own, _ = overlap(file_rows, existing.get(chosen_id, []), coverage.get(chosen_id))
    best = None
    for aid, name in names.items():
        if aid == chosen_id:
            continue
        matched, compared = overlap(file_rows, existing.get(aid, []), coverage.get(aid))
        if is_likely_match(matched, compared) and matched > own and (best is None or matched > best["matched"]):
            best = {"account_id": aid, "account": name, "matched": matched, "compared": compared,
                    "matched_on_chosen": own}
    return best


def other_account_message(match: dict, chosen: str) -> str:
    own, other = match["matched_on_chosen"], match["account"]
    return (f"This file looks like it belongs to {other}: {match['matched']} of its "
            f"{match['compared']} transactions in the dates {other} covers are already there, "
            f"and {'none' if own == 0 else f'only {own}'} {'is' if own == 1 else 'are'} on {chosen}.")
