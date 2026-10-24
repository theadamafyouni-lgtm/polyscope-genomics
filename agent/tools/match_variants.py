#!/usr/bin/env python3
"""match_variants.py -- match model positions to the person's dosages, recording every
strand decision (SCO-4).

Mechanical. It compares alleles and records what it found. The one thing it cannot settle from
alleles alone -- a palindromic (A/T, C/G) marker, where the two alleles are reverse complements
of each other so the orientation is genuinely ambiguous -- is NOT guessed here. The agent names a
policy on the command line and it is recorded; there is no default, because a silent flip erases
provenance, which is exactly what INTAKE-2 and SCO-4 forbid.

Emits three things:
  * dosages.tsv          -- chrom, pos, r2, source(typed|imputed), for weight_coverage measure
  * plink_score_input.tsv -- ID, effect allele, weight, for the RESOLVED positions only
  * matching_report.json  -- every decision class, counted

On the score input: it carries resolved positions only. prd §2's residual-missing rule means the
scorer never faces a missing genotype -- not because missing genotypes are filled, but because
they are not in the set it is handed. Unresolved weight is not silently dropped either: it flows
into weight_coverage's fraction and is surfaced by the report as a transparency control (REP-1).

Usage:
  python3 tools/match_variants.py --run <RUN_ID> \
    --scorefile runs/<RUN_ID>/select/models/PGS000001_hmPOS_GRCh37.txt.gz \
    --chr-col hm_chr --pos-col hm_pos --effect-allele-col effect_allele \
    --other-allele-col hm_inferOtherAllele --effect-weight-col effect_weight \
    --imputed-dir runs/<RUN_ID>/score/imputed --typed-vcf runs/<RUN_ID>/door/normalized.vcf.gz \
    --palindromic-policy exclude --r2-bar 0.80 --out-dir runs/<RUN_ID>/score
"""
import argparse
import collections
import glob
import gzip
import os
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import lib_run  # noqa: E402

COMP = {"A": "T", "T": "A", "C": "G", "G": "C"}
PALINDROMES = ({"A", "T"}, {"C", "G"})


def revcomp(a):
    return "".join(COMP.get(b, "N") for b in reversed(a))


def open_maybe_gz(p):
    return gzip.open(p, "rt", errors="replace") if p.endswith(".gz") else open(p, errors="replace")


def model_positions(path, chr_col, pos_col):
    """(chrom, pos) of every parseable model row: the only positions this tool ever looks up."""
    keys = set()
    with open_maybe_gz(path) as fh:
        cols = None
        for line in fh:
            if line.startswith("#"):
                continue
            f = line.rstrip("\n").split("\t")
            if cols is None:
                cols = {n: i for i, n in enumerate(f)}
                for need in (chr_col, pos_col):
                    if need not in cols:
                        raise SystemExit(f"column {need!r} absent. Present: {sorted(cols)}")
                continue
            try:
                keys.add((f[cols[chr_col]].strip(), int(f[cols[pos_col]].strip())))
            except (ValueError, IndexError):
                continue
    return keys


def load_imputed(imputed_dir, wanted, targets_path):
    """chrom,pos -> (ref, alt, dr2, imputed_flag) for the model's positions only. Beagle puts DR2
    and the IMP flag in INFO.

    Only the model's positions are kept: Beagle writes every panel marker, ~78M for the genome,
    and holding all of them in one dict needs ~22 GB -- more than the box has. bcftools does the
    filtering (-T, streamed), so the records kept, their order, and which record wins when a
    position appears twice are all exactly what the unfiltered read produced."""
    table = {}
    files = sorted(glob.glob(os.path.join(imputed_dir, "*.vcf.gz")))
    if not files:
        raise SystemExit(f"match_variants.py: no imputed VCFs in {imputed_dir}")
    if not wanted:
        # No model row has a usable coordinate. bcftools refuses an empty targets file, and
        # the honest result is every row unresolved, which main() produces from an empty table.
        return table, files
    with open(targets_path, "w") as fh:
        for c, p in sorted(wanted, key=lambda k: (k[0], k[1])):
            fh.write(f"{c}\t{p}\n")
    for f in files:
        cmd = ["bcftools", "query", "-T", targets_path,
               "-f", "%CHROM\t%POS\t%REF\t%ALT\t%INFO/DR2\t%INFO/IMP\n", f]
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        for line in proc.stdout:
            c, p, ref, alt, dr2, imp = line.rstrip("\n").split("\t")
            # A multiallelic panel site carries one DR2 per ALT. Rather than silently picking one
            # -- which would attach a quality figure to the wrong allele -- such sites are marked
            # and left for the matcher to treat as unresolvable.
            multiallelic = ("," in alt) or ("," in dr2)
            if multiallelic:
                dr2v = None
            else:
                try:
                    dr2v = float(dr2)
                except ValueError:
                    dr2v = None
            key = (c, int(p))
            if key in table:
                # Two records at one position (a multiallelic site the panel carries as split
                # biallelic records, e.g. G>A and G>T). plink2 drops both under --rm-dup
                # exclude-all, so passing either one on would make score_plink2 use fewer
                # variants than it was handed -- an internal-consistency STOP. It is the same
                # situation as a multiallelic record, and is treated the same way.
                ref0 = table[key][0]
                table[key] = (ref0, alt, None, imp.strip() == "1", True)
                continue
            table[key] = (ref, alt, dr2v, imp.strip() == "1", multiallelic)
        err = proc.stderr.read()
        if proc.wait() != 0:
            raise SystemExit(f"match_variants.py: bcftools query failed on {f}\n{err[-2000:]}")
    return table, files


def load_typed(vcf):
    """Positions where the person's own file carries a CALLED genotype. An array no-call is kept
    in the normalized VCF as a missing genotype; its presence is not a measurement, so it must
    not earn the typed route's r2 = 1.0. Beagle fills such a site like any other, and it is
    judged on its DR2 like any other imputed value."""
    proc = subprocess.run(["bcftools", "query", "-i", 'GT!="mis"', "-f", "%CHROM\t%POS\n", vcf],
                          capture_output=True, text=True)
    if proc.returncode != 0:
        raise SystemExit(f"match_variants.py: bcftools query failed on {vcf}\n{proc.stderr[-2000:]}")
    return {(c, int(p)) for c, p in (l.split("\t") for l in proc.stdout.splitlines())}


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--run", required=True)
    ap.add_argument("--scorefile", required=True)
    ap.add_argument("--chr-col", required=True)
    ap.add_argument("--pos-col", required=True)
    ap.add_argument("--effect-allele-col", required=True)
    ap.add_argument("--other-allele-col")
    ap.add_argument("--effect-weight-col", required=True)
    ap.add_argument("--imputed-dir", required=True)
    ap.add_argument("--typed-vcf", required=True)
    ap.add_argument("--palindromic-policy", required=True,
                    choices=["exclude", "keep-as-plus-strand"],
                    help="no default on purpose: SCO-4 forbids an unrecorded flip")
    ap.add_argument("--r2-bar", type=float, required=True)
    ap.add_argument("--out-dir", required=True)
    args = ap.parse_args()

    os.makedirs(args.out_dir, exist_ok=True)
    wanted = model_positions(args.scorefile, args.chr_col, args.pos_col)
    imputed, imputed_files = load_imputed(args.imputed_dir, wanted,
                                          os.path.join(args.out_dir, "model_positions.tsv"))
    typed = load_typed(args.typed_vcf)

    counts = collections.Counter()
    flips = []
    dosage_rows = []
    score_rows = []

    with open_maybe_gz(args.scorefile) as fh:
        cols = None
        for line in fh:
            if line.startswith("#"):
                continue
            f = line.rstrip("\n").split("\t")
            if cols is None:
                cols = {n: i for i, n in enumerate(f)}
                for need in (args.chr_col, args.pos_col, args.effect_allele_col,
                             args.effect_weight_col):
                    if need not in cols:
                        raise SystemExit(f"column {need!r} absent. Present: {sorted(cols)}")
                continue
            counts["model_rows"] += 1
            try:
                c = f[cols[args.chr_col]].strip()
                p = int(f[cols[args.pos_col]].strip())
                ea = f[cols[args.effect_allele_col]].strip().upper()
                w = float(f[cols[args.effect_weight_col]])
            except (ValueError, IndexError, KeyError):
                counts["skipped_unparseable_row"] += 1
                continue
            oa = ""
            if args.other_allele_col and args.other_allele_col in cols:
                try:
                    oa = f[cols[args.other_allele_col]].strip().upper()
                except IndexError:
                    oa = ""

            hit = imputed.get((c, p))
            if hit is None:
                counts["absent_from_dosage_set"] += 1
                continue
            ref, alt, dr2, imp_flag, multiallelic = hit
            if multiallelic:
                counts["multiallelic_panel_site"] += 1
                dosage_rows.append((c, p, "", "imputed", "multiallelic_unresolvable"))
                continue
            source = "typed" if (c, p) in typed and not imp_flag else "imputed"
            r2 = 1.0 if source == "typed" else dr2

            vcf_alleles = {ref.upper(), alt.upper()}
            model_alleles = {a for a in (ea, oa) if a}
            palindromic = vcf_alleles in PALINDROMES

            if palindromic:
                counts["palindromic_marker"] += 1
                if args.palindromic_policy == "exclude":
                    counts["palindromic_excluded"] += 1
                    dosage_rows.append((c, p, r2, source, "excluded_palindromic"))
                    continue
                counts["palindromic_kept_as_plus_strand"] += 1
                decision = "kept-as-plus-strand (ambiguous, policy-driven)"
                effect_in_vcf = ea
            elif ea in vcf_alleles and (not oa or oa in vcf_alleles):
                counts["match_same_strand"] += 1
                decision = "no-flip"
                effect_in_vcf = ea
            elif revcomp(ea) in vcf_alleles and (not oa or revcomp(oa) in vcf_alleles):
                counts["match_strand_flip"] += 1
                decision = "flip (reverse complement)"
                effect_in_vcf = revcomp(ea)
                flips.append({"chrom": c, "pos": p, "model_effect_allele": ea,
                              "vcf_ref": ref, "vcf_alt": alt, "used_allele": effect_in_vcf})
            else:
                counts["allele_mismatch_unresolvable"] += 1
                dosage_rows.append((c, p, r2, source, "allele_mismatch"))
                continue

            resolved = (source == "typed") or (r2 is not None and r2 >= args.r2_bar)
            dosage_rows.append((c, p, r2, source,
                                "resolved" if resolved else "below_r2_bar"))
            if resolved:
                counts["resolved_into_score_input"] += 1
                score_rows.append((f"{c}:{p}", effect_in_vcf, w, decision))
            else:
                counts["unresolved_below_r2_bar"] += 1

    dpath = os.path.join(args.out_dir, "dosages.tsv")
    with open(dpath, "w") as fh:
        fh.write("chrom\tpos\tr2\tsource\tstatus\n")
        for c, p, r2, src, st in dosage_rows:
            fh.write(f"{c}\t{p}\t{'' if r2 is None else r2}\t{src}\t{st}\n")

    spath = os.path.join(args.out_dir, "plink_score_input.tsv")
    with open(spath, "w") as fh:
        fh.write("ID\tEA\tBETA\n")
        for vid, ea, w, _d in score_rows:
            fh.write(f"{vid}\t{ea}\t{w}\n")

    report = {
        "tool": "match_variants.py",
        "generated_utc": lib_run.utcnow(),
        "scorefile": args.scorefile,
        "columns_used": {"chrom": args.chr_col, "pos": args.pos_col,
                         "effect_allele": args.effect_allele_col,
                         "other_allele": args.other_allele_col,
                         "effect_weight": args.effect_weight_col},
        "imputed_files": imputed_files,
        "r2_bar": args.r2_bar,
        "palindromic_policy": args.palindromic_policy,
        "strand_policy_note": "SCO-4: strand is resolved HERE, at matching, not at normalization, "
                              "and every flip is recorded below.",
        "counts": dict(counts),
        "n_strand_flips_recorded": len(flips),
        "strand_flips_sample": flips[:50],
        "outputs": {"dosages_tsv": dpath, "plink_score_input_tsv": spath},
        "score_input_contains": "resolved positions only -- so the scorer never faces a missing "
                                "genotype (prd §2's residual-missing rule). Unresolved weight is "
                                "accounted for by weight_coverage, not silently discarded.",
    }
    rpath = os.path.join(args.out_dir, "matching_report.json")
    lib_run.write_json(rpath, report)
    lib_run.ledger_append(args.run, {
        "kind": "facts", "stage": "score", "tool": "match_variants.py",
        "resolved_into_score_input": counts.get("resolved_into_score_input", 0),
        "strand_flips": len(flips), "artifact": rpath,
    })
    print(f"matching report -> {rpath}")
    for k, v in sorted(counts.items()):
        print(f"  {k:38s} {v}")


if __name__ == "__main__":
    main()
