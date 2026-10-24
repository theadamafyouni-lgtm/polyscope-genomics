#!/usr/bin/env python3
"""cg_to_vcf.py -- a Complete Genomics var file as a VCF the door's other tools read.

Mechanical. A var file describes the whole genome locus by locus, one row per allele where the
alleles differ: reference stretches, no-call stretches and variant loci, each allele row with
its own scores (varScoreVAF, varScoreEAF) and filter (varFilter). This writes one VCF record
for every single-base SNV position in it, in the file's own order:

  GT            from the allele rows covering the position: the reference base, the SNV's base,
                or missing where an allele is a no-call or not a single base there -- a half-call
                is missing, never half a genotype; haploid where the locus has ploidy 1
  FILTER        PASS where every allele's varFilter is PASS, else the others it carries
  FORMAT/VAFS, EAFS, CGFT   each allele's varScoreVAF, varScoreEAF and varFilter, in allele order
  FORMAT/VAFMIN, EAFMIN     the lower of the alleles' scores: one value a policy can test

Reference stretches are not written: the VCF is variant-only, so an absent position is unknown,
as on any variant-only VCF, never reference. Insertions, deletions and substitutions are not
SNVs and are counted, not written. The var file's # header travels as ##CG_<KEY>=<value>. It
says nothing about which calls are reliable: that is the agent's policy (call_quality.py).

Usage:
  python3 tools/cg_to_vcf.py --run <RUN_ID> --input <the var file> --sample-id <name> \\
      --out runs/<RUN_ID>/door/cg_converted.vcf.gz
  (also writes cg_converted.json beside it: what was written, and what was not)
"""
import argparse
import bz2
import collections
import gzip
import io
import os
import subprocess
import sys
import tempfile
import zipfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import lib_run  # noqa: E402

CALLED = {"snp", "ref"}
STRETCHES = {"ref", "no-call", "no-ref", "no-call-rc", "no-call-ri"}
NEEDED = ("locus", "ploidy", "allele", "chromosome", "begin", "end", "varType", "reference",
          "alleleSeq", "varScoreVAF", "varScoreEAF", "varFilter")


def open_text(path):
    with open(path, "rb") as fh:
        magic = fh.read(4)
    if magic.startswith(b"BZh"):
        return io.TextIOWrapper(bz2.open(path, "rb"), encoding="utf-8", errors="replace")
    if magic.startswith(b"\x1f\x8b"):
        return io.TextIOWrapper(gzip.open(path, "rb"), encoding="utf-8", errors="replace")
    if magic.startswith(b"PK"):
        archive = zipfile.ZipFile(path)
        (member,) = [m for m in archive.infolist() if not m.is_dir()]
        return io.TextIOWrapper(archive.open(member), encoding="utf-8", errors="replace")
    return open(path, encoding="utf-8", errors="replace")


def score(text):
    return int(text) if text.strip().lstrip("-").isdigit() else None


def records_for(rows, counts):
    """The VCF records of one locus: each single-base SNV position in it, with every allele's
    state there. rows are dicts of the columns."""
    ploidy = int(rows[0]["ploidy"] or 2)
    alleles = ["all"] if ploidy == 1 else ["1", "2"]
    for kind in {r["varType"] for r in rows} - CALLED - {"no-call", "no-ref", "no-call-rc", "no-call-ri"}:
        counts["variant_loci_not_snv_dropped"][kind] += 1
    positions = sorted({int(r["begin"]) for r in rows if r["varType"] == "snp"
                        and int(r["end"]) - int(r["begin"]) == 1})
    out = []
    for p in positions:
        ref = next(r["reference"] for r in rows if r["varType"] == "snp" and int(r["begin"]) == p).upper()
        states = []
        for a in alleles:
            covering = [r for r in rows if r["allele"] in (a, "all") and int(r["begin"]) <= p < int(r["end"])]
            row = covering[0] if len(covering) == 1 else None
            if row is None or row["varType"] not in CALLED or (row["varType"] == "snp" and int(row["end"]) - p != 1):
                states.append((None, row))
            else:
                states.append((ref if row["varType"] == "ref" else row["alleleSeq"].upper(), row))
        alts = []
        for base, _ in states:
            if base and base != ref and base not in alts:
                alts.append(base)
        if not alts:
            continue
        missing = any(base is None for base, _ in states)
        codes = [(0 if base == ref else 1 + alts.index(base)) for base, _ in states] if not missing else None
        gt = "/".join(str(c) for c in codes) if codes else "/".join("." for _ in states)
        rows_at = [row for _, row in states]
        vaf = [score(r["varScoreVAF"]) if r else None for r in rows_at]
        eaf = [score(r["varScoreEAF"]) if r else None for r in rows_at]
        flt = [(r["varFilter"] or ".") if r else "." for r in rows_at]
        failing = sorted({f for value in flt if value not in (".", "PASS") for f in value.split(";")})
        show = lambda values: ",".join("." if v is None else str(v) for v in values)
        lower = lambda values: str(min(values)) if not missing and all(v is not None for v in values) else "."
        out.append((rows[0]["chromosome"], p + 1, ref, ",".join(alts), ";".join(failing) or "PASS",
                    "%s:%s:%s:%s:%s:%s" % (gt, show(vaf), show(eaf), ",".join(flt), lower(vaf), lower(eaf))))
        counts["filters"].update(failing or ["PASS"])
        counts["gt_missing" if missing else "gt_called"] += 1
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--run")
    ap.add_argument("--input", required=True, help="a Complete Genomics var file, plain, bzip2, gzip or zip")
    ap.add_argument("--sample-id", required=True)
    ap.add_argument("--out", required=True, help="the bgzipped VCF to write, .vcf.gz")
    args = ap.parse_args()
    if not args.out.endswith(".vcf.gz"):
        raise SystemExit("cg_to_vcf.py: --out is a .vcf.gz")

    header, columns = [], None
    counts = {"loci": 0, "snv_records_written": 0, "gt_called": 0, "gt_missing": 0,
              "variant_loci_not_snv_dropped": collections.Counter(), "filters": collections.Counter()}
    contigs = []
    body = tempfile.NamedTemporaryFile("w", delete=False, dir=os.path.dirname(os.path.abspath(args.out)),
                                       prefix=".cg_to_vcf.", suffix=".body")
    try:
        with open_text(args.input) as fh, body:
            locus, rows = None, []

            def flush():
                if not rows:
                    return
                counts["loci"] += 1
                for chrom, pos, ref, alt, flt, sample in records_for(rows, counts):
                    name = chrom[3:] if chrom[:3].lower() == "chr" else chrom
                    name = "MT" if name in ("M", "MT") else name
                    if not contigs or contigs[-1] != name:
                        contigs.append(name)
                    body.write("%s\t%d\t.\t%s\t%s\t.\t%s\t.\tGT:VAFS:EAFS:CGFT:VAFMIN:EAFMIN\t%s\n"
                               % (name, pos, ref, alt, flt, sample))
                    counts["snv_records_written"] += 1

            for line in fh:
                line = line.rstrip("\n").rstrip("\r")
                if columns is None:
                    if line.startswith("#"):
                        key, _, value = line[1:].partition("\t")
                        if key:
                            header.append((key.strip(), value.strip()))
                    elif line.startswith(">"):
                        columns = line[1:].split("\t")
                        missing = [c for c in NEEDED if c not in columns]
                        if missing:
                            raise SystemExit("cg_to_vcf.py: not a var file layout: no %s column" % ", ".join(missing))
                        i_locus, i_allele, i_type = (columns.index(c) for c in ("locus", "allele", "varType"))
                        if max(i_locus, i_allele, i_type) > 6:
                            raise SystemExit("cg_to_vcf.py: locus, allele and varType must be among the first seven columns")
                    continue
                if not line:
                    continue
                f = line.split("\t", 7)
                if f[i_locus] != locus:
                    flush()
                    locus, rows = f[i_locus], []
                    # Most loci are one stretch for every allele, reference or no-call, with
                    # nothing to write: counted and passed over without being parsed.
                    if f[i_allele] == "all" and f[i_type] in STRETCHES:
                        counts["loci"] += 1
                        locus = None
                        continue
                f = line.split("\t")
                rows.append(dict(zip(columns, f + [""] * (len(columns) - len(f)))))
            flush()
        filters = sorted(set(counts["filters"]) - {"PASS"})
        head = ["##fileformat=VCFv4.2", "##source=cg_to_vcf.py (Complete Genomics var file, SNVs only)"]
        head += ["##CG_%s=%s" % (k, v) for k, v in header]
        head += ["##contig=<ID=%s>" % c for c in dict.fromkeys(contigs)]
        head += ['##FILTER=<ID=%s,Description="Complete Genomics varFilter %s on an allele">' % (f, f) for f in filters]
        head += ['##FORMAT=<ID=GT,Number=1,Type=String,Description="Genotype; missing where an allele is not a called single base">',
                 '##FORMAT=<ID=VAFS,Number=.,Type=Integer,Description="Each allele\'s varScoreVAF, in allele order">',
                 '##FORMAT=<ID=EAFS,Number=.,Type=Integer,Description="Each allele\'s varScoreEAF, in allele order">',
                 '##FORMAT=<ID=CGFT,Number=.,Type=String,Description="Each allele\'s varFilter, in allele order">',
                 '##FORMAT=<ID=VAFMIN,Number=1,Type=Integer,Description="The lower of the alleles\' varScoreVAF">',
                 '##FORMAT=<ID=EAFMIN,Number=1,Type=Integer,Description="The lower of the alleles\' varScoreEAF">',
                 "#CHROM\tPOS\tID\tREF\tALT\tQUAL\tFILTER\tINFO\tFORMAT\t" + args.sample_id]
        with open(args.out, "wb") as out:
            gzip_proc = subprocess.Popen(["bgzip", "-c"], stdin=subprocess.PIPE, stdout=out)
            gzip_proc.stdin.write(("\n".join(head) + "\n").encode())
            with open(body.name, "rb") as fh:
                for block in iter(lambda: fh.read(1 << 20), b""):
                    gzip_proc.stdin.write(block)
            gzip_proc.stdin.close()
            if gzip_proc.wait():
                raise SystemExit("cg_to_vcf.py: bgzip failed")
    finally:
        os.unlink(body.name)
    subprocess.run(["bcftools", "index", "-f", "-t", args.out], check=True)

    account = {
        "tool": "cg_to_vcf.py",
        "generated_utc": lib_run.utcnow(),
        "input": args.input,
        "input_sha256": lib_run.sha256_file(args.input),
        "out": args.out,
        "cg_header": dict(header),
        "loci_read": counts["loci"],
        "snv_records_written": counts["snv_records_written"],
        "snv_records_with_a_called_genotype": counts["gt_called"],
        "snv_records_with_genotype_missing": counts["gt_missing"],
        "filter_values_written": dict(counts["filters"]),
        "variant_loci_not_snv_dropped": dict(counts["variant_loci_not_snv_dropped"]),
        "not_written": "reference and no-call stretches (absent positions are unknown, not "
                       "reference), and variants that are not single-base SNVs",
    }
    lib_run.write_json(args.out[:-len(".vcf.gz")] + ".json", account)
    print("wrote %s: %d SNV records from %d loci" % (args.out, counts["snv_records_written"], counts["loci"]))
    if args.run:
        lib_run.ledger_append(args.run, {"kind": "artifact", "stage": "door", "tool": "cg_to_vcf.py",
                                         "snv_records_written": counts["snv_records_written"],
                                         "artifact": args.out})


if __name__ == "__main__":
    main()
