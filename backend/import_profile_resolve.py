"""
Which saved import format reads a given file, for a given account.

``choose`` is pure: given a file and candidate formats it returns

* ``match``: a format reads the file as described (header fingerprint equal, or a
  headerless format that reads it cleanly);
* ``drift``: a format that clearly describes this file's bank export, but the
  header has changed (columns added, renamed, moved or gone). Flow never imports
  through a changed header: the user confirms an updated format first, starting
  from ``draft`` (the old format re-pointed at the new header by name);
* ``none``: nothing fits; ``draft`` is ``import_infer``'s suggestion.

The database helpers below load an account's linked formats and turn a
resolution into the HTTP error the import routes return. Links are stored per
account kind: ``cash`` (register accounts), ``card`` and ``balance``.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone

import import_infer
import import_profile as ip

HEADERLESS_FIT = 0.9     # share of rows a headerless format must read cleanly
DRIFT_OVERLAP = 0.5      # share of a format's header still present for a changed header to count as drift


@dataclass
class Candidate:
    id: int
    name: str
    kind: str
    spec: ip.Spec
    institution: str = ""
    accounts: list[tuple[str, int]] = field(default_factory=list)   # (account_kind, account_id) links


@dataclass
class Resolution:
    status: str                         # match | drift | none
    candidate: Candidate | None = None
    parsed: ip.ParsedFile | None = None
    diff: dict | None = None
    draft: dict | None = None           # a Draft's JSON (none) or a spec JSON (drift)
    header_cells: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


def _file_header(content: bytes, filename: str, kind: str | None = None) -> tuple[list[str], dict | None]:
    """The file's own header (as inference finds it) and the inferred draft, if any."""
    try:
        d = import_infer.infer(content, filename, kind)
    except (import_infer.InferError, ip.FormatMismatch, ip.SpecError):
        return [], None
    return (d.spec.header_cells if d.spec.has_header else []), d.to_json()


def _kind_fits(spec: ip.Spec, content: bytes, filename: str) -> bool:
    return (spec.file_type == "xlsx") == ip.is_xlsx(content, filename)


def _overlap(old: list[str], new: list[str]) -> float:
    o = {ip.norm(c) for c in old if ip.norm(c)}
    return len(o & {ip.norm(c) for c in new}) / len(o) if o else 0.0


def repoint(spec: ip.Spec, new_header: list[str], inferred: dict | None) -> dict:
    """
    The old format aimed at a changed header: roles keep their column wherever its
    header text still exists; roles whose column is gone take the inferred draft's
    column, if it found one.
    """
    data = spec.to_json()
    data["header_cells"] = list(new_header)
    present = {ip.norm(c) for c in new_header}
    inferred_cols = ((inferred or {}).get("spec") or {}).get("columns", {})
    cols = {}
    for role, refs in data["columns"].items():
        kept = [r for r in refs if r.get("header") is None or ip.norm(r["header"]) in present]
        if kept:
            cols[role] = kept
        elif role in inferred_cols:
            cols[role] = inferred_cols[role]
        elif any(r.get("optional") for r in refs):
            continue
    data["columns"] = cols
    return data


@dataclass
class Evaluation:
    exact: list = field(default_factory=list)        # (score, Candidate, ParsedFile): header formats that read it
    positional: list = field(default_factory=list)   # (score, Candidate, ParsedFile): headerless formats that read it
    drifted: list = field(default_factory=list)      # (overlap, Candidate, ParsedFile | None, header, diff)
    file_header: list[str] | None = None
    inferred: dict | None = None
    content: bytes = field(default=b"", repr=False)
    filename: str = ""
    kind: str | None = None

    def header(self) -> list[str]:
        """The file's own header as inference reads it (computed once; also fills ``inferred``)."""
        if self.file_header is None:
            self.file_header, self.inferred = _file_header(self.content, self.filename, self.kind)
        return self.file_header

    def matches(self) -> list:
        """
        Formats that read the file, best first. A file that clearly resembles one of the
        header formats (half its columns or more) is that export with a changed header:
        headerless formats don't get to read it by position then.
        """
        out = sorted(self.exact, key=lambda t: t[0], reverse=True)
        if not self.drifted:
            out += sorted(self.positional, key=lambda t: t[0], reverse=True)
        return out


def evaluate(content: bytes, filename: str, candidates: list[Candidate], kind: str | None = None) -> Evaluation:
    ev = Evaluation(content=content, filename=filename, kind=kind)
    for c in candidates:
        if not _kind_fits(c.spec, content, filename):
            continue
        if not c.spec.has_header:
            score = ip.fits(content, filename, c.spec)
            if score >= HEADERLESS_FIT:
                ev.positional.append((score, c, ip.parse(content, filename, c.spec)))
            continue
        try:
            parsed = ip.parse(content, filename, c.spec)
        except ip.FormatMismatch as exc:
            new_header = ev.header() or exc.header_cells
            ov = _overlap(c.spec.header_cells, new_header)
            if ov >= DRIFT_OVERLAP:
                ev.drifted.append((ov, c, None, new_header, ip.header_diff(c.spec.header_cells, new_header)))
            continue
        if parsed.fingerprint_ok is not False:
            ev.exact.append((len(parsed.rows) / max(1, len(parsed.rows) + len(parsed.errors)), c, parsed))
        else:
            # Its required columns are present but the header differs: the same export with columns
            # added or moved, only if the headers still largely agree (a format needing only
            # Date/Description/Amount reads many unrelated files).
            ov = _overlap(c.spec.header_cells, parsed.header_cells)
            if ov >= DRIFT_OVERLAP:
                ev.drifted.append((ov, c, parsed, parsed.header_cells, parsed.diff or {}))
    return ev


def choose(content: bytes, filename: str, candidates: list[Candidate], kind: str | None = None) -> Resolution:
    """The best format for a file; ``kind`` (the account's) shapes the draft when none fits."""
    ev = evaluate(content, filename, candidates, kind)
    found = ev.matches()
    if found:
        _score, c, parsed = found[0]
        return Resolution("match", candidate=c, parsed=parsed, header_cells=parsed.header_cells)
    if ev.drifted:
        _ov, c, parsed, header, diff = sorted(ev.drifted, key=lambda t: t[0], reverse=True)[0]
        ev.header()
        return Resolution("drift", candidate=c, parsed=parsed, diff=diff, header_cells=header,
                          draft=repoint(c.spec, header, ev.inferred))
    ev.header()
    return Resolution("none", draft=ev.inferred, header_cells=ev.file_header or [])


# ── Database ─────────────────────────────────────────────────────────────────

def candidate_from_row(row, links: list[tuple[str, int]] | None = None) -> Candidate:
    return Candidate(id=row.id, name=row.name, kind=row.kind, institution=row.institution or "",
                     spec=ip.Spec.from_json(json.loads(row.spec_json)), accounts=links or [])


def load(db, kind: str | None = None, account: tuple[str, int] | None = None) -> list[Candidate]:
    """Formats (of one kind, or linked to one account), with their links. Unreadable rows are skipped."""
    from models import ImportProfile, ImportProfileLink
    q = db.query(ImportProfile)
    if kind:
        q = q.filter(ImportProfile.kind == kind)
    if account:
        q = q.join(ImportProfileLink, ImportProfileLink.profile_id == ImportProfile.id).filter(
            ImportProfileLink.account_kind == account[0], ImportProfileLink.account_id == account[1])
    rows = q.order_by(ImportProfile.last_used_at.desc().nullslast(), ImportProfile.id).all()
    links: dict[int, list[tuple[str, int]]] = {}
    for link in db.query(ImportProfileLink).filter(ImportProfileLink.profile_id.in_([r.id for r in rows])).all():
        links.setdefault(link.profile_id, []).append((link.account_kind, link.account_id))
    out = []
    for r in rows:
        try:
            out.append(candidate_from_row(r, links.get(r.id, [])))
        except (ip.SpecError, ValueError, TypeError):
            continue
    return out


def summary(c: Candidate) -> dict:
    return {"id": c.id, "name": c.name, "kind": c.kind, "institution": c.institution,
            "row_source": c.spec.row_source if c.kind == "cash" else None}


def resolve_for_account(db, content: bytes, filename: str, account_kind: str, account_id: int,
                        profile_id: int | None = None) -> Resolution:
    """
    The format to read ``content`` for one account. ``profile_id`` forces one of the
    account's linked formats. With no linked match, formats of the same kind that
    read the file exactly are offered in ``notes`` (as "use format N") by the caller.
    """
    linked = load(db, account=(account_kind, account_id))
    if profile_id is not None:
        linked = [c for c in linked if c.id == profile_id]
        if not linked:
            _h, draft = _file_header(content, filename, account_kind)
            return Resolution("none", draft=draft, notes=["That format isn't linked to this account."])
    return choose(content, filename, linked, account_kind)


def unlinked_matches(db, content: bytes, filename: str, kind: str, account_id: int) -> list[Candidate]:
    """Saved formats of this kind, not linked to the account, that read the file exactly."""
    out = []
    for c in load(db, kind=kind):
        if (kind, account_id) in c.accounts:
            continue
        r = choose(content, filename, [c])
        if r.status == "match":
            out.append(c)
    return out


def problem(db, res: Resolution, content: bytes, filename: str, kind: str, account_id: int):
    """The HTTPException for a resolution that isn't a match (structured, for the import dialog)."""
    from fastapi import HTTPException
    if res.status == "drift":
        c = res.candidate
        return HTTPException(status_code=409, detail={
            "code": "profile_drift",
            "message": f"This file's columns have changed since the format \"{c.name}\" was saved. "
                       "Check the updated format before importing.",
            "profile": summary(c), "diff": res.diff, "header_cells": res.header_cells, "draft": res.draft,
        })
    others = unlinked_matches(db, content, filename, kind, account_id)
    return HTTPException(status_code=422, detail={
        "code": "no_profile",
        "message": (res.notes[0] + " " if res.notes else "")
                   + ("A saved format reads this file: use it for this account?" if others
                      else "Flow hasn't seen this file layout for this account yet. Set up its format first."),
        "matches": [summary(c) for c in others], "draft": res.draft,
    })


def sign_problem(check: ip.SignCheck, c: Candidate):
    from fastapi import HTTPException
    return HTTPException(status_code=422, detail={
        "code": "sign_reversed", "profile": summary(c),
        "message": f"Money in and out look reversed with the format \"{c.name}\": {check.reason}. "
                   "Fix the format's sign before importing.",
    })


def mark_previewed(db, c: Candidate, parsed: ip.ParsedFile) -> None:
    """A format imported from a JSON file has now read a real file cleanly (an import check counts)."""
    from models import ImportProfile
    if parsed.rows and not parsed.errors:
        row = db.get(ImportProfile, c.id)
        if row is not None and row.needs_preview:
            row.needs_preview = False
            db.commit()


def touch(db, c: Candidate) -> None:
    from models import ImportProfile
    row = db.get(ImportProfile, c.id)
    if row is not None:
        row.last_used_at = datetime.now(timezone.utc)


def link(db, profile_id: int, account_kind: str, account_id: int) -> None:
    from models import ImportProfileLink
    exists = db.query(ImportProfileLink).filter_by(profile_id=profile_id, account_kind=account_kind,
                                                   account_id=account_id).first()
    if exists is None:
        db.add(ImportProfileLink(profile_id=profile_id, account_kind=account_kind, account_id=account_id))


def unlink_account(db, account_kind: str, account_id: int) -> None:
    """An account is being deleted: its links go (the formats stay, for other accounts)."""
    from models import ImportProfileLink
    db.query(ImportProfileLink).filter_by(account_kind=account_kind, account_id=account_id).delete(
        synchronize_session=False)
