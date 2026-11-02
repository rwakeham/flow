"""
Bulk import: suggest the account each file belongs to.

Pure functions (no DB): ``routers/upload.py`` loads the rows and accounts, and
the browser sends each file to the normal single-file import endpoint with the
account the user confirms. Nothing here writes anything.

A file is pre-assigned ("sure") only on positive evidence that points one way:

- its rows overlap exactly one account (``import_dedup.is_likely_match``, with
  at least 3 matching rows unless the file is smaller), or
- a card number in it is known to exactly one card, or
- the import format that reads it is linked to exactly one account of the
  right kind (and nothing above points elsewhere).

An account can hold several cards (authorized users, a reissued number), so a
number the matched card doesn't list yet is no objection: the import adds it.
Only a number already listed on a different card is. Anything else (no overlap,
several accounts overlap, a number that belongs to another card, a guessed sign
with no overlap to confirm it) is left for the user to pick, with the reason. A file the batch has assigned counts as history
for the files after it, so a run of consecutive exports chains from one that
overlaps the database.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timedelta

from import_dedup import OTHER_ACCOUNT_MIN_ROWS, is_likely_match, overlap


@dataclass
class BatchFile:
    index: int
    kind: str                       # 'card' (card accounts only), 'bank' (cash or card), 'ledger'
    cash_rows: list[tuple] = field(default_factory=list)   # (date, amount, description) as a cash import reads them
    card_rows: list[tuple] = field(default_factory=list)   # the same, as a card import reads them (charges negative)
    numbers: list[str] = field(default_factory=list)   # last digits of each card number in the file
    sign_guessed: bool = False      # nothing in the file confirmed the card format's sign
    format_accounts: list[str] = field(default_factory=list)  # accounts linked to the formats that read it


@dataclass
class BatchAccount:
    key: str                        # "r:<id>" cash register, "c:<id>" credit card
    name: str
    numbers: list[str] = field(default_factory=list)     # last digits of the cards on the account
    archived: bool = False

    @property
    def kind(self) -> str:
        return self.key[0]


@dataclass
class Suggestion:
    account: str | None = None      # account key when sure
    sure: bool = False
    hint: str | None = None         # best guess when not sure (never pre-selected)
    reasons: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def _rows_for(f: BatchFile, kind: str) -> list[tuple]:
    return f.card_rows if kind == "c" else f.cash_rows


def _candidates(f: BatchFile, accounts: list[BatchAccount]) -> list[BatchAccount]:
    kinds = {"card": "c", "bank": "rc", "ledger": "r"}.get(f.kind, "")
    # A ledger file isn't compared with history (it has no bank lines), so every cash account is a candidate.
    return [a for a in accounts if a.kind in kinds and (f.kind == "ledger" or _rows_for(f, a.kind))]


def _digits(s: str | None) -> str:
    return "".join(ch for ch in (s or "") if ch.isdigit())


def _bounds(rows: list[tuple]) -> tuple:
    dates = [r[0] for r in rows]
    return min(dates), max(dates)


def _decide(f: BatchFile, accounts: list[BatchAccount], existing: dict[str, list[tuple]],
            coverage: dict[str, tuple], on_file: dict[str, list[tuple]], on_file_coverage: dict[str, tuple]) -> Suggestion:
    cands = _candidates(f, accounts)
    if not cands:
        return Suggestion(reasons=["No account of the right kind exists yet."])
    scores = {}
    for a in cands:
        rows = _rows_for(f, a.kind)
        scores[a.key] = (*overlap(rows, existing.get(a.key, []), coverage.get(a.key)), len(rows))
    by_key = {a.key: a for a in cands}

    def strong(key):
        matched, compared, n = scores[key]
        return is_likely_match(matched, compared) and matched >= min(OTHER_ACCOUNT_MIN_ROWS, n)

    likely = [k for k in scores if strong(k)]
    file_numbers = [_digits(n)[-4:] for n in f.numbers if _digits(n)]
    # Which card each of the file's numbers is already listed on (archived cards too).
    owners = {n: [a.key for a in cands if a.kind == "c" and n in {_digits(x)[-4:] for x in a.numbers}]
              for n in file_numbers}
    owning = list(dict.fromkeys(k for ks in owners.values() for k in ks))

    def ending(ns):
        ns = list(ns)
        return ("card number ending " if len(ns) == 1 else "card numbers ending ") + " and ".join(ns)

    def new_numbers_note(k):
        new = [n for n in file_numbers if k not in owners[n]]
        if new:
            s.reasons.append(f"New {ending(new)}: it will be added to {by_key[k].name}.")

    def overlap_reason(k):
        matched, compared, _ = scores[k]
        m, c = overlap(_rows_for(f, k[0]), on_file.get(k, []), on_file_coverage.get(k))
        if is_likely_match(m, c):
            return f"{m} of its {c} transactions in the dates {by_key[k].name} covers are already there."
        # The match came from files earlier in this batch, not from what is stored.
        return f"{matched} of its {compared} transactions are in other files here that go to {by_key[k].name}."

    s = Suggestion()
    if len(likely) > 1:
        s.reasons.append("Its transactions are already in more than one account: "
                         + ", ".join(by_key[k].name for k in likely) + ".")
        s.hint = max(likely, key=lambda k: scores[k][0])
        return s
    if likely:
        k = likely[0]
        acct = by_key[k]
        elsewhere = [j for j in owning if j != k]
        if elsewhere:
            nums = [n for n in file_numbers if any(j in owners[n] for j in elsewhere)]
            s.reasons.append(f"Its transactions match {acct.name}, but its {ending(nums)} "
                             f"{'is' if len(nums) == 1 else 'are'} listed on {by_key[elsewhere[0]].name}.")
            s.hint = k
            return s
        if acct.archived:
            s.reasons.append(f"Its transactions match {acct.name}, which is archived.")
            s.hint = k
            return s
        s.account, s.sure = k, True
        s.reasons.append(overlap_reason(k))
        new_numbers_note(k)
        return s
    if len(owning) > 1:
        s.reasons.append("Its card numbers are listed on more than one card: "
                         + ", ".join(by_key[k].name for k in owning) + ".")
        return s
    if owning:
        k = owning[0]
        acct = by_key[k]
        known = [n for n in file_numbers if k in owners[n]]
        matched, compared, _ = scores[k]
        if compared and not is_likely_match(matched, compared):
            s.reasons.append(f"Its {ending(known)} {'is' if len(known) == 1 else 'are'} on {acct.name}, but only "
                             f"{matched} of {compared} transactions in the dates {acct.name} covers are there.")
            s.hint = k
            return s
        if f.sign_guessed:
            s.reasons.append(f"Its {ending(known)} {'is' if len(known) == 1 else 'are'} on {acct.name}, but the sign "
                             "of its amounts was guessed and nothing on file confirms it.")
            s.hint = k
            return s
        if acct.archived:
            s.reasons.append(f"Its {ending(known)} {'is' if len(known) == 1 else 'are'} on {acct.name}, which is archived.")
            s.hint = k
            return s
        s.account, s.sure = k, True
        s.reasons.append(f"Its {ending(known)} {'is' if len(known) == 1 else 'are'} on {acct.name}.")
        new_numbers_note(k)
        cov = coverage.get(k)
        if cov:
            lo, hi = _bounds(f.card_rows)
            if lo - cov[1] > timedelta(days=1) or cov[0] - hi > timedelta(days=1):
                s.warnings.append(f"It doesn't touch the dates already in {acct.name} ({cov[0].isoformat()} to "
                                  f"{cov[1].isoformat()}): transactions in between may be missing.")
        return s
    linked = [k for k in dict.fromkeys(f.format_accounts) if k in by_key]
    if len(linked) == 1:
        k = linked[0]
        acct = by_key[k]
        if acct.archived:
            s.reasons.append(f"Its import format is only used by {acct.name}, which is archived.")
            s.hint = k
            return s
        if not (k[0] == "c" and f.sign_guessed):
            s.account, s.sure = k, True
            s.reasons.append(f"It's read by the import format only {acct.name} uses.")
            if k[0] == "c":
                new_numbers_note(k)
            return s
    best = max(scores, key=lambda k: scores[k][0]) if scores else None
    if best and scores[best][0]:
        matched, compared, _ = scores[best]
        s.hint = best
        s.reasons.append(f"Only {matched} of {compared} transactions in the dates {by_key[best].name} covers "
                         "are already there: not enough to be sure.")
    else:
        s.reasons.append("None of its transactions are in any account yet"
                         + (f", and its {ending(file_numbers)} {'is' if len(file_numbers) == 1 else 'are'} on no card "
                            "(a new or reissued card?)." if file_numbers else ", and nothing else identifies it."))
    return s


def plan(files: list[BatchFile], accounts: list[BatchAccount], existing: dict[str, list[tuple]],
         coverage: dict[str, tuple], pinned: dict[int, str] | None = None) -> dict[int, Suggestion]:
    """
    Suggest an account for every file. ``existing`` holds each account's rows
    (keyed like ``BatchAccount.key``) and ``coverage`` the dates its data covers.
    ``pinned`` maps file index to an account the user chose; those count as
    sure, and as history for the other files, like any sure file.
    """
    pinned = pinned or {}
    known = {a.key for a in accounts}
    out: dict[int, Suggestion] = {}
    for f in files:
        if f.index in pinned and pinned[f.index] in known:
            out[f.index] = Suggestion(account=pinned[f.index], sure=True, reasons=["You picked this account."])
        elif f.kind == "ledger" and len({k for k in f.format_accounts if k[0] == "r"}) != 1:
            out[f.index] = Suggestion(reasons=["A ledger spreadsheet isn't matched against history, and its "
                                               "format isn't tied to one cash account: pick the account."])

    on_file, on_file_coverage = existing, coverage
    existing = {k: list(v) for k, v in existing.items()}
    coverage = dict(coverage)

    def absorb(f: BatchFile, key: str):
        rows = _rows_for(f, key[0])
        if not rows:
            return
        existing.setdefault(key, []).extend(rows)
        lo, hi = _bounds(rows)
        if key in coverage:
            lo, hi = min(lo, coverage[key][0]), max(hi, coverage[key][1])
        coverage[key] = (lo, hi)

    for f in files:
        s = out.get(f.index)
        if s and s.account:
            absorb(f, s.account)

    pending = [f for f in files if f.index not in out]
    while True:
        progressed = False
        for f in pending:
            if f.index in out:
                continue
            s = _decide(f, accounts, existing, coverage, on_file, on_file_coverage)
            if s.sure:
                out[f.index] = s
                absorb(f, s.account)
                progressed = True
        if not progressed:
            break
    for f in pending:
        if f.index not in out:
            out[f.index] = _decide(f, accounts, existing, coverage, on_file, on_file_coverage)

    return out
