# Releasing Flow

Flow is developed and released in this one repository. A release is a tag `v<version>` on
`main` with a GitHub Release made from it; `.github/workflows/release.yml` makes both.

## The model

- **The version** is the one line in `backend/VERSION` (semver). The app shows it under
  Sign out ▾ and `deploy.sh` prints it. Nobody edits it by hand: the release run does.
- **Releases are manual.** **Actions → Release → Run workflow**, on `main`. Inputs:
  - **Version bump**: `auto` (default) works it out from the commits since the last release;
    `patch`, `minor` or `major` overrides that.
  - **Dry run**: work it all out and attach the preview, change nothing.
- **What `auto` picks**, highest across the commits since the last release:
  - a `Release: major|minor|patch|none` trailer in a commit message decides for that commit;
  - else `BREAKING CHANGE:` in the body, or a subject starting "Breaking", is major;
  - else a commit touching only docs, tests or `.github` is none;
  - else a subject starting Add / Introduce / New / Support / Enable / Allow is minor;
  - else patch. A release with only docs, tests or CI changes is a patch.

  The run summary lists the commits that decided it. The first release publishes
  `backend/VERSION` as it is, and a version already raised by hand above the last release is
  used as it is. With no commits since the last release, there is nothing to release.
- **A release run**, in order: tests; the next version; the bump committed locally
  (`Release <version>`); the notes (commit subjects since the previous release, merges and the
  bump commit left out); the privacy check over every tracked file and the notes; the preview
  uploaded as `flow-<version>-release-preview`. Then, unless it is a dry run: the bump pushed to
  `main` (if `main` moved since the run started, this push fails and nothing is published: run it
  again), the commit tagged `v<version>` with the notes, and the GitHub Release created from the
  tag with the same notes.
- **Pushes to `main` and pull requests** run the same steps up to the preview, so every merge
  shows what the next release would be and whether the tree passes the privacy check. They
  never commit, tag or publish.
- **If a run fails after pushing the bump**, run it again: the raised version is used as it is,
  an existing tag is kept, and a missing Release is created.

People who cloned the repository update with `git pull && ./deploy.sh`, or pin a release with
`git checkout v<version>`.

## The privacy check

`release.py check` fails the release on any of:

- secrets: private keys, Anthropic / GitHub / AWS / Slack / OpenAI keys, Claude session links
- email addresses outside placeholder domains (`example.com` and the like)
- 13 to 19 digit numbers that pass the card checksum
- home-directory paths (`/home/<name>/`, `/Users/<name>/`)
- files that never belong in public: `.env*` (but `.env.example`), keys and certificates,
  databases and dumps, zips, `flow_backup_*`, anything over 5 MB
- **the denylist**: your own list of strings that must never appear (names, account
  numbers, addresses, employer, email addresses), matched case-insensitively as whole words
  in every file, path and the notes (so a surname inside the owner's username, which is
  public anyway, is not a hit; the surname on its own is); reported by entry number only,
  so the log never repeats it

The denylist is never committed. The workflow reads it from the `PUBLISH_DENYLIST`
secret; a real release refuses to run without one. A false positive of a built-in rule
goes in `allow.txt` as `<path>:<rule>`; a denylist hit can't be allowed, only fixed.

The same built-in rules run on every pull request through `test_release.py`, over every
tracked file, so a secret or a personal email fails CI before it reaches `main`.

## Setting it up (once)

1. **Secret** in **Settings → Secrets and variables → Actions → Secrets**: `PUBLISH_DENYLIST`,
   one entry per line, at least 3 characters each.
2. **Actions permissions** in **Settings → Actions → General**: allow Actions, and give the
   workflow token **Read and write permissions** (the release run commits the version bump,
   pushes the tag and creates the Release).
3. If `main` gets branch protection, let GitHub Actions push to it: the release run commits
   the version bump there.

## Before a release: review locally

```bash
python3 .github/release/release.py preview --out /tmp/flow-preview --denylist-file ~/flow-denylist.txt
```

That writes `/tmp/flow-preview/tree` (the tracked files, as the check reads them) and
`/tmp/flow-preview/notes.md`, and runs the check. `--ref <commit>` previews another commit,
such as `origin/main`. The same preview is attached to every workflow run.

## Tests

```bash
python3 -m pytest .github/release
```
