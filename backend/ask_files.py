"""
A statement file attached in the Assistant, so it can propose an import format.

The file never leaves this server. ``summarize`` reads it into two things that
may reach the model:

* the outline (``import_infer.outline``): header cells only when they are plainly
  column names (otherwise "~") and an enumerated shape per column;
* a draft summary: Flow's own draft format reduced to enums and column numbers
  (which column plays which role, the amount layout, sign, date order, decimal
  mark, footer rows, what still needs confirming).

The browser keeps the full draft (header text, the first rows) for the format
editor and sends back only the outline and the summary with each question. They
are not trusted: ``checked_attachment`` rebuilds both from scratch, keeping only
known keys, enum values and small integers, and cleaning every header cell again,
so nothing free-form a browser adds can reach the model.

Pure: no database, no network.
"""

from __future__ import annotations

import json

import import_infer
import import_profile as ip

MAX_COLUMNS = 64
CONFIRMABLE = ("sign", "date_format", "debit_credit")
DELIMITER_NAMES = ("", "tab", "comma", "semicolon", "pipe")


class AttachmentError(ValueError):
    """The attachment in a question is malformed (the request is refused)."""


def summarize(content: bytes, filename: str, kind_hint: str | None = None) -> dict:
    """{"outline", "summary"} for the model, and the full "draft" for the browser's editor."""
    outline = import_infer.outline(content, filename)
    try:
        draft = import_infer.infer(content, filename, kind_hint)
    except (import_infer.InferError, ip.FormatMismatch, ip.SpecError):
        draft = None
    summary = draft_summary(draft) if draft else None
    return {"outline": outline, "summary": summary, "draft": draft.to_json() if draft else None}


def draft_summary(draft: import_infer.Draft) -> dict:
    s = draft.spec
    roles = {role: [r.index for r in refs if r.index is not None] for role, refs in s.columns.items()}
    return {
        "kind": s.kind, "has_header": s.has_header, "header_row": s.header_row,
        "roles": {k: v for k, v in roles.items() if v},
        "amount_mode": s.amount_mode, "sign": s.sign, "date_format": s.date_format, "decimal": s.decimal,
        "skip_footer_rows": s.skip_footer_rows,
        "needs_confirmation": [c for c in draft.needs_confirmation if c in CONFIRMABLE],
    }


def _int(v, lo: int, hi: int, what: str) -> int:
    if isinstance(v, bool) or not isinstance(v, int) or not lo <= v <= hi:
        raise AttachmentError(f"attachment: {what} must be a whole number from {lo} to {hi}")
    return v


def _one_of(v, allowed, what: str):
    if v not in allowed:
        raise AttachmentError(f"attachment: unknown {what}")
    return v


def checked_attachment(raw) -> dict | None:
    """The attachment as the model may see it, rebuilt from known parts only; None when absent."""
    if raw is None:
        return None
    if not isinstance(raw, dict) or set(raw) - {"outline", "summary"} or "outline" not in raw:
        raise AttachmentError("attachment must be {outline, summary}")
    o = raw["outline"]
    if not isinstance(o, dict) or set(o) != {"file_type", "delimiter", "header_row", "rows_above_header",
                                             "data_rows", "columns"}:
        raise AttachmentError("attachment: malformed outline")
    cols = o["columns"]
    if not isinstance(cols, list) or not 1 <= len(cols) <= MAX_COLUMNS:
        raise AttachmentError(f"attachment: 1 to {MAX_COLUMNS} columns")
    outline = {
        "file_type": _one_of(o["file_type"], ip.FILE_TYPES, "file type"),
        "delimiter": _one_of(o["delimiter"], DELIMITER_NAMES, "delimiter"),
        "header_row": None if o["header_row"] is None else _int(o["header_row"], 0, 39, "header_row"),
        "rows_above_header": _int(o["rows_above_header"], 0, 39, "rows_above_header"),
        "data_rows": _int(o["data_rows"], 0, 10_000_000, "data_rows"),
        "columns": [],
    }
    for i, c in enumerate(cols):
        if not isinstance(c, dict) or set(c) != {"index", "header", "shape"} or not isinstance(c["header"], str):
            raise AttachmentError("attachment: each column is {index, header, shape}")
        if _int(c["index"], 0, MAX_COLUMNS - 1, "column index") != i:
            raise AttachmentError("attachment: columns must be in order")
        outline["columns"].append({"index": i, "header": import_infer.safe_header(c["header"][:80]),
                                   "shape": _one_of(c["shape"], import_infer.SHAPES, "column shape")})

    summary = None
    s = raw.get("summary")
    if s is not None:
        keys = {"kind", "has_header", "header_row", "roles", "amount_mode", "sign", "date_format", "decimal",
                "skip_footer_rows", "needs_confirmation"}
        if not isinstance(s, dict) or set(s) != keys:
            raise AttachmentError("attachment: malformed draft summary")
        kind = _one_of(s["kind"], ip.KINDS, "kind")
        if not isinstance(s["roles"], dict) or not isinstance(s["needs_confirmation"], list):
            raise AttachmentError("attachment: malformed draft summary")
        roles = {}
        for role, idx in s["roles"].items():
            _one_of(role, ip.ROLES[kind], "role")
            if not isinstance(idx, list) or not 1 <= len(idx) <= 8:
                raise AttachmentError("attachment: a role takes 1 to 8 columns")
            roles[role] = [_int(i, 0, len(cols) - 1, "role column") for i in idx]
        summary = {
            "kind": kind, "has_header": bool(s["has_header"]),
            "header_row": None if s["header_row"] is None else _int(s["header_row"], 0, 39, "header_row"),
            "roles": roles,
            "amount_mode": _one_of(s["amount_mode"], ip.AMOUNT_MODES, "amount mode"),
            "sign": _one_of(s["sign"], (*ip.SIGNS, None), "sign"),
            "date_format": _one_of(s["date_format"], ip.DATE_FORMATS, "date format"),
            "decimal": _one_of(s["decimal"], (".", ","), "decimal mark"),
            "skip_footer_rows": _int(s["skip_footer_rows"], 0, 999, "skip_footer_rows"),
            "needs_confirmation": [_one_of(c, CONFIRMABLE, "confirmation item") for c in s["needs_confirmation"]],
        }
    return {"outline": outline, "summary": summary}


def attachment_text(att: dict) -> str:
    """The block appended to the user's question (only checked_attachment output goes in)."""
    return ("\n\nATTACHED FILE (a statement file; column shapes and Flow's draft format only, no values):\n"
            + json.dumps(att, sort_keys=True))
