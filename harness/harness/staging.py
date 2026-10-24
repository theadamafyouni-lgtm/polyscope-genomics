"""Building a sandbox from scratch, and proving it was built from scratch.

Two ideas do all the work here.

The first is the APFS clone. refdata is 27 GB and the brief assumed it had to be shared
read-only because copying it was impossible. Measured on this machine: `cp -Rc` clones all
753 files in 247 ms with free disk unchanged, and the clone is independent -- writing to it
does not touch the original. So refdata is not shared at all. Every run gets its own, which
removes the symlink and therefore the traversal question, and -- far more importantly --
makes it possible to DELETE things from it. A shared mount could never have done that, and
three subtrees have to go: they were built by subsetting 1000 Genomes through the patient's
own array positions.

The second is that staging is recorded as it happens, file by file, with hashes. The
manifest is not written from a description of what should have been staged; it is written
from what was.

Nothing here modifies agent/ or reference-provider/. Scrubbing operates on the copy.
"""
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time

from . import config


class StagingError(Exception):
    pass


def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


class StagingRecord:
    """Every file placed, with where it came from and what it hashed to."""

    def __init__(self):
        self.files = []
        self.deleted = []
        self.scrubbed = []
        self.notes = []
        self.clone = None

    def add(self, dest, source, kind, hashed=True, note=""):
        rec = {
            "dest": dest, "source": source, "kind": kind,
            "bytes": os.path.getsize(dest) if os.path.isfile(dest) else None,
            "mode": oct(os.stat(dest).st_mode & 0o777),
        }
        if hashed and os.path.isfile(dest):
            rec["sha256"] = sha256(dest)
        if note:
            rec["note"] = note
        self.files.append(rec)
        return rec

    def to_dict(self):
        return {
            "files": self.files,
            "n_files": len(self.files),
            "refdata_clone": self.clone,
            "refdata_deleted": self.deleted,
            "scrubbed": self.scrubbed,
            "notes": self.notes,
        }


# ---------------------------------------------------------------------------
# refdata
# ---------------------------------------------------------------------------

def clone_refdata(dest, record, withhold=()):
    """Cheap per-case copy of refdata, then delete the patient-derived subtrees.

    `withhold` is more (path, why) pairs to delete from this clone the same way:
    config.GB10_REFDATA_DELETE on the gb10 route.

    Two mechanisms, one per platform, both giving an instant copy that costs no
    real disk:

      macOS   cp -Rc, an APFS copy-on-write clone via clonefile(2). If the
              destination is not on the same APFS volume the flag silently
              degrades to a real copy, which would take minutes and tens of GB.
      Linux   cp -al, recursive with hard links. Directories are created, files
              are linked.

    Either way the elapsed time is recorded and anything slow is flagged, because
    a degraded clone looks exactly like a working one from the outside.

    Hard links are the same inode under a second name, so a write through the
    clone would reach the original. Nothing is permitted to write there: the
    sandbox profile binds refdata read-only and the write probe attempts
    refdata/.harness_write_probe on every run and fails the run if it succeeds.
    The deletions below are unlinks and leave the source untouched, which is what
    makes this safe rather than merely fast.
    """
    if os.path.exists(dest):
        raise StagingError("refdata destination already exists: %s" % dest)
    # -Rc is clonefile(2) and is macOS only; Linux cp rejects the flag outright.
    # -al is the Linux equivalent: recursive, hard-linked, instant, no real disk.
    cp_args = ["-Rc"] if sys.platform == "darwin" else ["-al"]
    t0 = time.time()
    proc = subprocess.run(["cp"] + cp_args + [config.REFDATA_SRC, dest],
                          capture_output=True, text=True)
    elapsed = time.time() - t0
    if proc.returncode != 0:
        raise StagingError("refdata clone failed: %s" % (proc.stderr or "")[-2000:])

    n_src = sum(len(f) for _, _, f in os.walk(config.REFDATA_SRC))
    n_dst = sum(len(f) for _, _, f in os.walk(dest))
    if n_src != n_dst:
        raise StagingError("clone is incomplete: %d files in source, %d in clone"
                           % (n_src, n_dst))

    record.clone = {
        "source": config.REFDATA_SRC,
        "dest": dest,
        "method": ("cp -Rc (APFS clonefile)" if sys.platform == "darwin"
                   else "cp -al (hard links)"),
        "platform": sys.platform,
        "elapsed_seconds": round(elapsed, 3),
        "n_files": n_dst,
        "cow_confirmed": elapsed < 30,
        "note": ("A clone taking longer than ~30s is not a clone. cp -Rc degrades to a "
                 "real copy across APFS volumes, and cp -al degrades the same way across "
                 "filesystems, where a hard link is impossible. Recorded so a degraded "
                 "clone is visible rather than merely slow."),
    }

    # The deletions. This is the finding that made the clone necessary rather than merely
    # convenient: these trees were cut to the patient's own array positions, and their
    # pruned marker counts are exactly the evidence the prior run's ancestry judgment cites.
    for rel, why in list(config.REFDATA_DELETE) + list(withhold):
        target = os.path.join(dest, rel)
        if not os.path.exists(target):
            record.deleted.append({"path": rel, "why": why, "status": "absent in source"})
            continue
        bytes_before = _tree_bytes(target)
        if os.path.isdir(target):
            shutil.rmtree(target)
        else:
            os.remove(target)
        if os.path.exists(target):
            raise StagingError("failed to delete %s from the clone" % target)
        record.deleted.append({
            "path": rel, "why": why, "status": "deleted",
            "bytes_removed": bytes_before,
        })

    for rel, why in config.REFDATA_FLAGGED:
        target = os.path.join(dest, rel)
        record.notes.append({
            "kind": "refdata_flagged", "path": rel, "why": why,
            "present": os.path.exists(target),
        })

    # Assert the deletions actually removed the answer, rather than trusting the path list.
    _assert_no_patient_positions(dest, record)
    return record.clone


def _tree_bytes(path):
    if os.path.isfile(path):
        return os.path.getsize(path)
    total = 0
    for root, _d, files in os.walk(path):
        for f in files:
            try:
                total += os.path.getsize(os.path.join(root, f))
            except OSError:
                pass
    return total


def _assert_no_patient_positions(clone_dir, record):
    """Grep the clone's .pvar headers for the subsetting provenance.

    The three deleted trees were found by reading `bcftools view -R
    /tmp/patient_positions.tsv` out of their .pvar headers. Checking for that string again,
    in the clone, is what turns the deletion list from an assertion into a measurement -- if
    a fourth tree carries it, this fails rather than shipping.
    """
    hits = []
    for root, _dirs, files in os.walk(clone_dir):
        for name in files:
            if not name.endswith(".pvar"):
                continue
            p = os.path.join(root, name)
            try:
                with open(p, "r", errors="ignore") as fh:
                    for line in fh:
                        if not line.startswith("#"):
                            break
                        if "patient_positions" in line:
                            hits.append(os.path.relpath(p, clone_dir))
                            break
            except OSError:
                continue
    record.notes.append({
        "kind": "patient_position_scan",
        "scanned": "every .pvar header in the clone",
        "hits": hits,
    })
    if hits:
        raise StagingError(
            "the clone still contains reference data subset through the patient's own "
            "positions: %s -- add these to REFDATA_DELETE" % hits)


# ---------------------------------------------------------------------------
# scrubbing
# ---------------------------------------------------------------------------

ALIGNMENT_SENSITIVE = ("AGENT.md",)


def assert_scrub_widths():
    """Alignment-sensitive files may only take same-width substitutions.

    config.py claimed this in a comment and both AGENT.md rules broke it, shifting the flow
    diagram's border one column right and its connector one column left. A comment is not an
    invariant; this is.
    """
    bad = []
    for rel, find, repl, _why in config.SCRUB_RULES:
        if rel in ALIGNMENT_SENSITIVE and len(find) != len(repl):
            bad.append({"file": rel, "find": find, "replace": repl,
                        "widths": [len(find), len(repl)]})
    if bad:
        raise StagingError(
            "scrub rules for alignment-sensitive files must preserve width: %s" % bad)
    return True


def scrub_file(path, rel, record):
    """Rewrite answer-key strings out of a staged copy.

    These files are inside the sandbox, so no boundary reaches them.

    Every substitution is recorded with its before/after line so the manifest carries the
    diff and a reviewer can see precisely what the agent was and was not shown.
    """
    try:
        with open(path, "r", errors="strict") as fh:
            text = fh.read()
    except (UnicodeDecodeError, OSError):
        return False

    original = text
    applied = []

    for rule_rel, find, repl, why in config.SCRUB_RULES:
        if rule_rel != rel or find not in text:
            continue
        n = text.count(find)
        text = text.replace(find, repl)
        applied.append({"find": find, "replace": repl, "occurrences": n, "why": why})

    n_run = len(re.findall(config.SCRUB_RUN_ID_PATTERN, text))
    if n_run:
        text = re.sub(config.SCRUB_RUN_ID_PATTERN, config.SCRUB_RUN_ID_REPLACEMENT, text)
        applied.append({"find": "%s (regex)" % config.PRIOR_RUN,
                        "replace": config.SCRUB_RUN_ID_REPLACEMENT,
                        "occurrences": n_run,
                        "why": "run id in docstrings and examples"})

    if text == original:
        return False

    with open(path, "w") as fh:
        fh.write(text)
    record.scrubbed.append({"path": rel, "substitutions": applied})
    return True


# ---------------------------------------------------------------------------
# copying
# ---------------------------------------------------------------------------

def copy_in(src, dest, record, kind, scrub_rel=None, mode=None):
    os.makedirs(os.path.dirname(os.path.abspath(dest)), exist_ok=True)
    shutil.copyfile(src, dest)
    if mode is not None:
        os.chmod(dest, mode)
    if scrub_rel is not None:
        scrub_file(dest, scrub_rel, record)
    return record.add(dest, src, kind)


# ---------------------------------------------------------------------------
# the jail HOME
# ---------------------------------------------------------------------------

def build_jail_home(home_dir, record):
    """A throwaway HOME inside the sandbox, seeded with the minimum needed to authenticate.

    This closes the largest leak found anywhere in the survey, and it is not in the repo:
    the real ~/.claude holds 165 files matching the answer tokens, including a verbatim
    content snapshot of a run's live record under file-history/, 34 MB of prior
    session transcripts for the build project, and seven sessions a sandboxed agent could
    simply `claude --resume` into. Any design that allows ~/.claude so the CLI can
    authenticate is defeated on turn one.

    Redirecting HOME moves the entire CLI state directory inside the sandbox: no transcripts,
    no file-history, no paste-cache, no history.jsonl, no memory store, and nothing to
    resume. Two details make it work, both measured rather than assumed:

      - the OAuth token is in the login keychain, not on disk (~/.claude/.credentials.json
        does not exist here), and Security resolves the keychain path from HOME -- so
        Library/Keychains is symlinked back to the real one and allowed read-only;
      - .claude.json is seeded by allow-list, not by stripping `projects`. A strip-list would
        silently admit whatever key a CLI upgrade adds next.

    CLAUDE_CONFIG_DIR does not work for this. It relocates the config directory but
    authentication still fails.
    """
    os.makedirs(home_dir, exist_ok=True)
    os.chmod(home_dir, 0o700)

    real = os.path.expanduser("~/.claude.json")
    seeded_keys, dropped = [], []
    payload = {}
    if os.path.exists(real):
        with open(real) as fh:
            full = json.load(fh)
        for k in sorted(full):
            if k in config.CLAUDE_JSON_SEED_KEYS:
                payload[k] = full[k]
                seeded_keys.append(k)
            else:
                dropped.append(k)
    # projects{} must exist and be empty: present so the CLI does not rebuild it from
    # anywhere else, empty so no prior project -- least of all the build project, whose
    # memory store is live -- is reachable.
    payload["projects"] = {}

    dest = os.path.join(home_dir, ".claude.json")
    with open(dest, "w") as fh:
        json.dump(payload, fh, indent=2)
    os.chmod(dest, 0o600)

    lib = os.path.join(home_dir, "Library")
    os.makedirs(lib, exist_ok=True)
    link = os.path.join(lib, "Keychains")
    if os.path.islink(link) or os.path.exists(link):
        os.remove(link)
    os.symlink(config.KEYCHAIN_DIR, link)

    record.add(dest, real, "jail-home-config",
               note="allow-listed keys only")
    record.notes.append({
        "kind": "jail_home",
        "home": home_dir,
        "claude_json_keys_seeded": seeded_keys,
        "claude_json_keys_dropped": dropped,
        "keychain_symlink": {"from": link, "to": config.KEYCHAIN_DIR,
                             "why": "Security resolves the login keychain relative to HOME; "
                                    "without this the CLI prints 'Not logged in'"},
        "closes": ["~/.claude/projects", "~/.claude/file-history", "~/.claude/paste-cache",
                   "~/.claude/history.jsonl", "~/.claude/memory (the memory feature's store)",
                   "claude --resume into a prior build session"],
    })
    return home_dir


# ---------------------------------------------------------------------------
# Guarding what lives OUTSIDE the sandbox
#
# The harness reaches outside itself in exactly one place: the jail HOME symlinks
# Library/Keychains to the real ~/Library/Keychains, because Security resolves the login
# keychain relative to HOME and the CLI cannot authenticate without it.
#
# That symlink caused real damage. _freeze() walked the sandbox and chmod'd every directory
# to 0555; os.walk lists a symlink-to-directory in its `dirs` list, and os.chmod follows
# symlinks, so the real ~/Library/Keychains went from drwx------ to dr-xr-xr-x. The keychain
# then could not be written, login succeeded but the credential could not be saved, and the
# machine's Claude Code auth failed with "OAuth access token has been revoked" -- a symptom
# that points nowhere near a file mode.
#
# Two defences, because the walker fix alone is one edit away from regressing:
#   1. no walker follows symlinks (see _iter_own for the traversal every chmod now uses);
#   2. the mode and ownership of every guarded external path are recorded before the run and
#      checked after it, restored if they moved, and the run fails loudly saying so.
# ---------------------------------------------------------------------------

def guarded_external_paths():
    """Paths outside the sandbox that the harness must leave exactly as it found them."""
    out = [config.KEYCHAIN_DIR]
    if os.path.isdir(config.KEYCHAIN_DIR):
        for name in sorted(os.listdir(config.KEYCHAIN_DIR)):
            if name.endswith(".keychain-db"):
                out.append(os.path.join(config.KEYCHAIN_DIR, name))
    return out


def snapshot_external(paths=None):
    """Mode, uid and gid of every guarded path, before anything runs."""
    snap = {}
    for p in (paths if paths is not None else guarded_external_paths()):
        try:
            st = os.lstat(p)
            snap[p] = {"mode": oct(st.st_mode & 0o7777), "uid": st.st_uid, "gid": st.st_gid}
        except OSError as exc:
            snap[p] = {"error": str(exc)}
    return snap


def assert_external_unchanged(before, restore=True):
    """Compare against the snapshot, restore what moved, and report.

    Restoring is not enough on its own: a harness that quietly repairs the damage it caused
    would keep causing it. The result carries `changed`, the caller raises, and the manifest
    records it.
    """
    after = snapshot_external(list(before))
    changed = []
    for path, was in before.items():
        now = after.get(path, {})
        if "error" in was or "error" in now:
            continue
        if was == now:
            continue
        entry = {"path": path, "before": was, "after": now, "restored": False}
        if restore and was.get("mode"):
            try:
                os.chmod(path, int(was["mode"], 8), follow_symlinks=False)
                entry["restored"] = (snapshot_external([path])[path] == was)
            except (OSError, ValueError) as exc:
                entry["restore_error"] = str(exc)
        changed.append(entry)
    return {
        "guarded_paths": list(before),
        "n_guarded": len(before),
        "changed": changed,
        "passed": not changed,
        "why": ("The jail HOME symlinks Library/Keychains to the real keychain directory so "
                "the CLI can authenticate. Nothing in the harness may alter it. A previous "
                "version did, via os.chmod following that symlink during teardown, and took "
                "the machine's Claude Code auth down for a day."),
    }


# ---------------------------------------------------------------------------
# hardlink refusal
# ---------------------------------------------------------------------------

def assert_no_hardlinks(root, record, allow_over=1):
    """Refuse to launch if any staged regular file has more than one link.

    A hardlink is a second name for the same inode. If a staged file were hardlinked to
    something outside the sandbox, a write through the sandboxed name would change the
    original -- and Seatbelt's path-based rules cannot see it, because there is only one
    inode and the deny rule matches the other path.

    The APFS clone does NOT create hardlinks (clonefile makes independent copy-on-write
    inodes, which is why writing to the clone leaves the original untouched), so a link
    count above one here means something else made it and it should stop the run.

    ONE EXEMPTION, LINUX ONLY: the refdata clone.

    Linux has no clonefile. `cp -al` is the only way to stage a 48 GB refdata tree per
    case, and it hardlinks, so on that platform every file in the clone trips this gate.

    The exemption is safe because the premise above does not hold there. Under bubblewrap
    the clone is bind-mounted read-only over itself, and a read-only mount is enforced by
    the kernel at write time rather than by matching a path, so every name for the inode
    inside the namespace returns EROFS. Inode aliasing gains nothing against it.

    It is also measured rather than asserted: the write probe attempts
    refdata/.harness_write_probe under the agent's own profile on every run and fails the
    run if it succeeds. On macOS this gate is the only line; on Linux it is the second of
    two, and the first one is tested every time.

    A hardlinked file anywhere else in the sandbox still stops the run on both platforms.
    Exempted files are counted and listed in the staging record, not silently skipped.
    """
    refdata_root = os.path.join(os.path.abspath(root), "refdata")
    exempt_refdata = sys.platform != "darwin"

    offenders, exempted = [], []
    for cur, _dirs, files in os.walk(root):
        for name in files:
            p = os.path.join(cur, name)
            try:
                st = os.lstat(p)
            except OSError:
                continue
            if not os.path.isfile(p) or os.path.islink(p):
                continue
            if st.st_nlink <= allow_over:
                continue
            hit = {"path": os.path.relpath(p, root),
                   "nlink": st.st_nlink, "inode": st.st_ino}
            ap = os.path.abspath(p)
            in_refdata = ap == refdata_root or ap.startswith(refdata_root + os.sep)
            if exempt_refdata and in_refdata:
                exempted.append(hit)
            else:
                offenders.append(hit)

    record.notes.append({
        "kind": "hardlink_scan",
        "root": root,
        "platform": sys.platform,
        "n_offenders": len(offenders),
        "offenders": offenders[:50],
        "n_exempted_refdata": len(exempted),
        "exempted_refdata": exempted[:50],
        "exemption": (
            "Files under <sandbox>/refdata are exempt on Linux only. The clone is made with "
            "cp -al because Linux has no clonefile, so every file in it is hardlinked. The "
            "gate's premise -- that path-based deny rules cannot see inode aliasing -- does "
            "not apply, because bubblewrap bind-mounts the clone read-only and a read-only "
            "mount refuses the write at the kernel regardless of how many names the inode "
            "has. The write probe tests exactly that on every run."
            if exempt_refdata else
            "No exemption applied. On macOS the clone is clonefile-based and every staged "
            "file has a link count of one."),
    })

    if offenders:
        raise StagingError(
            "%d staged file(s) have a link count above 1, so a write inside the sandbox "
            "could change a file outside it that Seatbelt's path rules cannot see: %s"
            % (len(offenders), offenders[:5]))
    return True


def assert_no_claude_md(root, record):
    """CLAUDE.md auto-loads silently into every session, from the cwd and every ancestor.

    A CLAUDE.md placed in the sandbox -- or in any directory above it, including the product
    root -- would be read into the agent's context with no tool call and no trace in the
    transcript. There is none on this machine today; this asserts it is still true at launch
    rather than at design time.
    """
    found = []
    for cur, _dirs, files in os.walk(root):
        if "CLAUDE.md" in files:
            found.append(os.path.join(cur, "CLAUDE.md"))

    # Every ancestor from the sandbox up to /.
    p = os.path.abspath(root)
    while True:
        cand = os.path.join(p, "CLAUDE.md")
        if os.path.exists(cand):
            found.append(cand)
        parent = os.path.dirname(p)
        if parent == p:
            break
        p = parent
    for extra in (os.path.expanduser("~/.claude/CLAUDE.md"),):
        if os.path.exists(extra):
            found.append(extra)

    record.notes.append({"kind": "claude_md_scan", "root": root, "found": found,
                         "why": "CLAUDE.md auto-loads from cwd and every ancestor with no "
                                "tool call and no transcript entry"})
    if found:
        raise StagingError("CLAUDE.md would auto-load into the agent's context: %s" % found)
    return True
