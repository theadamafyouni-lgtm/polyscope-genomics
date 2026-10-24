#!/usr/bin/env python3
"""chromosome_counts.py -- how many rows a scoring file holds on each chromosome.

Counts only. It reads the chromosome column the agent named after reading the file's own
header (SEL-13), counts the rows on each value, and lists every autosome, 1 to 22, that has
none. It sets no threshold and refuses nothing: whether a missing chromosome matters for this
model and this person is the agent's judgment (ARCH-5).

Why it exists: weighted coverage is built from the rows a model file holds, so a chromosome
with no rows is in neither its numerator nor its denominator. A model can measure high
coverage and still say nothing about a whole chromosome, and no coverage figure shows it.

A value counts as autosome N when it is N, with or without a leading "chr" in any case. Every
other value is counted as it appears; a row with no value in the column is counted apart.

Usage:
  python3 tools/chromosome_counts.py \
      --scorefile runs/R/select/models/PGS000001_hmPOS_GRCh37.txt.gz --chr-col hm_chr \
      --run R --out runs/R/select/chromosome_counts_PGS000001.json
"""
import argparse
import collections
import gzip
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import lib_run  # noqa: E402

AUTOSOMES = [str(i) for i in range(1, 23)]


def open_maybe_gz(path):
    return gzip.open(path, "rt", encoding="utf-8", errors="replace") if path.endswith(".gz") \
        else open(path, encoding="utf-8", errors="replace")


def autosome(value):
    v = value[3:] if value[:3].lower() == "chr" else value
    return v if v in AUTOSOMES else None


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--scorefile", required=True)
    ap.add_argument("--chr-col", required=True,
                    help="the chromosome column, as the file's own header names it")
    ap.add_argument("--run")
    ap.add_argument("--out", required=True)
    args = ap.parse_args()

    ix = None
    counts = collections.Counter()
    n_rows = n_no_value = 0
    with open_maybe_gz(args.scorefile) as fh:
        for line in fh:
            if line.startswith("#"):
                continue
            f = line.rstrip("\r\n").split("\t")
            if ix is None:
                if args.chr_col not in f:
                    raise SystemExit(
                        f"chromosome_counts.py: column {args.chr_col!r} not in the scoring "
                        f"file. Present: {sorted(f)}")
                ix = f.index(args.chr_col)
                continue
            if not line.strip():
                continue
            n_rows += 1
            value = f[ix].strip() if ix < len(f) else ""
            if value:
                counts[value] += 1
            else:
                n_no_value += 1
    if ix is None:
        raise SystemExit("chromosome_counts.py: no column header line in the scoring file")

    per_autosome = {a: 0 for a in AUTOSOMES}
    for value, n in counts.items():
        a = autosome(value)
        if a:
            per_autosome[a] += n
    zero = [a for a in AUTOSOMES if not per_autosome[a]]

    def order(value):
        a = autosome(value)
        return (0, int(a), value) if a else (1, 0, value)

    result = {
        "tool": "chromosome_counts.py",
        "generated_utc": lib_run.utcnow(),
        "scorefile": args.scorefile,
        "scorefile_sha256": lib_run.sha256_file(args.scorefile),
        "column_used": args.chr_col,
        "n_data_rows": n_rows,
        "rows_per_chromosome_value": {v: counts[v] for v in sorted(counts, key=order)},
        "n_rows_with_no_chromosome": n_no_value,
        "rows_per_autosome": per_autosome,
        "autosomes_with_zero_rows": zero,
        "decision_is_not_here": "Counts only. Whether a chromosome with no rows matters for this "
                                "model and this person is the agent's call; this file sets no "
                                "threshold and refuses nothing.",
    }
    lib_run.emit(result, args.out)
    if args.run:
        lib_run.ledger_append(args.run, {
            "kind": "facts", "stage": "select", "tool": "chromosome_counts.py",
            "autosomes_with_zero_rows": zero, "artifact": args.out,
        })


if __name__ == "__main__":
    main()
