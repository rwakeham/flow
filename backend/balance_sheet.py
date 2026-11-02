"""
Balance-sheet accounts entered by hand or pasted from a bank's account summary.

Pure functions (no DB, no network):

* ``carry_forward``: an account with no balance for a period shows its last
  known value there, marked as carried, so a half-entered month doesn't dip.
* ``extract``: reads a pasted account-summary page into (label, last 4, amount,
  kind) items, locally, from the common masked-number shapes ("ending in 1234",
  "x1234", "****1234") with an amount soon after. It is a first reading only:
  paste markers (``read_with_markers``) teach any other layout, and the words it
  knows (kinds, closed accounts, ``VOCAB``) are English.
* ``outline``: the scrubbed version of a paste for the AI fallback. Every amount
  and account number is a token, every word not in a small finance vocabulary is
  ``~``; the model only says which amount token goes with which number token.
* ``plan``: matches items to accounts by last 4 and runs the checks the preview
  shows before anything is written.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from datetime import date

# ── Periods ──────────────────────────────────────────────────────────────────

def first_of_month(d: date) -> date:
    return d.replace(day=1)


def current_period(d: date) -> date:
    """The period suggested for balances entered on ``d``: the 1st of that month (Nov 1 and Nov 20 → Nov 1).
    The user can pick another, up to ``next_period``."""
    return d.replace(day=1)


def next_period(d: date) -> date:
    """The 1st of the month after ``d``: the latest period that can be written, for a month-end
    snapshot the user would rather file as the next month (Oct 31 → Nov 1)."""
    return date(d.year + (d.month == 12), d.month % 12 + 1, 1)


def client_today(header: str | None, server_today: date) -> date:
    """
    The user's own date, from the browser's X-Flow-Today header (YYYY-MM-DD). The server runs
    on UTC, so on the evening of Oct 31 in the US its date is already Nov 1, and the snapshot
    would be filed a month late. Believed only within a day of the server's date (time zones
    span about that); anything else, or nothing, falls back to the server's date.
    """
    try:
        d = date.fromisoformat((header or "").strip())
    except ValueError:
        return server_today
    return d if abs((d - server_today).days) <= 1 else server_today


def period_problem(period: date, today: date) -> str | None:
    """Why ``period`` can't be written, or None. Periods are the 1st of a month, at most next month."""
    if period.day != 1:
        return "A period is the 1st of a month."
    if period > next_period(today):
        return f"{period.isoformat()} is in the future (latest allowed is {next_period(today).isoformat()})."
    return None


# ── Carry-forward ────────────────────────────────────────────────────────────

def carry_forward(values: list[float | None], archived: bool = False) -> tuple[list[float | None], list[bool]]:
    """
    Fill each gap after an account's first value with its last known value, and
    say which entries were carried. An archived (closed) account is not carried
    past its last real value.
    """
    out: list[float | None] = []
    carried: list[bool] = []
    last = None
    last_real = max((i for i, v in enumerate(values) if v is not None), default=-1)
    for i, v in enumerate(values):
        if v is not None:
            last = v
            out.append(v)
            carried.append(False)
        elif last is not None and not (archived and i > last_real):
            out.append(last)
            carried.append(True)
        else:
            out.append(None)
            carried.append(False)
    return out, carried


# ── Last-4 lists ─────────────────────────────────────────────────────────────

def numbers(stored: str | None) -> list[str]:
    """The last-4 digits saved on an account (comma-separated), in order, no duplicates."""
    out: list[str] = []
    for part in (stored or "").split(","):
        p = re.sub(r"\D", "", part)[-4:]
        if len(p) == 4 and p not in out:
            out.append(p)
    return out


def join_numbers(nums: list[str]) -> str | None:
    clean = numbers(",".join(nums))
    return ",".join(clean) or None


# ── Paste extraction ─────────────────────────────────────────────────────────

MAX_PASTE_CHARS = 60_000

# "Account number ending in...1234", "ending in 1234", "x1234", "****1234", "...1234", "(…1234)".
_ANCHOR = re.compile(
    r"(?:ending\s*in|ending|acct\.?|account\s*(?:number|no\.?|#)?)[^\w\n]{0,8}(?:x+|\*+|\.{2,}|…)?\s*(\d{4})(?!\d)"
    r"|(?<![A-Za-z0-9])(?:[xX]{1,}|\*{2,}|\.{3,}|…|•{2,})\s?(\d{4})(?!\d)",
    re.IGNORECASE,
)
_MONEY = re.compile(r"(?<![\d$.,])(\(?-?\s?\$\s?-?\s?\d{1,3}(?:,\d{3})*(?:\.\d{1,2})?\)?|\(?-?\s?\$\s?-?\s?\d+(?:\.\d{1,2})?\)?)")
_CLOSED = re.compile(r"\b(?:pd|paid)\s+in\s+full\b|\baccount\s+is\s+closed\b|\bclosed\b", re.IGNORECASE)
_REWARDS = re.compile(r"\brewards?\b|\bpoints?\b|\bmiles\b", re.IGNORECASE)
_OWED = re.compile(r"\boutstanding\b|\bprincipal\b|\bowed?\b|\bpayoff\b|\bstatement balance\b|\bamount due\b", re.IGNORECASE)
_NOISE_LINE = re.compile(r"^(?:\d+\.|[*•\-])?\s*(?:begin region|end of region)?\s*$", re.IGNORECASE)
_LIST_MARK = re.compile(r"^\s*(?:\d+\.|[*•\-])\s*")
_INSTITUTION_TAIL = re.compile(r"[A-Z][a-z]+(?: [A-Z][a-z]+)*,? (?:LLC|N\.A\.|Inc\.?)\d?\s*$")
# Screen-reader and UI text some banking sites put between an account and its balance.
_UI_NOISE = re.compile(r"Opens a dialog|\(Selected View\)|navigate to [^\n]*|Footnote", re.IGNORECASE)
WINDOW_LINES = 6            # an amount must follow its account number within this many lines


@dataclass
class Item:
    """One account found in a paste."""
    last4: str
    label: str
    amount: float | None        # as shown on the page (owed balances are positive there)
    kind: str                   # 'balance' | 'owed' | 'rewards' | 'closed' | 'none'
    detail: str = ""            # the words between the number and the amount ("Available balance")
    source: str = "local"       # 'local' | 'ai' | 'marker'
    account_id: int | None = None   # set when a paste marker read it for that account


@dataclass
class Extracted:
    items: list[Item]
    unread: list[str] = field(default_factory=list)   # last-4s found with no amount and no reason
    stray_amounts: int = 0                              # amounts not claimed by any account
    needs_ai: bool = False                              # local reading looks incomplete


def parse_money(text: str) -> float | None:
    t = text.strip()
    neg = t.startswith("(") and t.endswith(")") or "-" in t
    digits = re.sub(r"[^\d.]", "", t)
    if not digits or digits == ".":
        return None
    try:
        v = float(digits)
    except ValueError:
        return None
    return -v if neg else v


def _clean_line(line: str) -> str:
    line = _UI_NOISE.sub(" ", line)
    return re.sub(r"\s+", " ", line).strip()


def clean_label(line: str) -> str:
    s = _LIST_MARK.sub("", _clean_line(line))
    s = _INSTITUTION_TAIL.sub("", s).strip(" ,-")
    return s[:80]


def _lines(text: str) -> list[str]:
    return [_clean_line(l) for l in text.replace("\r", "\n").split("\n")]


def _anchors(lines: list[str]) -> list[tuple[int, str, int, int]]:
    """(line index, last 4, match start, match end) for every account number, repeats on adjacent lines dropped."""
    out: list[tuple[int, str, int, int]] = []
    for i, line in enumerate(lines):
        for m in _ANCHOR.finditer(line):
            last4 = m.group(1) or m.group(2)
            if out and out[-1][1] == last4 and i - out[-1][0] <= 1:
                continue
            out.append((i, last4, m.start(), m.end()))
    return out


def _label_lines(lines: list[str], anchors) -> list[tuple[str, int]]:
    """Each account's name and the line it sits on: text before the number, else the nearest line above."""
    out = []
    prev_line = -1
    for li, _last4, start, _end in anchors:
        before = re.sub(r"(?i)account\s*(number|no\.?|#)?\s*$", "", lines[li][:start])
        label, at = clean_label(before), li
        if len(label) < 2:
            label = ""
            for j in range(li - 1, prev_line, -1):
                if lines[j] and not _NOISE_LINE.match(lines[j]) and not _MONEY.search(lines[j]):
                    label, at = clean_label(lines[j]), j
                    break
        out.append((label, at))
        prev_line = li
    return out


def extract(text: str) -> Extracted:
    """Read accounts out of a pasted summary page. Never raises on odd input."""
    lines = _lines(text[:MAX_PASTE_CHARS])
    anchors = _anchors(lines)
    labels = _label_lines(lines, anchors)

    items: list[Item] = []
    claimed: set[tuple[int, int]] = set()
    for k, (li, last4, _start, end) in enumerate(anchors):
        # The account's region runs from its number to the next account (its name line), within a window.
        if k + 1 < len(anchors):
            n_li, _n4, n_start, _n_end = anchors[k + 1]
            n_label_line = labels[k + 1][1]
            stop_line, stop_col = (n_label_line, 0) if n_label_line > li else (n_li, n_start)
        else:
            stop_line, stop_col = len(lines), 0
        last_line = min(stop_line, li + WINDOW_LINES)
        parts: list[tuple[int, int, str]] = []
        for j in range(li, last_line + 1):
            if j >= len(lines):
                break
            lo = end if j == li else 0
            hi = stop_col if (j == stop_line and stop_col) else len(lines[j])
            if j == stop_line and not stop_col:
                break
            parts.append((j, lo, lines[j][lo:hi]))
        words: list[str] = []
        amount = None
        for j, offset, chunk in parts:
            mm = _MONEY.search(chunk)
            if mm:
                words.append(chunk[:mm.start()])
                amount = parse_money(mm.group(1))
                claimed.add((j, offset + mm.start()))
                break
            words.append(chunk)
        detail = re.sub(r"\s+", " ", " ".join(words)).strip()
        region = " ".join(c for _, _, c in parts)
        label = labels[k][0]
        if amount is not None and _REWARDS.search(detail):
            kind, amount = "rewards", None
        elif amount is None and _CLOSED.search(region):
            kind = "closed"
        elif amount is None:
            kind = "rewards" if _REWARDS.search(region) else "none"
        else:
            kind = "owed" if _OWED.search(detail) or _OWED.search(label) else "balance"
        items.append(Item(last4=last4, label=label, amount=amount, kind=kind, detail=detail[:120]))

    strays = [j for j, line in enumerate(lines) for mm in _MONEY.finditer(line) if (j, mm.start()) not in claimed]
    unread = [it.last4 for it in items if it.kind == "none"]
    # Totals at the foot of a page are unclaimed too, so only an unclaimed amount among
    # the accounts (or amounts with no accounts at all) says the layout wasn't understood.
    among = [j for j in strays if anchors and j <= anchors[-1][0] + WINDOW_LINES]
    needs_ai = (bool(unread) and bool(among)) or (not items and bool(strays))
    stray = len(strays)
    return Extracted(items=items, unread=unread, stray_amounts=stray, needs_ai=needs_ai)


# ── Scrubbed outline for the AI fallback ─────────────────────────────────────

# Words that describe balances and account types. Anything else (names,
# institutions, marketing copy) is replaced with "~" before the outline leaves Flow.
VOCAB = frozenset("""
account accounts number ending in available current balance balances total value outstanding principal
as of previous close closed paid pd full credit line card loan mortgage auto checking savings
money market cd certificate brokerage investment investments ira roth traditional rollover sep 401k 403b
457 529 hsa fsa managed non rewards reward points miles cash back statement minimum payment due
amount date limit owed payoff pending posted ledger interest rate apr apy assets liabilities
net worth type status open opened region begin end of the a an and for with your on this
acct no retirement pension annuity heloc equity home student personal joint individual trust custodial
fund funds holdings securities portfolio margin deposit deposits share shares loans cards
""".split())

_TOKEN_AMT = "A"
_TOKEN_NUM = "N"


@dataclass
class Outline:
    text: str                              # exactly what is sent
    amounts: dict[str, float]              # token → value (kept in Flow)
    numbers: dict[str, str]                # token → last 4 (kept in Flow)
    labels: dict[str, str]                 # number token → label read locally (kept in Flow)


def _scrub_words(s: str) -> str:
    out = []
    for w in re.findall(r"[A-Za-z]+|\d+|[^\sA-Za-z\d]", s):
        lw = w.lower()
        if lw in VOCAB:
            out.append(lw)
        elif w.isdigit():
            out.append("#")
        elif re.match(r"[^\sA-Za-z\d]", w):
            out.append(w if w in "()$:,.-/" else "")
        else:
            out.append("~")
    s = " ".join(x for x in out if x)
    s = re.sub(r"(?:~\s*){2,}", "~ ", s)
    return s.strip()


def outline(text: str) -> Outline:
    """The paste with amounts and account numbers as tokens and unknown words as ``~``."""
    lines = _lines(text[:MAX_PASTE_CHARS])
    amounts: dict[str, float] = {}
    nums: dict[str, str] = {}
    labels: dict[str, str] = {}
    num_tokens: dict[str, str] = {}
    out_lines: list[str] = []
    anchors = _anchors(lines)
    label_by_num = {a[1]: lab for a, (lab, _at) in zip(anchors, _label_lines(lines, anchors))}
    for line in lines:
        if not line or _NOISE_LINE.match(line):
            continue
        pieces: list[str] = []
        pos = 0
        events = sorted(
            [(m.start(), m.end(), "N", m.group(1) or m.group(2)) for m in _ANCHOR.finditer(line)]
            + [(m.start(), m.end(), "A", m.group(1)) for m in _MONEY.finditer(line)]
        )
        for s, e, kind, val in events:
            if s < pos:
                continue
            pieces.append(_scrub_words(line[pos:s]))
            if kind == "N":
                tok = num_tokens.get(val)
                if tok is None:
                    tok = f"<{_TOKEN_NUM}{len(num_tokens) + 1}>"
                    num_tokens[val] = tok
                    nums[tok] = val
                    labels[tok] = label_by_num.get(val, "")
                pieces.append("ending in " + tok)
            else:
                v = parse_money(val)
                if v is None:
                    continue
                tok = f"<{_TOKEN_AMT}{len(amounts) + 1}>"
                amounts[tok] = v
                pieces.append(tok)
            pos = e
        pieces.append(_scrub_words(line[pos:]))
        scrubbed = re.sub(r"\s+", " ", " ".join(p for p in pieces if p)).strip()
        if scrubbed and scrubbed != "~":
            out_lines.append(scrubbed)
        elif scrubbed == "~" and (not out_lines or out_lines[-1] != "~"):
            out_lines.append("~")
    return Outline(text="\n".join(out_lines)[:20_000], amounts=amounts, numbers=nums, labels=labels)


def items_from_ai(pairs: list[dict], ol: Outline) -> list[Item]:
    """Turn the model's token pairs back into items. Unknown tokens are ignored; each amount is used once."""
    items: list[Item] = []
    used: set[str] = set()
    seen: set[str] = set()
    for p in pairs:
        ntok = str(p.get("account", "")).strip()
        if ntok not in ol.numbers or ntok in seen:
            continue
        seen.add(ntok)
        kind = p.get("kind") if p.get("kind") in ("balance", "owed", "rewards", "closed", "none") else "none"
        atok = p.get("amount")
        amount = None
        if kind in ("balance", "owed") and atok in ol.amounts and atok not in used:
            used.add(atok)
            amount = ol.amounts[atok]
        if amount is None and kind in ("balance", "owed"):
            kind = "none"
        items.append(Item(last4=ol.numbers[ntok], label=ol.labels.get(ntok, ""), amount=amount, kind=kind,
                          source="ai"))
    return items


def _key(it: Item) -> str:
    """Items with an account number are one per number; a marker's item without one is one per account."""
    return it.last4 or (f"account:{it.account_id}" if it.account_id is not None else f"label:{it.label}")


def merge_items(local: list[Item], ai: list[Item]) -> list[Item]:
    """Local readings (markers included) stand; the AI only fills accounts the local reader couldn't."""
    by = {_key(it): it for it in local}
    for it in ai:
        cur = by.get(_key(it))
        if cur is None:
            by[_key(it)] = it
        elif cur.kind == "none" and it.kind != "none":
            it.label = it.label or cur.label
            by[_key(it)] = it
    local_keys = [_key(it) for it in local]
    order = local_keys + [_key(it) for it in ai if _key(it) not in set(local_keys)]
    return [by[k] for k in dict.fromkeys(order)]


# ── Paste markers: where an account's balance sits, taught by example ────────
#
# A marker belongs to one balance-sheet account: the text of the line naming it
# (amounts and long numbers left out, since they change), optionally its last 4
# digits (which must then appear near that line), and how to find the amount
# from there: the first amount after the label ("first_after"), or the first
# amount after a phrase that follows it, such as "Total value" ("on_line_with").
# Markers are read before the anchored reader and win over it; the AI fills only
# what neither found. Everything here runs on Flow's server: nothing is sent.

AMOUNT_RULES = ("first_after", "on_line_with")
_DIGIT_RUN = re.compile(r"\d{4,}")


@dataclass
class Marker:
    account_id: int
    label_text: str
    last4: str | None = None
    amount_rule: str = "first_after"
    amount_label: str | None = None
    archived: bool = False          # the account's; an archived account's markers are not read


def _norm_text(s: str) -> str:
    return re.sub(r"\s+", " ", s or "").strip().lower()


def marker_label(line: str) -> str:
    """A line's text as a marker label: no amounts, no long numbers, no list marks."""
    t = _MONEY.sub(" ", line)
    t = _ANCHOR.sub(" ", t)
    t = _DIGIT_RUN.sub(" ", t)
    return clean_label(t)


def _first_money(lines: list[str], line: int, col: int, until_line: int) -> tuple[int, int, float] | None:
    """(line, column, amount) of the first amount at or after (line, col), no further than until_line."""
    for j in range(line, min(until_line, len(lines) - 1) + 1):
        start = col if j == line else 0
        mm = _MONEY.search(lines[j], start)
        if mm:
            v = parse_money(mm.group(1))
            if v is not None:
                return j, mm.start(1), v
    return None


def _has_last4(lines: list[str], first: int, last: int, last4: str) -> bool:
    pat = re.compile(rf"(?<!\d){re.escape(last4)}(?!\d)")
    return any(pat.search(lines[j]) for j in range(max(first, 0), min(last, len(lines) - 1) + 1))


def _apply_marker(lines: list[str], m: Marker) -> tuple[int, int, int, float] | None:
    """(label line, amount line, amount column, amount) the marker finds in a paste, or None.

    Lines are already whitespace-collapsed (_lines), so a label is found with a plain
    case-insensitive search; the amount is the first one after the label (first_after) or
    after the marker's phrase within the window below it (on_line_with)."""
    want = _norm_text(m.label_text)
    if len(want) < 3:
        return None
    for i, line in enumerate(lines):
        k = line.lower().find(want)
        if k < 0:
            continue
        if m.last4 and not _has_last4(lines, i - 1, i + WINDOW_LINES, m.last4):
            continue
        last = i + WINDOW_LINES
        if m.amount_rule == "on_line_with":
            phrase = _norm_text(m.amount_label or "")
            if not phrase:
                return None
            for j in range(i, min(last, len(lines) - 1) + 1):
                start = k + len(want) if j == i else 0
                p = lines[j].lower().find(phrase, start)
                if p >= 0:
                    hit = _first_money(lines, j, p + len(phrase), last)
                    if hit:
                        return i, hit[0], hit[1], hit[2]
                    break
            continue
        hit = _first_money(lines, i, k + len(want), last)
        if hit:
            return i, hit[0], hit[1], hit[2]
    return None


def read_with_markers(text: str, markers: list[Marker]) -> list[Item]:
    """One item per marker that finds its account's amount in the paste (archived accounts skipped)."""
    lines = _lines(text[:MAX_PASTE_CHARS])
    items: list[Item] = []
    at: dict[tuple[int, int], int] = {}             # amount position -> index in items
    for m in markers:                               # oldest first: a later marker for the same spot wins
        if m.archived:
            continue
        found = _apply_marker(lines, m)
        if found is None:
            continue
        li, aj, acol, amount = found
        if (aj, acol) in at:
            items[at[(aj, acol)]] = None            # an amount belongs to one account: the newest marker's
        at[(aj, acol)] = len(items)
        between = " ".join(lines[li:aj + 1]) if aj > li else lines[li]
        detail = _clean_line(_MONEY.sub(" ", between))[:120]
        kind = "owed" if _OWED.search(detail) else "balance"
        items.append(Item(last4=m.last4 or "", label=clean_label(lines[li])[:80] or m.label_text, amount=amount,
                          kind=kind, detail=detail, source="marker", account_id=m.account_id))
    return [it for it in items if it is not None]


def same_spot(text: str, a: Marker, b: Marker) -> bool:
    """Whether two markers find the same amount in this paste."""
    lines = _lines(text[:MAX_PASTE_CHARS])
    fa, fb = _apply_marker(lines, a), _apply_marker(lines, b)
    return fa is not None and fb is not None and fa[1:3] == fb[1:3]


def combine(marker_items: list[Item], local: list[Item]) -> list[Item]:
    """Markers win: a local reading of the same account number (or label) gives way to the marker's."""
    taken_num = {it.last4 for it in marker_items if it.last4}
    taken_label = {_norm_text(it.label) for it in marker_items}
    out, placed = [], set()
    for it in local:
        rep = next((m for m in marker_items if id(m) not in placed and
                    ((it.last4 and m.last4 == it.last4) or (not m.last4 and _norm_text(m.label) == _norm_text(it.label)))),
                   None)
        if rep is not None:
            out.append(rep)
            placed.add(id(rep))
        elif not (it.last4 and it.last4 in taken_num) and _norm_text(it.label) not in taken_label:
            out.append(it)
    out += [m for m in marker_items if id(m) not in placed]
    return out


class MarkerError(ValueError):
    pass


def marker_from_example(text: str, account_id: int, label_line: int, amount_line: int, amount_start: int) -> Marker:
    """
    The marker for "this line names the account, that is its balance" (two clicks in the
    preview). Refused unless reading the same paste with it finds that same amount.
    """
    lines = _lines(text[:MAX_PASTE_CHARS])
    if not (0 <= label_line < len(lines) and 0 <= amount_line < len(lines)):
        raise MarkerError("Those lines aren't in the paste.")
    if not label_line <= amount_line <= label_line + WINDOW_LINES:
        raise MarkerError(f"The amount must be on the account's line or within {WINDOW_LINES} lines below it.")
    target = _MONEY.search(lines[amount_line], max(0, amount_start - 2))
    if not target:
        raise MarkerError("Click an amount.")
    want = (amount_line, target.start(1), parse_money(target.group(1)))
    label = marker_label(lines[label_line])
    if len(_norm_text(label)) < 3:
        raise MarkerError("Pick the line with the account's name (it has no words to recognise).")
    nums = [m.group(1) or m.group(2) for j in range(label_line, amount_line + 1) for m in _ANCHOR.finditer(lines[j])]
    last4 = nums[0] if nums else None
    candidates = [Marker(account_id, label, last4, "first_after")]
    words = marker_label(lines[amount_line][:target.start(1)])
    if amount_line > label_line and len(_norm_text(words)) >= 3:
        candidates.append(Marker(account_id, label, last4, "on_line_with", words))
    for j in range(amount_line - 1, label_line, -1):          # a phrase on a line above the amount
        w = marker_label(lines[j])
        if len(_norm_text(w)) >= 3 and _norm_text(w) != _norm_text(label):
            candidates.append(Marker(account_id, label, last4, "on_line_with", w))
            break
    for m in candidates:
        got = _apply_marker(lines, m)
        if got and got[0] == label_line and (got[1], got[2], got[3]) == want:
            return m
    raise MarkerError("Flow couldn't make a rule that finds that amount again from that line: pick the line "
                      "with the account's name just above the amount.")


def marker_for_item(text: str, account_id: int, last4: str, amount: float) -> Marker | None:
    """A marker reproducing an AI reading (account number and amount), or None if none does."""
    lines = _lines(text[:MAX_PASTE_CHARS])
    for li, n, _s, end in _anchors(lines):
        if n != last4:
            continue
        label_line = li
        before = marker_label(lines[li][:_s])
        if len(_norm_text(before)) < 3:
            label_line = next((j for j in range(li - 1, max(li - 3, -1), -1)
                               if len(_norm_text(marker_label(lines[j]))) >= 3), li)
        for j in range(li, min(li + WINDOW_LINES, len(lines) - 1) + 1):
            for mm in _MONEY.finditer(lines[j], end if j == li else 0):
                if parse_money(mm.group(1)) is not None and abs(abs(parse_money(mm.group(1))) - abs(amount)) < 0.005:
                    try:
                        return marker_from_example(text, account_id, label_line, j, mm.start(1))
                    except MarkerError:
                        return None
    return None


def paste_lines(text: str) -> list[dict]:
    """The paste as the teach view shows it: each cleaned line and the amounts in it (start, end, value)."""
    lines = _lines(text[:MAX_PASTE_CHARS])
    def span(mm):
        g = mm.group(1)
        return mm.start(1) + len(g) - len(g.lstrip()), mm.end(1)
    return [{"text": l, "amounts": [{"start": span(mm)[0], "end": span(mm)[1], "value": parse_money(mm.group(1))}
                                    for mm in _MONEY.finditer(l) if parse_money(mm.group(1)) is not None]}
            for l in lines]


# ── Validation / preview ─────────────────────────────────────────────────────

DEFAULT_ALERT_PCT = 10.0        # % change vs previous value
DEFAULT_ALERT_ABS = 50_000.0    # $ change vs previous value
MAX_AMOUNT = 1e12


@dataclass
class AccountInfo:
    id: int
    name: str
    account_type: str           # 'asset' | 'liability'
    last4: list[str] = field(default_factory=list)
    archived: bool = False
    ignored: bool = False
    alert_pct: float | None = None
    alert_abs: float | None = None
    previous: tuple[date, float] | None = None   # latest balance before the target period
    existing: float | None = None                # balance already stored for the target period


@dataclass
class Thresholds:
    pct: float = DEFAULT_ALERT_PCT
    abs: float = DEFAULT_ALERT_ABS


def signed_value(account_type: str, shown: float, kind: str = "balance") -> float:
    """Liabilities are stored negative. Banks show what you owe as a positive balance."""
    if account_type == "liability":
        return -abs(shown) if kind == "owed" or shown >= 0 else abs(shown)
    return shown


def change_alert(acct: AccountInfo, value: float, th: Thresholds) -> str | None:
    if acct.previous is None:
        return None
    prev_period, prev = acct.previous
    pct_lim = acct.alert_pct if acct.alert_pct is not None else th.pct
    abs_lim = acct.alert_abs if acct.alert_abs is not None else th.abs
    diff = value - prev
    pct = abs(diff) / abs(prev) * 100 if prev else (math.inf if diff else 0.0)
    hits = []
    if pct_lim is not None and pct_lim > 0 and pct > pct_lim:
        hits.append(f"{'+' if diff >= 0 else '-'}{pct:.1f}%" if pct != math.inf else "from zero")
    if abs_lim is not None and abs_lim > 0 and abs(diff) > abs_lim:
        hits.append(f"{'+' if diff >= 0 else '-'}${abs(diff):,.0f}")
    if not hits:
        return None
    return f"Large change vs {prev_period.isoformat()} ({', '.join(hits)})."


@dataclass
class Entry:
    """One proposed balance: from a paste (last4 set) or typed (account_id set)."""
    amount: float | None
    last4: str = ""
    label: str = ""
    kind: str = "balance"
    detail: str = ""
    account_id: int | None = None      # chosen by the user (typed entry or a first-time link)
    source: str = "local"


def plan(entries: list[Entry], accounts: list[AccountInfo], period: date, today: date,
         latest_period: date | None, th: Thresholds) -> dict:
    """
    The preview: one row per entry with the matched account, the value to store,
    the previous value, and any warnings or errors. Rows with an error can't be
    applied; rows with warnings start ticked but are flagged. Pure.
    """
    by_id = {a.id: a for a in accounts}
    by_num: dict[str, list[AccountInfo]] = {}
    for a in accounts:
        for n in a.last4:
            by_num.setdefault(n, []).append(a)

    period_errors = []
    period_warnings = []
    p = period_problem(period, today)
    if p:
        period_errors.append(p)
    if latest_period and period < latest_period:
        period_warnings.append(f"{period.isoformat()} is before the latest period ({latest_period.isoformat()}): "
                               "this changes history.")

    rows = []
    used_accounts: dict[int, int] = {}
    for i, e in enumerate(entries):
        row = {"index": i, "last4": e.last4, "label": e.label, "kind": e.kind, "detail": e.detail,
               "shown": e.amount, "source": e.source, "account_id": None, "account_name": None,
               "value": None, "previous": None, "existing": None, "warnings": [], "errors": [],
               "skip": None, "link": False, "checked": False}
        acct = None
        if e.account_id is not None:
            acct = by_id.get(e.account_id)
            if acct is None:
                row["errors"].append("That account doesn't exist.")
            elif e.last4 and e.last4 not in acct.last4:
                others = [a.name for a in by_num.get(e.last4, []) if a.id != acct.id]
                if others:
                    row["errors"].append(f"…{e.last4} is saved on {others[0]}.")
                else:
                    row["link"] = True      # applying saves this number on the account
        elif e.last4:
            hits = by_num.get(e.last4, [])
            if len(hits) > 1:
                row["errors"].append(f"…{e.last4} is saved on more than one account: pick one.")
            elif hits:
                acct = hits[0]
        if acct is None and not row["errors"]:
            if e.kind in ("rewards", "none", "closed"):
                row["skip"] = {"rewards": "Rewards or points, not money.",
                               "none": "No balance shown for this account.",
                               "closed": "Shown as closed or paid in full."}[e.kind]
            else:
                row["skip"] = "Not on your balance sheet (pick an account to link it, or leave it)."
        if acct is not None:
            row["account_id"], row["account_name"] = acct.id, acct.name
            if acct.archived:
                row["warnings"].append("This account is archived.")
            if acct.ignored:
                row["warnings"].append("This account is ignored on the dashboard.")
            if acct.previous:
                row["previous"] = {"period": acct.previous[0].isoformat(), "amount": acct.previous[1]}
            row["existing"] = acct.existing
            if e.kind == "closed":
                row["skip"] = "The bank shows this account closed or paid in full: enter 0 and archive it if so."
            elif e.kind == "rewards":
                row["skip"] = "Rewards or points, not money."
            elif e.amount is None:
                row["skip"] = "No balance found for this account."
            elif not math.isfinite(e.amount) or abs(e.amount) > MAX_AMOUNT:
                row["errors"].append("That amount isn't a usable number.")
            else:
                value = round(signed_value(acct.account_type, e.amount, e.kind), 2)
                row["value"] = value
                if acct.account_type == "asset" and e.kind == "owed":
                    row["warnings"].append("The bank calls this an amount owed, but the account is an asset.")
                alert = change_alert(acct, value, th)
                if alert:
                    row["warnings"].append(alert)
                if acct.existing is not None:
                    if round(acct.existing, 2) == value:
                        row["skip"] = "Already stored for this period."
                    else:
                        row["warnings"].append(f"Replaces ${acct.existing:,.2f} already stored for this period.")
                if acct.id in used_accounts:
                    row["errors"].append(f"{acct.name} appears twice in this update.")
                    rows[used_accounts[acct.id]]["errors"].append(f"{acct.name} appears twice in this update.")
                used_accounts.setdefault(acct.id, i)
        rows.append(row)

    for row in rows:
        row["checked"] = (row["value"] is not None and not row["errors"] and not row["skip"]
                          and not period_errors)
        row["applicable"] = row["value"] is not None and not row["errors"] and not period_errors

    matched = {r["account_id"] for r in rows if r["account_id"] is not None}
    missing = [{"id": a.id, "name": a.name, "last4": a.last4,
                "carried": a.previous[1] if a.previous else None}
               for a in accounts
               if a.last4 and a.id not in matched and not a.archived and not a.ignored]
    return {"period": period.isoformat(), "period_errors": period_errors, "period_warnings": period_warnings,
            "rows": rows, "not_in_paste": missing}


# ── Balance files (an import format of kind "balance") ───────────────────────

def assign_accounts(items: list[Item], accounts: list[AccountInfo], markers: list[Marker]) -> list[Item]:
    """
    Which account each row of a balance file belongs to: its last 4 digits when an account
    lists them (the preview matches those itself), else a paste marker whose label is the
    row's account name, else an account with exactly that name. Unmatched rows stay for the
    user to pick in the preview. Archived accounts are not matched by name or marker.
    """
    numbers = {n for a in accounts for n in a.last4}
    by_marker = {_norm_text(m.label_text): m.account_id for m in markers if not m.archived}
    by_name = {_norm_text(a.name): a.id for a in accounts if not a.archived}
    for it in items:
        if it.account_id is not None or (it.last4 and it.last4 in numbers):
            continue
        key = _norm_text(it.label)
        it.account_id = by_marker.get(key) or by_name.get(key)
    return items
