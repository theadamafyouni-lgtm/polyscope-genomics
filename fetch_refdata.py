#!/usr/bin/env python3
"""Fetch the reference data this harness needs, from the recipe that records it.

    python3 fetch_refdata.py --plan              # what it would do, downloads nothing
    python3 fetch_refdata.py                     # selection profile, about 36 GB
    python3 fetch_refdata.py --profile full      # everything, about 43 GB
    python3 fetch_refdata.py --verify            # sha256 what is already here

agent/refdata is tens of gigabytes and cannot live in a git repository. It is not
a black box either: refdata_recipe.json, tracked beside this script, records each
external artifact with its source URL, its size and its sha256, so this reads that
file rather than carrying a URL list of its own.

agent/refdata/MANIFEST.json is a different file. It is the 32-record manifest the
Mac staging wrote, tracked byte for byte as the published runs had it, because every
run clone contains it and agents read it. Do not edit it; the recipe is derived from
it (its "derived_from" field has the hash).

WHAT THE RECIPE COVERS, AND WHAT IT DOES NOT

81 records, 42.5 GB, each hash-checked against a full download on 2026-09-23. For a
plain file its sha256 equals the working box's copy. For an archive, its sha256 is
the download's and each listed member equals the working box's file. That includes
ancestry-ref/hgdp_1kgp/ (as the 16 GB PGS Catalog tarball it ships in) and
ancestry-ref/phase3/ (1000 Genomes phase 3 v5a, 44 files). Archives land in
agent/refdata-downloads/, outside agent/refdata, so that no run clone sees them;
build_refdata.py unpacks them and derives the rest. What neither script produces is
listed in SETUP.md section 8, which also has the evidence for every record.

This downloads. It does not check what it downloaded: a size mismatch is printed,
not fatal. Run --verify afterwards. build_refdata.py re-checks every archive it
unpacks before it uses it. An archive whose members are all in place at their
recorded sizes counts as present, so deleting agent/refdata-downloads/ after a
build does not make the next run fetch it again.

TWO PROFILES, AND WHY THE SMALLER ONE IS THE DEFAULT

    selection   about 36 GB. 59 artifacts. Everything except the Beagle panel.
    full        about 43 GB. Adds the 22 per-chromosome panel files.

The selection benchmark runs read-and-reason, ancestry and select, and stops
before scoring. Imputation is a scoring-stage concern, so no case in this
benchmark ever opens the panel. Downloading 6 GB to leave it untouched is not
caution, it is just 6 GB. Scoring also needs Java, and the only JRE recorded is the
macOS one the Mac staged (below), so a Linux box cannot score from this alone.

hgdp_1kgp and phase3 are in the selection profile. The ancestry stage builds its PCA
basis from hgdp_1kgp in every run. phase3 is staged into every run clone, and at
least one published selection result (p75) cites it.

ONE DOWNLOAD IS SLOW

The HGDP+1kGP tarball is 15,953,388,718 bytes on ftp.ebi.ac.uk, and one connection
from the working box got 0.6-0.7 MB/s: six to eight hours, with no resume here.
Fetch it first, any other way, to agent/refdata-downloads/pgsc_HGDP+1kGP_v1.tar.zst,
and let it finish before running this (a partial file counts as wrong-size and is
fetched again over it). 16 ranged connections took 37 min. --verify and
build_refdata.py check its sha256 either way, and the distributor publishes its md5
(2bd3586e9a09700f9bcbe62ba4dcee49) in md5s.txt beside it.

TWO ARTIFACTS ARE PLATFORM-SPECIFIC

plink2 is the Linux x86_64 build the published runs used. The JRE is the macOS
aarch64 build the Mac staged, which the published runs also had in refdata/jre/ and
never started. Both are fetched as recorded, on every platform, so that what lands
is what the runs had. (This script used to substitute a linux/x64 "latest" JRE; it
never matched the recorded hash and was fetched again on every run.)
"""

import argparse
import hashlib
import json
import os
import sys
import urllib.request

REPO = os.path.dirname(os.path.abspath(__file__))
AGENT = os.path.join(REPO, "agent")
# The rebuild recipe. Not agent/refdata/MANIFEST.json, which run clones see and which
# stays byte-identical to the published runs' copy (see the docstring).
MANIFEST = os.path.join(REPO, "refdata_recipe.json")

# The Beagle imputation panel: one file per autosome. Not touched by any case in
# the selection benchmark, which stops before scoring.
SCORING_ONLY_PREFIXES = ("panel_chr",)

# api.adoptium.net answers Python's default User-Agent with 403; every other host in the
# recipe accepts either. So say who is asking.
USER_AGENT = "fetch_refdata.py (prs-agent; +https://github.com/theadamafyouni-lgtm/prs-agent)"

GB = 1000 ** 3


def human(b):
    """Decimal units, as the docs and build_refdata.py use them."""
    b = float(b)
    for u in ("B", "KB", "MB", "GB"):
        if b < 1000 or u == "GB":
            return "%.1f %s" % (b, u)
        b /= 1000.0
    return "%.1f GB" % b


def load_manifest():
    if not os.path.isfile(MANIFEST):
        sys.exit("no recipe at %s\n"
                 "It is the record of where every external artifact came from, and "
                 "without it this script has nothing to follow. It is tracked in "
                 "this repository: git checkout refdata_recipe.json" % MANIFEST)
    with open(MANIFEST) as fh:
        return json.load(fh)


def wanted(artifacts, profile):
    if profile == "full":
        return list(artifacts)
    return [a for a in artifacts
            if not a.get("key", "").startswith(SCORING_ONLY_PREFIXES)]


def dest_of(a):
    # manifest paths are relative to agent/
    return os.path.join(AGENT, a["path"])


def sha256_of(path, cap_mb=None):
    h = hashlib.sha256()
    read = 0
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
            read += len(chunk)
            if cap_mb and read > cap_mb * (1 << 20):
                return None
    return h.hexdigest()


def members_of(a):
    """What an archive record unpacks or is re-compressed to: {name: {dest, sha256,
    bytes}}, or {}."""
    if "members" in a:
        return a["members"]
    if "yields" in a:
        return a["yields"]
    if "member" in a:
        return {a["member"]["name"]: a["member"]}
    return {}


def unpacked(a):
    """True if every member of an archive record is in place at its recorded size."""
    ms = members_of(a)
    return bool(ms) and all(
        os.path.isfile(os.path.join(AGENT, m["dest"]))
        and os.path.getsize(os.path.join(AGENT, m["dest"])) == m["bytes"]
        for m in ms.values())


def build_headroom(arts):
    """Free space build_refdata.py still needs beyond the downloads: the hgdp_1kgp
    members if not yet extracted, 10 GB for the PCA basis if not yet built, and about
    1 GB for fasta and sites_index if missing."""
    need = 0
    for a in arts:
        if a.get("key") == "hgdp_1kgp_tarball" and not unpacked(a):
            need += sum(m["bytes"] for m in members_of(a).values())
        if a.get("key") == "fasta" and not unpacked(a):
            need += 1 * GB
    if not os.path.isdir(os.path.join(AGENT, "refdata", "ancestry-ref", "hgdp_basis")):
        need += 10 * GB
    return need


def state_of(a):
    """present / wrong-size / missing, without hashing gigabytes every run."""
    p = dest_of(a)
    if not os.path.isfile(p):
        return "present" if unpacked(a) else "missing"
    want = a.get("bytes")
    if want and os.path.getsize(p) != want:
        return "wrong-size"
    return "present"


def fetch(a, url=None):
    p = dest_of(a)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    tmp = p + ".part"
    src = urllib.request.Request(url or a["url"], headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(src) as r, open(tmp, "wb") as out:
        total = a.get("bytes") or 0
        got = 0
        while True:
            chunk = r.read(1 << 20)
            if not chunk:
                break
            out.write(chunk)
            got += len(chunk)
            if total:
                pct = 100.0 * got / total
                sys.stdout.write("\r    %-28s %5.1f%%  %s" %
                                 (a["key"][:28], pct, human(got)))
                sys.stdout.flush()
    sys.stdout.write("\r" + " " * 60 + "\r")
    os.replace(tmp, p)
    return os.path.getsize(p)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--profile", choices=("selection", "full"), default="selection")
    ap.add_argument("--plan", action="store_true", help="report only, download nothing")
    ap.add_argument("--verify", action="store_true",
                    help="sha256 what is already on disk. Slow: it reads every byte.")
    args = ap.parse_args()

    m = load_manifest()
    arts = m.get("artifacts") or []
    keep = wanted(arts, args.profile)
    skipped = len(arts) - len(keep)

    print("recipe     %s" % MANIFEST)
    print("written    %s" % m.get("written_utc", m.get("assembled_utc", "?")))
    print("profile    %s   (%d of %d artifacts%s)"
          % (args.profile, len(keep), len(arts),
             ", %d imputation-only skipped" % skipped if skipped else ""))
    print()

    if args.verify:
        bad = 0
        for a in keep:
            p = dest_of(a)
            if not os.path.isfile(p):
                if unpacked(a):
                    print("unpacked  %-28s archive not kept; build_refdata.py --check "
                          "hashes the %d file(s) it yields" % (a["key"][:28], len(members_of(a))))
                    continue
                print("MISSING   %s" % a["key"])
                bad += 1
                continue
            want = a.get("sha256")
            if not want:
                print("no hash   %s" % a["key"])
                continue
            got = sha256_of(p)
            if got == want:
                print("ok        %-28s %s" % (a["key"][:28], human(os.path.getsize(p))))
            else:
                print("MISMATCH  %-28s on disk %s..." % (a["key"][:28], (got or "?")[:16]))
                bad += 1
        print()
        print("%d problem(s)" % bad)
        return 1 if bad else 0

    todo, have, bytes_todo = [], 0, 0
    for a in keep:
        st = state_of(a)
        if st == "present":
            have += 1
        else:
            todo.append((a, st))
            bytes_todo += a.get("bytes") or 0

    print("already here : %d" % have)
    print("to fetch     : %d   %s" % (len(todo), human(bytes_todo)))
    if todo:
        print()
        for a, st in todo[:40]:
            print("  %-10s %-26s %s" % (st, a["key"][:26], human(a.get("bytes") or 0)))
        if len(todo) > 40:
            print("  ... and %d more" % (len(todo) - 40))

    st = os.statvfs(AGENT)
    free = st.f_bavail * st.f_frsize
    headroom = build_headroom(arts)
    print()
    print("disk free    : %s" % human(free))
    if bytes_todo and free < bytes_todo + headroom:
        print("NOT ENOUGH ROOM. This wants %s: the downloads, plus %s for what "
              "build_refdata.py still has to derive (SETUP.md section 8)."
              % (human(bytes_todo + headroom), human(headroom)))
        if not args.plan:
            return 1

    if args.plan:
        print("\nplan only. nothing downloaded.")
        return 0
    if not todo:
        print("\nnothing to do.")
        return 0

    print()
    failed = []
    for i, (a, _st) in enumerate(todo, start=1):
        print("[%d/%d] %s" % (i, len(todo), a["key"]))
        try:
            n = fetch(a)
        except Exception as exc:  # noqa: BLE001
            print("       FAILED: %s" % str(exc)[:160])
            failed.append(a["key"])
            continue
        want = a.get("bytes")
        if want and n != want:
            print("       size differs: got %s, manifest says %s"
                  % (human(n), human(want)))

    print()
    print("%d fetched, %d failed" % (len(todo) - len(failed), len(failed)))
    for k in failed:
        print("  failed: %s" % k)
    print()
    print("Downloads only. Check them, then build what is derived from them")
    print("(plink2, hgdp_1kgp, jre and maps out of their archives, fasta, the")
    print("hgdp_1kgp panel, the phase3 markers, sites_index and the PCA basis):")
    print("  python3 fetch_refdata.py --verify")
    print("  python3 build_refdata.py")
    print()
    print("Then check the machine as a whole:")
    print("  python3 preflight.py")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
