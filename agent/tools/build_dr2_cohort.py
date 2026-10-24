#!/usr/bin/env python3
"""build_dr2_cohort.py -- split the 1000G sample set into a DR2 hold-out cohort and an
imputation reference set, for joint imputation.

Beagle's DR2 is a per-variant statistic computed across the target samples, so a single patient
imputed alone gives it nothing to spread over. This tool draws a hold-out cohort from one
superpopulation; those samples are imputed as Beagle targets alongside the patient, and everyone
else in the panel -- all superpopulations, not just the chosen one -- becomes the reference.

The two sets must be disjoint because a sample present in both would find its own haplotypes in
the reference panel and impute itself, inflating DR2 to a number that says nothing about how well
the patient's genotypes were recovered.

The superpopulation and the cohort size are judgments, not defaults: the tool refuses to run
without them. The seed is fixed for every run (lib_run.DEFAULT_SEED) unless the agent's stability
check asks for a second, and the output says which it was.

Usage:
  python3 tools/build_dr2_cohort.py \
      --panel refdata/ancestry-ref/integrated_call_samples_v3.20130502.ALL.panel \
      --super-pop EUR --size 100 \
      --out-dir runs/<RUN_ID>/score/dr2
"""
import argparse
import datetime
import hashlib
import json
import os
import random
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import lib_run  # noqa: E402


def utcnow():
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def read_panel(path):
    """Return [(sample, super_pop), ...] from the .panel file, header line skipped."""
    rows = []
    with open(path) as fh:
        for lineno, line in enumerate(fh, start=1):
            if lineno == 1:
                continue
            fields = line.rstrip("\n").split("\t")
            if len(fields) < 3 or not fields[0].strip():
                continue
            rows.append((fields[0].strip(), fields[2].strip()))
    return rows


def die(msg):
    print(f"build_dr2_cohort.py: {msg}", file=sys.stderr)
    sys.exit(1)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--panel", required=True)
    ap.add_argument("--super-pop", required=True, help="no default -- an agent judgment")
    ap.add_argument("--size", required=True, type=int, help="no default -- an agent judgment")
    ap.add_argument("--seed", type=int, default=lib_run.DEFAULT_SEED,
                    help="fixed for every run and recorded; another only for the score skill's "
                         "second-seed check")
    ap.add_argument("--out-dir", default=".")
    args = ap.parse_args()

    if not os.path.exists(args.panel):
        die(f"panel file not found: {args.panel}")

    rows = read_panel(args.panel)
    if not rows:
        die(f"no data rows read from {args.panel}")

    all_samples = [s for s, _ in rows]
    if len(set(all_samples)) != len(all_samples):
        die("panel contains duplicate sample IDs; refusing to split")

    present = sorted({sp for _, sp in rows})
    if args.super_pop not in present:
        die(f"--super-pop {args.super_pop!r} not present in {args.panel}. "
            f"Valid values found: {', '.join(present)}")

    in_pop = sorted(s for s, sp in rows if sp == args.super_pop)
    n_available = len(in_pop)
    if args.size < 0:
        die(f"--size must be non-negative, got {args.size}")
    if args.size > n_available:
        die(f"--size {args.size} exceeds the {n_available} samples available in "
            f"superpopulation {args.super_pop}")

    rng = random.Random(args.seed)
    holdout = set(rng.sample(in_pop, args.size))
    panel_set = set(all_samples)
    reference = panel_set - holdout

    if holdout & reference:
        die("hold-out and reference sets overlap -- a sample would impute itself")
    if holdout | reference != panel_set:
        die("hold-out plus reference does not reconstruct the full panel")

    os.makedirs(args.out_dir, exist_ok=True)
    holdout_path = os.path.join(args.out_dir, "dr2_holdout.txt")
    reference_path = os.path.join(args.out_dir, "dr2_reference.txt")
    json_path = os.path.join(args.out_dir, "dr2_cohort.json")

    for path, ids in ((holdout_path, holdout), (reference_path, reference)):
        with open(path, "w") as fh:
            for sample in sorted(ids):
                fh.write(sample + "\n")

    provenance = {
        "tool": "build_dr2_cohort.py",
        "generated_utc": utcnow(),
        "purpose": "DR2 hold-out cohort + imputation reference set for joint imputation",
        "panel": args.panel,
        "panel_sha256": sha256_file(args.panel),
        "super_pop": args.super_pop,
        "size": args.size,
        "seed": args.seed,
        "seed_is_the_default": args.seed == lib_run.DEFAULT_SEED,
        "n_holdout": len(holdout),
        "n_reference": len(reference),
        "n_panel_total": len(panel_set),
        "n_available_in_super_pop": n_available,
        "super_pops_present": present,
        "disjoint_by_construction": True,
        "union_equals_panel_by_construction": True,
        "holdout_file": holdout_path,
        "reference_file": reference_path,
    }
    with open(json_path, "w") as fh:
        json.dump(provenance, fh, indent=2, sort_keys=True)
        fh.write("\n")

    print(f"{args.super_pop}: n_holdout={len(holdout)} n_reference={len(reference)} seed={args.seed}")


if __name__ == "__main__":
    main()
