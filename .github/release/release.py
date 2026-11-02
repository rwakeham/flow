#!/usr/bin/env python3
"""
Flow's release tool: the next version, the notes, and whether the tree is safe to publish.

A release is a commit on ``main`` whose ``backend/VERSION`` has no ``v<VERSION>`` tag yet.
The release workflow (.github/workflows/release.yml) runs this tool, commits the bump,
tags the commit with the notes and publishes the GitHub Release. See README.md beside
this file.

Commands (standard library only; run from anywhere in the checkout):

  plan                 version, tag, previous release, and whether it is released
                       (key=value lines, for $GITHUB_OUTPUT)
  next [--bump LEVEL]  the version a release run would publish: auto (from the commits
                       since the last release), patch, minor or major; key=value lines,
                       and why on stderr
  snapshot OUT         the tracked files of a commit (default HEAD), into OUT: the tree
                       the check reads
  notes OUT            release notes: commit subjects since the previous release
  check DIR [--notes F] privacy check of a tree (and the notes): secrets, personal data,
                       files that never belong in public, and the denylist
  preview [--out DIR]  snapshot + notes + check in one go, for a review before release

The denylist is personal strings that must never be published (names, account
numbers, addresses, email addresses). It is never committed: the workflow reads it
from the PUBLISH_DENYLIST secret, a local run from $PUBLISH_DENYLIST or the file
named by --denylist-file / $PUBLISH_DENYLIST_FILE. One entry per line, # comments.
"""

from __future__ import annotations

import argparse
import io
import os
import re
import subprocess
import sys
import tarfile
import tempfile
from dataclasses import dataclass
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
VERSION_PATH = "backend/VERSION"
SEMVER = re.compile(r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)$")


# ── git ──────────────────────────────────────────────────────────────────────

def git(*args: str, binary: bool = False):
    out = subprocess.run(["git", "-C", str(ROOT), *args], check=True, capture_output=True)
    return out.stdout if binary else out.stdout.decode("utf-8", "replace")


def parse_version(text: str) -> tuple[int, int, int] | None:
    m = SEMVER.match(text.strip())
    return (int(m[1]), int(m[2]), int(m[3])) if m else None


def release_tags(names: list[str]) -> list[tuple[tuple[int, int, int], str]]:
    """The v<semver> tags among ``names``, oldest version first."""
    found = []
    for n in names:
        v = parse_version(n[1:]) if n.startswith("v") else None
        if v:
            found.append((v, n))
    return sorted(found)


# ── plan ─────────────────────────────────────────────────────────────────────

@dataclass
class Plan:
    version: str
    tag: str
    released: bool
    previous: str          # "" for the first release

    def lines(self) -> list[str]:
        return [f"version={self.version}", f"tag={self.tag}",
                f"released={'true' if self.released else 'false'}", f"previous={self.previous}"]


def make_plan(version_text: str, tags: list[str]) -> Plan:
    """What releasing this VERSION means. Raises ValueError for a VERSION that can't be released."""
    v = parse_version(version_text)
    if v is None:
        raise ValueError(f"{VERSION_PATH} must be MAJOR.MINOR.PATCH, not {version_text.strip()!r}")
    version = version_text.strip()
    tag = f"v{version}"
    known = release_tags(tags)
    if tag in {n for _, n in known}:
        older = [n for ver, n in known if ver < v]
        return Plan(version, tag, True, older[-1] if older else "")
    if known and known[-1][0] > v:
        raise ValueError(f"{VERSION_PATH} is {version}, below the latest release {known[-1][1]}: "
                         "bump it above that")
    return Plan(version, tag, False, known[-1][1] if known else "")


def current_plan(ref: str = "HEAD") -> Plan:
    try:
        version_text = git("show", f"{ref}:{VERSION_PATH}")
    except subprocess.CalledProcessError:
        raise ValueError(f"{ref} has no {VERSION_PATH}") from None
    return make_plan(version_text, git("tag", "-l", "v*").split())


# ── next version (the bump a release makes) ──────────────────────────────────

LEVELS = ("none", "patch", "minor", "major")
TRAILER = re.compile(r"^Release:\s*(major|minor|patch|none)\s*$", re.I | re.M)
BREAKING = re.compile(r"^BREAKING[ -]CHANGE\b|^Breaking:", re.M)
FEATURE_VERBS = {"add", "adds", "introduce", "introduces", "new", "support", "supports", "enable", "allow"}
# Paths whose changes alone don't change Flow for its users (they still ship, with the next change).
NOT_USER_FACING = re.compile(r"^(docs/|tests/|\.github/|\.claude/|backend/test_[^/]*\.py$|backend/testdata/|[^/]*\.md$)")


@dataclass
class Commit:
    sha: str
    subject: str
    body: str
    files: list[str]


def commit_level(c: Commit) -> tuple[str, str]:
    """(level, why) for one commit: a Release: trailer wins, then breaking-change markers,
    then a docs/tests-only change (none), then the subject's first word (feature or fix)."""
    m = TRAILER.search(c.body)
    if m:
        return m.group(1).lower(), "Release: trailer"
    if BREAKING.search(c.body) or c.subject.lower().startswith("breaking"):
        return "major", "marked breaking"
    if c.files and all(NOT_USER_FACING.match(f) for f in c.files):
        return "none", "docs, tests or CI only"
    first = (c.subject.split() or [""])[0].lower().rstrip(":,")
    if first in FEATURE_VERBS:
        return "minor", f"subject starts with {first.capitalize()!r}"
    return "patch", "a fix or change"


def bump(version: str, level: str) -> str:
    major, minor, patch = parse_version(version)
    if level == "major":
        return f"{major + 1}.0.0"
    if level == "minor":
        return f"{major}.{minor + 1}.0"
    return f"{major}.{minor}.{patch + 1}"


@dataclass
class Next:
    current: str           # backend/VERSION now
    version: str           # what this release will be
    level: str             # initial | manual | patch | minor | major
    previous: str          # the latest release tag, "" for the first
    reasons: list[str]     # one line per commit that decided the level
    trivial: bool = False  # only docs, tests or CI changed since the last release: an automatic
                           # release waits for a user-facing change; a manual one is a patch

    def lines(self) -> list[str]:
        return [f"current={self.current}", f"version={self.version}", f"tag=v{self.version}",
                f"level={self.level}", f"previous={self.previous}",
                f"bumped={'true' if self.version != self.current else 'false'}",
                f"trivial={'true' if self.trivial else 'false'}"]


def next_release(version_text: str, tags: list[str], commits: list[Commit], requested: str = "auto") -> Next:
    """The version a release publishes. The first release publishes VERSION as it is; a
    VERSION already raised by hand above the latest release is used as it is; otherwise it is
    bumped by ``requested``, or for "auto" by the highest level among the commits since then."""
    v = parse_version(version_text)
    if v is None:
        raise ValueError(f"{VERSION_PATH} must be MAJOR.MINOR.PATCH, not {version_text.strip()!r}")
    current = version_text.strip()
    known = release_tags(tags)
    if not known:
        return Next(current, current, "initial", "", ["first release: backend/VERSION as it is"])
    latest_v, latest = known[-1]
    if v > latest_v:
        return Next(current, current, "manual", latest, [f"backend/VERSION already raised above {latest}"])
    if v < latest_v:
        raise ValueError(f"{VERSION_PATH} is {current}, below the latest release {latest}")
    if not commits:
        raise ValueError(f"nothing to release: no commits since {latest}")
    if requested != "auto":
        if requested not in LEVELS[1:]:
            raise ValueError(f"unknown bump {requested!r}")
        return Next(current, bump(current, requested), requested, latest, [f"{requested} chosen when the release was run"])
    judged = [(c, *commit_level(c)) for c in commits]
    top = max((lvl for _, lvl, _ in judged), key=LEVELS.index)
    level = top if top != "none" else "patch"     # a release asked for is at least a patch
    reasons = [f"{lvl}: {c.subject} ({why})" for c, lvl, why in judged if lvl == top]
    if top == "none":
        reasons = ["patch: only docs, tests or CI changed since " + latest]
    return Next(current, bump(current, level), level, latest, reasons, trivial=top == "none")


def commits_since(tag: str, ref: str = "HEAD") -> list[Commit]:
    if not tag:
        return []
    out = []
    log = git("log", "--no-merges", "--format=%H%x1f%s%x1f%b%x1e", f"{tag}..{ref}")
    for rec in filter(None, (r.strip("\n") for r in log.split("\x1e"))):
        sha, subject, body = (rec.split("\x1f") + ["", ""])[:3]
        files = git("show", "--name-only", "--format=", sha).split()
        out.append(Commit(sha, subject, body, files))
    return out


def current_next(requested: str = "auto", ref: str = "HEAD") -> Next:
    tags = git("tag", "-l", "v*").split()
    known = release_tags(tags)
    commits = commits_since(known[-1][1] if known else "", ref)
    return next_release(git("show", f"{ref}:{VERSION_PATH}"), tags, commits, requested)


# ── snapshot ─────────────────────────────────────────────────────────────────

def load_list(path: Path) -> list[str]:
    if not path.exists():
        return []
    return [l.strip() for l in path.read_text(encoding="utf-8").splitlines()
            if l.strip() and not l.strip().startswith("#")]


def snapshot(out: Path, ref: str = "HEAD") -> list[str]:
    """Write the tracked files of ``ref`` into ``out``: the tree a release publishes, as the
    check reads it. Returns the paths."""
    if out.exists() and any(out.iterdir()):
        raise SystemExit(f"{out} is not empty")
    out.mkdir(parents=True, exist_ok=True)
    published = []
    with tarfile.open(fileobj=io.BytesIO(git("archive", "--format=tar", ref, binary=True))) as tar:
        members = [m for m in tar.getmembers() if m.isfile() or m.isdir() or m.issym()]
        published = [m.name for m in members if m.isfile() or m.issym()]
        kwargs = {"filter": "data"} if hasattr(tarfile, "data_filter") else {}
        tar.extractall(out, members=members, **kwargs)
    return sorted(published)


# ── notes ────────────────────────────────────────────────────────────────────

RELEASE_COMMIT = re.compile(r"^Release \d+\.\d+\.\d+$")     # the workflow's own version bump


def clean_subject(subject: str) -> str | None:
    """A commit subject as a release-note line; merges and the bump commit are left out.
    A pull request number ("(#41)") stays: GitHub links it to the pull request."""
    s = subject.strip()
    if not s or s.lower().startswith(("merge ", "wip")) or RELEASE_COMMIT.match(s):
        return None
    return s


def make_notes(version: str, previous: str, subjects: list[str]) -> str:
    lines = [f"## Flow {version}", ""]
    if previous:
        items = [c for c in (clean_subject(s) for s in subjects) if c]
        lines += ["### Changes", ""] + [f"- {c}" for c in items or ["Maintenance release."]]
    else:
        lines += ["First public release."]
    lines += ["", "### Updating", "",
              "From your Flow folder: `git pull && ./deploy.sh`. To run exactly this release instead:",
              f"`git fetch --tags && git checkout v{version} && ./deploy.sh`.", ""]
    return "\n".join(lines)


def notes_for(plan: Plan, ref: str = "HEAD") -> str:
    subjects = []
    if plan.previous:
        subjects = git("log", "--no-merges", "--reverse", "--format=%s", f"{plan.previous}..{ref}").splitlines()
    return make_notes(plan.version, plan.previous, subjects)


# ── check ────────────────────────────────────────────────────────────────────

SECRET_RULES = [
    ("private-key", re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----")),
    ("anthropic-key", re.compile(r"sk-ant-[A-Za-z0-9_-]{20,}")),
    ("github-token", re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{30,})")),
    ("aws-key", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")),
    ("slack-token", re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}")),
    ("openai-key", re.compile(r"\bsk-(?:proj-)?[A-Za-z0-9]{32,}")),
    ("claude-session-link", re.compile(r"claude\.ai/code/session_\w+")),
]
EMAIL = re.compile(r"\b[A-Za-z0-9._%+-]+@([A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,})\b")
# Placeholder and service addresses that are fine in public text.
EMAIL_DOMAINS_OK = {"example.com", "example.org", "example.net", "users.noreply.github.com",
                    "noreply.github.com", "anthropic.com"}
DIGIT_RUN = re.compile(r"(?<![\d.])\d{13,19}(?![\d.])")
HOME_PATH = re.compile(r"(?:/home/|/Users/|C:\\Users\\)(?!user\b|runner\b|<)[A-Za-z][\w.-]*")

FORBIDDEN_NAMES = re.compile(r"(^|/)(\.env(\.(?!example$)[^/]+)?|id_[a-z0-9]+|flow_backup_[^/]*)$")
FORBIDDEN_SUFFIXES = (".pem", ".key", ".p12", ".pfx", ".sqlite", ".sqlite3", ".db", ".dump", ".bak", ".zip")
MAX_FILE_BYTES = 5 * 1024 * 1024


def luhn(digits: str) -> bool:
    total, alt = 0, False
    for ch in reversed(digits):
        d = int(ch)
        if alt:
            d = d * 2 - 9 if d > 4 else d * 2
        total += d
        alt = not alt
    return total % 10 == 0


def looks_like_card(digits: str) -> bool:
    return len(set(digits)) > 2 and luhn(digits) and digits[0] in "23456"


@dataclass
class Finding:
    path: str
    line: int
    rule: str
    detail: str

    def __str__(self) -> str:
        where = f"{self.path}:{self.line}" if self.line else self.path
        return f"{where} [{self.rule}] {self.detail}"


def load_denylist(file: str | None) -> list[str]:
    raw = os.environ.get("PUBLISH_DENYLIST", "")
    path = file or os.environ.get("PUBLISH_DENYLIST_FILE")
    if path:
        p = Path(path).expanduser()
        if not p.is_file():
            raise SystemExit(f"denylist file {p} not found: create it with one entry per line (names, account "
                             "numbers, addresses, an employer, personal email addresses; # comments allowed), "
                             "or run without --denylist-file to use the built-in rules only")
        raw += "\n" + p.read_text(encoding="utf-8")
    terms = []
    for line in raw.splitlines():
        t = line.strip()
        if not t or t.startswith("#"):
            continue
        if len(t) < 3:
            raise SystemExit(f"denylist entry {t!r} is shorter than 3 characters: it would match everywhere")
        terms.append(t.lower())
    return terms


def mask(value: str) -> str:
    return value[:6] + "…" if len(value) > 6 else "…"


def denylist_hit(text_lower: str, term: str) -> int:
    """Where ``term`` occurs in the lower-cased text as a whole word, or -1. Whole words, so a
    surname in the repository owner's username ("jpublic") is not the surname on its own, while
    "jane public", "public jane" and a bare account number still are."""
    m = re.search(r"(?<![a-z0-9])" + re.escape(term) + r"(?![a-z0-9])", text_lower)
    return m.start() if m else -1


def scan_text(path: str, text: str, deny: list[str], binary: bool) -> list[Finding]:
    found: list[Finding] = []
    lower = text.lower()
    for i, term in enumerate(deny, 1):
        pos = denylist_hit(lower, term)
        if pos >= 0:
            found.append(Finding(path, 0 if binary else text.count("\n", 0, pos) + 1,
                                 "denylist", f"entry {i} of the denylist"))
    for rule, rx in SECRET_RULES:
        for m in rx.finditer(text):
            found.append(Finding(path, 0 if binary else text.count("\n", 0, m.start()) + 1, rule, mask(m.group(0))))
    if binary:
        return found
    for m in EMAIL.finditer(text):
        domain = m.group(1).lower()
        if domain not in EMAIL_DOMAINS_OK and not domain.endswith((".example", ".test", ".invalid", ".local")):
            found.append(Finding(path, text.count("\n", 0, m.start()) + 1, "email", m.group(0)))
    for m in DIGIT_RUN.finditer(text):
        if looks_like_card(m.group(0)):
            found.append(Finding(path, text.count("\n", 0, m.start()) + 1, "card-number", mask(m.group(0))))
    for m in HOME_PATH.finditer(text):
        found.append(Finding(path, text.count("\n", 0, m.start()) + 1, "home-path", m.group(0)))
    return found


def check(tree: Path, notes: Path | None, deny: list[str]) -> list[Finding]:
    allow = set(load_list(HERE / "allow.txt"))
    findings: list[Finding] = []
    for f in sorted(p for p in tree.rglob("*") if p.is_file() or p.is_symlink()):
        rel = f.relative_to(tree).as_posix()
        if FORBIDDEN_NAMES.search(rel) or rel.lower().endswith(FORBIDDEN_SUFFIXES):
            findings.append(Finding(rel, 0, "forbidden-file", "this kind of file is never published"))
        for i, term in enumerate(deny, 1):
            if denylist_hit(rel.lower(), term) >= 0:
                findings.append(Finding(rel, 0, "denylist", f"entry {i} of the denylist (in the path)"))
        if f.is_symlink():
            continue
        data = f.read_bytes()
        if len(data) > MAX_FILE_BYTES:
            findings.append(Finding(rel, 0, "large-file", f"{len(data) // 1024} KB"))
        binary = b"\0" in data[:8192]
        text = data.decode("latin-1") if binary else data.decode("utf-8", "replace")
        findings += scan_text(rel, text, deny, binary)
    if notes is not None:
        findings += scan_text("(release notes)", notes.read_text(encoding="utf-8"), deny, False)
    return [x for x in findings if x.rule == "denylist" or f"{x.path}:{x.rule}" not in allow]


def report(findings: list[Finding], deny: list[str], files: int) -> int:
    print(f"Privacy check: {files} files, {len(deny)} denylist entries.")
    if not deny:
        print("warning: no denylist loaded; personal names and numbers are only caught by the built-in rules")
    for x in findings:
        print(f"  ✗ {x}")
    if findings:
        print(f"{len(findings)} finding(s). Fix them (or, for a false positive of a built-in rule, "
              f"add '<path>:<rule>' to .github/release/allow.txt).")
        return 1
    print("  ✓ nothing found")
    return 0


# ── commands ─────────────────────────────────────────────────────────────────

def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("plan")
    p = sub.add_parser("next"); p.add_argument("--bump", default="auto", choices=("auto", "patch", "minor", "major"))
    p.add_argument("--ok-if-nothing", action="store_true", help="print nothing=true instead of failing")
    p = sub.add_parser("snapshot"); p.add_argument("out", type=Path); p.add_argument("--ref", default="HEAD")
    p = sub.add_parser("notes"); p.add_argument("out", type=Path); p.add_argument("--ref", default="HEAD")
    p = sub.add_parser("check"); p.add_argument("tree", type=Path); p.add_argument("--notes", type=Path)
    p.add_argument("--denylist-file"); p.add_argument("--require-denylist", action="store_true")
    p = sub.add_parser("preview"); p.add_argument("--out", type=Path); p.add_argument("--ref", default="HEAD")
    p.add_argument("--denylist-file")
    args = ap.parse_args(argv)

    if args.cmd == "plan":
        try:
            print("\n".join(current_plan().lines()))
        except ValueError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
        return 0
    if args.cmd == "next":
        try:
            nxt = current_next(args.bump)
        except (ValueError, subprocess.CalledProcessError) as exc:
            if args.ok_if_nothing and str(exc).startswith("nothing to release"):
                print("nothing=true")
                print(str(exc), file=sys.stderr)
                return 0
            print(f"error: {exc}", file=sys.stderr)
            return 1
        print("\n".join(nxt.lines() + ["nothing=false"]))
        print(f"Flow {nxt.current} -> {nxt.version} ({nxt.level})", file=sys.stderr)
        for r in nxt.reasons:
            print(f"  {r}", file=sys.stderr)
        return 0
    if args.cmd == "snapshot":
        print(f"{len(snapshot(args.out, args.ref))} files -> {args.out}")
        return 0
    if args.cmd == "notes":
        try:
            plan = current_plan(args.ref)
        except ValueError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
        args.out.write_text(notes_for(plan, args.ref), encoding="utf-8")
        return 0
    if args.cmd == "check":
        deny = load_denylist(args.denylist_file)
        if args.require_denylist and not deny:
            print("error: no denylist (the PUBLISH_DENYLIST secret is empty); refusing to publish", file=sys.stderr)
            return 1
        files = sum(1 for p in args.tree.rglob("*") if p.is_file())
        return report(check(args.tree, args.notes, deny), deny, files)
    if args.cmd == "preview":
        out = args.out or Path(tempfile.mkdtemp(prefix="flow-release-"))
        tree = out / "tree"
        files = snapshot(tree, args.ref)
        notes = None
        try:
            plan = current_plan(args.ref)
        except ValueError as exc:    # e.g. a commit from before the version file: still worth checking
            print(f"warning: {exc}; no release notes")
        else:
            notes = out / "notes.md"
            notes.write_text(notes_for(plan, args.ref), encoding="utf-8")
            state = "already released" if plan.released else "not released yet"
            print(f"Flow {plan.version} ({plan.tag}, {state}; previous: {plan.previous or 'none'})")
        print(f"Snapshot: {len(files)} files in {tree}" + (f"\nNotes:    {notes}" if notes else "") + "\n")
        deny = load_denylist(args.denylist_file)
        return report(check(tree, notes, deny), deny, len(files))
    return 2


if __name__ == "__main__":
    sys.exit(main())
