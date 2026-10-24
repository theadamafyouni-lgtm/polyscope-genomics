#!/usr/bin/env python3
"""own_calls.py -- a whole genome's own calls at a model's positions, where the file says what
it read (SCO-5, JC-7).

Mechanical. For each of the model's autosomal positions it writes what the person's own file
called there, and nothing it did not:

  a variant call    the door's genotype at that position, from its normalized VCF; a call the
                    quality policy set missing stays missing (call_failed)
  a reference call  only inside the Complete Genomics var file's reference loci, where every
                    allele was called reference: written 0/0, REF the genome's base, ALT the
                    model's other allele on the genome's strand, so match_variants.py places
                    the effect allele exactly as it does for an imputed record
  anything else     not called: a no-call, a variant the VCF does not hold as one SNV, or no
                    locus at all. Absent is never reference.

It writes calls.vcf.gz (GT and DS, indexed), which match_variants.py reads as --imputed-dir and
--typed-vcf alike, and score_plink2.py as --imputed-dir: every record a typed call, r2 1. And
own_calls.json: how many positions fall in each class, the share of the model's autosomal |beta|
the file called, and the largest positions it did not. X, Y and MT are left out, as coverage
leaves them out. Whether these calls are enough to score without imputing is the agent's to
decide (score skill, section 2): this tool counts.

Usage:
  python3 tools/own_calls.py --run <RUN_ID> \\
      --scorefile runs/<RUN_ID>/select/models/<PGS>_hmPOS_GRCh37.txt.gz \\
      --chr-col hm_chr --pos-col hm_pos --effect-allele-col effect_allele \\
      --other-allele-col <as the header names it> --effect-weight-col <as named> \\
      --calls runs/<RUN_ID>/door/normalized.vcf.gz --cg-var <the person's var file> \\
      --fasta refdata/fasta/GRCh37.fa.gz --out-dir runs/<RUN_ID>/score/own_calls
"""
import argparse
import bisect
import bz2
import collections
import gzip
import io
import os
import subprocess
import sys
import zipfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import lib_run  # noqa: E402

AUTOSOMES = [str(c) for c in range(1, 23)]
LEFT_OUT = {"X": "X", "23": "X", "Y": "Y", "24": "Y", "XY": "XY", "25": "XY", "MT": "MT", "M": "MT", "26": "MT"}
COMP = {"A": "T", "T": "A", "C": "G", "G": "C"}
# A base for a record's ALT where the model names no other allele: never the REF's complement, so
# the record is not read as a palindrome. The person is 0/0 there; the ALT is never carried.
PLACEHOLDER = {"A": "C", "C": "A", "G": "A", "T": "C"}


def contig(name):
    name = name.strip()
    return name[3:] if name.lower().startswith("chr") else name


def open_text(path):
    with open(path, "rb") as fh:
        magic = fh.read(4)
    if magic.startswith(b"BZh"):
        return io.TextIOWrapper(bz2.open(path, "rb"), encoding="utf-8", errors="replace")
    if magic.startswith(b"\x1f\x8b"):
        return io.TextIOWrapper(gzip.open(path, "rb"), encoding="utf-8", errors="replace")
    if magic.startswith(b"PK"):
        archive = zipfile.ZipFile(path)
        return io.TextIOWrapper(archive.open(archive.namelist()[0]), encoding="utf-8", errors="replace")
    return open(path, encoding="utf-8", errors="replace")


def model_rows(path, args):
    """(contig, pos, effect allele, other allele, |weight|) for every parseable row."""
    rows = []
    with open_text(path) as fh:
        cols = None
        for line in fh:
            if line.startswith("#"):
                continue
            f = line.rstrip("\n").split("\t")
            if cols is None:
                cols = {n: i for i, n in enumerate(f)}
                for need in (args.chr_col, args.pos_col, args.effect_allele_col, args.effect_weight_col):
                    if need not in cols:
                        raise SystemExit("own_calls.py: column %r absent. Present: %s" % (need, sorted(cols)))
                continue
            try:
                c = contig(f[cols[args.chr_col]])
                p = int(f[cols[args.pos_col]])
                ea = f[cols[args.effect_allele_col]].strip().upper()
                w = abs(float(f[cols[args.effect_weight_col]]))
            except (ValueError, IndexError):
                continue
            oa = ""
            if args.other_allele_col and args.other_allele_col in cols and cols[args.other_allele_col] < len(f):
                oa = f[cols[args.other_allele_col]].strip().upper()
            rows.append((c, p, ea, oa, w))
    return rows


def reference_loci(path):
    """Per contig, the sorted (begin, end) of every locus whose alleles were all called
    reference, 0-based half-open as the var file writes them."""
    loci, current, rows = collections.defaultdict(lambda: ([], [])), None, []

    def flush():
        if rows and all(r[3] == "ref" for r in rows):
            c, b, e = rows[0][0], min(r[1] for r in rows), max(r[2] for r in rows)
            begins, ends = loci[c]
            begins.append(b)
            ends.append(e)

    with open_text(path) as fh:
        columns = None
        for line in fh:
            if line.startswith("#") or not line.strip():
                continue
            f = line.rstrip("\n").split("\t")
            if columns is None:
                if not f[0].startswith(">"):
                    raise SystemExit("own_calls.py: %s has no '>locus' column header: not a var file" % path)
                columns = {n.lstrip(">"): i for i, n in enumerate(f)}
                for need in ("locus", "chromosome", "begin", "end", "varType"):
                    if need not in columns:
                        raise SystemExit("own_calls.py: the var file has no %r column" % need)
                continue
            locus = f[columns["locus"]]
            if locus != current:
                flush()
                current, rows = locus, []
            rows.append((contig(f[columns["chromosome"]]), int(f[columns["begin"]]), int(f[columns["end"]]),
                         f[columns["varType"]]))
        flush()
    return {c: (b, e) for c, (b, e) in loci.items()}


def inside(loci, c, pos):
    """True when 1-based `pos` lies in one of contig c's reference loci."""
    begins, ends = loci.get(c, ((), ()))
    i = bisect.bisect_right(begins, pos - 1) - 1
    return i >= 0 and begins[i] <= pos - 1 < ends[i]


def variant_calls(vcf, targets):
    """(contig, pos) -> (REF, ALT, GT) where the VCF holds one record there; None where it
    holds more than one."""
    calls = {}
    proc = subprocess.Popen(["bcftools", "query", "-T", targets, "-f", "%CHROM\t%POS\t%REF\t%ALT\t[%GT]\n", vcf],
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    for line in proc.stdout:
        c, p, ref, alt, gt = line.rstrip("\n").split("\t")[:5]
        key = (contig(c), int(p))
        calls[key] = None if key in calls else (ref.upper(), alt.upper(), gt)
    err = proc.stderr.read()
    if proc.wait() != 0:
        raise SystemExit("own_calls.py: bcftools query failed on %s\n%s" % (vcf, err[-2000:]))
    return calls


def genome_bases(fasta, wanted):
    """(contig, pos) -> the genome's base, read once through the FASTA for the contigs wanted."""
    if not wanted:
        return {}
    need = collections.defaultdict(list)
    for c, p in wanted:
        need[c].append(p)
    bases, name, chunks = {}, None, []

    def take():
        if name in need:
            seq = "".join(chunks)
            for p in need[name]:
                bases[(name, p)] = seq[p - 1].upper() if 0 < p <= len(seq) else "N"

    with open_text(fasta) as fh:
        for line in fh:
            if line.startswith(">"):
                take()
                name, chunks = contig(line[1:].split()[0]), []
                if len(bases) == len(wanted):
                    break
            elif name in need:
                chunks.append(line.strip())
        take()
    return bases


def reference_alt(ref, ea, oa):
    """The ALT for a reference call: the model's other allele on the genome's strand, or a
    placeholder where the model names none; None where the model's alleles do not fit the base."""
    alleles = [a for a in (ea, oa) if a]
    if ref in alleles:
        other = [a for a in alleles if a != ref]
        return other[0] if other else PLACEHOLDER.get(ref)
    if COMP.get(ref) in alleles:
        other = [COMP.get(a) for a in alleles if a != COMP.get(ref)]
        return other[0] if other and other[0] else PLACEHOLDER.get(ref)
    if len(alleles) == 1 and len(ea) == 1 and ea in COMP:
        return ea  # the model names only its effect allele, and the person carries the other
    return None


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--run")
    ap.add_argument("--scorefile", required=True)
    ap.add_argument("--chr-col", required=True)
    ap.add_argument("--pos-col", required=True)
    ap.add_argument("--effect-allele-col", required=True)
    ap.add_argument("--other-allele-col")
    ap.add_argument("--effect-weight-col", required=True)
    ap.add_argument("--calls", required=True, help="the door's normalized VCF: the variant calls")
    ap.add_argument("--cg-var", required=True, help="the Complete Genomics var file: where it called reference")
    ap.add_argument("--fasta", required=True, help="the genome the model's positions are on (GRCh37)")
    ap.add_argument("--out-dir", required=True)
    args = ap.parse_args()
    os.makedirs(args.out_dir, exist_ok=True)

    rows = model_rows(args.scorefile, args)
    left_out = collections.Counter(LEFT_OUT[c] for c, *_ in rows if c in LEFT_OUT)
    rows = [r for r in rows if r[0] in AUTOSOMES]
    targets = os.path.join(args.out_dir, "model_positions.tsv")
    with open(targets, "w") as fh:
        for c, p in sorted({(c, p) for c, p, *_ in rows}, key=lambda k: (int(k[0]), k[1])):
            fh.write("%s\t%d\n" % (c, p))
    calls = variant_calls(args.calls, targets) if rows else {}
    loci = reference_loci(args.cg_var)
    referenced = {(c, p) for c, p, *_ in rows if (c, p) not in calls and inside(loci, c, p)}
    bases = genome_bases(args.fasta, referenced)

    counts = collections.Counter()
    records, called_w, total_w, uncalled = {}, 0.0, 0.0, collections.Counter()
    for c, p, ea, oa, w in rows:
        total_w += w
        key = (c, p)
        status = None
        if key in calls:
            call = calls[key]
            if call is None or "," in call[1]:
                status = "not_called"
            elif "." in call[2]:
                status = "call_failed"
            else:
                alleles = call[2].replace("|", "/").split("/")
                if len(alleles) != 2 or not all(a in ("0", "1") for a in alleles):
                    status = "not_called"
                else:
                    records.setdefault(key, (call[0], call[1], "/".join(sorted(alleles)), alleles.count("1")))
                    status = "called_variant"
        elif key in referenced:
            ref = bases.get(key, "N")
            alt = reference_alt(ref, ea, oa) if ref in COMP else None
            if alt is None:
                status = "alleles_unplaceable"
            else:
                records.setdefault(key, (ref, alt, "0/0", 0))
                status = "called_reference"
        else:
            status = "not_called"
        counts[status] += 1
        if status in ("called_variant", "called_reference"):
            called_w += w
        else:
            uncalled[key] += w

    vcf_text = os.path.join(args.out_dir, "calls.vcf")
    contigs = sorted({c for c, _ in records}, key=int)
    with open(vcf_text, "w") as fh:
        fh.write("##fileformat=VCFv4.2\n##source=own_calls.py\n")
        for c in contigs:
            fh.write("##contig=<ID=%s>\n" % c)
        # Declared for match_variants.py, which reads both from every record it is given; never
        # set, since nothing here is imputed: each record is the person's own call.
        fh.write('##INFO=<ID=DR2,Number=A,Type=Float,Description="Never set: no record here is imputed">\n'
                 '##INFO=<ID=IMP,Number=0,Type=Flag,Description="Never set: no record here is imputed">\n')
        fh.write('##FORMAT=<ID=GT,Number=1,Type=String,Description="The person\'s own call">\n'
                 '##FORMAT=<ID=DS,Number=A,Type=Float,Description="ALT alleles called: 0, 1 or 2">\n'
                 "#CHROM\tPOS\tID\tREF\tALT\tQUAL\tFILTER\tINFO\tFORMAT\tPERSON\n")
        for (c, p) in sorted(records, key=lambda k: (int(k[0]), k[1])):
            ref, alt, gt, ds = records[(c, p)]
            fh.write("%s\t%d\t%s:%d\t%s\t%s\t.\tPASS\t.\tGT:DS\t%s:%d\n" % (c, p, c, p, ref, alt, gt, ds))
    out_vcf = os.path.join(args.out_dir, "calls.vcf.gz")
    for cmd in (["bcftools", "view", "-Oz", "-o", out_vcf, vcf_text], ["bcftools", "index", "-f", "-t", out_vcf]):
        done = subprocess.run(cmd, capture_output=True, text=True)
        if done.returncode:
            raise SystemExit("own_calls.py: %s failed\n%s" % (cmd[1], done.stderr[-2000:]))
    os.remove(vcf_text)

    largest = sorted(uncalled.items(), key=lambda kv: -kv[1])[:10]
    facts = {
        "tool": "own_calls.py",
        "generated_utc": lib_run.utcnow(),
        "scorefile": args.scorefile, "calls": args.calls, "cg_var": args.cg_var, "fasta": args.fasta,
        "model_rows_autosomal": len(rows),
        "left_out_of_coverage": dict(left_out),
        "positions": {k: counts.get(k, 0) for k in ("called_variant", "called_reference", "call_failed",
                                                     "not_called", "alleles_unplaceable")},
        "abs_beta": {"total_autosomal": total_w, "called": called_w,
                     "fraction_called": called_w / total_w if total_w else None},
        "largest_not_called": [{"chrom": c, "pos": p, "abs_beta": w, "share": w / total_w if total_w else None}
                               for (c, p), w in largest],
        "reference_calls_from": "the var file's loci whose every allele was called reference",
        "records": out_vcf,
        "fraction_note": "Called |beta| over the model's autosomal |beta|, before strand and palindrome "
                         "matching: an upper bound on what match_variants.py and weight_coverage.py "
                         "measure from calls.vcf.gz.",
        "does_not_decide": "whether these calls are enough to score without imputing: the agent's, "
                           "against the coverage gate (score skill, section 2)",
    }
    lib_run.emit(facts, os.path.join(args.out_dir, "own_calls.json"))
    if args.run:
        lib_run.ledger_append(args.run, {"kind": "facts", "stage": "score", "tool": "own_calls.py",
                                         "fraction_called": facts["abs_beta"]["fraction_called"],
                                         "artifact": os.path.join(args.out_dir, "own_calls.json")})


if __name__ == "__main__":
    main()
