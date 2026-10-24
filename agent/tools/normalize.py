#!/usr/bin/env python3
"""normalize.py -- convert the patient's file to the one standard format, carrying provenance
(INTAKE-2), then restore ALT (INTAKE-3).

Mechanical. Every policy that could go more than one way is a REQUIRED argument with no
default, so the agent chooses it and the choice lands in provenance rather than in this file:
duplicate positions, haploid rows, and ALT restoration each have to be named on the command line.

What it does NOT do: resolve strand. Palindromic (A/T, C/G) ambiguity survives into the
normalized file on purpose and is settled at matching time (SCO-4). A silent flip here would
erase provenance.

The standard format for this slice: bgzipped VCF on the patient's own build, plus a
provenance.json sidecar. Stages read the sidecar, never the file extension.

Usage:
  python3 tools/normalize.py --run <RUN_ID> \
    --input sample/patient.array-23andme.zip --member patient_genotypes.txt \
    --build GRCh37 --fasta refdata/fasta/GRCh37.fa.gz \
    --sites refdata/panel/1kg.phase3.v5c.sites.b37.vcf.gz \
    --sample-id PATIENT --classification runs/<RUN_ID>/door/classification.json \
    --duplicate-policy keep-first --haploid-policy exclude --alt-restore-policy biallelic-only \
    --header-sample-repair none --info-policy keep \
    --out-dir runs/<RUN_ID>/door
"""
import argparse
import collections
import gzip
import io
import os
import re
import shutil
import subprocess
import sys
import tempfile
import zipfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import lib_run  # noqa: E402

DIPLOID_RE = re.compile(r"^[ACGT]{2}$")
HAPLOID_RE = re.compile(r"^[ACGT]$")


def sh(cmd, **kw):
    proc = subprocess.run(cmd, capture_output=True, text=True, **kw)
    if proc.returncode != 0:
        raise SystemExit(f"normalize.py: command failed: {' '.join(cmd)}\n{proc.stderr[-4000:]}")
    return proc


def sniff_container(path):
    """Name the container from the file's first bytes, never from its extension.

    The fixture audit found filenames that lie in both directions: a `.txt` that was a
    zip, a vendor `.csv` that was gzip. Extension is a claim; magic bytes are evidence.
    This is the same principle route_assay applies one level up.
    """
    with open(path, "rb") as fh:
        head = fh.read(4)
    if head[:2] == b"PK":          # PK\x03\x04, and the empty/spanned variants
        return "zip"
    if head[:2] == b"\x1f\x8b":
        return "gzip"
    return "plain"


def open_array_text(archive, member, dest):
    """Materialise the vendor TSV at `dest`, whatever it arrived wrapped in.

    Returns (container, member). `member` is the name inside the archive when there is
    one and None otherwise, and both land in provenance: which container the bytes came
    out of is part of how the file was read, the same way R8a treats which converter ran.

    A zip holding more than one file still requires --member. That is a real ambiguity
    and guessing at it would be the kind of silent choice this tool exists to avoid.
    """
    container = sniff_container(archive)

    if container == "zip":
        with zipfile.ZipFile(archive) as zf:
            names = [i.filename for i in zf.infolist() if not i.is_dir()]
            member = member or (names[0] if len(names) == 1 else None)
            if member is None:
                raise SystemExit(f"pass --member (one of {names})")
            with zf.open(member) as src, open(dest, "wb") as out:
                shutil.copyfileobj(src, out)
        return container, member

    if member:
        raise SystemExit(
            f"normalize.py: --member was given but {archive} is a {container} file, "
            f"which holds no members. Drop --member, or check the input path.")

    if container == "gzip":
        with gzip.open(archive, "rb") as src, open(dest, "wb") as out:
            shutil.copyfileobj(src, out)
        return container, None

    shutil.copyfile(archive, dest)
    return container, None


def preprocess(src_path, out_path, dup_policy, haploid_policy):
    """CRLF->LF, apply the duplicate and haploid policies, and count everything dropped.

    Nothing is inferred here: rows the policies exclude are counted by category so the loss is
    visible in the report rather than absorbed."""
    counts = collections.Counter()
    seen = {}
    rows = []
    with open(src_path, "r", encoding="utf-8", errors="replace") as fh:
        for raw in fh:
            line = raw.rstrip("\n").rstrip("\r")
            if line.startswith("#") or not line:
                counts["header_or_blank"] += 1
                continue
            f = line.split("\t")
            # Two vendor layouts, distinguished by column count rather than by anything
            # the file claims about itself. 23andMe writes the genotype as one column
            # ("AA"); AncestryDNA splits it across two ("A", "A"). Nothing else differs.
            if len(f) == 4:
                rsid, chrom, pos, gt = f
            elif len(f) == 5:
                rsid, chrom, pos, a1, a2 = f
                # AncestryDNA's no-call is "0" per allele. Fold it onto the 23andMe token
                # so it lands in the same no_call_token count below: how a vendor spells
                # a no-call is not a difference worth carrying downstream.
                gt = "--" if (a1 == "0" or a2 == "0") else a1 + a2
            else:
                counts["dropped_wrong_column_count"] += 1
                continue

            # AncestryDNA ships a bare column-name row with no "#" prefix, so it survives
            # the comment check above and would be read as data.
            if rsid == "rsid" and chrom == "chromosome":
                counts["header_or_blank"] += 1
                continue
            counts["data_rows_in"] += 1
            key = (chrom, pos)

            if gt == "--":
                counts["no_call_token"] += 1
            elif DIPLOID_RE.match(gt):
                counts["diploid_acgt"] += 1
            elif HAPLOID_RE.match(gt):
                counts["haploid_acgt"] += 1
                if haploid_policy == "exclude":
                    counts["dropped_haploid"] += 1
                    continue
            elif set(gt) <= set("ID"):
                counts["indel_id_token"] += 1
                counts["dropped_indel_id_token"] += 1
                continue          # I/D carries no sequence; it cannot become a VCF allele
            else:
                counts["dropped_unrecognised_token"] += 1
                continue

            if key in seen:
                counts["duplicate_position_row"] += 1
                if dup_policy == "keep-first":
                    counts["dropped_duplicate"] += 1
                    continue
                if dup_policy == "drop-all":
                    seen[key] = None
                    counts["dropped_duplicate"] += 1
                    continue
            seen[key] = len(rows)
            rows.append((rsid, chrom, pos, gt))

    if dup_policy == "drop-all":
        keep = {i for i in seen.values() if i is not None}
        dropped_first = len(rows) - len(keep)
        rows = [r for i, r in enumerate(rows) if i in keep]
        counts["dropped_duplicate"] += dropped_first

    # bcftools --tsv2vcf needs the input in coordinate order per contig.
    def chrom_key(c):
        return (0, int(c)) if c.isdigit() else (1, c)
    rows.sort(key=lambda r: (chrom_key(r[1]), int(r[2])))

    with open(out_path, "w") as out:
        for rsid, chrom, pos, gt in rows:
            out.write(f"{rsid}\t{chrom}\t{pos}\t{gt}\n")
    counts["rows_written_for_conversion"] = len(rows)
    return counts


def repair_vcf_header(input_vcf, work, policy):
    """INTAKE-2: some vendor VCFs declare more sample columns in #CHROM than the data carry.

    Measured case: Veritas exports declare `unknown` and `Sample1` in #CHROM but write ten
    fields per data row, so bcftools stops at the first record with "Number of columns ...
    does not match the number of samples (1 vs 2)". The genotype sits in the FIRST sample
    column; the trailing name has nothing behind it.

    Truncating #CHROM to the observed column count is a HEADER-ONLY edit. No data row is
    touched and the surviving column keeps the name the header already gave it. The repair
    is still a judgment -- which column is the person -- so it is named on the command line
    and recorded, never inferred here.

    Returns (path_to_use, stats).
    """
    stats = {"policy": policy, "applied": False}
    if policy == "none":
        return input_vcf, stats

    opener = gzip.open if input_vcf.endswith(".gz") else open
    meta, chrom_line, first_data = [], None, None
    with opener(input_vcf, "rt", errors="replace") as fh:
        for line in fh:
            if line.startswith("##"):
                meta.append(line.rstrip("\n"))
            elif line.startswith("#CHROM"):
                chrom_line = line.rstrip("\n")
            elif line.strip():
                first_data = line.rstrip("\n")
                break

    if chrom_line is None or first_data is None:
        raise SystemExit(
            "normalize.py: could not read both a #CHROM line and a data row from "
            f"{input_vcf}; the column counts cannot be compared.")

    n_declared = len(chrom_line.split("\t"))
    n_data = len(first_data.split("\t"))
    stats.update({"declared_columns": n_declared, "first_data_row_columns": n_data})

    if n_declared == n_data:
        stats["why"] = "declared and observed column counts already agree; nothing to repair"
        return input_vcf, stats

    if n_declared < n_data:
        raise SystemExit(
            f"normalize.py: {input_vcf} declares {n_declared} columns but a data row carries "
            f"{n_data}. Truncation cannot fix that -- a genotype column would have no name. "
            "Not repaired.")

    fields = chrom_line.split("\t")
    stats.update({
        "applied": True,
        "dropped_trailing_sample_names": fields[n_data:],
        "kept_sample_names": fields[9:n_data],
        "what_changed": "the #CHROM line only; no data row was read, rewritten or reordered",
    })

    new_chrom = "\t".join(fields[:n_data])

    # Written for the record, not consumed: it is what the repaired header looks like.
    hdr = os.path.join(work, "repaired_header.txt")
    with open(hdr, "w") as fh:
        for line in meta:
            fh.write(line + "\n")
        fh.write(new_chrom + "\n")
    stats["repaired_header_file"] = hdr

    # `bcftools reheader` refuses plain-gzip input ("cannot reheader gzip-compressed
    # files, first convert with bcftools view"), and that conversion is unavailable
    # precisely because bcftools cannot parse this file at all. So the rewrite happens on
    # the text stream, before any VCF parser sees the bytes: the #CHROM line is replaced
    # and every other line is copied through unchanged.
    out = os.path.join(work, "header_repaired.vcf.gz")
    with open(out, "wb") as sink:
        bg = subprocess.Popen(["bgzip", "-c"], stdin=subprocess.PIPE, stdout=sink,
                              text=True)
        n_copied = 0
        with opener(input_vcf, "rt", errors="replace") as fh:
            for line in fh:
                if line.startswith("#CHROM"):
                    bg.stdin.write(new_chrom + "\n")
                else:
                    bg.stdin.write(line)
                n_copied += 1
        bg.stdin.close()
        rc = bg.wait()
    if rc != 0:
        raise SystemExit(
            f"normalize.py: bgzip exited {rc} while rewriting the header of {input_vcf}")
    sh([BCFTOOLS, "index", "-f", "-t", out])
    stats["lines_copied"] = n_copied
    stats["repaired_file"] = out
    return out, stats


def restore_alt(vcf_in, vcf_out, sites_vcf, policy, workdir):
    """INTAKE-3: at a hom-ref site the converter has no population ALT and writes '.'.
    Phasing/imputation tools reject such records and silently drop directly-typed SNPs.
    Fill ALT from the 1000G site list so those markers survive."""
    stats = collections.Counter()
    if policy == "none":
        shutil.copyfile(vcf_in, vcf_out)
        stats["alt_restoration_skipped_by_policy"] = 1
        return stats

    # positions needing an ALT
    need = []
    with gzip.open(vcf_in, "rt") as fh:
        for line in fh:
            if line.startswith("#"):
                continue
            f = line.split("\t", 5)
            if f[4] == ".":
                need.append((f[0], f[1], f[3]))
    stats["sites_with_missing_alt"] = len(need)
    if not need:
        shutil.copyfile(vcf_in, vcf_out)
        return stats

    regions = os.path.join(workdir, "need.tsv")
    with open(regions, "w") as fh:
        for c, p, _ in need:
            fh.write(f"{c}\t{p}\n")

    lookup = {}
    q = sh([BCFTOOLS, "query", "-R", regions, "-f", "%CHROM\t%POS\t%REF\t%ALT\n", sites_vcf])
    for line in q.stdout.splitlines():
        c, p, ref, alt = line.split("\t")
        lookup.setdefault((c, p), []).append((ref, alt))

    with gzip.open(vcf_in, "rt") as fin, open(os.path.join(workdir, "restored.vcf"), "w") as fout:
        for line in fin:
            if line.startswith("#"):
                fout.write(line)
                continue
            f = line.rstrip("\n").split("\t")
            if f[4] == ".":
                cands = [(r, a) for (r, a) in lookup.get((f[0], f[1]), [])
                         if r == f[3] and re.fullmatch(r"[ACGT]", a)]
                if len(cands) == 1:
                    f[4] = cands[0][1]
                    stats["alt_restored_unique_biallelic"] += 1
                elif len(cands) > 1:
                    stats["alt_not_restored_multiallelic_in_panel"] += 1
                else:
                    stats["alt_not_restored_absent_from_panel"] += 1
            fout.write("\t".join(f) + "\n")

    sh(["bash", "-c", f"bgzip -c {os.path.join(workdir, 'restored.vcf')} > {vcf_out}"])
    return stats


BCFTOOLS = "bcftools"
AUTOSOMES = ",".join(str(i) for i in range(1, 23))


def route_assay(assay_class):
    """Route on the classified assay (INTAKE-2), NOT on the file extension.

    genotyping-array -> the 23andMe/AncestryDNA TSV converter (array path).
    variant-only WGS VCF -> the VCF-native path.
    gVCF / WES -> named but not handled in this pass (they need reference-block
    or capture-BED handling that is not built here)."""
    a = (assay_class or "").strip().lower()
    if a in ("genotyping-array", "genotyping array", "array"):
        return "array"
    if "gvcf" in a:
        return "unsupported-gvcf"
    if "wes" in a or "exome" in a:
        return "unsupported-wes"
    if a.startswith("wgs") or a in ("vcf", "variant-only vcf", "variant-only-vcf"):
        return "vcf"
    return "unsupported-unknown"


STRAND_DEFERRED_WHY = (
    "SCO-4: strand ambiguity at A/T and C/G markers is deferred to scoring-time matching, where "
    "every flip decision is recorded. Resolving it during normalization would erase provenance.")


def build_array(args, work, classification):
    """The 23andMe/AncestryDNA TSV path (INTAKE-2 + INTAKE-3). Behaviour unchanged."""
    # Fail fast and clearly rather than after a conversion: INTAKE-3 needs an indexed site list.
    if args.alt_restore_policy != "none":
        if not args.sites or not os.path.exists(args.sites):
            raise SystemExit(f"normalize.py: site list not found: {args.sites}\n"
                             f"  INTAKE-3 needs it to restore ALT. Stage it with:\n"
                             f"    bash tools/stage_refdata.sh sites")
        if not os.path.exists(args.sites + ".tbi") and not os.path.exists(args.sites + ".csi"):
            raise SystemExit(f"normalize.py: no index beside {args.sites}\n"
                             f"  Still downloading, or index not fetched. Re-run:\n"
                             f"    bash tools/stage_refdata.sh sites")

    container, member = open_array_text(args.input, args.member,
                                        os.path.join(work, "member.txt"))
    counts = preprocess(os.path.join(work, "member.txt"), os.path.join(work, "clean.tsv"),
                        args.duplicate_policy, args.haploid_policy)

    raw_vcf = os.path.join(work, "converted.vcf.gz")
    conv = sh([BCFTOOLS, "convert", "-c", "ID,CHROM,POS,AA", "-s", args.sample_id,
               "-f", args.fasta, "--tsv2vcf", os.path.join(work, "clean.tsv"),
               "-Oz", "-o", raw_vcf])

    final = os.path.join(args.out_dir, "normalized.vcf.gz")
    alt_stats = restore_alt(raw_vcf, final, args.sites, args.alt_restore_policy, work)
    sh([BCFTOOLS, "index", "-f", "-t", final])
    n_records = int(sh([BCFTOOLS, "index", "-n", final]).stdout.strip())

    return {
        "final": final,
        "n_records": n_records,
        "note": "INTAKE-2: the stages read THIS record, never the file extension. A 23andMe txt "
                "converted to a VCF looks like a VCF and has array semantics.",
        "conversion_path": {
            "tool": "bcftools convert --tsv2vcf",
            "version": lib_run.tool_version([BCFTOOLS, "--version"]),
            "command": "bcftools convert -c ID,CHROM,POS,AA -s <sample> -f <fasta> --tsv2vcf",
            "why_named": "reference-aware. The PLINK --recode vcf path is observation-only and "
                         "writes no ALT at a hom-ref site, so the two paths produce different "
                         "VCFs from the same bytes; which one ran is itself provenance (R8a §4.3).",
            "bcftools_stderr": conv.stderr.strip().splitlines()[-12:],
        },
        "strand_decisions": {
            "resolved_here": False,
            "why": STRAND_DEFERRED_WHY,
            "source_orientation_claimed_by_header": "plus strand of the human reference sequence",
        },
        "absence_field": classification.get("absence_means"),
        "input_container": {
            "detected": container,
            "member": member,
            "how": "magic bytes, not the filename. The fixture audit found a .txt that "
                   "was a zip and a .csv that was gzip, so the extension is a claim and "
                   "the first bytes are the evidence.",
        },
        "row_accounting": dict(counts),
        "alt_restoration": dict(alt_stats),
        "known_losses": [
            "indels (23andMe I/D tokens) carry no sequence and cannot become VCF alleles",
            "no-call ('--') rows become missing genotypes",
            "haploid X/Y/MT rows excluded under --haploid-policy exclude; they are not doubled "
            "into fake diploid calls" if args.haploid_policy == "exclude" else
            "haploid rows passed through to bcftools",
            "sites whose ALT could not be restored will not match a panel record at imputation",
        ],
    }


def build_vcf(args, work, classification):
    """The VCF-native path (INTAKE-2 for a variant-only VCF, e.g. WGS).

    A VCF already carries REF/ALT, so there is no INTAKE-3 ALT restoration and no REF/ALT
    inference here. Staged so the record accounting is visible: strip any 'chr' prefix, restrict
    to autosomes 1-22, split multiallelics and left-align against the FASTA, keep biallelic SNVs,
    drop exact duplicates. Absence is NOT filled and NOT assumed reference; it is carried forward
    in the sidecar as unknown-not-reference (a variant-only VCF cannot tell hom-ref from
    no-coverage)."""
    def count(vcf):
        return int(sh([BCFTOOLS, "index", "-n", vcf]).stdout.strip())

    rename = os.path.join(work, "rename_chrs.txt")
    with open(rename, "w") as fh:
        for i in range(1, 23):
            fh.write(f"chr{i}\t{i}\n")
        for old, new in (("chrX", "X"), ("chrY", "Y"), ("chrM", "MT"), ("chrMT", "MT")):
            fh.write(f"{old}\t{new}\n")

    src, header_repair = repair_vcf_header(args.input, work, args.header_sample_repair)

    s0 = os.path.join(work, "s0.renamed.vcf.gz")
    sh([BCFTOOLS, "annotate", "--rename-chrs", rename, src, "-Oz", "-o", s0])
    sh([BCFTOOLS, "index", "-f", "-t", s0])
    n_input = count(s0)

    s1 = os.path.join(work, "s1.autosomes.vcf.gz")
    sh([BCFTOOLS, "view", "-t", AUTOSOMES, s0, "-Oz", "-o", s1])
    sh([BCFTOOLS, "index", "-f", "-t", s1])
    n_autosome = count(s1)

    # A vendor VCF can contradict its own header -- the measured case is Veritas declaring
    # INFO/AB with Number=2 and writing one value, which stops bcftools norm. `--force` would
    # carry that data forward anyway. Dropping the INFO block instead is an enumerable loss:
    # this path is consumed for GT, and no stage downstream reads INFO. (It WOULD be
    # load-bearing for a gVCF, where DP and END live there -- which is one reason gVCF is a
    # separate route.)
    s1b = s1
    if args.info_policy == "drop-all":
        s1b = os.path.join(work, "s1b.no_info.vcf.gz")
        sh([BCFTOOLS, "annotate", "-x", "INFO", s1, "-Oz", "-o", s1b])
        sh([BCFTOOLS, "index", "-f", "-t", s1b])

    s2 = os.path.join(work, "s2.split.vcf.gz")
    p2 = sh([BCFTOOLS, "norm", "-f", args.fasta, "-m", "-any", s1b, "-Oz", "-o", s2])
    sh([BCFTOOLS, "index", "-f", "-t", s2])
    n_split = count(s2)

    s3 = os.path.join(work, "s3.biallelic_snv.vcf.gz")
    sh([BCFTOOLS, "view", "-v", "snps", "-m2", "-M2", s2, "-Oz", "-o", s3])
    sh([BCFTOOLS, "index", "-f", "-t", s3])
    n_snv = count(s3)

    final = os.path.join(args.out_dir, "normalized.vcf.gz")
    p4 = sh([BCFTOOLS, "norm", "-d", "exact", s3, "-Oz", "-o", final])
    sh([BCFTOOLS, "index", "-f", "-t", final])
    n_final = count(final)

    counts = {
        "records_in_input_after_rename": n_input,
        "non_autosome_dropped": n_input - n_autosome,
        "records_after_autosome_restrict": n_autosome,
        "records_after_multiallelic_split": n_split,
        "multiallelic_split_net_expansion": n_split - n_autosome,
        "non_snv_or_multiallelic_dropped": n_split - n_snv,
        "biallelic_snv_records": n_snv,
        "exact_duplicates_dropped": n_snv - n_final,
        "records_written": n_final,
    }

    absence = classification.get("absence_means")
    absence_field = (
        "unknown -- NOT reference (VCF/variant-only intake: an absent position may be hom-ref OR "
        "no-coverage; normalize performs no assume-reference). classification.absence_means: "
        + repr(absence))

    return {
        "final": final,
        "n_records": n_final,
        "header_repair": header_repair,
        "note": "INTAKE-2: the stages read THIS record, never the file extension. A variant-only "
                "VCF is not a complete genome; an absent position is unknown, NOT reference.",
        "conversion_path": {
            "tool": "bcftools VCF intake (annotate --rename-chrs | view | norm)",
            "version": lib_run.tool_version([BCFTOOLS, "--version"]),
            "command": "bcftools annotate --rename-chrs <strip-chr> | view -t 1..22 | "
                       "norm -f <fasta> -m -any | view -v snps -m2 -M2 | norm -d exact",
            "why_named": "VCF-native intake: strip 'chr' prefix, restrict to autosomes 1-22, split "
                         "multiallelics and left-align against the FASTA, keep biallelic SNVs, drop "
                         "exact duplicates. No REF/ALT inference and no assume-reference; absent "
                         "positions are carried as unknown (not reference). check-ref left at "
                         "bcftools default (exit on REF mismatch), since the door supplies the "
                         "build-matched FASTA.",
            "bcftools_stderr": (p2.stderr + p4.stderr).strip().splitlines()[-12:],
        },
        "strand_decisions": {
            "resolved_here": False,
            "why": STRAND_DEFERRED_WHY,
            "source_orientation_claimed_by_header": "VCF records are on the reference forward "
                "strand as emitted by the caller; strand-ambiguous SNPs are still deferred to "
                "matching (SCO-4).",
        },
        "absence_field": absence_field,
        "row_accounting": counts,
        "alt_restoration": {
            "not_applicable": "VCF intake -- the file already carries REF/ALT; INTAKE-3 ALT "
                              "restoration is only for the array->tsv2vcf path where hom-ref sites "
                              "get ALT='.'."
        },
        "known_losses": [
            "non-autosomal records (chrX/chrY/chrM/MT and any non-1..22 contig) dropped",
            "indels and MNVs dropped -- only biallelic SNVs are kept in this pass",
            "multiallelic sites split into biallelic records before the SNV filter",
            "exact-duplicate records dropped",
            "absent positions are NOT filled and NOT assumed reference (carried as unknown)",
            "INFO fields dropped when --info-policy drop-all (see policies_chosen_by_agent); "
            "GT is what this path is consumed for, and no stage downstream reads INFO",
        ],
    }


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--run", required=True)
    ap.add_argument("--input", required=True)
    ap.add_argument("--member")
    ap.add_argument("--build", required=True, help="the build the AGENT determined, not a guess")
    ap.add_argument("--fasta", required=True)
    ap.add_argument("--sites", required=True, help="1000G site list for INTAKE-3 ALT restoration "
                                                   "(array path only; unused by the VCF path)")
    ap.add_argument("--sample-id", required=True)
    ap.add_argument("--classification", required=True, help="the door's classification.json")
    ap.add_argument("--duplicate-policy", required=True, choices=["keep-first", "drop-all"])
    ap.add_argument("--haploid-policy", required=True, choices=["exclude", "pass-through"])
    ap.add_argument("--alt-restore-policy", required=True, choices=["biallelic-only", "none"])
    ap.add_argument("--info-policy", required=True, choices=["keep", "drop-all"],
                    help="VCF path only. A vendor INFO block that contradicts the file's own "
                         "header stops bcftools norm. No default: dropping it is a loss and "
                         "keeping it may be impossible, so the choice is stated and recorded. "
                         "Never --force.")
    ap.add_argument("--header-sample-repair", required=True,
                    choices=["none", "truncate-to-data-columns"],
                    help="VCF path only. Some vendor VCFs declare more sample columns in #CHROM "
                         "than the data rows carry and bcftools then refuses the file. No "
                         "default: choosing to rewrite a header is a judgment and is recorded.")
    ap.add_argument("--out-dir", required=True)
    args = ap.parse_args()

    os.makedirs(args.out_dir, exist_ok=True)
    work = tempfile.mkdtemp(prefix="normalize-")
    classification = lib_run.read_json(args.classification)
    assay = classification.get("assay_class")
    route = route_assay(assay)

    if route == "array":
        res = build_array(args, work, classification)
    elif route == "vcf":
        res = build_vcf(args, work, classification)
    else:
        shutil.rmtree(work, ignore_errors=True)
        raise SystemExit(
            f"normalize.py: assay_class {assay!r} (routed {route!r}) is not handled in this pass.\n"
            f"  Handled: 'genotyping-array' (23andMe/AncestryDNA TSV export) and variant-only "
            f"'WGS' VCF.\n"
            f"  Not handled: gVCF (needs reference-block DP/GQ handling) and WES (needs the "
            f"capture-target BED to split on-target from off-target). Named here, not closed.")

    final = res["final"]
    n_records = res["n_records"]
    provenance = {
        "note": res["note"],
        "run": args.run,
        "generated_utc": lib_run.utcnow(),
        "standard_format": "bgzipped VCF + this sidecar",
        "normalized_file": final,
        "normalized_sha256": lib_run.sha256_file(final),
        "records_in_normalized_vcf": n_records,

        # -- the defined provenance list from INTAKE-2, every field populated --
        "source_assay_class": classification.get("assay_class"),
        "vendor": classification.get("vendor"),
        "chip_generation": classification.get("chip_generation"),
        "declared_build": args.build,
        "build_evidence": classification.get("genome_build"),
        "reference_fasta_used": {
            "path": args.fasta,
            "sha256_note": "see refdata/MANIFEST.json for the artifact's URL and hash",
        },
        "conversion_path": res["conversion_path"],
        "strand_decisions": res["strand_decisions"],
        "absence_semantics_carried_forward": res["absence_field"],

        # -- enumerated losses, per INTAKE-2's "lossy in enumerable ways" --
        "policies_chosen_by_agent": {
            "duplicate_policy": args.duplicate_policy,
            "haploid_policy": args.haploid_policy,
            "alt_restore_policy": args.alt_restore_policy,
            "header_sample_repair": args.header_sample_repair,
            "info_policy": args.info_policy,
        },
        "header_repair": res.get("header_repair"),
        "row_accounting": res["row_accounting"],
        "alt_restoration": res["alt_restoration"],
        "known_losses": res["known_losses"],
    }
    lib_run.write_json(os.path.join(args.out_dir, "provenance.json"), provenance)
    lib_run.ledger_append(args.run, {
        "kind": "artifact", "stage": "door", "tool": "normalize.py",
        "normalized_records": n_records,
        "artifact": os.path.join(args.out_dir, "provenance.json"),
    })
    shutil.rmtree(work, ignore_errors=True)
    print(f"normalized -> {final}  ({n_records} records)")
    print(f"provenance -> {os.path.join(args.out_dir, 'provenance.json')}")
    for k, v in sorted(res["row_accounting"].items()):
        print(f"  {k:38s} {v}")
    for k, v in sorted(res["alt_restoration"].items()):
        print(f"  {k:38s} {v}")


if __name__ == "__main__":
    main()
