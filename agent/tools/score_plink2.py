#!/usr/bin/env python3
"""score_plink2.py -- the mechanical weighted sum (prd §2). Arithmetic, nothing else.

plink2 `--score` computes sum(dosage x beta) over the set it is handed. It makes no decision,
and the way it is invoked here makes sure it cannot make one by accident:

  * `no-mean-imputation` is set as a GUARD. Without it plink2 fills a missing genotype with 2x the
    allele frequency -- inserting a population average as this person's dosage (collision 2).
  * But `no-mean-imputation` is only half the rule, and the other half is the trap: it does not
    make plink skip the variant, it scores the missing genotype as ZERO dosage. Zero dosage =
    homozygous reference = absence treated as reference, which is the exact JC-7 error the whole
    coverage architecture exists to prevent.
  * So the real guarantee is architectural: the score input holds RESOLVED positions only, so
    there are no missing genotypes left by the time plink runs. This tool verifies that -- if
    plink used fewer variants than it was given, it reports the shortfall as an internal
    inconsistency for the agent to treat as a `stop` (prd §2), and does not quietly proceed.

Scores per chromosome and sums. A weighted sum is linear, so per-chromosome sums add exactly.

Usage:
  python3 tools/score_plink2.py --run <RUN_ID> \
      --score-input runs/<RUN_ID>/score/plink_score_input.tsv \
      --imputed-dir runs/<RUN_ID>/score/imputed \
      --out-dir runs/<RUN_ID>/score
"""
import argparse
import csv
import glob
import gzip
import os
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import lib_run  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PLINK2 = os.path.join(ROOT, "refdata", "bin", "plink2")


def vcf_has_ds(vcf):
    """True if the target VCF declares FORMAT/DS in its header.

    plink2 refuses dosage=GP when FORMAT/DS is present, and Beagle always emits DS, so DS is the
    right default whenever it exists (pgsc_calc gates dosage=DS on metadata the same way in
    modules/local/plink2_vcf.nf). Detected, not assumed."""
    opener = gzip.open if vcf.endswith(".gz") else open
    with opener(vcf, "rt") as fh:
        for line in fh:
            if line.startswith("#CHROM"):
                return False
            if line.startswith("##FORMAT=<ID=DS,"):
                return True
    return False


def read_sscore(path):
    with open(path) as fh:
        rd = csv.DictReader(fh, delimiter="\t")
        for row in rd:
            return row
    return None


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--run", required=True)
    ap.add_argument("--score-input", required=True)
    ap.add_argument("--imputed-dir", required=True)
    ap.add_argument("--dosage-field", default=None, choices=["GP", "DS", "HDS"],
                    help="override the dosage field. Default: auto-detect per target VCF -- DS when "
                         "FORMAT/DS is present in its header (Beagle always emits it; plink2 refuses "
                         "GP when DS exists), else GP.")
    ap.add_argument("--out-dir", required=True)
    args = ap.parse_args()

    os.makedirs(args.out_dir, exist_ok=True)
    work = os.path.join(args.out_dir, "plink")
    os.makedirs(work, exist_ok=True)

    n_input = sum(1 for _ in open(args.score_input)) - 1
    # Hashed here, before plink reads it, so the record says which bytes this raw score came
    # from. The reference cohort is scored from "the same file" later, and a later hash of
    # the path proves nothing about what was scored now.
    score_input_sha256 = lib_run.sha256_file(args.score_input)
    files = sorted(glob.glob(os.path.join(args.imputed_dir, "*.vcf.gz")))
    if not files:
        raise SystemExit(f"score_plink2.py: no imputed VCFs in {args.imputed_dir}")

    per_chrom, total_sum, total_used = [], 0.0, 0
    for vcf in files:
        # Fix 2: pick the dosage field per target VCF. Explicit --dosage-field overrides; otherwise
        # DS when the header declares FORMAT/DS (plink2 rejects GP when DS is present), else GP.
        dosage = args.dosage_field or ("DS" if vcf_has_ds(vcf) else "GP")
        stem = os.path.join(work, os.path.basename(vcf).replace(".vcf.gz", ""))
        # Fix 3: exclude multiallelic variants -- Beagle emits ~30k multiallelic dosage records per
        # chromosome from the 1kg panel, and plink2 dies parsing a multiallelic dosage. The
        # exclusion must happen AT IMPORT (--import-max-alleles 2): the post-import --max-alleles
        # filter runs too late, since plink2 parses the dosage (and errors) during --vcf import
        # before any post-import variant filter applies. Verified on a synthetic multiallelic-DS VCF.
        cmd = [PLINK2, "--vcf", vcf, f"dosage={dosage}",
               "--set-all-var-ids", "@:#", "--rm-dup", "exclude-all", "--import-max-alleles", "2",
               "--score", args.score_input, "1", "2", "3", "header",
               "cols=+scoresums,+denom", "no-mean-imputation",
               "--out", stem]
        proc = subprocess.run(cmd, capture_output=True, text=True)
        with open(stem + ".cmd.log", "w") as fh:
            fh.write(" ".join(cmd) + "\n\n" + proc.stdout + "\n" + proc.stderr)
        sscore = stem + ".sscore"
        if proc.returncode != 0 or not os.path.exists(sscore):
            per_chrom.append({"vcf": vcf, "status": "no_variants_or_failed",
                              "dosage_field_used": dosage, "stderr_tail": proc.stderr[-600:]})
            continue
        row = read_sscore(sscore)
        used = int(row.get("ALLELE_CT", 0)) // 2 if row.get("ALLELE_CT") else None
        ssum = float(row.get("SCORE1_SUM", 0.0))
        total_sum += ssum
        if used:
            total_used += used
        per_chrom.append({"vcf": os.path.basename(vcf), "status": "ok",
                          "dosage_field_used": dosage,
                          "score_sum": ssum, "variants_used": used,
                          "named_score_alleles": row.get("NAMED_ALLELE_DOSAGE_SUM")})

    shortfall = n_input - total_used
    result = {
        "tool": "score_plink2.py",
        "generated_utc": lib_run.utcnow(),
        "plink2_version": lib_run.tool_version([PLINK2, "--version"]),
        "dosage_field": args.dosage_field or "auto (per VCF: DS if FORMAT/DS present, else GP)",
        "import_max_alleles": 2,
        "guard": "no-mean-imputation set; see the module docstring for why that is only half the rule",
        "score_input": args.score_input,
        "score_input_sha256": score_input_sha256,
        # Provenance, not computation: the artifact recorded WHICH VARIANT SET produced the score
        # but not WHICH DOSAGE SOURCE, so the imputed directory had to be supplied from outside to
        # check it. An artifact that does not name its own inputs cannot be checked against them.
        "imputed_dir": os.path.abspath(args.imputed_dir),
        "n_variants_in_score_input": n_input,
        "n_variants_used_by_plink": total_used,
        "shortfall": shortfall,
        "internal_consistency": (
            "OK -- plink used every variant it was handed"
            if shortfall == 0 else
            f"INCONSISTENT -- {shortfall} of {n_input} resolved variants were not used. Per prd §2 "
            f"a position still missing at score time is a STOP, not a fill of any kind. The agent "
            f"decides; this tool does not proceed as if the number were sound."),
        "per_chromosome": per_chrom,
        "raw_score": total_sum,
        "raw_score_meaning": "sum(dosage x beta) over the resolved set. It is NOT a risk and NOT a "
                             "percentile; placing it is interpret's job (INT-1..INT-3), against a "
                             "reference built for this model and this resolved set.",
    }
    out = os.path.join(args.out_dir, "raw_score.json")
    lib_run.emit(result, out)
    lib_run.ledger_append(args.run, {
        "kind": "artifact", "stage": "score", "tool": "score_plink2.py",
        "raw_score": total_sum, "shortfall": shortfall, "artifact": out,
    })


if __name__ == "__main__":
    main()
