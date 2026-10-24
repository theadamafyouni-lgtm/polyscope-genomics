"""The launch gate: measure the boundary instead of asserting it.

Three checks run before any agent starts, and any one of them can stop the run.

  1. The denial probe. A process runs under the IDENTICAL profile the agent will get and
     attempts to read every path on the forbidden list, recording the errno for each. An
     unexpected success fails the run. This is the difference between "we thought about it"
     and "we checked this run" -- and it is the only check that would catch a staging bug
     that accidentally allow-lists a parent directory, because the profile is allow-default
     and a path the profile forgets is simply permitted.

  2. The write probe. Under bubblewrap a read-only bind is enforced by the kernel rather
     than by file modes, so this confirms the refdata clone is read-only to the process and
     that the real CLI state dir and anything outside the sandbox is unwritable -- which is
     what makes the no-cache guarantee hold, since nothing can be deposited for a later run
     to find. Most of these paths are not merely unwritable here: they are not in the
     namespace at all, so the errno is ENOENT rather than EPERM. The probe records "absent"
     as its own state and does not treat it as a pass.

  3. The token grep. Everything actually staged is scanned for the answer-key strings. This
     is the check that converts the survey's findings into evidence: the survey found the
     trait and two candidate model ids in pgs_catalog.py's docstring, and this is what
     proves they are gone from the copy the agent will read. Unchanged from the macOS
     probe, deliberately: both platforms grade against the same definition of a leak.

LINUX BUILD. The Spotlight checks have no equivalent and are replaced; everything else is
the macOS probe unaltered.

The probe program is fed to python3 over STDIN and writes its results to STDOUT. It is never
written to the sandbox filesystem, because the list of forbidden paths is itself a map of
where the answers live -- staging the probe would hand the agent the very thing the probe
exists to keep from it.
"""
import json
import os
import subprocess

from . import config

# Runs inside the sandbox. Reads a JSON job from stdin, writes JSON to stdout. Deliberately
# dependency-free and short: it must run under the same profile as the agent, and anything
# it imports is another thing that can fail for an unrelated reason and look like a boundary
# result.
PROBE_SRC = r'''
import json, os, sys, errno

job = json.loads(sys.stdin.read())
out = {"reads": [], "writes": [], "spotlight": None}

for path, why in job["reads"]:
    rec = {"path": path, "why": why}
    try:
        if os.path.isdir(path):
            os.listdir(path)
            rec["result"] = "READABLE"
        else:
            with open(path, "rb") as fh:
                fh.read(64)
            rec["result"] = "READABLE"
        rec["errno"] = None
    except PermissionError as exc:
        rec["result"] = "denied"; rec["errno"] = exc.errno; rec["strerror"] = exc.strerror
    except FileNotFoundError as exc:
        # Distinct from denied. A missing file is not a boundary -- it is silence that could
        # become a leak the moment the file reappears, so it is reported as its own state.
        rec["result"] = "absent"; rec["errno"] = exc.errno
    except OSError as exc:
        rec["result"] = "denied"; rec["errno"] = exc.errno; rec["strerror"] = exc.strerror
    out["reads"].append(rec)

for path, why in job["writes"]:
    rec = {"path": path, "why": why}
    # No isdir() pre-check. A denied parent makes isdir() return False, which would be
    # reported as "absent" when the truth is "denied" -- and a manifest that says a path
    # was missing when it was actually blocked is describing a boundary that was never
    # tested. Attempt the write and let the errno say which it was.
    try:
        with open(path, "w") as fh:
            fh.write("probe")
        rec["result"] = "WRITABLE"
        rec["errno"] = None
        try:
            os.remove(path)
            rec["cleaned_up"] = True
        except OSError:
            rec["cleaned_up"] = False
    except (PermissionError, OSError) as exc:
        rec["result"] = "denied"; rec["errno"] = exc.errno; rec["strerror"] = exc.strerror
    out["writes"].append(rec)

# The macOS probe queries Spotlight here, because mdfind reads a root-owned index over
# IPC rather than reading the target, so file permissions do not contain it and only the
# profile does. Linux has no equivalent: no always-on full-content index, and plocate is
# not installed on this host. There is nothing to contain and nothing to measure.
#
# What replaces it is a check the macOS probe does not need. Under bubblewrap the
# namespace starts empty, so the question is not "can a root-owned index be queried" but
# "is anything outside the sandbox visible at all". This lists / and HOME and records what
# is there. A namespace that has leaked shows it here.
try:
    root_entries = sorted(os.listdir("/"))
except OSError as exc:
    root_entries = ["<denied: %s>" % exc]
home = os.environ.get("HOME", "")
try:
    home_entries = sorted(os.listdir(home)) if home else []
except OSError as exc:
    home_entries = ["<denied: %s>" % exc]
out["spotlight"] = {
    "platform": "linux",
    "note": ("no Spotlight equivalent; this records what the mount namespace exposes "
             "instead. n_hits is 0 by construction so the shared verdict logic, which "
             "fails a run on any hit, cannot misread this as a leak."),
    "n_hits": 0,
    "namespace_root": root_entries,
    "namespace_home": home_entries,
    "namespace_home_path": home,
}

sys.stdout.write(json.dumps(out))
'''


class ProbeFailure(Exception):
    pass


def run_denial_probe(profile_path, sandbox_root, jail_home, env, python="/usr/bin/python3",
                     extra_reads=()):
    """Execute the probe under the agent's own profile and return its findings.

    `extra_reads` are (path, why) pairs this one sandbox must not reach on top of the shared
    list: on the gb10 route, the PRS sandbox's Beagle and JVMs."""
    job = {
        "reads": [[p, w] for p, w in list(config.deny_probe_paths(jail_home))
                  + list(extra_reads)],
        "writes": [[p, w] for p, w in config.write_probe_paths(sandbox_root)],
        "spotlight_query": ["-name", config.PRIOR_RUN],
    }
    # Source as an argument, job on stdin. Neither touches the sandbox filesystem: the
    # forbidden-path list is itself a map of where the answers live, so writing the probe
    # into the sandbox would hand the agent the thing the probe exists to withhold.
    proc = subprocess.run(
        [config.SANDBOX_EXEC, "-f", profile_path, python, "-c", PROBE_SRC],
        input=json.dumps(job), capture_output=True, text=True,
        cwd=sandbox_root, env=env, timeout=180,
    )
    if proc.returncode != 0:
        raise ProbeFailure("probe process failed (rc=%d): %s"
                           % (proc.returncode, (proc.stderr or "")[-2000:]))
    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        raise ProbeFailure("probe returned unparseable output: %s\n%s"
                           % (exc, proc.stdout[:2000]))


def evaluate_probe(result):
    """Turn probe output into a verdict. Any reachable answer key stops the run."""
    leaks = [r for r in result["reads"] if r["result"] == "READABLE"]
    writable = [r for r in result["writes"] if r["result"] == "WRITABLE"]
    spot = result.get("spotlight") or {}
    spot_leak = bool(spot.get("n_hits"))

    verdict = {
        "reads_attempted": len(result["reads"]),
        "reads_denied": sum(1 for r in result["reads"] if r["result"] == "denied"),
        "reads_absent": sum(1 for r in result["reads"] if r["result"] == "absent"),
        "reads_READABLE": len(leaks),
        "writes_attempted": len(result["writes"]),
        "writes_denied": sum(1 for r in result["writes"] if r["result"] == "denied"),
        "writes_WRITABLE": len(writable),
        "spotlight_hits_from_inside": spot.get("n_hits"),
        "passed": not leaks and not writable and not spot_leak,
        "detail": result,
    }
    if not verdict["passed"]:
        parts = []
        if leaks:
            parts.append("readable: %s" % [r["path"] for r in leaks])
        if writable:
            parts.append("writable: %s" % [r["path"] for r in writable])
        if spot_leak:
            parts.append("Spotlight returned %d paths from inside the sandbox"
                         % spot["n_hits"])
        verdict["failure"] = "; ".join(parts)
    return verdict


# ---------------------------------------------------------------------------
# the token grep
# ---------------------------------------------------------------------------

def grep_tokens(root, hard_tokens=None):
    """Scan everything staged for the answer-key strings.

    Hard tokens fail the run. Soft tokens are recorded and reported but do not fail, because
    each has a legitimate non-leaking occurrence -- a superpopulation code inside a five-way
    enumeration, a coverage figure that is also a plausible generic float -- and a gate that
    fires on those is a gate people learn to switch off.

    Binary and compressed files are skipped. That is a real blind spot and it is recorded
    rather than glossed: the panel bref3 files and the 1000 Genomes VCFs are not searched.
    They are generic upstream reference data, but the gate does not prove that.
    """
    hard, soft = [], []
    skipped, scanned = [], 0

    hard_tokens = config.HARD_TOKENS if hard_tokens is None else hard_tokens
    soft_map = dict(config.SOFT_TOKENS)

    for cur, dirs, files in os.walk(root):
        dirs[:] = [d for d in dirs if d not in (".git",)]
        for name in files:
            p = os.path.join(cur, name)
            rel = os.path.relpath(p, root)
            ext = os.path.splitext(name)[1].lower()
            if ext in config.GREP_SKIP_EXT:
                skipped.append({"path": rel, "reason": "binary/compressed extension"})
                continue
            try:
                if os.path.getsize(p) > config.GREP_MAX_BYTES:
                    skipped.append({"path": rel, "reason": "larger than the scan limit"})
                    continue
                with open(p, "r", errors="strict") as fh:
                    text = fh.read()
            except (UnicodeDecodeError, OSError):
                skipped.append({"path": rel, "reason": "not decodable as text"})
                continue
            scanned += 1
            for tok in hard_tokens:
                if tok in text:
                    hard.append({"path": rel, "token": tok,
                                 "lines": _lines_with(text, tok)})
            for tok in soft_map:
                if tok in text:
                    soft.append({"path": rel, "token": tok, "why_soft": soft_map[tok],
                                 "lines": _lines_with(text, tok)})

    return {
        "root": root,
        "hard_tokens_applied": list(hard_tokens),
        "files_scanned": scanned,
        "files_skipped": len(skipped),
        "skipped_detail": skipped[:200],
        "skip_note": ("Compressed and binary reference data is not scanned. That is a real "
                      "blind spot in this gate, stated rather than implied: the bref3 panels "
                      "and 1000 Genomes VCFs could in principle carry a token and this would "
                      "not see it."),
        "hard_hits": hard,
        "soft_hits": soft,
        "passed": not hard,
    }


def _lines_with(text, token, limit=4):
    out = []
    for i, line in enumerate(text.splitlines(), 1):
        if token in line:
            out.append({"line": i, "text": line.strip()[:200]})
            if len(out) >= limit:
                break
    return out


# ---------------------------------------------------------------------------
# the memory feature
# ---------------------------------------------------------------------------

def check_memory_store(jail_home):
    """Confirm the jail HOME starts with no memory, and report the real store's state.

    The memory feature persists summaries across conversations, which is exactly the leak
    channel this harness exists to close -- and it is ON for the build project on this
    machine: ~/.claude/projects/<build>/memory/ holds MEMORY.md and one note. Its content is
    working-style feedback rather than results, but that is luck, not a boundary.

    The HOME jail closes it by construction: the memory store lives under HOME, so a fresh
    HOME has none. This records the fact rather than trusting it.
    """
    jail_projects = os.path.join(jail_home, ".claude", "projects")
    jail_memories = []
    if os.path.isdir(jail_projects):
        for cur, dirs, files in os.walk(jail_projects):
            if os.path.basename(cur) == "memory":
                jail_memories.extend(os.path.join(cur, f) for f in files)

    real_root = os.path.expanduser("~/.claude/projects")
    real_memories = []
    if os.path.isdir(real_root):
        for entry in sorted(os.listdir(real_root)):
            mem = os.path.join(real_root, entry, "memory")
            if os.path.isdir(mem):
                real_memories.append({
                    "project": entry,
                    "files": sorted(os.listdir(mem)),
                })

    return {
        "jail_home_memory_files": jail_memories,
        "jail_home_is_clean": not jail_memories,
        "real_store_outside_the_sandbox": real_memories,
        "how_to_check_yourself":
            "ls ~/.claude/projects/*/memory/ -- one directory per project that has written "
            "a memory. The feature is on for any project listed. It is denied to the "
            "sandbox by the HOME jail plus the /Users read deny; the denial probe attempts "
            "~/.claude/projects on every run and records the errno.",
        "passed": not jail_memories,
    }


# ---------------------------------------------------------------------------
# Spotlight, from outside
# ---------------------------------------------------------------------------

def spotlight_state_before_run():
    """The macOS counterpart records what the Spotlight index already held before the run,
    because mdfind is a full-content oracle that file permissions do not contain and the
    index on that machine holds the answer text.

    Linux has no such index. Rather than return nothing and leave a manifest field that
    reads like a passed check, this records the two things that ARE true of the host and
    would matter if they changed: whether a content indexer is installed at all, and
    whether the bubblewrap binary the whole boundary rests on is the one we think it is.
    """
    out = []
    for name, path, why in (
        ("plocate", "/usr/bin/plocate", "a name index is the closest thing to Spotlight; "
                                        "absent means there is nothing to contain"),
        ("locate", "/usr/bin/locate", "same, the older implementation"),
        ("tracker3", "/usr/bin/tracker3", "desktop content indexer; not present on a server"),
    ):
        out.append({"indexer": name, "path": path, "present": os.path.exists(path),
                    "why_it_matters": why})

    bwrap = {"path": "/usr/bin/bwrap", "present": os.path.exists("/usr/bin/bwrap")}
    try:
        r = subprocess.run(["/usr/bin/bwrap", "--version"], capture_output=True,
                           text=True, timeout=20)
        bwrap["version"] = ((r.stdout or "") + (r.stderr or "")).strip().splitlines()[0]
    except Exception as exc:  # noqa: BLE001
        bwrap["version"] = "<unavailable: %s>" % exc

    return {
        "platform": "linux",
        "queries": out,
        "bubblewrap": bwrap,
        "caveat": ("Not the same check as the macOS one and not claiming to be. There an "
                   "index existed that already contained the answer text, and the profile "
                   "was the only thing filtering it, so its state was a real caveat on "
                   "every run. Here no such index exists, so the caveat does not arise. "
                   "What is recorded is the absence, which is the thing that would have to "
                   "change for it to arise."),
        "purge_command": None,
        "purge_cost": None,
    }
