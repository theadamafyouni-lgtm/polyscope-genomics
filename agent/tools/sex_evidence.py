#!/usr/bin/env python3
"""sex_evidence.py -- what the person's own file shows on the sex chromosomes, counted.

Mechanical. For X, Y, XY (the pseudoautosomal code some arrays use) and MT, however spelled, it
counts each record's genotype as a no-call, haploid (one allele), homozygous or heterozygous,
inside and outside the pseudoautosomal regions the agent names, and the same counts over the
autosomes for comparison. It does not say what sex anyone is, does not decide whether a pattern
is typical of anything, and holds no table of coordinates: the pseudoautosomal regions belong to
the file's build, and the agent, who settled the build, passes them (or --no-par).

It reads the person's file as received -- a genotype table or a VCF, plain, .gz or a one-member
.zip -- because the normalized VCF drops what this needs: haploid X and Y calls at an array's
door, and X and Y altogether on the VCF route.

Usage:
  python3 tools/sex_evidence.py --run <RUN_ID> --input <the person's file> \\
      --chrom-col 1 --pos-col 2 --genotype-col 3 --no-call-token=-- \\
      --par X:60001-2699520 --par X:154931044-155260560 \\
      --par Y:10001-2649520 --par Y:59034050-59363566 \\
      --out runs/<RUN_ID>/door/sex_evidence.json
  (a VCF needs no columns: GT is read from FORMAT, for --sample or its only sample)
"""
import argparse
import gzip
import io
import os
import re
import sys
import zipfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import lib_run  # noqa: E402

CONTIGS = {"X": "X", "23": "X", "Y": "Y", "24": "Y", "XY": "XY", "25": "XY", "MT": "MT", "M": "MT", "26": "MT"}
AUTOSOMES = {str(i) for i in range(1, 23)}
KINDS = ("no_call", "haploid", "homozygous", "heterozygous", "other")


def contig(name):
    name = name.strip()
    if name[:3].lower() == "chr":
        name = name[3:]
    name = name.upper()
    return CONTIGS.get(name) or (name if name.lstrip("0") in AUTOSOMES else None)


def open_text(path):
    with open(path, "rb") as fh:
        magic = fh.read(4)
    if magic.startswith(b"PK"):
        archive = zipfile.ZipFile(path)
        (member,) = [m for m in archive.infolist() if not m.is_dir()]
        return io.TextIOWrapper(archive.open(member), encoding="utf-8", errors="replace")
    if magic.startswith(b"\x1f\x8b"):
        return io.TextIOWrapper(gzip.open(path, "rb"), encoding="utf-8", errors="replace")
    return open(path, encoding="utf-8", errors="replace")


def classify_alleles(alleles, no_calls):
    """A genotype as its alleles, already split: one is haploid, two equal homozygous."""
    if not alleles or any(a in no_calls or a in ("", ".") for a in alleles):
        return "no_call"
    if len(alleles) == 1:
        return "haploid"
    if len(alleles) == 2:
        return "homozygous" if alleles[0] == alleles[1] else "heterozygous"
    return "other"


def classify_token(token, no_calls):
    """A table's one-token genotype: "AG", "A", "--"."""
    token = token.strip()
    if token in no_calls or not token:
        return "no_call"
    if len(token) in (1, 2) and token.isalpha():
        return classify_alleles(list(token), no_calls)
    return "other"


def parse_par(text):
    found = re.fullmatch(r"([^:]+):(\d+)-(\d+)", text)
    if not found or contig(found[1]) not in ("X", "Y"):
        raise argparse.ArgumentTypeError("--par is CONTIG:START-END on X or Y, e.g. X:60001-2699520")
    return contig(found[1]), int(found[2]), int(found[3])


def tally():
    return {kind: 0 for kind in ("records", "called") + KINDS}


def finished(counts):
    diploid = counts["homozygous"] + counts["heterozygous"]
    return dict(counts, heterozygous_share_of_diploid_calls=(counts["heterozygous"] / diploid) if diploid else None)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--run")
    ap.add_argument("--input", required=True)
    ap.add_argument("--chrom-col", type=int, help="table only: 0-based chromosome column")
    ap.add_argument("--pos-col", type=int, help="table only: 0-based position column")
    ap.add_argument("--genotype-col", type=int, help="table only: one genotype column, e.g. AG")
    ap.add_argument("--allele-cols", help="table only: two allele columns, e.g. 3,4")
    ap.add_argument("--no-call-token", action="append", default=[],
                    help="a token the file writes for no call (repeatable), e.g. -- or 0")
    ap.add_argument("--sample", help="VCF only: the sample, where it has more than one")
    ap.add_argument("--par", action="append", type=parse_par, default=[],
                    help="a pseudoautosomal region of the file's build, CONTIG:START-END (repeatable)")
    ap.add_argument("--no-par", action="store_true", help="name no pseudoautosomal region")
    ap.add_argument("--out", required=True)
    args = ap.parse_args()
    if bool(args.par) == args.no_par:
        ap.error("name the file's pseudoautosomal regions with --par, or pass --no-par")
    no_calls = set(args.no_call_token)
    allele_cols = [int(c) for c in args.allele_cols.split(",")] if args.allele_cols else None

    counts = {}
    autosomes = tally()
    fmt = None
    sample_index = gt_index = None
    with open_text(args.input) as fh:
        for raw in fh:
            line = raw.rstrip("\n").rstrip("\r")
            if line.startswith("##"):
                fmt = fmt or ("vcf" if line.startswith("##fileformat=VCF") else None)
                continue
            if line.startswith("#CHROM"):
                fmt = "vcf"
                names = line.split("\t")[9:]
                if args.sample:
                    sample_index = 9 + names.index(args.sample)
                elif len(names) == 1:
                    sample_index = 9
                else:
                    raise SystemExit("sex_evidence.py: the VCF has %d samples; name one with --sample" % len(names))
                continue
            if not line or line.startswith("#"):
                continue
            fields = line.split("\t") if fmt == "vcf" else re.split(r"\t|,", line)
            if fmt == "vcf":
                where, pos = contig(fields[0]), fields[1]
                keys = fields[8].split(":")
                if gt_index is None or keys[gt_index:gt_index + 1] != ["GT"]:
                    gt_index = keys.index("GT") if "GT" in keys else None
                gt = fields[sample_index].split(":")[gt_index] if gt_index is not None else "."
                kind = classify_alleles(re.split(r"[/|]", gt), no_calls)
            else:
                if args.chrom_col is None or args.pos_col is None or (args.genotype_col is None) == (allele_cols is None):
                    raise SystemExit("sex_evidence.py: a table needs --chrom-col, --pos-col and one of "
                                     "--genotype-col or --allele-cols")
                if max([args.chrom_col, args.pos_col, args.genotype_col or 0] + (allele_cols or [])) >= len(fields):
                    continue
                where, pos = contig(fields[args.chrom_col]), fields[args.pos_col]
                kind = (classify_token(fields[args.genotype_col], no_calls) if allele_cols is None
                        else classify_alleles([fields[c].strip() for c in allele_cols], no_calls))
            if where is None or not pos.isdigit():
                continue
            if where in AUTOSOMES or where.lstrip("0") in AUTOSOMES:
                bucket = autosomes
            else:
                inside = any(c == where and lo <= int(pos) <= hi for c, lo, hi in args.par)
                bucket = counts.setdefault(where, {"outside_par": tally(), "in_par": tally()})[
                    "in_par" if inside else "outside_par"]
            bucket["records"] += 1
            bucket[kind] += 1
            bucket["called"] += kind != "no_call"

    result = {
        "tool": "sex_evidence.py",
        "generated_utc": lib_run.utcnow(),
        "input": args.input,
        "format": fmt or "table",
        "no_call_tokens": sorted(no_calls),
        "par_regions": [{"contig": c, "start": lo, "end": hi} for c, lo, hi in args.par],
        "contigs": {name: {side: finished(c) for side, c in sides.items()}
                    for name, sides in sorted(counts.items())},
        "autosomes_for_comparison": finished(autosomes),
        "what_this_does_not_do": [
            "say what sex the person is, chromosomally or otherwise -- that is the agent's judgment",
            "decide whether a count is typical of any karyotype",
            "check that the pseudoautosomal regions passed belong to the file's build",
        ],
    }
    lib_run.emit(result, args.out)
    if args.run:
        lib_run.ledger_append(args.run, {"kind": "facts", "stage": "door", "tool": "sex_evidence.py",
                                         "artifact": args.out})


if __name__ == "__main__":
    main()
