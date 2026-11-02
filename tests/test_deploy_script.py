"""Behaviour tests for deploy.sh and setup.sh, run without deploying anything.

Adapted from a deployment-test template: the block marked "Adjust for this repo" holds
this repo's settings, and case 5b (placeholder secrets) is Flow's own. Run it with either of:

    python3 tests/test_deploy_script.py
    pytest tests/test_deploy_script.py

Each case copies the real deploy.sh into a throwaway directory (a git checkout with a
bare origin, or no git at all), puts `docker`, `curl` and `sleep` shims first on PATH,
and runs it. Nothing is built, started or pushed anywhere real. The cases pin the rules
whose plausible-looking wrong implementation passes a grep:

- git.hands-off: deploy.sh deploys the checkout as it is. It must not fetch, merge,
  reset, check out or push, must work in a copy with no git at all (a downloaded
  release), and must refuse --branch rather than quietly ignore it;
- build.dep-diff: a dependency change since DEPLOY_OLD_HEAD (set by whatever updated
  the checkout) builds --no-cache, also on a diff large enough that `grep -q` at the
  end of the pipe would miss it; no baseline means a cached build, and --no-cache forces
  one;
- fail.silent-abort: a failure names its stage, nothing reports success after it, and a
  successful run prints no failure line;
- fail.guarded-reads: a .env missing a key is the normal case, not a silent abort.
"""
import os, re, shlex, shutil, subprocess, sys, tempfile

# ── Adjust for this repo ──────────────────────────────────────────────────────
# A .env the deploy accepts. Include every key deploy.sh needs to have a value.
ENV_TEXT = "POSTGRES_PASSWORD=x\nFLOW_PASSWORD=x\nSECRET_KEY=kkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkk\nAPP_PORT=8000\n"
# A .env that lacks the optional keys, to prove a missing key is not an abort.
# Keep only what deploy.sh genuinely cannot run without.
ENV_MINIMAL = "POSTGRES_PASSWORD=x\nFLOW_PASSWORD=x\nSECRET_KEY=kkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkk\n"
# Repo files deploy.sh reads besides itself (compose file, config it greps, ...).
EXTRA_FILES = []
# What the curl shim prints, for health checks that inspect the body.
CURL_OUTPUT = '{"status":"ok"}'
# What the docker shim prints, by argument prefix (first match wins). For health
# checks that read compose's own output rather than curl, e.g.
#   "compose ps --status running --services": "app\nbackup\n",
#   "compose logs backup": "backup-1  | backup started: ...\n",
DOCKER_OUTPUT = {}
# ──────────────────────────────────────────────────────────────────────────────

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ROOT = tempfile.mkdtemp(prefix="deployscript-")
GIT_ENV = {
    "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
    "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t",
}
# A build step, however it is spelled: `docker compose build`, `docker-compose build`,
# or through the `$COMPOSE` variable. Mirrors COMPOSE_CMD in scan_conformance.sh.
BUILD_LINE = re.compile(r"(docker\s+compose|docker-compose|\$\{?COMPOSE\}?)\s+build\b")


def sh(cwd, *args):
    return subprocess.run(args, cwd=cwd, capture_output=True, text=True,
                          env={**os.environ, **GIT_ENV}).stdout.strip()


def first_error(out):
    """The first ✗ line and what follows it: one cause, not eighty lines of run."""
    lines = out.splitlines()
    for i, line in enumerate(lines):
        if "✗" in line:
            return "\n".join(lines[i:i + 3])
    return "\n".join(lines[-5:])


def check(name, condition, detail=""):
    print(f"  {'PASS' if condition else 'FAIL'}  {name}"
          f"{'  — ' + detail if detail and not condition else ''}")
    return condition


def make_shims(fail_build=False, git_log=None):
    """docker logs its arguments and succeeds; curl prints CURL_OUTPUT; sleep returns
    at once, so a health check that never passes fails the case in milliseconds.
    With git_log, a git wrapper records every git call before running the real one."""
    bindir = tempfile.mkdtemp(dir=ROOT, prefix="bin-")
    log = os.path.join(bindir, "docker.log")
    build_exit = "exit 1" if fail_build else "true"
    cases = "".join(f'  "{prefix}"*) printf %s {shlex.quote(out)} ;;\n'
                    for prefix, out in DOCKER_OUTPUT.items())
    with open(f"{bindir}/docker", "w") as f:
        f.write(f'#!/bin/sh\necho "$*" >> "{log}"\n'
                f'case "$*" in\n  "compose build"*) {build_exit} ;;\n{cases}esac\n')
    with open(f"{bindir}/curl", "w") as f:
        f.write(f"#!/bin/sh\necho '{CURL_OUTPUT}'\n")
    with open(f"{bindir}/sleep", "w") as f:
        f.write("#!/bin/sh\nexit 0\n")
    names = ["docker", "curl", "sleep"]
    if git_log:
        real = shutil.which("git")
        with open(f"{bindir}/git", "w") as f:
            f.write(f'#!/bin/sh\necho "$*" >> "{git_log}"\nexec "{real}" "$@"\n')
        names.append("git")
    for name in names:
        os.chmod(f"{bindir}/{name}", 0o755)
    return bindir, log


def make_repo(name, env_text=ENV_TEXT, git=True):
    """A copy of the app's deploy files, as a checkout pushed to a bare origin, or
    (git=False) as a plain directory, like an unpacked release.

    requirements.txt is always created: the dependency-diff pattern matches it
    whatever manifests the real app uses, so cases can change "a dependency".
    """
    work = os.path.join(ROOT, name)
    os.makedirs(work)
    shutil.copy2(os.path.join(REPO, "deploy.sh"), work)
    for rel in EXTRA_FILES:
        os.makedirs(os.path.dirname(os.path.join(work, rel)) or work, exist_ok=True)
        shutil.copy2(os.path.join(REPO, rel), os.path.join(work, rel))
    open(f"{work}/requirements.txt", "a").write("")
    open(f"{work}/.gitignore", "w").write(".env\n")
    if git:
        origin = work + "-origin.git"
        subprocess.run(["git", "init", "-q", "--bare", origin], check=True)
        subprocess.run(["git", "init", "-q", work], check=True)
        sh(work, "git", "add", "-A"); sh(work, "git", "commit", "-qm", "initial")
        sh(work, "git", "branch", "-M", "main")
        sh(work, "git", "remote", "add", "origin", origin)
        sh(work, "git", "push", "-q", "-u", "origin", "main")
    open(f"{work}/.env", "w").write(env_text)
    return work


def commit(work, message, files):
    for rel, text in files.items():
        path = os.path.join(work, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        open(path, "a").write(text)
    sh(work, "git", "add", "-A"); sh(work, "git", "commit", "-qm", message)
    return sh(work, "git", "rev-parse", "HEAD")


def run(work, bindir, *args, env=None):
    """stdout and stderr interleaved, so 'nothing after the error' is checkable."""
    base_env = {k: v for k, v in os.environ.items()
                if k not in ("DEPLOY_OLD_HEAD", "DEPLOY_RESUMED")}
    p = subprocess.run(["./deploy.sh", *args], cwd=work, stdin=subprocess.DEVNULL,
                       stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                       env={**base_env, **GIT_ENV, **(env or {}),
                            "PATH": f"{bindir}:{os.environ['PATH']}"})
    return p.stdout, p.returncode


def docker_log(log):
    return open(log).read() if os.path.exists(log) else ""


def shell_function(script, name):
    """A top-level function's text from one of the scripts (`name() {` to the closing `}`)."""
    text = open(os.path.join(REPO, script)).read()
    m = re.search(rf"^{name}\(\) \{{\n.*?^\}}\n", text, re.M | re.S)
    return m.group(0) if m else None


# Passwords people really use, and the characters .env files and URLs trip over.
TRICKY_VALUES = [
    "pa$word #1x", "it's", "ends in a backslash\\", "a\\\\b", 'q"uote', 'x\\"y', "$HOME", "${HOME}", "$$",
    "tab\tin", " spaces at both ends ", "back`tick`", "!bang ~ & | ; < > ( ) * ? [ ] { }", "unicodé ☃ 密码",
    "'", '"', "\\n", "#start", "a=b", "mix '\"$\\#", "p@ss/w:rd?%20", "-starts-with-dash",
]


def compose_reads(work, key):
    """What Docker Compose reads for `key` from work/.env, or None when Compose is not
    installed. `compose config` interpolates without a Docker daemon."""
    if subprocess.run(["docker", "compose", "version"], capture_output=True).returncode != 0:
        return None
    open(f"{work}/docker-compose.yml", "w").write(
        f"services:\n  app:\n    image: alpine\n    environment:\n      V: ${{{key}}}\n")
    p = subprocess.run(["docker", "compose", "config", "--format", "json"], cwd=work,
                       capture_output=True, text=True)
    if p.returncode != 0:
        return "<compose error: " + p.stderr.strip()[-200:] + ">"
    import json
    return json.loads(p.stdout)["services"]["app"]["environment"]["V"].replace("$$", "$")


def build_stage():
    """The header deploy.sh prints before its build step, or None when no build line
    is found, so case 7 fails saying so rather than blaming whatever header came last."""
    stage = None
    for line in open(os.path.join(REPO, "deploy.sh")):
        m = re.match(r'\s*header\s+"([^"]+)"', line)
        if m:
            stage = m.group(1)
        if BUILD_LINE.search(line) and not line.lstrip().startswith("#"):
            return stage
    return None


# git subcommands that change the checkout, its refs or the remote. Reading (rev-parse,
# diff, log, status) is allowed: the dependency diff needs it.
WRITES = re.compile(r"^(fetch|pull|push|merge|reset|checkout|switch|rebase|stash|clean|"
                    r"commit|cherry-pick|revert|am|branch|tag|update-ref|restore|rm|mv|add)\b")


def main():
    ok = True
    scripts = [s for s in ("deploy.sh", "setup.sh") if os.path.exists(os.path.join(REPO, s))]
    try:
        # 1. every script parses
        for script in scripts:
            p = subprocess.run(["bash", "-n", os.path.join(REPO, script)], capture_output=True)
            ok &= check(f"1. {script} passes bash -n", p.returncode == 0, p.stderr.decode().strip())

        # 2. fail.guarded-reads: no assignment from a bare grep, and no grep feeding a
        #    process substitution without a guard (under -E a no-match there fires the trap)
        for script in scripts:
            bad = [l.strip() for l in open(os.path.join(REPO, script))
                   if not l.lstrip().startswith("#")
                   and (re.match(r"^\s*\w+=\$\(.*\bgrep\b", l) or re.search(r"<\s*<\(.*\bgrep\b", l))
                   and "||" not in l]
            ok &= check(f"2. {script} has no unguarded grep read", not bad, "; ".join(bad))

        # 3. fail.silent-abort: the trap names the stage. From a file, not `bash -c`,
        #    because the preamble reads BASH_SOURCE.
        for script in scripts:
            lines = open(os.path.join(REPO, script)).read().splitlines()
            end = next((i for i, l in enumerate(lines) if l.startswith("trap ")), None)
            if end is None:
                ok &= check(f"3. {script} has an ERR trap", False, "no top-level `trap` line")
                continue
            probe = os.path.join(ROOT, f"probe-{script}")
            open(probe, "w").write("\n".join(lines[:end + 1] +
                                   ['header "Probe stage"', "false", "echo NOT-REACHED"]) + "\n")
            p = subprocess.run(["bash", probe], capture_output=True, text=True, cwd=ROOT)
            ok &= check(f"3. {script} ERR trap names the stage",
                        "Probe stage" in p.stderr and "NOT-REACHED" not in p.stdout
                        and p.returncode != 0, f"exit={p.returncode} stderr={p.stderr.strip()!r}")

        # 4. args.unknown-args and the flags that changed meaning in v7
        work = make_repo("args")
        bindir, log = make_shims()
        out, code = run(work, bindir, "--bogus")
        ok &= check("4. unknown argument refused with a usage line",
                    code != 0 and "Usage" in out and not docker_log(log), f"exit={code}\n{out}")
        out, code = run(work, bindir, "--help")
        ok &= check("4b. --help prints usage and exits 0",
                    code == 0 and "Usage" in out and not docker_log(log), f"exit={code}\n{out}")
        out, code = run(work, bindir, "--branch", "claude/x")
        ok &= check("4c. --branch refused, saying deploy.sh does not merge",
                    code != 0 and "merge" in out.lower() and not docker_log(log), f"exit={code}\n{out}")
        out, code = run(work, bindir, "--skip-git")
        ok &= check("4d. --skip-git still accepted (older callers pass it)",
                    code == 0 and "compose build" in docker_log(log), f"exit={code}\n{first_error(out)}")

        # 5. git.env-preflight: no .env, no deploy
        work = make_repo("no-env")
        os.remove(f"{work}/.env")
        bindir, log = make_shims()
        out, code = run(work, bindir)
        ok &= check("5. missing .env refused before Docker",
                    code != 0 and ".env" in out and "compose up" not in docker_log(log),
                    f"exit={code}\n{out}")

        # 5b. Flow: the .env.example placeholders (and a short key) are refused before Docker
        for label, env_text in [
            ("example password", ENV_TEXT.replace("FLOW_PASSWORD=x", "FLOW_PASSWORD=mysecretpassword")),
            ("example key", ENV_TEXT.replace("SECRET_KEY=" + "k" * 64, "SECRET_KEY=replacewithsecretrandomhex")),
            ("short key", ENV_TEXT.replace("SECRET_KEY=" + "k" * 64, "SECRET_KEY=abc123")),
        ]:
            work = make_repo("weak-" + label.replace(" ", "-"), env_text=env_text)
            bindir, log = make_shims()
            out, code = run(work, bindir)
            ok &= check(f"5b. {label} in .env refused before Docker",
                        code != 0 and "setup.sh" in out and "compose build" not in docker_log(log)
                        and "compose up" not in docker_log(log), f"exit={code}\n{out}")

        # 6. git.hands-off: a checkout behind origin, with a claude/* branch pending,
        #    is deployed exactly as it is, and git is only ever read
        work = make_repo("hands-off")
        other = work + "-other"
        sh(ROOT, "git", "clone", "-q", work + "-origin.git", other)
        commit(other, "upstream", {"app.txt": "new\n"})
        sh(other, "git", "push", "-q", "origin", "main")
        sh(other, "git", "push", "-q", "origin", "main:claude/pending")
        sh(work, "git", "fetch", "-q", "origin")
        refs_before = sh(work, "git", "for-each-ref")
        head_before = sh(work, "git", "rev-parse", "HEAD")
        origin_before = sh(work + "-origin.git", "git", "for-each-ref")
        git_log = os.path.join(ROOT, "hands-off-git.log")
        bindir, log = make_shims(git_log=git_log)
        out, code = run(work, bindir, env={"DEPLOY_OLD_HEAD": head_before})
        calls = open(git_log).read().splitlines() if os.path.exists(git_log) else []
        writes = [c for c in calls if WRITES.match(c)]
        ok &= check("6. deploys a stale checkout as it is", code == 0
                    and sh(work, "git", "rev-parse", "HEAD") == head_before
                    and not os.path.exists(f"{work}/app.txt"), f"exit={code}\n{first_error(out)}")
        ok &= check("6b. no git command that writes", not writes, "; ".join(writes))
        ok &= check("6c. refs here and on origin unchanged",
                    sh(work, "git", "for-each-ref") == refs_before
                    and sh(work + "-origin.git", "git", "for-each-ref") == origin_before)

        # 7. fail.silent-abort: a failed build names its stage, and nothing claims success
        work = make_repo("fail-build")
        bindir, log = make_shims(fail_build=True)
        out, code = run(work, bindir)
        stage = build_stage()
        ok &= check("7. failed build exits non-zero", code != 0, f"exit={code}")
        ok &= check("7b. and names the build stage", stage is not None and stage in out,
                    f"stage={stage!r}\n{first_error(out)}")
        ok &= check("7c. and does not start anything after it", "compose up" not in docker_log(log))
        ok &= check("7d. and prints no success banner",
                    not re.search(r"is running|deployed", out.split("✗")[-1], re.I), out[-300:])

        # 8. fail.guarded-reads: a .env without the optional keys deploys
        work = make_repo("minimal-env", env_text=ENV_MINIMAL)
        bindir, log = make_shims()
        out, code = run(work, bindir)
        ok &= check("8. minimal .env deploys", code == 0, f"exit={code}\n{first_error(out)}")
        ok &= check("8b. and prints no failure line", "✗" not in out, first_error(out))

        # 9. no git at all: an unpacked release deploys, with a cached build
        work = make_repo("no-git", git=False)
        bindir, log = make_shims()
        out, code = run(work, bindir, env={"DEPLOY_OLD_HEAD": "0" * 40})
        ok &= check("9. a directory that is not a git checkout deploys", code == 0
                    and "compose up" in docker_log(log), f"exit={code}\n{first_error(out)}")
        ok &= check("9b. and prints no failure line", "✗" not in out, first_error(out))

        # 10. build.dep-diff: the baseline comes from DEPLOY_OLD_HEAD
        work = make_repo("deps")
        old = sh(work, "git", "rev-parse", "HEAD")
        commit(work, "dependency", {"requirements.txt": "newdep\n"})
        bindir, log = make_shims()
        out, code = run(work, bindir, env={"DEPLOY_OLD_HEAD": old})
        ok &= check("10. dependency changed since DEPLOY_OLD_HEAD: --no-cache",
                    code == 0 and "--no-cache" in docker_log(log), f"exit={code}\n{first_error(out)}")
        bindir, log = make_shims()
        out, code = run(work, bindir)
        ok &= check("10b. no baseline: a cached build",
                    code == 0 and "compose build" in docker_log(log)
                    and "--no-cache" not in docker_log(log), docker_log(log))
        bindir, log = make_shims()
        out, code = run(work, bindir, env={"DEPLOY_OLD_HEAD": "f" * 40})
        ok &= check("10c. a baseline git does not know: cached build, no failure",
                    code == 0 and "--no-cache" not in docker_log(log) and "✗" not in out,
                    f"exit={code}\n{first_error(out)}")
        bindir, log = make_shims()
        out, code = run(work, bindir, "--no-cache")
        ok &= check("10d. --no-cache forces one", code == 0 and "--no-cache" in docker_log(log))
        # Older versions were called with --skip-git (and DEPLOY_RESUMED); that still works
        bindir, log = make_shims()
        out, code = run(work, bindir, "--skip-git", env={"DEPLOY_RESUMED": "1", "DEPLOY_OLD_HEAD": old})
        ok &= check("10e. the older --skip-git call still works and keeps --no-cache",
                    code == 0 and "--no-cache" in docker_log(log), f"exit={code}\n{first_error(out)}")

        # 11. fail.guarded-reads: a large change still rebuilds. `grep -q` ending the
        #     dependency-diff pipe exits at its first match, git dies of SIGPIPE, and
        #     under pipefail the match reads as a miss. Needs output past the pipe buffer.
        work = make_repo("large-diff")
        old = sh(work, "git", "rev-parse", "HEAD")
        files = {"requirements.txt": "bigdep\n"}
        files.update({f"zz-vendored/a-fairly-long-vendored-file-name-{i:05d}.txt": "x"
                      for i in range(4000)})
        commit(work, "large change", files)
        bindir, log = make_shims()
        out, code = run(work, bindir, env={"DEPLOY_OLD_HEAD": old})
        ok &= check("11. a large change deploys", code == 0, f"exit={code}\n{first_error(out)}")
        ok &= check("11b. and its dependency change still builds --no-cache",
                    "--no-cache" in docker_log(log), "does the dependency diff end in grep -q?")

        # 12. Flow: setup.sh and deploy.sh read .env with the same function
        get_setup, get_deploy = shell_function("setup.sh", "_env_get"), shell_function("deploy.sh", "_env_get")
        ok &= check("12. _env_get is identical in setup.sh and deploy.sh",
                    get_setup is not None and get_setup == get_deploy)

        # 13. Flow: setup.sh writes any password so that it reads back, and Compose reads it, exactly;
        #     .env ends up readable by its owner only, and other lines are kept
        helpers = "".join(shell_function("setup.sh", f) or "" for f in ("_env_get", "_env_quote", "_env_set"))
        work = os.path.join(ROOT, "env-quoting")
        os.makedirs(work)
        open(f"{work}/.env", "w").write("# a comment\nPOSTGRES_PASSWORD=keep\nFLOW_PASSWORD=old\n")
        os.chmod(f"{work}/.env", 0o644)
        os.chmod(work, 0o755)
        driver = helpers + 'IFS= read -r val < "$1" || true\n_env_set FLOW_PASSWORD "$val"\n_env_get FLOW_PASSWORD\n'
        open(f"{work}/driver.sh", "w").write("set -Eeuo pipefail\n" + driver)
        bad_read, bad_compose, compose_seen = [], [], False
        for i, value in enumerate(TRICKY_VALUES):
            open(f"{work}/value", "w").write(value)
            p = subprocess.run(["bash", "driver.sh", "value"], cwd=work, capture_output=True, text=True)
            if p.returncode != 0 or p.stdout != value:
                bad_read.append(f"{value!r} -> {p.stdout!r} {p.stderr.strip()[-120:]}")
                continue
            got = compose_reads(work, "FLOW_PASSWORD")
            if got is not None:
                compose_seen = True
                if got != value:
                    bad_compose.append(f"{value!r} -> {got!r}")
        lines = open(f"{work}/.env").read().splitlines()
        ok &= check("13. setup.sh's .env values read back exactly", not bad_read, "; ".join(bad_read))
        ok &= check("13b. and Docker Compose reads them exactly"
                    + ("" if compose_seen else " (skipped: docker compose not installed)"),
                    not bad_compose, "; ".join(bad_compose))
        ok &= check("13c. .env is mode 600 after setup.sh writes it",
                    (os.stat(f"{work}/.env").st_mode & 0o777) == 0o600, oct(os.stat(f"{work}/.env").st_mode))
        ok &= check("13d. other lines are kept and the key appears once",
                    lines[:2] == ["# a comment", "POSTGRES_PASSWORD=keep"]
                    and sum(l.startswith("FLOW_PASSWORD=") for l in lines) == 1, "\n".join(lines))
        ok &= check("13e. no temporary file is left behind",
                    not [f for f in os.listdir(work) if f.startswith(".env.")], str(os.listdir(work)))

        # 14. Flow: deploy.sh reads the quoted values setup.sh writes
        quoted = ('POSTGRES_PASSWORD="p@ss/w:rd"\nFLOW_PASSWORD="pa\\$word #1x"\n'
                  'SECRET_KEY="' + "k" * 64 + '"\nAPP_PORT="8123"\n')
        work = make_repo("quoted-env", env_text=quoted)
        bindir, log = make_shims()
        out, code = run(work, bindir)
        ok &= check("14. a quoted .env deploys, on the quoted port",
                    code == 0 and "localhost:8123" in out and '"' not in out.split("APP_PORT=")[1].split()[0],
                    f"exit={code}\n{first_error(out)}")
        for label, env_text in [
            ("quoted example password", quoted.replace('"pa\\$word #1x"', '"mysecretpassword"')),
            ("quoted short key", quoted.replace('"' + "k" * 64 + '"', '"abc123"')),
            ("quoted blank password", quoted.replace('"pa\\$word #1x"', '""')),
        ]:
            work = make_repo("q-" + label.replace(" ", "-"), env_text=env_text)
            bindir, log = make_shims()
            out, code = run(work, bindir)
            ok &= check(f"14b. {label} refused before Docker",
                        code != 0 and "compose build" not in docker_log(log), f"exit={code}\n{out}")

        # 14c. Flow: setup.sh's _env_unset removes one key's line and nothing else
        helpers = "".join(shell_function("setup.sh", f) or "" for f in ("_env_get", "_env_quote", "_env_set", "_env_unset"))
        work = make_repo("unset")
        open(f"{work}/.env", "w").write('# keep\nPOSTGRES_PASSWORD=keep\nANTHROPIC_API_KEY="sk-ant-x"\nFLOW_PASSWORD=pw\nANTHROPIC_API_KEY=dup\n')
        subprocess.run(["bash", "-c", helpers + "_env_unset ANTHROPIC_API_KEY"], cwd=work, check=True)
        lines = open(f"{work}/.env").read().splitlines()
        ok &= check("14c. _env_unset removes only that key's lines",
                    lines == ["# keep", "POSTGRES_PASSWORD=keep", "FLOW_PASSWORD=pw"], "\n".join(lines))

        # 14d. Flow: once a database exists, FLOW_PASSWORD may be blank (Flow keeps the password);
        #      before the first start it is required, and a key still in .env is only noted
        work = make_repo("blank-pw-first", env_text=ENV_TEXT.replace("FLOW_PASSWORD=x", "FLOW_PASSWORD="))
        bindir, log = make_shims()
        out, code = run(work, bindir)
        ok &= check("14d. blank FLOW_PASSWORD before the first start refused, naming setup.sh",
                    code != 0 and "setup.sh" in out and "compose build" not in docker_log(log), f"exit={code}\n{out}")
        DOCKER_OUTPUT["volume ls"] = "flow_pgdata\n"
        try:
            work = make_repo("blank-pw-later", env_text=ENV_TEXT.replace("FLOW_PASSWORD=x", "FLOW_PASSWORD="))
            bindir, log = make_shims()
            out, code = run(work, bindir)
            ok &= check("14d. blank FLOW_PASSWORD with a database deploys",
                        code == 0 and "compose up" in docker_log(log), f"exit={code}\n{first_error(out)}")
            work = make_repo("key-later", env_text=ENV_TEXT + "ANTHROPIC_API_KEY=sk-ant-x\n")
            bindir, log = make_shims()
            out, code = run(work, bindir)
            ok &= check("14d. values Flow keeps are noted, not refused",
                        code == 0 and "ANTHROPIC_API_KEY" in out and "FLOW_PASSWORD" in out
                        and "compose up" in docker_log(log), f"exit={code}\n{out[-400:]}")
        finally:
            DOCKER_OUTPUT.pop("volume ls", None)

        # 15. Flow: deploy.sh warns about an .env other users can read, and only then
        for mode, expect in ((0o644, True), (0o640, True), (0o600, False)):
            work = make_repo(f"mode-{mode:o}")
            os.chmod(f"{work}/.env", mode)
            bindir, log = make_shims()
            out, code = run(work, bindir)
            ok &= check(f"15. .env mode {mode:o}: {'warns' if expect else 'no warning'}",
                        code == 0 and ("chmod 600 .env" in out) == expect, f"exit={code}\n{out[-300:]}")
    finally:
        shutil.rmtree(ROOT, ignore_errors=True)

    print("\nRESULT:", "all cases pass" if ok else "FAILURES ABOVE")
    return 0 if ok else 1


def test_deploy_script():
    """pytest entry point; the assertions print their own PASS/FAIL lines."""
    assert main() == 0, "see the FAIL lines above"


if __name__ == "__main__":
    sys.exit(main())
