#!/usr/bin/env python3
"""build_reference_panel.py -- build the reference panel VCF a distribution is scored from.

This file had no generating script. The one in use was made by hand on 2026-07-28 and
its own header is the only record of how:

    bcftools view -T /tmp/refgeno/chr1.pos ...
    bcftools concat ... -o /tmp/refgeno/ref_1kg_<PGS>.vcf.gz

The position list lived in /tmp and is gone. The consequence is measurable: that panel
holds fewer records than the model has rows, so some of one patient's resolved positions
were absent from it, worth 0.55% of the score's weight -- and placing the patient against
a cohort scored over a slightly different set of variants moved the percentile by 8.3
points.

WHY THE MODEL'S POSITIONS, NOT THE PATIENT'S. A patient's imputed dosages come out of
this same phase3 panel, so every position they can resolve exists in it. Build the
reference at the MODEL's positions and it covers everything any patient could resolve on
that model: the parity gap goes to zero by construction rather than being corrected
afterwards. It also makes the panel reusable across patients for that model, which a
patient-specific one would not be.

Everything requested and everything found is recorded per chromosome, so a shortfall is
visible in the artifact instead of surfacing later as a parity gap.

Usage:
  python3 build_reference_panel.py \\
    --scoring inputs/PGS000001_hmPOS_GRCh37.txt.gz \\
    --chr-col hm_chr --pos-col hm_pos \\
    --phase3-dir ../agent/refdata/ancestry-ref/phase3 \\
    --chromosomes 1-22 \\
    --out inputs/ref_1kg_PGS000001.vcf.gz
"""
import argparse
import collections
import concurrent.futures
import datetime
import gzip
import hashlib
import json
import os
import shutil
import subprocess
import sys
import time

BCFTOOLS = "bcftools"


def utcnow():
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def die(msg):
    sys.stderr.write("build_reference_panel.py: %s\n" % msg)
    sys.exit(1)


def sha256_of(path, chunk=1 << 20):
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(chunk), b""):
            h.update(block)
    return h.hexdigest()


def parse_chroms(spec):
    out = []
    for part in spec.split(","):
        if "-" in part:
            a, b = part.split("-")
            out.extend(str(i) for i in range(int(a), int(b) + 1))
        else:
            out.append(part.strip())
    return out


def _report(e):
    """One line per chromosome, printed as it completes."""
    if e.get("status") == "ok":
        print("  chr%-3s requested %8d  found %8d  (%+d)  %.1fs"
              % (e["chrom"], e["n_positions_requested"], e["n_records_found"],
                 e["n_records_found"] - e["n_positions_requested"], e["seconds"]))
    elif e.get("status") == "FAILED":
        print("  chr%-3s FAILED after %.1fs" % (e["chrom"], e.get("seconds", 0)))
    elif e.get("status") == "SKIPPED_no_source":
        print("  chr%-3s SOURCE MISSING: %s" % (e["chrom"], e["source"]))
    else:
        print("  chr%-3s no model positions" % e["chrom"])


def run_checked(step, cmd):
    """Like run(), but raises instead of exiting.

    die() calls sys.exit, which inside a worker thread kills that thread and leaves the
    pool to carry on -- the failure would surface as a missing part rather than an error.
    """
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError("%s failed (exit %d)\n  %s\n%s"
                           % (step, proc.returncode, " ".join(cmd), proc.stderr[-2000:]))
    return proc


def run(step, cmd):
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        die("%s failed (exit %d)\n  %s\n%s"
            % (step, proc.returncode, " ".join(cmd), proc.stderr[-2000:]))
    return proc


def read_model_positions(scoring_gz, chr_col, pos_col):
    """{chrom: sorted set of positions} from the scoring file, by COLUMN NAME.

    The column names are supplied rather than assumed: only six columns are universal
    across PGS Catalog scoring files, and a layout learned once is wrong later (SEL-13).
    """
    opener = gzip.open if scoring_gz.endswith(".gz") else open
    by_chrom = collections.defaultdict(set)
    n_rows = n_skipped = 0
    header_meta = {}

    with opener(scoring_gz, "rt", errors="replace") as fh:
        cols = None
        for line in fh:
            if line.startswith("#"):
                s = line.lstrip("#").strip()
                if "=" in s:
                    k, v = s.split("=", 1)
                    header_meta[k.strip()] = v.strip()
                continue
            f = line.rstrip("\n").split("\t")
            if cols is None:
                cols = {n: i for i, n in enumerate(f)}
                for need in (chr_col, pos_col):
                    if need not in cols:
                        die("column %r not in the scoring file. Present: %s"
                            % (need, sorted(cols)))
                continue
            n_rows += 1
            try:
                c = f[cols[chr_col]].strip()
                p = f[cols[pos_col]].strip()
            except IndexError:
                n_skipped += 1
                continue
            if not c or not p or not p.isdigit() or c in ("NA", "."):
                n_skipped += 1
                continue
            by_chrom[c].add(int(p))

    return by_chrom, n_rows, n_skipped, header_meta


def main():
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--scoring", required=True,
                    help="the PGS Catalog scoring file, gzipped, in the panel's build")
    ap.add_argument("--chr-col", required=True,
                    help="chromosome column, read off the scoring file's own header")
    ap.add_argument("--pos-col", required=True,
                    help="position column, read off the scoring file's own header")
    ap.add_argument("--phase3-dir", required=True,
                    help="per-chromosome 1000G VCFs named chr<N>.vcf.gz")
    ap.add_argument("--chromosomes", required=True, help="e.g. 1-22")
    ap.add_argument("--out", required=True, help="output VCF path (.vcf.gz)")
    ap.add_argument("--samples",
                    help="optional file of sample IDs to keep, one per line. Omitted: "
                         "every sample in the panel, which is what the cohort selection "
                         "downstream expects.")
    ap.add_argument("--threads", default="2",
                    help="bcftools threads PER chromosome. With --workers > 1 this "
                         "multiplies: workers x threads should stay near the core count.")
    ap.add_argument("--workers", type=int, default=4,
                    help="chromosomes extracted concurrently. The per-chromosome calls are "
                         "independent -- different input, different output, no shared state "
                         "-- so this is a straight speedup bounded by disk. 1 restores the "
                         "serial behaviour and the directly comparable per-chromosome times.")
    ap.add_argument("--workdir", help="default: <out>.work")
    ap.add_argument("--keep-work", action="store_true")
    args = ap.parse_args()

    # Line-buffered, so progress and the error that ends a run land in a redirected log in
    # the order they happened. Block-buffered, the error (stderr) was written at line 1 and
    # the progress after it at exit, so a `tail` of the log showed "concatenating" forever.
    sys.stdout.reconfigure(line_buffering=True)

    # Checked before any work, because any other name built the whole panel and then died at
    # the end. bcftools (1.19 here) finds a .tbi only beside a .vcf.gz name, so .vcf.bgz, .gz
    # or no extension died at `bcftools index -n`. concat writes by the name's extension over
    # -Oz, so .vcf or .bcf died one step earlier, at `bcftools index -t`.
    if not args.out.endswith(".vcf.gz"):
        die("--out must end in .vcf.gz (got %s): under any other name the panel is built "
            "and then fails when bcftools indexes it" % args.out)
    if not os.path.exists(args.scoring):
        die("scoring file not found: %s" % args.scoring)
    work = args.workdir or (args.out + ".work")
    os.makedirs(work, exist_ok=True)
    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)

    print("=" * 72)
    print("reading the model's positions")
    print("=" * 72)
    by_chrom, n_rows, n_skipped, header_meta = read_model_positions(
        args.scoring, args.chr_col, args.pos_col)
    total_requested = sum(len(v) for v in by_chrom.values())
    print("  scoring file rows           : %d" % n_rows)
    print("  rows skipped (no position)  : %d" % n_skipped)
    print("  distinct positions wanted   : %d across %d chromosomes"
          % (total_requested, len(by_chrom)))
    print("  model                       : %s (%s)"
          % (header_meta.get("pgs_id", "?"), header_meta.get("HmPOS_build", "?")))

    def extract(c):
        """One chromosome, start to finish. Returns its record; never raises.

        A failure is recorded and returned rather than raised, both because raising inside
        a worker thread would take the pool down mid-flight and because a per-chromosome
        failure is more useful reported alongside the twenty-one that worked. main() stops
        on any FAILED entry after the pool drains.
        """
        src = os.path.join(args.phase3_dir, "chr%s.vcf.gz" % c)
        wanted = sorted(by_chrom.get(c, ()))
        entry = {"chrom": c, "source": src, "n_positions_requested": len(wanted)}

        if not wanted:
            entry["status"] = "SKIPPED_no_model_positions"
            return entry
        if not os.path.exists(src):
            entry["status"] = "SKIPPED_no_source"
            return entry

        t0 = time.time()
        try:
            pos_file = os.path.join(work, "chr%s.pos" % c)
            with open(pos_file, "w") as fh:
                for p in wanted:
                    fh.write("%s\t%d\n" % (c, p))

            out_vcf = os.path.join(work, "chr%s.vcf.gz" % c)
            cmd = [BCFTOOLS, "view", "-T", pos_file]
            if args.samples:
                cmd += ["-S", args.samples, "--force-samples"]
            cmd += [src, "-Oz", "--threads", args.threads, "-o", out_vcf]
            run_checked("bcftools view chr%s" % c, cmd)
            run_checked("bcftools index chr%s" % c,
                        [BCFTOOLS, "index", "-f", "-t", out_vcf])
            n_found = int(run_checked("bcftools index -n chr%s" % c,
                                      [BCFTOOLS, "index", "-n", out_vcf]).stdout.strip())
        except RuntimeError as exc:
            entry.update({"status": "FAILED", "error": str(exc)[:2000],
                          "seconds": round(time.time() - t0, 1)})
            return entry

        entry.update({
            "status": "ok",
            "n_records_found": n_found,
            "n_positions_not_in_panel": len(wanted) - n_found,
            "position_file": pos_file,
            "part": out_vcf,
            "seconds": round(time.time() - t0, 1),
        })
        return entry

    chroms = parse_chroms(args.chromosomes)
    t_start = time.time()
    workers = max(1, args.workers)
    print("  extracting with %d worker(s), %s bcftools thread(s) each"
          % (workers, args.threads))

    results = {}
    if workers == 1:
        for c in chroms:
            results[c] = extract(c)
            _report(results[c])
    else:
        with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {pool.submit(extract, c): c for c in chroms}
            for fut in concurrent.futures.as_completed(futures):
                e = fut.result()
                results[e["chrom"]] = e
                _report(e)

    # Chromosome order, not completion order: bcftools concat wants its parts ordered, and
    # a provenance record that reads in a different order every run is harder to compare.
    per_chrom = [results[c] for c in chroms if c in results]
    parts = [e["part"] for e in per_chrom if e.get("status") == "ok"]

    failed = [e for e in per_chrom if e.get("status") == "FAILED"]
    if failed:
        for e in failed:
            sys.stderr.write("chr%s FAILED: %s\n" % (e["chrom"], e.get("error", "")))
        die("%d chromosome(s) failed; not concatenating a partial panel" % len(failed))

    if not parts:
        die("no chromosome produced output; nothing to concatenate")

    print()
    print("=" * 72)
    print("concatenating")
    print("=" * 72)
    run("bcftools concat",
        [BCFTOOLS, "concat", "-Oz", "--threads", args.threads, "-o", args.out] + parts)
    run("bcftools index", [BCFTOOLS, "index", "-f", "-t", args.out])
    n_total = int(run("bcftools index -n",
                      [BCFTOOLS, "index", "-n", args.out]).stdout.strip())
    samples = run("bcftools query -l",
                  [BCFTOOLS, "query", "-l", args.out]).stdout.split()

    # Count only the chromosomes that actually ran. Comparing against the whole model
    # when --chromosomes named a subset reports every unprocessed chromosome as missing.
    total_requested_run = sum(e["n_positions_requested"] for e in per_chrom
                              if e.get("status") == "ok")
    total_found = sum(e.get("n_records_found", 0) for e in per_chrom)
    shortfall = total_requested_run - total_found

    record = {
        "tool": "build_reference_panel.py",
        "generated_utc": utcnow(),
        "purpose": ("the reference panel a distribution is scored from, built at the "
                    "MODEL's positions so that every position any patient could resolve "
                    "on this model is present -- the parity gap is zero by construction "
                    "rather than corrected afterwards"),
        "scoring_file": args.scoring,
        "scoring_file_sha256": sha256_of(args.scoring),
        "scoring_file_header": header_meta,
        "columns_used": {"chrom": args.chr_col, "pos": args.pos_col},
        "phase3_dir": args.phase3_dir,
        "samples_file": args.samples,
        "chromosomes": args.chromosomes,
        "n_scoring_rows": n_rows,
        "n_scoring_rows_without_position": n_skipped,
        "n_positions_requested_all_chromosomes": total_requested,
        "n_positions_requested_chromosomes_run": total_requested_run,
        "n_records_written": n_total,
        "n_positions_not_in_panel": shortfall,
        "shortfall_note": (
            "positions the model names that do not exist in the 1000G panel at all. They "
            "cannot be scored for the cohort OR imputed for a patient, so they are not a "
            "parity gap -- they are outside the panel for everyone. A non-zero number "
            "here is expected and is recorded rather than hidden."),
        "per_chromosome": per_chrom,
        "output": args.out,
        "output_sha256": sha256_of(args.out),
        "workers": workers,
        "bcftools_threads_per_chromosome": args.threads,
        "elapsed_seconds": round(time.time() - t_start, 1),
        "summed_chromosome_seconds": round(
            sum(e.get("seconds", 0) for e in per_chrom), 1),
        "timing_note": (
            "elapsed_seconds is true wall time. summed_chromosome_seconds adds the "
            "per-chromosome times, which OVERLAP when workers > 1 and each of which "
            "includes contention from the others -- so the two are only equal at "
            "workers=1, and their ratio is the speedup actually achieved."),
        "samples_in_output": len(samples),
    }
    rec_path = args.out + ".provenance.json"
    with open(rec_path, "w") as fh:
        json.dump(record, fh, indent=2)
        fh.write("\n")

    if not args.keep_work:
        shutil.rmtree(work, ignore_errors=True)

    print()
    print("  positions requested         : %d  (chromosomes run)" % total_requested_run)
    print("  records written             : %d" % n_total)
    if shortfall > 0:
        print("  not present in the panel    : %d" % shortfall)
    elif shortfall < 0:
        print("  multi-record positions      : %d  (one position, several VCF records --"
              % (-shortfall))
        print("                                resolved downstream by allele match)")
    else:
        print("  not present in the panel    : 0")
    print("  samples                     : %d" % len(samples))
    _elapsed = time.time() - t_start
    _summed = sum(e.get("seconds", 0) for e in per_chrom)
    print("  wall time                   : %.1fs  (summed per-chromosome %.1fs, %.1fx)"
          % (_elapsed, _summed, (_summed / _elapsed) if _elapsed else 0.0))
    print("  panel                       : %s" % args.out)
    print("  provenance                  : %s" % rec_path)


if __name__ == "__main__":
    main()
