#!/usr/bin/env python3
"""Check that this machine can actually run the harness. Changes nothing.

    python3 preflight.py            # report
    python3 preflight.py --quiet    # only problems
    python3 preflight.py --json     # machine-readable

Exit code 0 if the machine can run cases, 1 if it cannot.

WHY THIS EXISTS

Every environment problem in the first week of running this on Linux was silent.
Not one of them raised an error that named the cause:

  numpy was not installed, so the ancestry tool could not project. The agent
  worked that out for itself, refused because ANC-4 forbids falling back to
  self-report, and select then refused because it had no ancestry to rank on.
  Four score cases graded FAIL. Every part of that is the agent behaving
  correctly, and the results were worthless.

  plink2 was present but mode 644. refdata is mounted read-only so the execute
  bit could not be restored. One agent built a shim directory with an executable
  copy, which pulled 48 GB of reference data into its own output and filled the
  disk. Again: sensible behaviour, documented by the agent, invisible from
  outside until the disk ran out.

  A stray /CLAUDE.md sat at the filesystem root and would have auto-loaded into
  the agent's context on every run. The launch gate caught that one, which is the
  only reason it was found.

The pattern is that a competent agent routes around a broken environment and
produces plausible output, so nothing looks wrong until the numbers are read
closely. Two seconds of checking beforehand is worth more than a day of reading
ledgers afterwards.

WHAT IT CHECKS THAT A NAIVE VERSION WOULD NOT

Things are checked where the agent will meet them, not where they are convenient
to test. numpy is imported INSIDE a bubblewrap namespace, because the host having
it proves nothing about the jail. Binaries are checked for the execute bit and
not merely for existing, because that is exactly how plink2 failed.
"""

import argparse
import json
import os
import shutil
import subprocess
import sys

REPO = os.path.dirname(os.path.abspath(__file__))
REFDATA = os.path.join(REPO, "agent", "refdata")

FAIL, WARN, OK = "FAIL", "WARN", "ok"
results = []


def note(level, what, detail="", fix=""):
    results.append({"level": level, "what": what, "detail": detail, "fix": fix})


def run(cmd, timeout=25):
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return p.returncode, (p.stdout or "") + (p.stderr or "")
    except FileNotFoundError:
        return 127, "not found"
    except subprocess.TimeoutExpired:
        return 124, "timed out"
    except Exception as exc:  # noqa: BLE001
        return 1, str(exc)


# ---------------------------------------------------------------- checks

def check_python_packages():
    for mod in ("numpy", "scipy", "pandas"):
        rc, out = run([sys.executable, "-c",
                       "import %s; print(%s.__version__)" % (mod, mod)])
        if rc == 0:
            note(OK, "python: %s" % mod, out.strip())
        else:
            note(FAIL, "python: %s missing" % mod,
                 "tools/ancestry.py imports numpy in three places; without it the "
                 "ancestry stage refuses and every score case fails for a reason "
                 "that is not the agent's",
                 "apt-get install -y python3-numpy python3-scipy python3-pandas "
                 "(NOT pip: the allow-list proxy blocks PyPI with 403)")


def check_host_binaries():
    for name, why, fix in (
        ("bwrap", "the sandbox itself", "apt-get install -y bubblewrap"),
        ("bcftools", "used by the normalisation tools", "apt-get install -y bcftools"),
        ("java", "Beagle, only needed if scoring runs", "apt-get install -y default-jre"),
        ("node", "Claude Code runs on it", "apt-get install -y nodejs npm"),
        ("claude", "the agent under test", "npm install -g @anthropic-ai/claude-code@2.1.293"),
    ):
        p = shutil.which(name)
        if p:
            note(OK, "binary: %s" % name, p)
        else:
            note(FAIL if name != "java" else WARN, "binary: %s missing" % name, why, fix)


def check_refdata_binaries():
    """Present is not enough. They have to be executable.

    plink2 shipped mode 644 once and refdata is read-only inside the sandbox, so
    the agent could not chmod it and built a 48 GB workaround instead.
    """
    for name in ("plink2", "liftOver"):
        p = os.path.join(REFDATA, "bin", name)
        if not os.path.isfile(p):
            note(FAIL, "refdata/bin/%s missing" % name, "",
                 "python3 build_refdata.py --only plink2 (the pinned build, after "
                 "fetch_refdata.py)" if name == "plink2" else
                 "see setup.sh; liftOver comes from "
                 "hgdownload.soe.ucsc.edu/admin/exe/linux.x86_64/liftOver")
            continue
        if not os.access(p, os.X_OK):
            note(FAIL, "refdata/bin/%s is not executable" % name,
                 "mode %o. refdata is read-only inside the sandbox so the agent "
                 "cannot fix this and will build a workaround instead"
                 % (os.stat(p).st_mode & 0o777),
                 "chmod 755 %s" % p)
            continue
        pinned = pinned_plink2() if name == "plink2" else None
        if pinned and sha256_file(p) != pinned:
            note(FAIL, "refdata/bin/plink2 is not the pinned build",
                 "the published runs used PLINK v2.0.0-a.6.6LM 64-bit Intel (16 Jan 2025), "
                 "sha256 %s..." % pinned[:12],
                 "move it aside, then python3 build_refdata.py --only plink2")
            continue
        rc, out = run([p, "--version"])
        if rc != 0:
            rc, out = run([p])   # liftOver prints usage and exits non-zero
        first = (out.strip().splitlines() or [""])[0][:60]
        if "cannot execute" in out or "Exec format error" in out:
            note(FAIL, "refdata/bin/%s will not run here" % name, first,
                 "wrong architecture; this is probably a macOS binary. Replace it.")
        else:
            note(OK, "refdata/bin/%s" % name, first)


def pinned_plink2():
    """The sha256 refdata_recipe.json pins for refdata/bin/plink2, or None."""
    try:
        with open(os.path.join(REPO, "refdata_recipe.json")) as fh:
            for a in json.load(fh).get("artifacts") or []:
                if a.get("key") == "plink2" and "member" in a:
                    return a["member"]["sha256"]
    except (OSError, ValueError):
        pass
    return None


def sha256_file(path):
    import hashlib
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def check_refdata_trees():
    # panel: 2.2 GB with the selection profile, 9 GB with --profile full (scoring).
    need = {"ancestry-ref": 30, "panel": 2, "fasta": 0.5, "chains": 0, "maps": 0}
    rebuild = ("python3 fetch_refdata.py, then python3 build_refdata.py, each with "
               "its check (--verify, --check); SETUP.md section 8")
    # A clone has agent/refdata/ with only MANIFEST.json in it, so test the contents.
    if not any(os.path.isdir(os.path.join(REFDATA, n)) for n in need):
        note(FAIL, "agent/refdata is empty",
             "about 49 GB of reference data. Only its MANIFEST.json is in the "
             "repository: the rest is far larger than git will take.",
             "rebuild it: " + rebuild + ". A few files cannot be reproduced; that "
             "section lists them.")
        return
    for name, min_gb in need.items():
        d = os.path.join(REFDATA, name)
        if not os.path.isdir(d):
            note(FAIL, "refdata/%s missing" % name, "", rebuild)
            continue
        rc, out = run(["du", "-sb", d], timeout=180)
        gb = (int(out.split()[0]) / 1024.0 ** 3) if rc == 0 and out.split() else 0
        if gb < min_gb:
            note(WARN, "refdata/%s looks small" % name,
                 "%.1f GB, expected at least %.0f" % (gb, min_gb))
        else:
            note(OK, "refdata/%s" % name, "%.1f GB" % gb)
    missing = refdata_missing()
    if missing:
        note(FAIL, "refdata incomplete: %d file(s) missing or the wrong size" % len(missing),
             ", ".join(missing[:3]) + (" ..." if len(missing) > 3 else ""), rebuild)
    else:
        note(OK, "refdata files", "every file a selection run reads is present at its "
             "recorded size (python3 build_refdata.py --check hashes them)")


def refdata_missing():
    """Files a selection run reads that are absent or the wrong size, by name: every
    selection-profile file in refdata_recipe.json (archives by the files they yield) and
    what build_refdata.py derives. Size only; build_refdata.py --check does the hashing."""
    recipe = os.path.join(REPO, "refdata_recipe.json")
    if not os.path.isfile(recipe):
        return ["refdata_recipe.json"]
    with open(recipe) as fh:
        arts = json.load(fh).get("artifacts") or []
    want = []
    for a in arts:
        if a.get("key", "").startswith("panel_chr"):
            continue
        outs = a.get("members") or a.get("yields") or \
            ({"x": a["member"]} if "member" in a else {"x": {"dest": a["path"], "bytes": a.get("bytes")}})
        want += [(m["dest"], m.get("bytes")) for m in outs.values()
                 if m["dest"].startswith("refdata/")]
    want += [("refdata/ancestry-ref/hgdp_1kgp.panel", 68200),
             ("refdata/panel/sites_index.bin", 392181824),
             ("refdata/ancestry-ref/hgdp_basis/prune.prune.in", 2679865)]
    want += [("refdata/ancestry-ref/phase3/chr%d.vcf.gz.done" % n, 0) for n in range(1, 23)]
    out = []
    for rel, size in want:
        p = os.path.join(REPO, "agent", rel)
        if not os.path.isfile(p) or (size is not None and os.path.getsize(p) != size):
            out.append(rel[len("refdata/"):])
    return out


def check_shim():
    # The harness's own order (config.SANDBOX_EXEC): $PRS_HARNESS_SANDBOX_EXEC, then the
    # root-installed /usr/bin/sandbox-exec, then the repository's copy, which needs no root.
    p = os.environ.get("PRS_HARNESS_SANDBOX_EXEC") or "/usr/bin/sandbox-exec"
    if not os.environ.get("PRS_HARNESS_SANDBOX_EXEC") and not os.access(p, os.X_OK):
        p = os.path.join(REPO, "harness", "sandbox-exec")
    if not os.path.isfile(p):
        note(FAIL, "sandbox-exec shim not found",
             "the harness launches every agent as `sandbox-exec -f <profile> ...`, "
             "which is a macOS command. On Linux that call is served by a shim that "
             "translates the profile into bubblewrap arguments.",
             "install -m 755 harness/sandbox-exec /usr/bin/sandbox-exec, or keep the "
             "repository's copy executable")
        return
    if not os.access(p, os.X_OK):
        note(FAIL, "sandbox-exec shim is not executable", "", "chmod 755 " + p)
        return
    try:
        head = open(p).read(400)
    except OSError:
        head = ""
    if "bwrap" not in head:
        note(WARN, "sandbox-exec exists but does not mention bwrap",
             "this may be a different program of the same name")
    else:
        note(OK, "sandbox-exec shim", p)


def check_sandbox_works():
    """The only check that matters: can the agent's environment actually run?

    Everything above is about the host. This runs a real bubblewrap namespace
    with the same binds and the same uid mapping the profile uses, and imports
    numpy inside it. The host having numpy proves nothing about the jail.
    """
    if not shutil.which("bwrap"):
        note(FAIL, "cannot test the sandbox", "bwrap is not installed")
        return
    args = ["bwrap"]
    for src, dest in (("/usr", "/usr"), ("/etc", "/etc"), ("/usr/bin", "/bin"),
                      ("/usr/sbin", "/sbin"), ("/usr/lib", "/lib"),
                      ("/usr/lib64", "/lib64"), ("/run", "/run")):
        if os.path.isdir(os.path.realpath(src)):
            args += ["--ro-bind", os.path.realpath(src), dest]
    args += ["--proc", "/proc", "--dev", "/dev", "--tmpfs", "/tmp",
             "--unshare-user", "--uid", "1000", "--gid", "1000",
             "--unshare-pid", "--die-with-parent", "--",
             sys.executable, "-c",
             "import os,numpy;print('uid',os.getuid(),'numpy',numpy.__version__)"]
    rc, out = run(args, timeout=60)
    if rc == 0 and "numpy" in out:
        note(OK, "sandbox: numpy reachable inside bwrap", out.strip())
    else:
        note(FAIL, "sandbox: cannot import numpy inside bwrap",
             out.strip()[:200],
             "if the host has numpy but the jail does not, the profile is missing "
             "a bind; if neither, install it on the host first")

    # Not being root inside matters: Claude Code refuses to skip its permission
    # prompts under root, and an unattended run cannot answer a prompt.
    # /lib64 must be bound too, or the ELF interpreter (/lib64/ld-linux-x86-64.so.2)
    # is absent and the interpreter cannot start at all. Without it this test failed
    # with "execvp /usr/bin/python3: No such file or directory" on every x86_64 box
    # and reported a uid-mapping problem that did not exist -- while the fuller test
    # above, which does bind /lib64, passed. A check that cries wolf is worse than no
    # check: this one was known-noisy and therefore ignored.
    args = ["bwrap", "--ro-bind", "/usr", "/usr", "--ro-bind", "/etc", "/etc",
            "--ro-bind", os.path.realpath("/usr/lib"), "/lib"]
    if os.path.isdir(os.path.realpath("/usr/lib64")):
        args += ["--ro-bind", os.path.realpath("/usr/lib64"), "/lib64"]
    args += ["--proc", "/proc", "--dev", "/dev", "--unshare-user",
             "--uid", "1000", "--unshare-pid", "--die-with-parent", "--",
             sys.executable, "-c", "import os;print(os.getuid())"]
    rc, out = run(args, timeout=40)
    if rc == 0 and out.strip() == "1000":
        note(OK, "sandbox: uid mapped to 1000", "not root inside the namespace")
    else:
        note(WARN, "sandbox: uid mapping did not take", out.strip()[:120],
             "the namespace ran as root. Claude Code refuses "
             "--dangerously-skip-permissions as root, so an unattended run cannot "
             "answer the prompt it then raises")


def check_cli():
    rc, out = run(["claude", "--version"])
    v = out.strip().split()[0] if rc == 0 and out.strip() else "?"
    if v.startswith("2.1.293"):
        note(OK, "claude version", v)
    else:
        note(WARN, "claude is not pinned to 2.1.293", v,
             "npm install -g @anthropic-ai/claude-code@2.1.293 -- and re-pin AFTER "
             "every sign-in, because signing in upgrades it")

    rc, out = run(["node", "--version"])
    major = 0
    if rc == 0 and out.strip().startswith("v"):
        try:
            major = int(out.strip()[1:].split(".")[0])
        except ValueError:
            pass
    if major >= 22:
        note(OK, "node version", out.strip())
    else:
        note(WARN, "node is older than the CLI asks for", out.strip() + " (wants >=22)",
             "it runs with an EBADENGINE warning; upgrade if anything behaves oddly")

    # Headless boxes authenticate with a long-lived token, per SETUP.md section 5. The
    # interactive credentials file is the fallback, and its four-hourly refresh does not
    # work headless, which is what killed every earlier batch every few hours.
    cred = os.path.expanduser("~/.claude/.credentials.json")
    if os.environ.get("CLAUDE_CODE_OAUTH_TOKEN"):
        note(OK, "claude auth", "CLAUDE_CODE_OAUTH_TOKEN is set")
        if os.path.isfile(cred):
            note(WARN, "credentials file beside the token", cred,
                 "SETUP.md section 5: move it aside so it cannot win over the token: "
                 "mv %s %s.bak" % (cred, cred))
    elif os.path.isfile(cred):
        age_h = (__import__("time").time() - os.path.getmtime(cred)) / 3600.0
        note(OK if age_h < 8 else WARN, "claude credentials (interactive login)",
             "written %.1f hours ago" % age_h,
             "" if age_h < 8 else "this refreshes every four hours and cannot do so "
                                  "headless; use the token in SETUP.md section 5")
    else:
        note(FAIL, "no claude authentication",
             "neither CLAUDE_CODE_OAUTH_TOKEN nor ~/.claude/.credentials.json",
             "SETUP.md section 5: `claude setup-token` on a machine with a browser, "
             "export CLAUDE_CODE_OAUTH_TOKEN in ~/.bashrc, then open a NEW shell -- "
             "one that predates the edit does not have it")


def check_context_pollution():
    """Any CLAUDE.md the agent can see is loaded into its context automatically."""
    for p in ("/CLAUDE.md", os.path.expanduser("~/.claude/CLAUDE.md"),
              os.path.expanduser("~/CLAUDE.md")):
        if os.path.exists(p):
            note(FAIL, "%s exists" % p,
                 "%d bytes. It would auto-load into the agent's context on every run."
                 % os.path.getsize(p), "rm %s" % p)
    if not any(r["what"].endswith("exists") for r in results):
        note(OK, "no stray CLAUDE.md", "nothing will auto-load into the context")


def check_disk():
    st = os.statvfs("/")
    free = (st.f_bavail * st.f_frsize) / 1024.0 ** 3
    if free < 25:
        note(FAIL, "disk", "%.0f GB free" % free,
             "a pipeline case writes about 8.5 GB while it runs; three at once "
             "needs roughly 30 GB of headroom")
    elif free < 50:
        note(WARN, "disk", "%.0f GB free" % free, "enough for one or two at a time")
    else:
        note(OK, "disk", "%.0f GB free" % free)


def check_private_restore():
    """The pieces this repo does not carry: benchmark data, kept private.

    Missing any of them is not silent -- config.py raises at import, and a case with
    no persona file fails at staging -- but it fails late, at the first run. Checking
    here turns that into two seconds.
    """
    tok = (os.environ.get("PRS_HARNESS_TOKENS")
           or os.path.expanduser("~/.config/prs-harness/tokens.json"))
    if not os.path.isfile(tok):
        note(FAIL, "launch-gate tokens missing", tok,
             "install -m 600 <your tokens.json> " + tok)
    elif os.stat(tok).st_mode & 0o077:
        note(FAIL, "launch-gate tokens readable by others",
             "mode %o" % (os.stat(tok).st_mode & 0o777), "chmod 600 " + tok)
    else:
        rc, out = run([sys.executable, "-c",
                       "import sys; sys.path.insert(0, %r); from harness import config; "
                       "print(len(config.HARD_TOKENS), len(config.PROVIDER_HARD_TOKENS))"
                       % os.path.join(REPO, "harness")])
        if rc == 0:
            hard, prov = (out.split() + ["?", "?"])[:2]
            note(OK, "launch-gate tokens", "%s hard, %s provider" % (hard, prov))
        else:
            last = (out.strip().splitlines() or [""])[-1][:120]
            note(FAIL, "launch-gate tokens rejected by config.py", last,
                 "restore the file again")

    pdir = os.path.join(REPO, "agent", "sim", "personas", "eval")
    n = (len([f for f in os.listdir(pdir) if f.startswith("p") and f.endswith(".json")])
         if os.path.isdir(pdir) else 0)
    if n == 99:
        note(OK, "personas", "99 in agent/sim/personas/eval")
    else:
        note(FAIL, "personas", "%d found, expected 99" % n,
             "benchmark data, kept private")

    key = os.path.join(REPO, "answer-side", "answer-sets.csv")
    if os.path.isfile(key):
        note(OK, "answer key", "answer-side/answer-sets.csv")
    else:
        note(WARN, "answer key missing",
             "cases run without it; grading cannot",
             "benchmark data, kept private")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--quiet", action="store_true", help="only problems")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    check_python_packages()
    check_host_binaries()
    check_private_restore()
    check_refdata_binaries()
    check_refdata_trees()
    check_shim()
    check_cli()
    check_context_pollution()
    check_sandbox_works()
    check_disk()

    if args.json:
        print(json.dumps(results, indent=2))
    else:
        for r in results:
            if args.quiet and r["level"] == OK:
                continue
            print("%-5s %-42s %s" % (r["level"], r["what"], r["detail"][:80]))
            if r["fix"] and r["level"] != OK:
                for line in _wrap(r["fix"], 74):
                    print("      %s" % line)

    bad = [r for r in results if r["level"] == FAIL]
    warn = [r for r in results if r["level"] == WARN]
    if not args.json:
        print()
        print("%d ok, %d warning(s), %d blocking"
              % (len(results) - len(bad) - len(warn), len(warn), len(bad)))
        print("blocking" if bad else "this machine can run cases")
    return 1 if bad else 0


def _wrap(s, n):
    words, line, out = s.split(), "", []
    for w in words:
        if len(line) + len(w) + 1 > n:
            out.append(line)
            line = w
        else:
            line = (line + " " + w).strip()
    if line:
        out.append(line)
    return out


if __name__ == "__main__":
    sys.exit(main())
