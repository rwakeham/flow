"""
Drafting an import format from a sample file, and the outline the Assistant sees.

``infer`` looks at a file nobody has described yet and proposes a ``Spec``: the
delimiter, where the header is (skipping summary blocks above it), what each
column holds, how amounts and dates are written and which way the sign runs.
It is only ever a draft: the setup screen shows it with a live preview and
nothing imports until the user saves it. Each guess carries a confidence, and
anything Flow can't settle from the file is listed in ``needs_confirmation``.

The header vocabulary below is words, not layouts: no bank or issuer is named
and no file is recognised by who produced it.

``outline`` is the only description of a file that may leave the machine (for
the Assistant): cleaned header cells and an enumerated shape per column, never
a cell's value.

Pure: no database, no network.
"""

from __future__ import annotations

import re
import statistics
from dataclasses import dataclass, field
from datetime import date

import import_profile as ip

# Header words for each role, best first. Matched against the whole normalized cell.
VOCAB: dict[str, tuple[str, ...]] = {
    "date": ("date", "transaction date", "trans date", "trans. date", "txn date", "posting date", "posted date",
             "post date", "booking date", "value date", "purchase date", "date posted", "effective date"),
    "post_date": ("post date", "posted date", "posting date", "post. date", "date posted", "settlement date"),
    "description": ("description", "transaction description", "payee", "merchant", "merchant name", "name",
                    "narrative", "details", "transaction details", "memo/description", "reference/description"),
    "memo": ("memo", "notes", "note", "additional info", "comments"),
    "amount": ("amount", "transaction amount", "amount (usd)", "amt", "billing amount", "value", "net amount"),
    "debit": ("debit", "debits", "withdrawal", "withdrawals", "money out", "paid out", "charges", "charge",
              "debit amount", "withdrawal amount", "outflow"),
    "credit": ("credit", "credits", "deposit", "deposits", "money in", "paid in", "payments", "credit amount",
               "deposit amount", "inflow"),
    "running_balance": ("balance", "running balance", "running bal.", "running bal", "account balance",
                        "available balance", "ledger balance"),
    "check_number": ("check #", "check number", "check no.", "check no", "check or slip #", "chk #", "cheque number"),
    "status": ("status", "transaction status"),
    "direction": ("details", "type", "transaction type", "dr/cr", "debit/credit", "cr/dr", "direction"),
    "reconciled": ("reconciled", "cleared", "r", "clr"),
    "card_number": ("card no.", "card no", "card number", "account #", "account number", "last 4", "card"),
    "cardholder": ("card member", "cardmember", "cardholder", "card holder", "name on card", "member name"),
    "issuer_category": ("category", "merchant category", "mcc description"),
    "issuer_type": ("type", "transaction type"),
    "reference": ("reference", "reference number", "transaction id", "ref"),
    "extended": ("extended details", "additional details"),
    "statement_as": ("appears on your statement as",),
    "city_state": ("city/state", "city", "location"),
    "country": ("country",),
    # balance files
    "account_ref": ("account", "account number", "account name", "account #", "acct", "symbol", "holding"),
    "value": ("market value", "current value", "value", "total value", "balance", "current balance", "amount"),
    "as_of": ("as of", "as of date", "date", "price date"),
}
OUT_WORDS = {"debit", "dr", "withdrawal", "withdrawals", "payment", "purchase", "sale", "out", "dbt", "d"}
IN_WORDS = {"credit", "cr", "deposit", "deposits", "refund", "in", "crd", "c", "return"}
PENDING_WORDS = {"pending", "authorized", "authorised", "processing"}
CARD_HINTS = {"card member", "cardmember", "card no.", "card no", "card number", "appears on your statement as",
              "name on card", "member name", "mcc description"}


@dataclass
class Column:
    index: int
    header: str
    filled: int = 0
    dates: dict = field(default_factory=dict)  # format -> count parsed
    day_first: int = 0                          # first part > 12 (only DMY reads it)
    month_first: int = 0                        # second part > 12 (only MDY reads it)
    iso: int = 0
    money: int = 0
    money_comma: int = 0                        # parses only with decimal comma
    negatives: int = 0
    positives: int = 0
    values: list = field(default_factory=list)

    @property
    def date_share(self) -> float:
        return max(self.dates.values(), default=0) / self.filled if self.filled else 0.0

    @property
    def money_share(self) -> float:
        return max(self.money, self.money_comma) / self.filled if self.filled else 0.0

    @property
    def distinct(self) -> set:
        return {str(v).strip().lower() for v in self.values if str(v).strip()}

    @property
    def text_share(self) -> float:
        texty = sum(1 for v in self.values if isinstance(v, str) and re.search(r"[^\W\d_]", v))
        return texty / self.filled if self.filled else 0.0


@dataclass
class Draft:
    spec: ip.Spec
    kind: str
    confidence: dict                  # setting -> "high" | "low"
    needs_confirmation: list[str]     # settings the user must look at before saving
    notes: list[str]
    header_row: int | None
    grid: list[list[str]]             # first rows, as the setup screen shows them

    def to_json(self) -> dict:
        return {"spec": self.spec.to_json(), "kind": self.kind, "confidence": self.confidence,
                "needs_confirmation": self.needs_confirmation, "notes": self.notes,
                "header_row": self.header_row, "grid": self.grid}


class InferError(ValueError):
    pass


# ── Reading ──────────────────────────────────────────────────────────────────

def _sheets(content: bytes, filename: str) -> tuple[str, list[tuple[str, list[list]]], str]:
    if ip.is_xlsx(content, filename):
        return "xlsx", ip.read_sheets(content, filename, ip.Spec(file_type="xlsx")), ""
    text = ip.decode(content)
    delim = ip.detect_delimiter(text)
    spec = ip.Spec(delimiter=delim)
    return "delimited", ip.read_sheets(content, filename, spec), delim


_CURRENCY_TAIL = re.compile(r"\s*(?:[(\[][^)\]]*[)\]]|[$£€¥])\s*$")
_CURRENCY_HEAD = re.compile(r"^\s*[$£€¥]\s*")


def vocab_key(cell) -> str:
    """A header cell as compared with VOCAB: ``norm`` without a trailing currency or unit note,
    so "Deposits ($)", "Amount (CAD)", "Withdrawals [GBP]" and "Balance $" read as the plain
    word. (Missing these used to drop the header row and read the columns by position, where
    the first money column is taken as money out: every sign of a deposits-first file flipped.)"""
    key = ip.norm(cell)
    for _ in range(2):                                    # "Amount ($) (CAD)"
        key = _CURRENCY_TAIL.sub("", key)
    return _CURRENCY_HEAD.sub("", key).strip()


def _vocab_hits(row: list) -> dict[str, int]:
    """role -> column index of the header cell that names it best (earliest word in VOCAB wins)."""
    raw = [ip.norm(c) for c in row]
    keys = [vocab_key(c) for c in row]
    hits: dict[str, int] = {}
    for role, words in VOCAB.items():
        for w in words:
            if w in raw:
                hits[role] = raw.index(w)
                break
            if w in keys:
                hits[role] = keys.index(w)
                break
    return hits


_MONEY_TEXT = re.compile(r"^[^\w\d]*[-+(]?[^\w\d]*\d[\d.,' ]*\)?-?\s*$")
_DECIMALS = re.compile(r"[.,]\d{1,2}\)?-?\s*$")
# A whole number, plain or in groups of three with commas: "2500", "-950", "2,500", "(1,234)".
# Only accepted in a column the header names as an amount (a whole number elsewhere is a check
# number or a reference). Groups with dots ("1.234") stay out: a US file would read them as 1.234.
_WHOLE_MONEY = re.compile(r"^[^\w\d]*[-+(]?[^\w\d]*(?:\d{1,3}(?:,\d{3})+|\d+)\)?-?\s*$")
# Header roles whose column may hold whole-number amounts (some banks and most JPY exports drop ".00").
NAMED_MONEY_ROLES = ("amount", "debit", "credit", "running_balance", "value")


def _looks_money(v, whole_ok: bool = False) -> bool:
    """A number written like money (a check number or a card number is not: no decimals, unless
    ``whole_ok``: the header names this column as an amount)."""
    if isinstance(v, bool):
        return False
    if isinstance(v, (int, float)):        # a spreadsheet's number cell (openpyxl gives -500.0 as -500)
        return True
    t = ip.cell_text(v).strip()
    if not t or ip.parse_date(t, "MDY") is not None or ip.parse_date(t, "DMY") is not None:
        return False
    if _MONEY_TEXT.match(t) and _DECIMALS.search(t):
        return True
    return whole_ok and bool(_WHOLE_MONEY.match(t))


def _header_index(rows: list[list]) -> int | None:
    """The first row (in the first 40) that names a date and an amount-like column and has data under it."""
    for i, row in enumerate(rows[:ip.HEADER_SEARCH_ROWS]):
        hits = _vocab_hits(row)
        has_date = "date" in hits or "as_of" in hits
        has_money = any(r in hits for r in ("amount", "debit", "credit", "value", "running_balance"))
        if not (has_date and has_money) and not ("account_ref" in hits and "value" in hits):
            continue
        if any(ip.parse_date(c, f) for f in ip.DATE_FORMATS for c in row):
            continue  # a data row, not a header
        below = rows[i + 1:i + 6]
        if any(any(ip.parse_date(c, f) for f in ip.DATE_FORMATS for c in r) or any(_looks_money(c) for c in r)
               for r in below):
            return i
    return None


def _profile(rows: list[list], width: int, header: list[str]) -> list[Column]:
    cols = [Column(i, header[i] if i < len(header) else "") for i in range(width)]
    named_hits = _vocab_hits(header) if header else {}
    named_money = {named_hits[r] for r in NAMED_MONEY_ROLES if r in named_hits}
    for row in rows[:300]:
        for c in cols:
            v = row[c.index] if c.index < len(row) else None
            t = ip.cell_text(v).strip()
            if not t and not isinstance(v, (int, float)):
                continue
            c.filled += 1
            if len(c.values) < 300:
                c.values.append(v)
            for f in ip.DATE_FORMATS:
                if ip.parse_date(v, f):
                    c.dates[f] = c.dates.get(f, 0) + 1
            m = re.match(r"^\s*(\d{1,2})[/.-](\d{1,2})[/.-]\d{2,4}", t)
            if m:
                a, b = int(m.group(1)), int(m.group(2))
                c.day_first += a > 12
                c.month_first += b > 12
            if re.match(r"^\s*\d{4}-\d{1,2}-\d{1,2}", t):
                c.iso += 1
            if not _looks_money(v, whole_ok=c.index in named_money):
                continue
            comma_decimal = isinstance(v, str) and bool(re.search(r",\d{1,2}\)?-?\s*$", t)) \
                and not re.search(r"\.\d{1,2}\)?-?\s*$", t)
            val = ip.parse_amount(v, ",", ".") if comma_decimal else ip.parse_amount(v, ".", ",")
            if val is None:
                continue
            if comma_decimal:
                c.money_comma += 1
            else:
                c.money += 1
            c.negatives += val < 0
            c.positives += val > 0
    return cols


# ── Inference ────────────────────────────────────────────────────────────────

_OFX_EXTENSIONS = (".ofx", ".qfx", ".qif", ".qbo", ".iif")
_OFX_HEAD = re.compile(rb"OFXHEADER|<OFX>|<\?OFX|^\s*!(Type|Account|Option)", re.I | re.M)
NOT_A_SPREADSHEET = ("Flow reads CSV, TSV and XLSX exports only, not OFX, QFX or QIF: "
                     "export the statement as CSV from your bank instead.")


def looks_like_ofx(content: bytes, filename: str) -> bool:
    """An OFX / QFX / QIF (Quicken, Money) file, by extension or by its first lines."""
    return (filename or "").lower().endswith(_OFX_EXTENSIONS) or bool(_OFX_HEAD.search(content[:4096]))


def infer(content: bytes, filename: str, kind_hint: str | None = None) -> Draft:
    if looks_like_ofx(content, filename):
        raise InferError(NOT_A_SPREADSHEET)
    file_type, sheets, delim = _sheets(content, filename)
    notes: list[str] = []
    conf: dict[str, str] = {}
    confirm: list[str] = []

    sheet_name, rows, header_idx = (sheets[0] if sheets else ("", [])) + (None,)
    for name, candidate in sheets:
        idx = _header_index(candidate)
        if idx is not None:
            sheet_name, rows, header_idx = name, candidate, idx
            break
    if not rows:
        raise InferError("The file has no rows.")
    header = ip.trimmed_header(rows[header_idx]) if header_idx is not None else []
    data = rows[header_idx + 1:] if header_idx is not None else rows
    if header_idx is None:
        start = next((i for i, r in enumerate(data)
                      if any(ip.parse_date(c, f) for f in ip.DATE_FORMATS for c in r)), None)
        if start is None:
            raise InferError("No dates found: this doesn't look like a statement (a CSV, TSV or XLSX export "
                             "with a date on every row).")
        data = data[start:]
        notes.append("No header row found; columns are read by position.")
    elif header_idx > 0:
        notes.append(f"The header is on row {header_idx + 1}; the rows above it are skipped.")
    width = max((len(r) for r in data[:300]), default=0)
    cols = _profile(data, width, header)
    hits = _vocab_hits(header) if header else {}
    by_index = {c.index: c for c in cols}

    kind = kind_hint or _guess_kind(header, hits, cols)
    roles: dict[str, list[int]] = {}

    def take(role: str, idx: int | None) -> None:
        if idx is not None and all(idx not in v for v in roles.values()):
            roles[role] = [idx]

    if kind == "balance":
        return _infer_balance(file_type, delim, sheet_name, header_idx, header, hits, cols, rows, notes)

    # Date
    date_idx = hits.get("date")
    # A column the header calls a date may hold a few footer rows; one found by shape alone must be all dates.
    if date_idx is None or by_index.get(date_idx, Column(0, "")).date_share < 0.6:
        date_idx = next((c.index for c in cols if c.date_share >= 0.8), None)
    if date_idx is None:
        raise InferError("No date column found.")
    take("date", date_idx)
    dc = by_index[date_idx]
    if dc.iso >= dc.filled * 0.8:
        date_format = "YMD"
    elif dc.day_first and not dc.month_first:
        date_format = "DMY"
    elif dc.month_first and not dc.day_first:
        date_format = "MDY"
    else:
        date_format = "MDY"
        if not any(isinstance(v, date) for v in dc.values) and dc.dates.get("MDY") and dc.dates.get("DMY"):
            confirm.append("date_format")
            notes.append("Dates could be month-first or day-first; month-first is assumed. Check a date above the 12th.")
    conf["date_format"] = "low" if "date_format" in confirm else "high"
    post = hits.get("post_date")
    if post is not None and post != date_idx and by_index[post].date_share >= 0.8:
        take("post_date", post)

    # Money columns
    money = [c for c in cols if c.money_share >= 0.6 and c.index not in roles.get("date", [])
             and c.date_share < 0.5]
    decimal, thousands = ".", ","
    if money and sum(c.money_comma for c in money) > sum(c.money for c in money):
        decimal, thousands = ",", "."
        notes.append("Amounts use a decimal comma (1.234,56).")
    rb = hits.get("running_balance")
    if rb is not None and rb in by_index and by_index[rb].money_share >= 0.6:
        take("running_balance", rb)
    debit, credit = hits.get("debit"), hits.get("credit")
    amount = hits.get("amount")
    amount_mode, sign = "single", "negative_is_out"
    direction_out: list[str] = []
    direction_in: list[str] = []
    if debit is not None and credit is not None and debit != credit:
        amount_mode = "debit_credit"
        take("debit", debit)
        take("credit", credit)
        conf["amount_mode"] = "high"
    elif amount is not None and amount in by_index and by_index[amount].money_share >= 0.6:
        take("amount", amount)
    else:
        free = [c for c in money if all(c.index not in v for v in roles.values())]
        pair = _exclusive_pair(free, data)
        if pair and not any(c.negatives for c in pair):
            amount_mode = "debit_credit"
            take("debit", pair[0].index)
            take("credit", pair[1].index)
            confirm.append("debit_credit")
            notes.append("Two amount columns that are never both filled: the first is taken as money out. Check it.")
        elif free:
            take("amount", free[0].index)
            conf["amount"] = "low"
        else:
            named = next((header[hits[r]] for r in ("amount", "debit", "credit")
                          if r in hits and hits[r] < len(header)), None)
            if named:
                raise InferError(f"No amount column found: {named!r} doesn't hold numbers written like money "
                                 "(12.00, 1,234.56 or 1.234,56), so Flow can't tell its values from reference "
                                 "numbers. If it is the amount column, give it the Amount role.")
            raise InferError("No amount column found.")

    # Amount always positive + a column saying in/out
    if amount_mode == "single":
        ac = by_index[roles["amount"][0]]
        if ac.positives and not ac.negatives:
            for c in cols:
                if any(c.index in v for v in roles.values()) or not c.filled:
                    continue
                vals = c.distinct
                if 1 <= len(vals) <= 6 and vals & OUT_WORDS and vals & IN_WORDS:
                    amount_mode = "amount_plus_direction"
                    take("direction", c.index)
                    direction_out = sorted(v for v in vals if v in OUT_WORDS)
                    direction_in = sorted(v for v in vals if v in IN_WORDS)
                    notes.append(f"Amounts are all positive; column {c.header or c.index + 1!r} says which way money moved.")
                    break
            if amount_mode == "single":
                # No English direction words, but a two-valued text column (Débit / Crédit, D / C in
                # another language) is probably the direction: point at the layout to choose, or
                # the sign question's one answer flips deposits and withdrawals alike.
                two = next((c for c in cols if not any(c.index in v for v in roles.values())
                            and c.filled >= max(1, len(data[:300])) * 0.9 and len(c.distinct) == 2
                            and all(len(ip.cell_text(v)) <= 12 for v in c.values)), None)
                if two is not None:
                    a, b = sorted(two.distinct)
                    notes.append(f"Every amount is positive and column {two.header or two.index + 1!r} has two values "
                                 f"({a}, {b}). If it says which way money moved, choose \"one column, with a column "
                                 "saying in or out\" under Amounts and enter the two words; otherwise answer the "
                                 "question in the preview.")

    # Description
    desc = hits.get("description")
    if desc is not None and by_index.get(desc) and by_index[desc].text_share >= 0.5 \
            and all(desc not in v for v in roles.values()):
        take("description", desc)
    else:
        cands = [c for c in cols if all(c.index not in v for v in roles.values()) and c.text_share >= 0.5]
        cands.sort(key=lambda c: (len(c.distinct), statistics.mean(len(ip.cell_text(v)) for v in c.values) if c.values else 0),
                   reverse=True)
        if cands:
            take("description", cands[0].index)
            conf["description"] = "low"
    if "description" not in roles:
        raise InferError("No description column found.")

    # Everything else named by its header
    # Only roles the kind allows (Spec.validate refuses the rest): a bank layout with a check-number
    # column can still be drafted for a card account, with that column left unassigned.
    optional_roles = ["memo", "status"] + (
        ["card_number", "cardholder", "issuer_category", "issuer_type", "reference", "extended",
         "statement_as", "city_state", "country"] if kind == "card" else ["check_number", "reconciled"])
    for role in optional_roles:
        if role in hits and role not in roles:
            take(role, hits[role])
    if kind == "cash" and "running_balance" not in roles and hits.get("running_balance") is not None:
        take("running_balance", hits["running_balance"])

    skip_status: list[str] = []
    if "status" in roles:
        vals = by_index[roles["status"][0]].distinct
        skip_status = sorted(vals & PENDING_WORDS)
        if skip_status:
            notes.append(f"Rows marked {', '.join(skip_status)} are skipped until they post.")

    spec = ip.Spec(
        kind=kind, file_type=file_type, delimiter=delim or "auto", quote_char='"' if delim and delim != "\t" else "auto",
        sheet=sheet_name if file_type == "xlsx" else None, has_header=header_idx is not None,
        header_row=header_idx, header_cells=header,
        columns={role: [ip.ColumnRef(header=header[i] if header_idx is not None and i < len(header) else None,
                                     index=i) for i in idxs] for role, idxs in roles.items()},
        amount_mode=amount_mode, sign=sign if amount_mode == "single" else None,
        direction_out_values=direction_out, direction_in_values=direction_in,
        decimal=decimal, thousands=thousands, date_format=date_format, skip_status_values=skip_status,
        collapse_whitespace=kind != "card",
    )
    if header_idx and any(ip._PERIOD_RE.search(ip.cell_text(c)) for r in rows[:header_idx] for c in r):
        spec.preamble = {"period": True}
        notes.append("The statement period is read from the rows above the header.")
    footer = _footer_rows(data, roles["date"][0], date_format)
    if footer:
        spec.skip_footer_rows = footer
        notes.append(f"The last {footer} row(s) have no date and are skipped.")

    # Sign
    if amount_mode == "single":
        spec.sign, how = _infer_sign(content, filename, spec)
        conf["sign"] = "high" if how == "confirmed" else "low"
        if how != "confirmed":
            confirm.append("sign")
    spec.validate()
    grid = [[ip.cell_text(c).strip() for c in r] for r in rows[:50]]
    return Draft(spec=spec, kind=kind, confidence=conf, needs_confirmation=confirm, notes=notes,
                 header_row=header_idx, grid=grid)


def _guess_kind(header: list[str], hits: dict, cols: list[Column]) -> str:
    cells = {ip.norm(c) for c in header}
    if cells & CARD_HINTS:
        return "card"
    if "date" not in hits and "account_ref" in hits and "value" in hits:
        return "balance"
    return "cash"


def _exclusive_pair(cols: list[Column], data: list[list]):
    """Two money columns where a row fills one or the other, not both: a debit/credit pair."""
    for i, a in enumerate(cols):
        for b in cols[i + 1:]:
            both = either = 0
            for r in data[:300]:
                fa = a.index < len(r) and ip.cell_text(r[a.index]).strip() != ""
                fb = b.index < len(r) and ip.cell_text(r[b.index]).strip() != ""
                both += fa and fb
                either += fa or fb
            if either and both / either < 0.05:
                return a, b
    return None


def _footer_rows(data: list[list], date_idx: int, fmt: str) -> int:
    n = 0
    for r in reversed(data):
        v = r[date_idx] if date_idx < len(r) else None
        # A cell that is a date, or looks like one but isn't valid ("13/45/2026"), is data:
        # the first is a transaction, the second must show up as an error, not vanish as a footer.
        if ip.parse_date(v, fmt) is not None or re.match(r"^\s*\d{1,4}[/.-]\d{1,2}[/.-]\d{1,4}", ip.cell_text(v)):
            break
        n += 1
    return n if n < len(data) else 0


def _infer_sign(content: bytes, filename: str, spec: ip.Spec) -> tuple[str, str]:
    """Try both signs against the file's own evidence (running balance, card payment rows)."""
    results = {}
    for sign in ("negative_is_out", "positive_is_out"):
        spec.sign = sign
        try:
            parsed = ip.parse(content, filename, spec)
        except ip.FormatMismatch:
            continue
        results[sign] = ip.verify_sign(parsed, spec).status
    for sign, status in results.items():
        if status == "confirmed":
            return sign, "confirmed"
    # No evidence: most statements write money out as negative numbers.
    return "negative_is_out", "unverified"


def _infer_balance(file_type, delim, sheet_name, header_idx, header, hits, cols, rows, notes) -> Draft:
    if header_idx is None:
        raise InferError("A balance file needs a header row naming the account and value columns.")
    acct, value = hits.get("account_ref"), hits.get("value")
    by_index = {c.index: c for c in cols}
    if value is None or by_index.get(value) is None or by_index[value].money_share < 0.6:
        value = next((c.index for c in cols if c.money_share >= 0.6 and c.index != acct), None)
    if acct is None or value is None:
        raise InferError("No account or value column found.")
    columns = {"account_ref": [ip.ColumnRef(header=header[acct], index=acct)],
               "value": [ip.ColumnRef(header=header[value], index=value)]}
    as_of = hits.get("as_of")
    if as_of is not None and by_index.get(as_of) and by_index[as_of].date_share >= 0.8:
        columns["as_of"] = [ip.ColumnRef(header=header[as_of], index=as_of)]
    accounts = [ip.cell_text(r[acct]).strip() for r in rows[header_idx + 1:] if acct < len(r)]
    aggregate = "sum_by_account" if len(set(accounts)) < len([a for a in accounts if a]) else "per_row"
    if aggregate == "sum_by_account":
        notes.append("Several rows per account: their values are added up.")
    spec = ip.Spec(kind="balance", file_type=file_type, delimiter=delim or "auto",
                   quote_char='"' if delim and delim != "\t" else "auto",
                   sheet=sheet_name if file_type == "xlsx" else None, has_header=True, header_row=header_idx,
                   header_cells=header, columns=columns, sign=None, aggregate=aggregate)
    spec.validate()
    grid = [[ip.cell_text(c).strip() for c in r] for r in rows[:50]]
    return Draft(spec=spec, kind="balance", confidence={}, needs_confirmation=[], notes=notes,
                 header_row=header_idx, grid=grid)


# ── The Assistant's view of a file ───────────────────────────────────────────

SHAPES = ("date:MDY", "date:DMY", "date:YMD", "money:mixed", "money:positive", "money:negative", "text",
          "category", "code", "blank")
_DIGIT_RUN = re.compile(r"\d{4,}")


# Words a column name is made of. A header cell is shown to the Assistant only if
# every word in it is one of these (or a VOCAB word): a name, a number or anything
# else in a header becomes "~".
HEADER_WORDS = {w for words in VOCAB.values() for phrase in words for w in re.findall(r"[a-z]+", phrase)} | {
    "id", "no", "nr", "num", "code", "symbol", "ticker", "quantity", "qty", "shares", "units", "price", "cost",
    "basis", "currency", "ccy", "fee", "fees", "tax", "category", "sub", "subcategory", "class", "kind", "group",
    "total", "net", "gross", "interest", "principal", "original", "local", "foreign", "rate", "exchange",
    "beneficiary", "counterparty", "recipient", "sender", "iban", "bic", "swift", "bank", "branch", "time",
    "day", "month", "year", "period", "start", "end", "opening", "closing", "current", "previous", "new", "old",
    "in", "out", "of", "on", "the", "and", "or", "to", "from", "for", "by", "usd", "eur", "gbp", "cad", "aud",
    "label", "labels", "tag", "tags", "comment", "remarks", "info", "information", "number", "datum",
}


def safe_header(cell: str) -> str:
    """A header cell as the Assistant may see it: shown only if it is made of plain column-name words."""
    s = re.sub(r"\s+", " ", cell or "").strip()
    if not s:
        return ""
    if _DIGIT_RUN.search(s) or len(s) > 40:
        return "~"
    words = re.findall(r"[^\W\d_]+", s.lower())
    return s if words and all(w in HEADER_WORDS for w in words) else "~"


def column_shape(c: Column) -> str:
    if not c.filled:
        return "blank"
    if c.date_share >= 0.8:
        if c.iso >= c.filled * 0.8:
            return "date:YMD"
        return "date:DMY" if c.day_first and not c.month_first else "date:MDY"
    if c.money_share >= 0.8:
        if c.negatives and c.positives:
            return "money:mixed"
        return "money:negative" if c.negatives else "money:positive"
    if len(c.distinct) <= 6 and len(c.distinct) <= c.filled / 2 and c.text_share >= 0.8:
        return "category"
    if c.text_share >= 0.5:
        return "text"
    return "code"


def outline(content: bytes, filename: str) -> dict:
    """
    Column shapes only: header cells (cleaned by ``safe_header``), an enumerated
    shape per column and row counts. No cell value, preamble row or description
    is ever included.
    """
    file_type, sheets, delim = _sheets(content, filename)
    rows = sheets[0][1] if sheets else []
    header_idx = None
    for _name, candidate in sheets:
        idx = _header_index(candidate)
        if idx is not None:
            rows, header_idx = candidate, idx
            break
    header = ip.trimmed_header(rows[header_idx]) if header_idx is not None else []
    data = rows[header_idx + 1:] if header_idx is not None else rows
    width = max((len(r) for r in data[:300]), default=0)
    cols = _profile(data, width, header)
    return {
        "file_type": file_type,
        "delimiter": {"\t": "tab", ",": "comma", ";": "semicolon", "|": "pipe"}.get(delim, "") if delim else "",
        "header_row": header_idx,
        "rows_above_header": header_idx or 0,
        "data_rows": len(data),
        "columns": [{"index": c.index, "header": safe_header(c.header), "shape": column_shape(c)} for c in cols],
    }
