"""
Tests for the release tool (release.py): version planning, the release notes and the
privacy check. Run with ``python3 -m pytest .github/release``. One test checks every
tracked file of the working tree, so a secret or a stray personal email fails CI here,
before any release.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location("release", HERE / "release.py")
R = importlib.util.module_from_spec(spec)
sys.modules["release"] = R
spec.loader.exec_module(R)


# ── plan ─────────────────────────────────────────────────────────────────────

def test_first_release():
    p = R.make_plan("3.0.0\n", [])
    assert (p.version, p.tag, p.released, p.previous) == ("3.0.0", "v3.0.0", False, "")


def test_a_bump_above_the_latest_tag_is_a_release():
    p = R.make_plan("3.1.0", ["v3.0.0", "v2.9.9", "not-a-release", "v3.0.1"])
    assert (p.released, p.previous) == (False, "v3.0.1")


def test_an_existing_tag_means_already_released():
    p = R.make_plan("3.0.1", ["v3.0.0", "v3.0.1"])
    assert (p.released, p.previous) == (True, "v3.0.0")


def test_versions_compare_as_numbers_not_text():
    assert R.make_plan("3.10.0", ["v3.9.0"]).previous == "v3.9.0"


@pytest.mark.parametrize("bad", ["v3.0.0", "3.0", "3.0.0-rc.1", "", "03.0.0"])
def test_a_malformed_version_is_refused(bad):
    with pytest.raises(ValueError):
        R.make_plan(bad, [])


def test_a_version_below_the_latest_release_is_refused():
    with pytest.raises(ValueError, match="below the latest"):
        R.make_plan("3.0.0", ["v3.1.0"])


# ── notes ────────────────────────────────────────────────────────────────────

def test_notes_keep_pr_numbers_and_drop_merges():
    assert R.clean_subject("Add bulk import (#41)") == "Add bulk import (#41)"       # links to the pull request
    assert R.clean_subject("Merge claude/x into main") is None
    assert R.clean_subject("  ") is None
    notes = R.make_notes("3.1.0", "v3.0.0", ["Add bulk import (#41)", "Merge pull request #9", "Fix sign check"])
    assert "- Add bulk import (#41)\n- Fix sign check" in notes and "Merge" not in notes
    assert "git checkout v3.1.0" in notes


def test_the_first_release_has_no_change_list():
    notes = R.make_notes("3.0.0", "", ["anything"])
    assert "First public release." in notes and "anything" not in notes


# ── privacy check ────────────────────────────────────────────────────────────

def _scan(text, deny=()):
    return {f.rule for f in R.scan_text("f.txt", text, list(deny), binary=False)}


def test_secrets_are_found():
    assert "anthropic-key" in _scan("ANTHROPIC_API_KEY=sk-ant-api03-" + "a" * 40)
    assert "github-token" in _scan("token ghp_" + "A1" * 18)
    assert "aws-key" in _scan("AKIAABCDEFGHIJKLMNOP")
    assert "private-key" in _scan("-----BEGIN OPENSSH PRIVATE KEY-----")
    assert "claude-session-link" in _scan("Claude-Session: https://claude.ai/code/session_01AbC")


def test_placeholders_are_not_secrets():
    assert not _scan('if [[ "$NEW_KEY" != sk-ant-* ]]; then')
    assert not _scan("ANTHROPIC_API_KEY=\nFLOW_PASSWORD=<password>")


def test_personal_emails_are_found_but_placeholders_pass():
    assert "email" in _scan("contact jane.doe@gmail.com")
    assert not _scan("someone@example.com, noreply@anthropic.com, 4123+bot@users.noreply.github.com")


def test_card_numbers_need_a_valid_checksum():
    assert "card-number" in _scan("card 4539578763621486 charged")       # made up, Luhn-valid
    assert not _scan("id 4539578763621487")                              # checksum fails
    assert not _scan("test card 4111111111111111")                      # the well-known test number
    assert not _scan("ts 1730419200000000")                             # not a card prefix


def test_home_paths_are_found_except_generic_ones():
    assert "home-path" in _scan("see /home/jsmith/flow")
    assert not _scan("/home/user/flow and /home/runner/work and /Users/<you>/flow")


def test_denylist_terms_are_found_case_insensitively_and_never_printed():
    found = R.scan_text("f.txt", "Prepared for JANE Q PUBLIC", ["jane q public"], binary=False)
    assert [f.rule for f in found] == ["denylist"]
    assert "jane" not in str(found[0]).lower()


def test_denylist_terms_match_whole_words_only():
    """The owner's username holds the surname ("jpublic"): that is public by definition and
    not a hit; the surname on its own, or beside another name, is."""
    assert not _scan("git clone https://github.com/jpublic/flow.git", ["public"])
    assert not _scan("1234567 and 712345", ["1234"])
    assert "denylist" in _scan("ZELLE FROM PUBLIC JANE ON 08/10", ["public"])
    assert "denylist" in _scan("Prepared for Jane Public.", ["public"])
    assert "denylist" in _scan("card ending in 1234", ["1234"])


def test_binary_files_are_checked_for_the_denylist_and_secrets_only():
    text = "\x89PNG\x00 Jane Q Public sk-ant-" + "b" * 30 + " x@gmail.com"
    rules = {f.rule for f in R.scan_text("a.png", text, ["jane q public"], binary=True)}
    assert rules == {"denylist", "anthropic-key"}


def test_files_that_never_belong_in_public(tmp_path):
    for name in (".env", ".env.local", "backups/flow_backup_20260101_000000_ab12cd34.zip", "id_ed25519",
                 "cert.pem", "data.sqlite"):
        (tmp_path / name).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / name).write_text("x")
    (tmp_path / ".env.example").write_text("SECRET_KEY=\n")
    found = {f.path for f in R.check(tmp_path, None, []) if f.rule == "forbidden-file"}
    assert ".env.example" not in found and len(found) == 6


def test_short_denylist_terms_are_refused(monkeypatch):
    monkeypatch.setenv("PUBLISH_DENYLIST", "ok term\nab")
    with pytest.raises(SystemExit):
        R.load_denylist(None)


def test_every_tracked_file_passes_the_built_in_rules(tmp_path):
    """The working tree's tracked files, as a release would publish them (denylist aside)."""
    import subprocess
    files = subprocess.run(["git", "-C", str(R.ROOT), "ls-files", "-z"], capture_output=True, check=True).stdout
    tree = tmp_path / "public"
    for rel in filter(None, files.decode().split("\0")):
        src = R.ROOT / rel
        if not src.is_file():
            continue
        dst = tree / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.write_bytes(src.read_bytes())
    findings = R.check(tree, None, [])
    assert not findings, "\n".join(map(str, findings))


# ── next version ─────────────────────────────────────────────────────────────

def _c(subject, body="", files=("backend/main.py",)):
    return R.Commit("0" * 40, subject, body, list(files))


def test_the_first_release_is_the_version_as_it_is():
    n = R.next_release("3.0.0\n", [], [], "minor")
    assert (n.version, n.level) == ("3.0.0", "initial")


def test_a_hand_raised_version_is_used_as_it_is():
    n = R.next_release("3.2.0", ["v3.0.0"], [_c("Fix a thing")])
    assert (n.version, n.level) == ("3.2.0", "manual")


def test_auto_takes_the_highest_level_among_the_commits():
    commits = [_c("Fix the sign check"), _c("Add a budget page"), _c("Tidy the README", files=["README.md"])]
    n = R.next_release("3.0.0", ["v3.0.0"], commits)
    assert (n.version, n.level) == ("3.1.0", "minor")
    assert n.reasons == ["minor: Add a budget page (subject starts with 'Add')"]


def test_trailers_win_over_the_subject():
    assert R.commit_level(_c("Add a typo fix", "Explained.\n\nRelease: patch"))[0] == "patch"
    assert R.commit_level(_c("Rework the register", "Release: minor"))[0] == "minor"
    n = R.next_release("3.4.2", ["v3.4.2"], [_c("Fix x"), _c("Rename a setting", "Release: major\nCo-Authored-By: x")])
    assert (n.version, n.level) == ("4.0.0", "major")


def test_breaking_markers_mean_major():
    assert R.commit_level(_c("Drop the old importer", "BREAKING CHANGE: formats must be set up again"))[0] == "major"
    assert R.commit_level(_c("Breaking: .env needs FLOW_X"))[0] == "major"


def test_docs_alone_are_a_patch_but_tests_and_ci_wait():
    docs = _c("Reword the quick start", files=["README.md", "docs/screenshots/a.png"])
    assert R.commit_level(docs)[0] == "patch"
    assert R.commit_level(_c("Docs and CI", files=["README.md", ".github/workflows/ci.yml"]))[0] == "patch"
    internal = [_c("More tests", files=["backend/test_cards.py", "tests/test_deploy_script.py"]),
                _c("Pin actions", files=[".github/workflows/ci.yml", ".claude/settings.json"]),
                _c("Update guide", files=["CLAUDE.md"])]
    assert all(R.commit_level(c)[0] == "none" for c in internal)
    n = R.next_release("3.1.0", ["v3.1.0"], internal)
    assert (n.version, n.level) == ("3.1.1", "patch")
    # ... and marked trivial, so a push to main doesn't release until something user-facing lands.
    assert n.trivial and "trivial=true" in n.lines()
    assert not R.next_release("3.1.0", ["v3.1.0"], internal + [docs]).trivial
    assert not R.next_release("3.1.0", ["v3.1.0"], internal + [_c("Fix a thing")]).trivial


def test_a_chosen_level_overrides_auto():
    assert R.next_release("3.1.4", ["v3.1.4"], [_c("Add x")], "patch").version == "3.1.5"
    assert R.next_release("3.1.4", ["v3.1.4"], [_c("Fix x")], "major").version == "4.0.0"


def test_nothing_to_release_and_a_lowered_version_are_refused():
    with pytest.raises(ValueError, match="nothing to release"):
        R.next_release("3.0.0", ["v3.0.0"], [])
    with pytest.raises(ValueError, match="below the latest"):
        R.next_release("2.9.0", ["v3.0.0"], [_c("Fix")])


def test_the_bump_commit_is_left_out_of_the_notes():
    assert R.clean_subject("Release 3.1.0") is None
    assert R.clean_subject("Release notes are clearer") == "Release notes are clearer"


# ── the pre-push hook (.claude/settings.json) ────────────────────────────────

def _hook(command, env=None):
    """Run push_check.py as Claude Code does: the tool call as JSON on stdin."""
    import json
    import os
    import subprocess
    return subprocess.run([sys.executable, str(HERE / "push_check.py")],
                          input=json.dumps({"tool_name": "Bash", "tool_input": {"command": command}}),
                          capture_output=True, text=True, env={**os.environ, **(env or {})})


def test_the_push_hook_only_acts_on_a_git_push():
    spec = importlib.util.spec_from_file_location("push_check", HERE / "push_check.py")
    P = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(P)
    assert P.is_push("git push -u origin feature")
    assert P.is_push("cd backend && git -C .. push origin main 2>&1 | tail -1")
    assert not P.is_push("git status") and not P.is_push("echo push it") and not P.is_push("")
    r = _hook("git status")
    assert (r.returncode, r.stdout, r.stderr) == (0, "", "")


def test_the_push_hook_passes_a_clean_commit_and_refuses_a_denylist_hit():
    """HEAD of this repository passes the built-in rules (the test above proves the tree does);
    a denylist term that the tree contains refuses the push, without printing the term."""
    r = _hook("git push -u origin feature")
    assert r.returncode == 0, r.stderr
    r = _hook("git push -u origin feature", {"PUBLISH_DENYLIST": "fastapi", "PUBLISH_DENYLIST_FILE": ""})
    assert r.returncode == 2
    assert "Push refused" in r.stderr and "entry 1 of the denylist" in r.stderr
    assert "fastapi" not in r.stderr.lower()
