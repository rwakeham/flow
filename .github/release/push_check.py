#!/usr/bin/env python3
"""
Claude Code PreToolUse hook (.claude/settings.json): before a ``git push`` runs, privacy-check
what is about to leave the machine. Reads the hook's JSON on stdin; a command that is not a
push passes untouched. A push runs ``release.py preview`` on HEAD (the tracked files of the
commit and the release notes) and is refused (exit 2, the reason on stderr) on any finding.

The denylist is read as everywhere else: $PUBLISH_DENYLIST or $PUBLISH_DENYLIST_FILE. With
neither set, the built-in rules still run and the preview says the denylist was not loaded;
CI applies it on the pull request before anything merges.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
PUSH = re.compile(r"\bgit\b[^;&|\n]*\bpush\b")


def is_push(command: str) -> bool:
    return bool(PUSH.search(command or ""))


def main() -> int:
    try:
        event = json.load(sys.stdin)
    except (json.JSONDecodeError, OSError):
        return 0
    command = (event.get("tool_input") or {}).get("command", "")
    if not is_push(command):
        return 0
    out = tempfile.mkdtemp(prefix="flow-push-check-")
    run = subprocess.run([sys.executable, str(HERE / "release.py"), "preview", "--out", out],
                         capture_output=True, text=True)
    if run.returncode == 0:
        return 0
    print("Push refused by the privacy check (.github/release/push_check.py). Fix the findings, "
          "amend the commit, and push again; a denylist hit is never allowlisted.\n"
          + run.stdout + run.stderr, file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
