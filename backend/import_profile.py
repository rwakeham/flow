"""
Import formats ("import profiles"): reading any statement file from a stored description.

A format is data, not code. ``Spec`` says how to read one file layout: the file
type, where the header and data start, which column plays which role, how amounts
and dates are written and which way the sign runs. ``parse`` reads a file with a
Spec into ``ParsedRow``s; the adapters turn those into the row types the existing
import code consumes (``BankRow``/``LedgerRow`` for the cash register,
``CardParseResult`` for cards, ``balance_sheet.Item`` for balances), so
de-duplication, classification and the rest are unchanged.

Rules that keep a format honest:

* A date that doesn't fit the format is an error, never a silently dropped row,
  unless the format says ``on_bad_date: "skip"`` (an explicit choice, for exports
  with summary or note rows between transactions).
* Columns are found by their header text (the stored index is only a fallback
  for headerless files), and a header that no longer has a column the format
  needs raises ``FormatMismatch`` with what changed: Flow never guesses.
* The sign is a fixed property of the format; ``verify_sign`` checks it against
  a running-balance column, the account's history, or card payment rows.

Pure: no database, no network. Kinds: ``cash`` (bank statements or a manual
ledger), ``card`` and ``balance``.
"""

from __future__ import annotations

import csv
import hashlib
import io
import re
import statistics
import zipfile
from dataclasses import dataclass, field
from datetime import date, datetime

SCHEMA_VERSION = 1
KINDS = ("cash", "card", "balance")
FILE_TYPES = ("delimited", "xlsx")
AMOUNT_MODES = ("single", "debit_credit", "amount_plus_direction")
SIGNS = ("negative_is_out", "positive_is_out")
DATE_FORMATS = ("MDY", "DMY", "YMD")
ROW_SOURCES = ("bank", "manual")
ON_BAD_DATE = ("error", "skip")
ENCODINGS = ("auto", "utf-8", "utf-8-sig", "latin-1")
DELIMITERS = ("auto", ",", "\t", ";", "|")
QUOTE_CHARS = ("auto", '"', "'", "")

# Column roles each kind may map. "description" may list several columns (joined with a space).
AMOUNT_ROLES = ("amount", "debit", "credit", "direction")
COMMON_ROLES = ("date", "post_date", "description", "memo", "status", "running_balance") + AMOUNT_ROLES
ROLES = {
    "cash": COMMON_ROLES + ("check_number", "reconciled"),
    "card": COMMON_ROLES + ("card_number", "cardholder", "issuer_category", "issuer_type", "reference",
                            "extended", "city_state", "country", "statement_as"),
    "balance": ("account_ref", "value", "as_of"),
}

HEADER_SEARCH_ROWS = 40
MAX_COLUMNS = 256
MAX_TEXT = 200
MAX_LIST = 50
# Rows read from one file (or one sheet). Statements are far smaller; past this, reading every
# saved format against the file takes minutes. Blank rows count toward MAX_SCANNED_ROWS only,
# which stops a spreadsheet whose used range runs a million rows down.
MAX_ROWS = 50_000
MAX_SCANNED_ROWS = 200_000
# An .xlsx is a zip, checked before openpyxl opens it: a 10 MB file can unpack to gigabytes, and
# openpyxl reads the whole shared-strings part into memory before any row (so MAX_SCANNED_ROWS
# can't help). A 50,000-row statement unpacks to tens of MB.
MAX_XLSX_MEMBERS = 500
MAX_XLSX_UNPACKED = 150 * 1024 * 1024
MAX_XLSX_SHARED_STRINGS = 30 * 1024 * 1024


class SpecError(ValueError):
    """The format itself is invalid (bad JSON, unknown role, missing setting)."""


class TooManyRows(Exception):
    """The file has more rows than Flow reads (MAX_ROWS). Not a ValueError, so no layout check swallows it."""

    def __init__(self, limit: int = MAX_ROWS):
        super().__init__(f"This file has more than {limit:,} rows, more than Flow reads in one import. "
                         "Export a shorter date range and import it in parts.")
        self.limit = limit


class FormatMismatch(ValueError):
    """The file does not have the layout the format describes (header changed, column missing)."""

    def __init__(self, message: str, header_cells: list[str] | None = None, diff: dict | None = None):
        super().__init__(message)
        self.header_cells = header_cells or []
        self.diff = diff or {}


# ── Row types the import code consumes ───────────────────────────────────────

@dataclass
class BankRow:
    """A bank statement line for the cash register (source='bank')."""
    date: date
    amount: float
    bank_description: str
    check_number: str | None


@dataclass
class LedgerRow:
    """A manual ledger entry (source='manual'): positive = money in."""
    date: date
    description: str
    amount: float
    is_reconciled: bool


_SUBSCRIPTION_HINT_RE = re.compile(r"MONTHLY DUES|SUBSCRIPTION|MEMBERSHIP|RECURRING|AUTO.?RENEW", re.I)


@dataclass
class CardRow:
    date: date
    raw_description: str
    amount: float                     # Flow convention: charges negative
    source_amount: float              # the number as the file wrote it
    post_date: date | None = None
    issuer_category: str = ""
    issuer_type: str = ""
    cardholder: str = ""
    account: str = ""
    reference: str = ""
    country: str = ""
    city_state: str = ""
    extended: str = ""

    @property
    def bank_description(self) -> str:  # lets import_dedup.plan_bank_import consume CardRows
        return self.raw_description

    @property
    def city(self) -> str:
        """Merchant city from a City/State column ("SPRINGFIELD\\nIL" or "SPRINGFIELD IL")."""
        parts = [p.strip() for p in re.split(r"[\n,]", self.city_state or "") if p.strip()]
        if not parts:
            return ""
        if len(parts) == 1:
            toks = parts[0].split()
            return " ".join(toks[:-1]) if len(toks) > 1 and len(toks[-1]) == 2 else parts[0]
        return parts[0]

    @property
    def subscription_hint(self) -> bool:
        return bool(_SUBSCRIPTION_HINT_RE.search(self.extended or ""))


@dataclass
class CardParseResult:
    issuer_format: str                # the import format's name
    sign_convention: str              # how the format reads charges (charges_negative, debit_credit_columns ...)
    sign_reason: str                  # what verify_sign found
    rows: list[CardRow]
    pending_dropped: int = 0
    rows_skipped: int = 0
    statement_start: date | None = None
    statement_end: date | None = None
    column_map: dict = field(default_factory=dict)
    suggested_cardholders: list[str] = field(default_factory=list)
    suggested_last4: str | None = None
    card_numbers: list[str] = field(default_factory=list)  # last 4 of every card number in the file
    warnings: list[str] = field(default_factory=list)
    institution: str = ""             # the format's institution, filled into a new card's issuer
    sign_status: str = "unverified"   # verify_sign: confirmed | unverified | conflict | reversed

    @property
    def issuer_label(self) -> str:
        return self.issuer_format

    @property
    def sign_needs_review(self) -> bool:
        return self.sign_status != "confirmed"


# ── The format ───────────────────────────────────────────────────────────────

@dataclass
class ColumnRef:
    header: str | None = None   # header text (matched case- and space-insensitively)
    index: int | None = None    # 0-based; the fallback for headerless files
    optional: bool = False      # a missing optional column is no mismatch (the role is just empty)

    def to_json(self) -> dict:
        out: dict = {}
        if self.header is not None:
            out["header"] = self.header
        if self.index is not None:
            out["index"] = self.index
        if self.optional:
            out["optional"] = True
        return out


@dataclass
class Spec:
    kind: str = "cash"
    file_type: str = "delimited"
    delimiter: str = "auto"
    quote_char: str = "auto"          # auto: '"' for commas, none for tabs (how the old bank parser read TSV)
    encoding: str = "auto"
    sheet: str | int | None = None    # XLSX: sheet name or index; None searches every sheet for the header
    has_header: bool = True
    header_row: int | None = None     # hint: where the header was when the format was made
    header_cells: list[str] = field(default_factory=list)  # the header as saved, for drift detection
    data_start_row: int | None = None  # headerless: None starts at the first row whose date parses
    skip_footer_rows: int = 0
    skip_rows_containing: list[dict] = field(default_factory=list)  # {"text": str, "column": role | None}
    columns: dict[str, list[ColumnRef]] = field(default_factory=dict)
    amount_mode: str = "single"
    sign: str | None = "negative_is_out"
    direction_out_values: list[str] = field(default_factory=list)
    direction_in_values: list[str] = field(default_factory=list)
    decimal: str = "."
    thousands: str = ","
    parens_negative: bool = True
    date_format: str = "MDY"
    post_date_format: str | None = None
    skip_status_values: list[str] = field(default_factory=list)
    row_source: str = "bank"          # cash only: bank statement lines or manual ledger entries
    reconciled_values: list[str] = field(default_factory=lambda: ["x"])
    collapse_whitespace: bool = True  # "A   B" -> "A B" in descriptions
    on_bad_date: str = "error"
    preamble: dict = field(default_factory=dict)  # card: {"period": bool, "cardholder_label": str, "account_label": str}
    aggregate: str = "per_row"        # balance: per_row | sum_by_account
    version: int = SCHEMA_VERSION

    # ── JSON ──
    def to_json(self) -> dict:
        out = {k: getattr(self, k) for k in self.__dataclass_fields__ if k != "columns"}
        out["columns"] = {role: [c.to_json() for c in refs] for role, refs in self.columns.items()}
        return out

    @classmethod
    def from_json(cls, data: dict) -> "Spec":
        if not isinstance(data, dict):
            raise SpecError("A format must be a JSON object.")
        known = set(cls.__dataclass_fields__)
        unknown = set(data) - known
        if unknown:
            raise SpecError(f"Unknown format setting(s): {', '.join(sorted(unknown))}.")
        kwargs = {k: v for k, v in data.items() if k != "columns"}
        cols: dict[str, list[ColumnRef]] = {}
        raw_cols = data.get("columns") or {}
        if not isinstance(raw_cols, dict):
            raise SpecError("columns must map a role to a column.")
        for role, refs in raw_cols.items():
            refs = refs if isinstance(refs, list) else [refs]
            out = []
            for r in refs:
                if not isinstance(r, dict) or set(r) - {"header", "index", "optional"}:
                    raise SpecError(f"Column for {role!r} must be {{header, index, optional}}.")
                out.append(ColumnRef(header=r.get("header"), index=r.get("index"), optional=bool(r.get("optional"))))
            cols[str(role)] = out
        spec = cls(**kwargs, columns=cols)
        spec.validate()
        return spec

    # ── checks ──
    def validate(self) -> None:
        def one_of(name, value, allowed):
            if value not in allowed:
                raise SpecError(f"{name} must be one of {', '.join(map(repr, allowed))}.")

        if self.version != SCHEMA_VERSION:
            raise SpecError(f"Unsupported format version {self.version!r}.")
        one_of("kind", self.kind, KINDS)
        one_of("file_type", self.file_type, FILE_TYPES)
        one_of("delimiter", self.delimiter, DELIMITERS)
        one_of("quote_char", self.quote_char, QUOTE_CHARS)
        one_of("encoding", self.encoding, ENCODINGS)
        one_of("amount_mode", self.amount_mode, AMOUNT_MODES)
        one_of("date_format", self.date_format, DATE_FORMATS)
        if self.post_date_format is not None:
            one_of("post_date_format", self.post_date_format, DATE_FORMATS)
        one_of("row_source", self.row_source, ROW_SOURCES)
        one_of("on_bad_date", self.on_bad_date, ON_BAD_DATE)
        one_of("aggregate", self.aggregate, ("per_row", "sum_by_account"))
        one_of("decimal", self.decimal, (".", ","))
        one_of("thousands", self.thousands, (",", ".", " ", "'", ""))
        if self.decimal == self.thousands:
            raise SpecError("The decimal and thousands separators must differ.")
        for name in ("header_row", "data_start_row"):
            v = getattr(self, name)
            if v is not None and (not isinstance(v, int) or isinstance(v, bool) or not 0 <= v < 10_000):
                raise SpecError(f"{name} must be a row number.")
        if not isinstance(self.skip_footer_rows, int) or not 0 <= self.skip_footer_rows < 1000:
            raise SpecError("skip_footer_rows must be a small number.")
        for name in ("header_cells", "direction_out_values", "direction_in_values", "skip_status_values",
                     "reconciled_values", "skip_rows_containing"):
            v = getattr(self, name)
            if not isinstance(v, list) or len(v) > (MAX_COLUMNS if name == "header_cells" else MAX_LIST):
                raise SpecError(f"{name} must be a list (at most {MAX_LIST} items).")
        for name in ("header_cells", "direction_out_values", "direction_in_values", "skip_status_values",
                     "reconciled_values"):
            if any(not isinstance(x, str) or len(x) > MAX_TEXT for x in getattr(self, name)):
                raise SpecError(f"{name} must hold short text values.")
        for rule in self.skip_rows_containing:
            if (not isinstance(rule, dict) or set(rule) - {"text", "column"} or not isinstance(rule.get("text"), str)
                    or not rule["text"].strip() or len(rule["text"]) > MAX_TEXT):
                raise SpecError("skip_rows_containing entries are {text, column}.")
            if rule.get("column") not in (None, *ROLES[self.kind]):
                raise SpecError(f"skip_rows_containing: unknown column {rule.get('column')!r}.")
        if not isinstance(self.preamble, dict) or set(self.preamble) - {"period", "cardholder_label", "account_label"}:
            raise SpecError("preamble takes period, cardholder_label and account_label.")

        allowed = set(ROLES[self.kind])
        for role, refs in self.columns.items():
            if role not in allowed:
                raise SpecError(f"{role!r} is not a column role for {self.kind} files.")
            if not refs or (role != "description" and len(refs) > 1) or len(refs) > 8:
                raise SpecError(f"{role!r} needs exactly one column" + (" (description: up to 8)." if role == "description" else "."))
            for r in refs:
                if r.header is None and r.index is None:
                    raise SpecError(f"Column for {role!r} needs a header or an index.")
                if r.header is not None and (not isinstance(r.header, str) or len(r.header) > MAX_TEXT):
                    raise SpecError(f"Column header for {role!r} is too long.")
                if r.index is not None and (not isinstance(r.index, int) or isinstance(r.index, bool)
                                            or not 0 <= r.index < MAX_COLUMNS):
                    raise SpecError(f"Column index for {role!r} must be 0-{MAX_COLUMNS - 1}.")
                if self.has_header and r.header is None:
                    raise SpecError(f"Column for {role!r} needs its header text (this format has a header row).")
                if not self.has_header and r.index is None:
                    raise SpecError(f"Column for {role!r} needs a position (this format has no header row).")

        has = self.columns.__contains__
        if self.kind == "balance":
            for role in ("account_ref", "value"):
                if not has(role):
                    raise SpecError(f"A balance format needs a {role} column.")
            return
        if not has("date"):
            raise SpecError("A format needs a date column.")
        if not has("description") and not (self.kind == "card" and has("statement_as")):
            raise SpecError("A format needs a description column.")
        if self.amount_mode == "single":
            if not has("amount"):
                raise SpecError("Pick the amount column.")
            one_of("sign", self.sign, SIGNS)
        elif self.amount_mode == "debit_credit":
            if not (has("debit") and has("credit")):
                raise SpecError("Pick both the money-out (debit) and money-in (credit) columns.")
        else:
            if not (has("amount") and has("direction")):
                raise SpecError("Pick the amount column and the column that says whether money went in or out.")
            if not self.direction_out_values or not self.direction_in_values:
                raise SpecError("List the values that mean money out and money in.")
        if self.row_source == "manual" and self.kind != "cash":
            raise SpecError("Only cash formats can import manual ledger entries.")

    @property
    def fingerprint(self) -> str:
        """Header formats: the header text. Headerless: the column layout (which role sits where)."""
        if self.has_header and self.header_cells:
            return fingerprint(self.header_cells)
        layout = sorted(f"{role}@{r.index}" for role, refs in self.columns.items() for r in refs)
        return hashlib.sha256(("l:" + "|".join(layout)).encode()).hexdigest()[:16]


# ── Normalizing ──────────────────────────────────────────────────────────────

def norm(v) -> str:
    """Header comparison form: lower case, whitespace collapsed."""
    return re.sub(r"\s+", " ", cell_text(v)).strip().lower()


def cell_text(v) -> str:
    if v is None:
        return ""
    if isinstance(v, bool):
        return str(v)
    if isinstance(v, float) and v.is_integer():
        return str(int(v))
    if isinstance(v, datetime):
        return v.isoformat(sep=" ")
    return str(v)


def fingerprint(header_cells: list) -> str:
    """A short stable id for a header row."""
    basis = "h:" + "|".join(norm(c) for c in header_cells)
    return hashlib.sha256(basis.encode()).hexdigest()[:16]


def fits(content: bytes, filename: str, spec: Spec, sample: int = 200) -> float:
    """
    How cleanly a file reads with a format: the share of its data rows that become
    transactions rather than errors (0 when nothing reads). Headerless formats have
    no header to compare, so this is how a file is matched to one.
    """
    try:
        parsed = parse(content, filename, spec)
    except (FormatMismatch, SpecError):
        return 0.0
    rows = len(parsed.rows[:sample])
    if not rows:
        return 0.0
    bad = len(parsed.errors) + parsed.skipped.get("bad_date", 0)
    return rows / (rows + bad)


def trimmed_header(row: list) -> list[str]:
    """Header cells as text, trailing empty cells dropped."""
    cells = [cell_text(c).strip() for c in row]
    while cells and not cells[-1]:
        cells.pop()
    return cells


def header_diff(old: list[str], new: list[str]) -> dict:
    """What changed between two headers: added, removed and moved columns."""
    o = [norm(c) for c in old]
    n = [norm(c) for c in new]
    added = [new[i] for i, c in enumerate(n) if c and c not in o]
    removed = [old[i] for i, c in enumerate(o) if c and c not in n]
    moved = [{"header": old[i], "from": i, "to": n.index(c)} for i, c in enumerate(o)
             if c and c in n and n.index(c) != i]
    return {"added": added, "removed": removed, "moved": moved, "same": o == n}


# ── Reading the grid ─────────────────────────────────────────────────────────

def decode(content: bytes, encoding: str = "auto") -> str:
    if encoding != "auto":
        return content.decode(encoding, errors="replace")
    if content.startswith(b"\xef\xbb\xbf"):
        content = content[3:]
    for enc in ("utf-8", "latin-1"):
        try:
            return content.decode(enc)
        except UnicodeDecodeError:
            continue
    return content.decode("utf-8", errors="replace")


def is_xlsx(content: bytes, filename: str = "") -> bool:
    return content[:2] == b"PK" or filename.lower().endswith((".xlsx", ".xlsm"))


def detect_delimiter(text: str) -> str:
    """A tab anywhere in the first 20 lines means tab-separated; otherwise the most consistent of , ; |."""
    lines = [ln for ln in text.splitlines()[:30] if ln.strip()]
    if any("\t" in ln for ln in lines[:20]):
        return "\t"
    best, best_score = ",", -1.0
    for d in (",", ";", "|"):
        counts = [len(r) for r in csv.reader(io.StringIO("\n".join(lines)), delimiter=d)]
        if not counts:
            continue
        mode = statistics.mode(counts)
        if mode < 2:
            continue
        score = sum(1 for c in counts if c == mode) / len(counts) + mode / 1000
        if score > best_score:
            best, best_score = d, score
    return best


def check_xlsx_limits(content: bytes) -> None:
    """Refuse a workbook that unpacks to far more than a statement could (FormatMismatch), from the
    zip's directory alone. The sizes there are the ones zipfile enforces while unpacking, so a
    member can't claim less than it holds."""
    try:
        infos = zipfile.ZipFile(io.BytesIO(content)).infolist()
    except zipfile.BadZipFile:
        return                                   # not a zip: openpyxl reports it the usual way
    too_big = FormatMismatch("This spreadsheet is too large once unpacked to be a statement. "
                             "Export a shorter date range, or save it as CSV.")
    if len(infos) > MAX_XLSX_MEMBERS or sum(i.file_size for i in infos) > MAX_XLSX_UNPACKED:
        raise too_big
    if any(i.filename.lower().endswith("sharedstrings.xml") and i.file_size > MAX_XLSX_SHARED_STRINGS
           for i in infos):
        raise too_big


def read_sheets(content: bytes, filename: str, spec: Spec | None = None) -> list[tuple[str, list[list]]]:
    """Every sheet as (name, rows of raw cell values); blank rows dropped. A delimited file is one sheet."""
    spec = spec or Spec(file_type="xlsx" if is_xlsx(content, filename) else "delimited")
    if spec.file_type == "xlsx":
        import openpyxl
        check_xlsx_limits(content)
        try:
            wb = openpyxl.load_workbook(io.BytesIO(content), read_only=True, data_only=True)
        except Exception as exc:  # noqa: BLE001 - any unreadable workbook is the same problem
            raise FormatMismatch(f"Could not read the spreadsheet: {exc}") from exc
        sheets = []
        try:
            for ws in wb.worksheets:
                kept = []
                for scanned, r in enumerate(ws.iter_rows(values_only=True), 1):
                    if scanned > MAX_SCANNED_ROWS:
                        raise TooManyRows(MAX_ROWS)
                    if any(c not in (None, "") for c in r):
                        kept.append(list(r))
                        if len(kept) > MAX_ROWS:
                            raise TooManyRows(MAX_ROWS)
                sheets.append((ws.title, kept))
        finally:
            wb.close()
        return sheets
    if is_xlsx(content, filename) and content[:2] == b"PK":
        raise FormatMismatch("This is a spreadsheet (XLSX), but the format describes a text file.")

    text = decode(content, spec.encoding)
    delim = detect_delimiter(text) if spec.delimiter == "auto" else spec.delimiter
    quote = spec.quote_char
    if quote == "auto":
        quote = "" if delim == "\t" else '"'
    if quote:
        reader = csv.reader(io.StringIO(text), delimiter=delim, quotechar=quote)
    else:
        reader = (ln.split(delim) for ln in text.splitlines())
    kept = []
    for r in reader:
        if any(cell_text(c).strip() for c in r):
            kept.append(r)
            if len(kept) > MAX_ROWS:
                raise TooManyRows(MAX_ROWS)
    return [("", kept)]


def read_grid(content: bytes, filename: str, spec: Spec | None = None, limit: int | None = None) -> list[list[str]]:
    """The first sheet (or the format's sheet) as text cells, for the setup screen."""
    sheets = read_sheets(content, filename, spec)
    rows = _pick_sheet(sheets, spec)[1] if spec and spec.sheet is not None else sheets[0][1] if sheets else []
    return [[cell_text(c).strip() for c in r] for r in (rows[:limit] if limit else rows)]


def _pick_sheet(sheets, spec: Spec):
    if isinstance(spec.sheet, int):
        if 0 <= spec.sheet < len(sheets):
            return sheets[spec.sheet]
    elif isinstance(spec.sheet, str):
        for s in sheets:
            if s[0] == spec.sheet:
                return s
    raise FormatMismatch(f"The workbook has no sheet {spec.sheet!r}.",
                         diff={"sheets": [s[0] for s in sheets]})


# ── Values ───────────────────────────────────────────────────────────────────

_MONTHS = "%b %d, %Y", "%B %d, %Y", "%d-%b-%Y", "%d %b %Y", "%d %B %Y", "%b %d %Y"
_DATE_RE = re.compile(r"^\s*(\d{1,4})[/.-](\d{1,2})[/.-](\d{1,4})(?:[ T].*)?$")


def parse_date(v, fmt: str = "MDY") -> date | None:
    """A date in the format's order. ISO (YYYY-MM-DD) and month names are read in any order."""
    if isinstance(v, datetime):
        return v.date()
    if isinstance(v, date):
        return v
    s = cell_text(v).strip()
    if not s:
        return None
    m = _DATE_RE.match(s)
    if m:
        a, b, c = m.groups()
        try:
            if len(a) == 4:                                 # YYYY-MM-DD, whatever the format
                return date(int(a), int(b), int(c))
            if fmt == "YMD":
                return None
            # Two-digit years follow strptime's %y: 00-68 -> 20xx, 69-99 -> 19xx.
            year = int(c) + (2000 if len(c) <= 2 and int(c) < 69 else 1900 if len(c) <= 2 else 0)
            mo, d = (int(a), int(b)) if fmt == "MDY" else (int(b), int(a))
            return date(year, mo, d)
        except ValueError:
            return None
    for f in _MONTHS:
        try:
            return datetime.strptime(s, f).date()
        except ValueError:
            continue
    return None


_NUM_JUNK = re.compile(r"[^\d.,'\s()+-]")
# Currency words an amount cell may carry; any other letter means the cell isn't an amount
# ("CHECK # 1001" is text, not 1001).
_CURRENCY_WORDS = re.compile(r"\b(?:USD|US|EUR|GBP|CAD|AUD|NZD|CHF|JPY|SEK|NOK|DKK|MXN|INR|ZAR)\b|[$€£¥]",
                             re.IGNORECASE)


def parse_amount(v, decimal: str = ".", thousands: str = ",", parens_negative: bool = True) -> float | None:
    """A money amount: currency symbols dropped, (12.00) and 12.00- negative, separators per the format."""
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return float(v)
    s = cell_text(v).strip()
    if not s:
        return None
    if re.search(r"[^\d.,'\s()+-]", _CURRENCY_WORDS.sub("", s)):
        return None                         # words, dates ("10/03/2026"), "#" ...: not an amount
    s = _NUM_JUNK.sub("", s).strip()
    paren = "(" in s and ")" in s
    s = s.replace("(", "").replace(")", "").replace(" ", "")
    minus = False
    if s.startswith("-"):
        s, minus = s[1:], True
    elif s.startswith("+"):
        s = s[1:]
    if s.endswith("-"):                     # trailing minus: "12.34-"
        s, minus = s[:-1], True
    neg = minus or (paren and parens_negative)
    if thousands:
        s = s.replace(thousands, "")
    if decimal == ",":
        s = s.replace(",", ".")
    elif thousands != ",":
        s = s.replace(",", "")
    if not re.fullmatch(r"\d+(\.\d+)?|\.\d+", s):
        return None
    val = float(s)
    return -val if neg else val


# ── Parsing ──────────────────────────────────────────────────────────────────

@dataclass
class ParsedRow:
    line: int                          # 1-based row number in the sheet, for messages
    date: date | None = None
    post_date: date | None = None
    description: str = ""
    statement_as: str = ""
    memo: str = ""
    amount: float = 0.0                # Flow convention: money out negative
    source_amount: float = 0.0         # the number as the file wrote it (debit/credit: debit positive)
    check_number: str | None = None
    status: str = ""
    reconciled: bool = False
    running_balance: float | None = None
    card_number: str = ""
    cardholder: str = ""
    issuer_category: str = ""
    issuer_type: str = ""
    reference: str = ""
    extended: str = ""
    city_state: str = ""
    country: str = ""
    account_ref: str = ""
    value: float | None = None
    as_of: date | None = None


@dataclass
class ParsedFile:
    rows: list[ParsedRow]
    errors: list[dict]                 # {"line": int, "message": str}
    skipped: dict                      # reason -> count
    header_cells: list[str]
    header_row: int | None
    sheet: str
    fingerprint: str
    fingerprint_ok: bool | None        # None: the format stored no header to compare with
    diff: dict | None
    preamble: dict                     # card: {"start", "end", "names", "last4"}


def _locate_header(rows: list[list], spec: Spec) -> int | None:
    """The first row (in the first 40) holding every required header the format maps."""
    need = [norm(r.header) for refs in spec.columns.values() for r in refs if r.header and not r.optional]
    if not need:
        return spec.header_row
    for i, row in enumerate(rows[:HEADER_SEARCH_ROWS]):
        cells = {norm(c) for c in row}
        if all(h in cells for h in need):
            return i
    return None


def _resolve(spec: Spec, header: list | None) -> tuple[dict[str, list[int]], list[str]]:
    """role -> column indexes; plus the required headers that are missing."""
    out: dict[str, list[int]] = {}
    missing: list[str] = []
    cells = [norm(c) for c in header] if header is not None else None
    for role, refs in spec.columns.items():
        idx = []
        for r in refs:
            if cells is not None and r.header is not None:
                h = norm(r.header)
                hits = [i for i, c in enumerate(cells) if c == h]
                if hits:
                    idx.append(r.index if r.index in hits else hits[0])
                elif not r.optional:
                    missing.append(r.header)
            elif r.index is not None:
                idx.append(r.index)
        if idx:
            out[role] = idx
    return out, missing


def parse(content: bytes, filename: str, spec: Spec) -> ParsedFile:
    """Read a file with a format. Raises FormatMismatch when the layout isn't the one described."""
    sheets = read_sheets(content, filename, spec)
    header_idx = None
    if spec.sheet is not None:
        sheets = [_pick_sheet(sheets, spec)]
    sheet_name, rows = sheets[0] if sheets else ("", [])
    if spec.has_header:
        for name, candidate in sheets:
            idx = _locate_header(candidate, spec)
            if idx is not None:
                sheet_name, rows, header_idx = name, candidate, idx
                break
        if header_idx is None:
            first = trimmed_header(rows[0]) if rows else []
            _cols, missing = _resolve(spec, first)
            raise FormatMismatch(
                "The file's columns don't match this format" + (f" (missing: {', '.join(missing)})." if missing else "."),
                header_cells=first, diff={**header_diff(spec.header_cells, first), "missing": missing})
        header = trimmed_header(rows[header_idx])
        cols, missing = _resolve(spec, rows[header_idx])
        data = list(enumerate(rows[header_idx + 1:], start=header_idx + 2))
    else:
        header = []
        cols, missing = _resolve(spec, None)
        data = list(enumerate(rows, start=1))
        if spec.data_start_row is not None:
            data = data[spec.data_start_row:]
        else:
            start = next((i for i, (_ln, r) in enumerate(data)
                          if _date_at(r, cols, spec) is not None), len(data))
            data = data[start:]
    if spec.skip_footer_rows:
        data = data[:-spec.skip_footer_rows] if spec.skip_footer_rows < len(data) else []

    fp = fingerprint(header) if spec.has_header else spec.fingerprint
    fp_ok = (fp == spec.fingerprint) if (not spec.has_header or spec.header_cells) else None
    diff = header_diff(spec.header_cells, header) if spec.has_header and spec.header_cells and not fp_ok else None

    out = ParsedFile(rows=[], errors=[], skipped={}, header_cells=header, header_row=header_idx,
                     sheet=sheet_name, fingerprint=fp, fingerprint_ok=fp_ok, diff=diff,
                     preamble=_preamble(rows[:header_idx] if header_idx else [], spec))
    for line, row in data:
        _read_row(row, line, cols, spec, out)
    return out


def _cell(row: list, cols: dict, role: str):
    idx = cols.get(role)
    if not idx or idx[0] >= len(row):
        return None
    return row[idx[0]]


def _text(row: list, cols: dict, role: str) -> str:
    return cell_text(_cell(row, cols, role)).strip()


def _date_at(row, cols, spec):
    return parse_date(_cell(row, cols, "date"), spec.date_format)


def _skip(out: ParsedFile, reason: str) -> None:
    out.skipped[reason] = out.skipped.get(reason, 0) + 1


def _amount(row, cols, role, spec):
    return parse_amount(_cell(row, cols, role), spec.decimal, spec.thousands, spec.parens_negative)


def _read_row(row: list, line: int, cols: dict, spec: Spec, out: ParsedFile) -> None:
    for rule in spec.skip_rows_containing:
        needle = rule["text"].strip().lower()
        hay = [_text(row, cols, rule["column"])] if rule.get("column") else [cell_text(c) for c in row]
        if any(needle in h.lower() for h in hay):
            return _skip(out, "matched_text")

    if spec.kind == "balance":
        return _read_balance_row(row, line, cols, spec, out)

    raw_date = _text(row, cols, "date")
    d = _date_at(row, cols, spec)
    if d is None:
        if not raw_date:
            return _skip(out, "no_date")
        if spec.on_bad_date == "skip":
            return _skip(out, "bad_date")
        out.errors.append({"line": line, "message": f"Date {raw_date!r} doesn't fit the format ({spec.date_format})."})
        return

    status = _text(row, cols, "status")
    if status and status.lower() in {v.strip().lower() for v in spec.skip_status_values}:
        return _skip(out, "status")

    if spec.amount_mode == "single":
        raw = _text(row, cols, "amount")
        if not raw and not isinstance(_cell(row, cols, "amount"), (int, float)):
            return _skip(out, "no_amount")
        val = _amount(row, cols, "amount", spec)
        if val is None:
            out.errors.append({"line": line, "message": f"Amount {raw!r} isn't a number."})
            return
        source = val
        amount = val if spec.sign == "negative_is_out" else -val
    elif spec.amount_mode == "debit_credit":
        raw_out, raw_in = _text(row, cols, "debit"), _text(row, cols, "credit")
        if not raw_out and not raw_in:
            return _skip(out, "no_amount")
        dv = _amount(row, cols, "debit", spec) if raw_out else None
        cv = _amount(row, cols, "credit", spec) if raw_in else None
        if (raw_out and dv is None) or (raw_in and cv is None):
            out.errors.append({"line": line, "message": f"Amount {(raw_out or raw_in)!r} isn't a number."})
            return
        amount = abs(cv or 0.0) - abs(dv or 0.0)
        source = abs(dv or 0.0) - abs(cv or 0.0)
    else:
        raw = _text(row, cols, "amount")
        if not raw:
            return _skip(out, "no_amount")
        val = _amount(row, cols, "amount", spec)
        if val is None:
            out.errors.append({"line": line, "message": f"Amount {raw!r} isn't a number."})
            return
        direction = _text(row, cols, "direction").lower()
        if direction in {v.strip().lower() for v in spec.direction_out_values}:
            amount = -abs(val)
        elif direction in {v.strip().lower() for v in spec.direction_in_values}:
            amount = abs(val)
        else:
            out.errors.append({"line": line, "message": f"{direction or 'An empty value'!r} doesn't say money in or out."})
            return
        source = val

    desc_parts = [cell_text(row[i]).strip() for i in cols.get("description", []) if i < len(row)]
    desc = " ".join(p for p in desc_parts if p)
    statement_as = _text(row, cols, "statement_as")
    if not desc and statement_as:
        desc = statement_as
    desc = re.sub(r"\s+", " ", desc).strip() if spec.collapse_whitespace else re.sub(r"[\r\n]+", " ", desc)

    post_raw = _cell(row, cols, "post_date")
    rb = _amount(row, cols, "running_balance", spec) if "running_balance" in cols else None
    rec = _text(row, cols, "reconciled").lower()
    out.rows.append(ParsedRow(
        line=line, date=d,
        post_date=parse_date(post_raw, spec.post_date_format or spec.date_format) if "post_date" in cols else None,
        description=desc, statement_as=statement_as, memo=_text(row, cols, "memo"),
        amount=round(amount, 2), source_amount=round(source, 2),
        check_number=_text(row, cols, "check_number") or None, status=status,
        reconciled=bool(rec) and rec in {v.strip().lower() for v in spec.reconciled_values},
        running_balance=rb,
        card_number=_text(row, cols, "card_number"), cardholder=_text(row, cols, "cardholder"),
        issuer_category=_text(row, cols, "issuer_category"), issuer_type=_text(row, cols, "issuer_type"),
        reference=_text(row, cols, "reference"), extended=_text(row, cols, "extended"),
        city_state=_text(row, cols, "city_state"), country=_text(row, cols, "country"),
    ))


def _read_balance_row(row, line, cols, spec, out):
    ref = _text(row, cols, "account_ref")
    raw = _text(row, cols, "value")
    if not ref and not raw:
        return _skip(out, "blank")
    if not raw and not isinstance(_cell(row, cols, "value"), (int, float)):
        return _skip(out, "no_amount")
    val = _amount(row, cols, "value", spec)
    if val is None:
        out.errors.append({"line": line, "message": f"Value {raw!r} isn't a number."})
        return
    if not ref:
        out.errors.append({"line": line, "message": "No account on this row."})
        return
    as_of = parse_date(_cell(row, cols, "as_of"), spec.date_format) if "as_of" in cols else None
    out.rows.append(ParsedRow(line=line, account_ref=ref, value=round(val, 2), amount=round(val, 2),
                              source_amount=round(val, 2), as_of=as_of, description=ref))


_PERIOD_RE = re.compile(r"([A-Z][a-z]{2,8} \d{1,2}, \d{4}) to ([A-Z][a-z]{2,8} \d{1,2}, \d{4})")


def _preamble(pre: list[list], spec: Spec) -> dict:
    """Statement period and labelled cells (e.g. 'Prepared for' -> name) from rows above the header."""
    info = {"start": None, "end": None, "names": [], "last4": None}
    if not spec.preamble or not pre:
        return info
    name_label = norm(spec.preamble.get("cardholder_label") or "")
    acct_label = norm(spec.preamble.get("account_label") or "")
    for i, row in enumerate(pre):
        if spec.preamble.get("period"):
            for c in row:
                m = _PERIOD_RE.search(cell_text(c))
                if m and info["start"] is None:
                    info["start"], info["end"] = parse_date(m.group(1)), parse_date(m.group(2))
        label = norm(row[0]) if row else ""
        nxt = cell_text(pre[i + 1][0]).strip() if i + 1 < len(pre) and pre[i + 1] else ""
        if name_label and label == name_label and nxt:
            info["names"].append(nxt)
        if acct_label and label == acct_label and nxt:
            digits = re.sub(r"\D", "", nxt)
            if digits:
                info["last4"] = digits[-4:]
    return info


# ── Sign check ───────────────────────────────────────────────────────────────

@dataclass
class SignCheck:
    status: str          # confirmed | reversed | conflict | unverified
    reason: str
    checked: int = 0


def _balance_fit(rows: list[ParsedRow], flip: bool) -> tuple[int, int]:
    """How many consecutive rows satisfy prev_balance + amount == balance (file order either way)."""
    pts = [(r.amount * (-1 if flip else 1), r.running_balance) for r in rows if r.running_balance is not None]
    best = (0, 0)
    for seq in (pts, pts[::-1]):
        ok = sum(1 for (_a0, b0), (a1, b1) in zip(seq, seq[1:]) if abs(b0 + a1 - b1) < 0.005)
        best = max(best, (ok, max(len(seq) - 1, 0)))
    return best


def date_order_ambiguous(content: bytes, filename: str, spec: Spec, parsed: ParsedFile) -> bool:
    """True when nothing in the file shows whether its dates are month-first or day-first: read
    the other way round, every row still parses, but some dates come out different (every day in
    the file is the 12th or earlier). Saving such a format needs the user to confirm the order,
    or a day-first file is stored with day and month swapped."""
    if spec.date_format not in ("MDY", "DMY") or not parsed.rows:
        return False
    alt = Spec.from_json({**spec.to_json(), "date_format": "DMY" if spec.date_format == "MDY" else "MDY"})
    try:
        other = parse(content, filename, alt)
    except (FormatMismatch, ValueError):
        return False
    if len(other.errors) > len(parsed.errors) or len(other.rows) != len(parsed.rows):
        return False                     # some date only reads one way: the order is settled
    return any(a.date != b.date for a, b in zip(parsed.rows, other.rows))


def verify_sign(parsed: ParsedFile, spec: Spec, history: list[tuple] | None = None) -> SignCheck:
    """
    Does money in and out run the way the format says? Evidence, strongest first:
    a running-balance column; rows already stored for the account (``history``:
    (date, amount, description) tuples, Flow sign); card payment / sale rows.
    """
    rows = parsed.rows
    if not rows:
        return SignCheck("unverified", "no rows to check")
    if any(r.running_balance is not None for r in rows):
        ok, total = _balance_fit(rows, flip=False)
        if total:
            flipped, _ = _balance_fit(rows, flip=True)
            if ok / total >= 0.9:
                return SignCheck("confirmed", f"the running balance agrees on {ok} of {total} rows", total)
            if flipped / total >= 0.9:
                return SignCheck("reversed", f"the running balance only agrees with the sign reversed "
                                             f"({flipped} of {total} rows)", total)
    if history:
        by_key: dict[tuple, list[float]] = {}
        for d, amt, desc in history:
            by_key.setdefault((d, round(abs(float(amt)), 2)), []).append(float(amt))
        same = opposite = 0
        for r in rows:
            prior = by_key.get((r.date, round(abs(r.amount), 2)))
            if prior and r.amount:
                if any((p > 0) == (r.amount > 0) for p in prior):
                    same += 1
                else:
                    opposite += 1
        if same + opposite >= 2:
            if opposite == 0:
                return SignCheck("confirmed", f"{same} rows match the account's history with the same sign", same)
            if same == 0:
                return SignCheck("reversed", f"{opposite} rows match the account's history with the opposite sign",
                                 opposite)
            return SignCheck("conflict", f"{same} rows agree with the account's history and {opposite} don't",
                             same + opposite)
    if spec.kind == "card":
        import card_rules
        is_pay = [bool(card_rules.PAYMENT_RE.search(r.description.upper())
                       or r.issuer_type.lower() in card_rules.ISSUER_PAYMENT_TYPES) for r in rows]
        pays = [r.amount for r, p in zip(rows, is_pay) if p and r.amount]
        rest = [r.amount for r, p in zip(rows, is_pay) if not p and r.amount]
        # A payment runs the opposite way from the charges. A row that is only *named* like one
        # ("ONLINE PAYMENT SOLUTIONS LLC") runs the same way as everything else: it is a merchant,
        # and no evidence, so one such row can't settle the sign of a whole file.
        if pays and rest:
            med, rest_med = statistics.median(pays), statistics.median(rest)
            if med > 0 and rest_med < 0:
                return SignCheck("confirmed", f"{len(pays)} payment row(s) reduce the balance", len(pays))
            if med < 0 and rest_med > 0:
                return SignCheck("reversed", f"{len(pays)} payment row(s) would add to the balance", len(pays))
        sales = [r.amount for r in rows if r.issuer_type.lower() in ("sale", "purchase")]
        if sales:
            med = statistics.median(sales)
            if med < 0:
                return SignCheck("confirmed", f"{len(sales)} Sale row(s) are charges", len(sales))
            if med > 0:
                return SignCheck("reversed", f"{len(sales)} Sale row(s) would be credits", len(sales))
        if pays and rest:
            return SignCheck("unverified", f"{len(pays)} row(s) named like a payment run the same way as the "
                                           "charges, so they don't show which way money moved")
    if spec.amount_mode in ("debit_credit", "amount_plus_direction"):
        return SignCheck("unverified", "the format's columns say which way money moved; nothing in the file confirms it")
    return SignCheck("unverified", "nothing in the file confirms which way money moved")


# ── Adapters: the row types the existing import code consumes ────────────────

def to_bank_rows(parsed: ParsedFile) -> list[BankRow]:
    return [BankRow(date=r.date, amount=r.amount, bank_description=r.description, check_number=r.check_number)
            for r in parsed.rows]


def to_ledger_rows(parsed: ParsedFile) -> list[LedgerRow]:
    """Manual ledger entries: rows with no description or no amount (placeholders, markers) are left out."""
    return [LedgerRow(date=r.date, description=r.description, amount=r.amount, is_reconciled=r.reconciled)
            for r in parsed.rows if r.description and r.amount]


def card_sign_convention(spec: Spec) -> str:
    if spec.amount_mode == "debit_credit":
        return "debit_credit_columns"
    if spec.amount_mode == "amount_plus_direction":
        return "direction_column"
    return "charges_negative" if spec.sign == "negative_is_out" else "charges_positive"


def to_card_result(parsed: ParsedFile, spec: Spec, name: str = "", check: SignCheck | None = None,
                   institution: str = "") -> CardParseResult:
    """The CardParseResult card_import expects. Rows without a description count as skipped."""
    rows: list[CardRow] = []
    skipped = sum(v for k, v in parsed.skipped.items() if k in ("no_date", "no_amount", "bad_date")) + len(parsed.errors)
    for r in parsed.rows:
        if not r.description:
            skipped += 1
            continue
        rows.append(CardRow(
            date=r.date, raw_description=r.description, amount=r.amount, source_amount=r.source_amount,
            post_date=r.post_date, issuer_category=r.issuer_category, issuer_type=r.issuer_type,
            cardholder=r.cardholder, account=r.card_number, reference=r.reference, country=r.country,
            city_state=r.city_state, extended=r.extended,
        ))
    pre = parsed.preamble
    seen: dict[str, str] = {}
    for n in [r.cardholder for r in rows if r.cardholder] + pre["names"]:
        seen.setdefault(n.strip().upper(), n.strip())
    last4 = pre["last4"]
    numbers = list(dict.fromkeys(([last4] if last4 else [])
                                 + [d[-4:] for d in (re.sub(r"\D", "", r.account) for r in rows) if d]))
    if last4 is None and numbers:
        last4 = numbers[0]
    check = check or verify_sign(parsed, spec)
    warnings = [e["message"] for e in parsed.errors[:20]]
    if check.status != "confirmed":
        warnings.append(f"Sign {check.status}: {check.reason}.")
    return CardParseResult(
        issuer_format=name or "import format",
        sign_convention=card_sign_convention(spec),
        sign_reason=check.reason,
        rows=rows,
        pending_dropped=parsed.skipped.get("status", 0),
        rows_skipped=skipped,
        statement_start=pre["start"],
        statement_end=pre["end"],
        column_map={role: ", ".join(r.header or f"column {r.index + 1}" for r in refs)
                    for role, refs in spec.columns.items()},
        suggested_cardholders=sorted(seen.values()),
        suggested_last4=last4,
        card_numbers=numbers,
        warnings=warnings,
        institution=institution,
        sign_status=check.status,
    )


def to_balance_items(parsed: ParsedFile, spec: Spec) -> list:
    """balance_sheet.Items: one per row, or one per account when the format sums them."""
    from balance_sheet import Item
    totals: dict[str, float] = {}
    order: list[str] = []
    for r in parsed.rows:
        if spec.aggregate == "sum_by_account":
            if r.account_ref not in totals:
                order.append(r.account_ref)
            totals[r.account_ref] = round(totals.get(r.account_ref, 0.0) + (r.value or 0.0), 2)
        else:
            order.append(r.account_ref)
            totals[f"{len(order)}:{r.account_ref}"] = r.value or 0.0
    items = []
    for i, ref in enumerate(order, start=1):
        value = totals[ref] if spec.aggregate == "sum_by_account" else totals[f"{i}:{ref}"]
        digits = re.sub(r"\D", "", ref)
        items.append(Item(last4=digits[-4:] if len(digits) >= 4 else "", label=ref, amount=value, kind="balance"))
    return items
