#!/usr/bin/env python3
"""impute_joint.py -- joint panel-based imputation: the patient imputed together with a
hold-out cohort, so DR2 is measured natively in the same Beagle run that produced the
patient's dosages.

Beagle's DR2 (and INFO) is a PER-VARIANT statistic estimated across the target samples, so a
patient imputed alone leaves it nothing to spread over and it collapses to ~0 at almost every
site -- which is not a low-quality measurement, it is no measurement at all, and a coverage gate
reading those sites sees unmeasurable positions as failures.

The fix is structural, not cosmetic. The Beagle target is the patient MERGED with a hold-out
cohort drawn from the same panel (tools/build_dr2_cohort.py), masked to the patient's typed
positions so the hold-outs are genotyped as if on the same chip; the reference is everyone else,
disjoint from the hold-outs so no sample imputes itself. One Beagle run per chromosome produces
both the patient's dosages and a DR2 estimated over ~300 real genomes. Nothing is transplanted
from a separate run: the DR2 written here belongs to the very run that wrote the dosages.

The patient is in neither sample list, and is verified to be in neither before any chromosome
runs -- a patient present in the reference would find their own haplotypes there.

Usage:
  python3 tools/impute_joint.py --run <RUN_ID> \
      --target runs/<RUN_ID>/door/normalized.vcf.gz --patient-id PATIENT \
      --holdout-samples runs/<RUN_ID>/score/dr2/dr2_holdout.txt \
      --reference-samples runs/<RUN_ID>/score/dr2/dr2_reference.txt \
      --phase3-dir refdata/ancestry-ref/phase3 --map-dir refdata/maps \
      --typed-positions runs/<RUN_ID>/score/patient_typed_positions.tsv \
      --chromosomes 1-22 --threads 6 --xmx 8g \
      --out-dir runs/<RUN_ID>/score/imputed_joint
"""
import argparse
import gzip
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import lib_run  # noqa: E402
import impute_remote  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BEAGLE = os.path.join(ROOT, "refdata", "bin", "beagle.jar")


def find_java():
    """The bundled refdata/jre is a macOS aarch64 build (its `release` file says
    OS_NAME="Darwin", OS_ARCH="aarch64") and ships mode 644, so on Linux it cannot run at
    all. Use it only if it actually starts; otherwise the java on PATH."""
    bundled = os.path.join(ROOT, "refdata", "jre", "bin", "java")
    if os.access(bundled, os.X_OK):
        try:
            if subprocess.run([bundled, "-version"], capture_output=True).returncode == 0:
                return bundled
        except OSError:
            pass
    found = shutil.which("java")
    if not found:
        die("no runnable java: the bundled refdata/jre does not start here and none is on PATH")
    return found


def parse_chroms(spec):
    out = []
    for part in spec.split(","):
        if "-" in part:
            a, b = part.split("-")
            out.extend(str(i) for i in range(int(a), int(b) + 1))
        else:
            out.append(part)
    return out


def die(msg):
    print(f"impute_joint.py: {msg}", file=sys.stderr)
    sys.exit(1)


def read_sample_list(path):
    """One sample ID per line; blank lines ignored."""
    if not os.path.exists(path):
        die(f"sample list not found: {path}")
    with open(path) as fh:
        return [line.strip() for line in fh if line.strip()]


def vcf_samples(path):
    """Sample names in a VCF, read off the file itself rather than assumed."""
    proc = subprocess.run(["bcftools", "query", "-l", path], capture_output=True, text=True)
    if proc.returncode != 0:
        die(f"bcftools query -l failed on {path}: {proc.stderr.strip()}")
    return [s.strip() for s in proc.stdout.splitlines() if s.strip()]


def n_markers(path):
    proc = subprocess.run(["bcftools", "index", "-n", path], capture_output=True, text=True)
    if proc.returncode != 0:
        return None
    try:
        return int(proc.stdout.strip())
    except ValueError:
        return None


class Failed(Exception):
    """A step returned non-zero. Carries what to record; never propagates past one chromosome."""

    def __init__(self, step, returncode, stderr):
        super().__init__(step)
        self.step = step
        self.returncode = returncode
        self.stderr = stderr or ""


# Wall-clock per step for the chromosome in progress, so Beagle and the bcftools steps
# around it can be told apart in impute_joint.json rather than guessed at. Per thread, because
# with --parallel-chromosomes several chromosomes are in progress at once.
_TLS = threading.local()


def timed(step, t0):
    steps = _TLS.__dict__.setdefault("steps", {})
    steps[step] = round(steps.get(step, 0) + time.time() - t0, 2)


# Hold-outs enter the Beagle target under this prefix so that excludesamples=, which Beagle
# applies to target and reference alike, removes them from the reference only.
HOLDOUT_PREFIX = "DR2HOLDOUT_"


def beagle_sample_counts(log_text):
    """'Reference samples: 2,203' / 'Study samples: 302' off Beagle's stdout."""
    n_ref = n_study = None
    for line in log_text.splitlines():
        parts = line.split(":")
        if len(parts) != 2:
            continue
        key, val = " ".join(parts[0].split()), parts[1].strip().replace(",", "")
        if key == "Reference samples" and val.isdigit():
            n_ref = int(val)
        elif key == "Study samples" and val.isdigit():
            n_study = int(val)
    return n_ref, n_study


def check_panel_split(panel_dir, chroms, holdout, reference, patient_id):
    """The prebuilt panel minus the hold-out must be exactly the reference list, on every
    chromosome: that is the same disjointness the per-run rebuild guaranteed by construction."""
    hold, ref = set(holdout), set(reference)
    for c in chroms:
        path = os.path.join(panel_dir, f"chr{c}.snv.vcf.gz")
        if not (os.path.exists(path) and os.path.exists(path + ".tbi")):
            continue  # reported per chromosome as SKIPPED_no_panel
        panel = set(vcf_samples(path))
        if not hold <= panel:
            die(f"chr{c}: {len(hold - panel)} hold-out sample(s) are not in {path}")
        if panel - hold != ref:
            die(f"chr{c}: {path} minus the hold-out is not the reference list "
                f"({len(panel - hold)} vs {len(ref)} samples)")
        if patient_id in panel or any(s.startswith(HOLDOUT_PREFIX) for s in panel):
            die(f"chr{c}: {path} contains the patient id or a {HOLDOUT_PREFIX} name")


def run_step(step, cmd):
    t0 = time.time()
    proc = subprocess.run(cmd, capture_output=True, text=True)
    timed(step, t0)
    if proc.returncode != 0:
        raise Failed(step, proc.returncode, proc.stderr)
    return proc


def run_pipe(step, cmd_a, cmd_b):
    """cmd_a | cmd_b, both stderr captured. Either non-zero fails the step."""
    t0 = time.time()
    p_a = subprocess.Popen(cmd_a, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    p_b = subprocess.Popen(cmd_b, stdin=p_a.stdout, stdout=subprocess.DEVNULL,
                           stderr=subprocess.PIPE)
    p_a.stdout.close()  # so cmd_a sees SIGPIPE if cmd_b dies
    err_b = p_b.communicate()[1]
    err_a = p_a.communicate()[1]
    rc = p_a.returncode or p_b.returncode
    timed(step, t0)
    if rc != 0:
        text = (err_a or b"").decode(errors="replace") + (err_b or b"").decode(errors="replace")
        raise Failed(step, rc, text)


def run_chain(step, cmds, stdout_path=None):
    """cmds[0] | cmds[1] | ... ; every stage's stderr captured, any non-zero fails the step."""
    t0 = time.time()
    procs, prev = [], None
    out_fh = open(stdout_path, "wb") if stdout_path else subprocess.DEVNULL
    try:
        for i, cmd in enumerate(cmds):
            last = i == len(cmds) - 1
            p = subprocess.Popen(cmd, stdin=prev, stdout=out_fh if last else subprocess.PIPE,
                                 stderr=subprocess.PIPE)
            if prev is not None:
                prev.close()  # so an upstream stage sees SIGPIPE if this one dies
            prev = p.stdout
            procs.append(p)
        errs = [p.communicate()[1] for p in reversed(procs)][::-1]
    finally:
        if stdout_path:
            out_fh.close()
    timed(step, t0)
    codes = [p.returncode for p in procs]
    if any(codes):
        # When a downstream stage dies, every stage upstream of it is killed by SIGPIPE; the
        # code worth recording is the first one that is not that.
        real = [rc for rc in codes if rc not in (0, -signal.SIGPIPE)]
        raise Failed(step, real[0] if real else next(rc for rc in codes if rc),
                     "".join((e or b"").decode(errors="replace") for e in errs))


def query_gzip(step, cmd, out_path):
    """Stream a bcftools query straight into a gzipped file."""
    t0 = time.time()
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    with gzip.open(out_path, "wb") as gz:
        for chunk in iter(lambda: proc.stdout.read(1 << 20), b""):
            gz.write(chunk)
    err = proc.communicate()[1]
    timed(step, t0)
    if proc.returncode != 0:
        raise Failed(step, proc.returncode, (err or b"").decode(errors="replace"))


def merge_chromosome_target(target, holdout_vcf, chrom, output, threads):
    """Merge only this chromosome, preserving the patient-first sample order.

    The patient input can span the genome. Restrict the indexed merge itself so a
    remote upload never carries unrelated chromosomes or their missing hold-out calls.
    """
    run_step("bcftools merge",
             ["bcftools", "merge", "--regions", str(chrom), target, holdout_vcf,
              "-Oz1", "--threads", str(threads), "-o", output])
    run_step("bcftools index (merged)", ["bcftools", "index", "-f", "-t", output])


# Written by the demo worker into a demo run's read-only refdata/ and kept current: how many demo
# runs are going. While this run is the only one, a third chromosome runs beside the two asked
# for. Each is its own Beagle at the same --threads, so how many run together changes when they
# finish, never what they write. Absent from every other run, which runs as asked.
DEMO_LOAD = Path(__file__).resolve().parent.parent / "refdata" / "demo_load.json"
LOAD_RECHECK_SECONDS = 10


def chromosomes_at_once(asked):
    try:
        going = json.loads(DEMO_LOAD.read_text()).get("demo_runs_going")
    except (OSError, ValueError, AttributeError):
        return asked
    return asked + 1 if asked == 2 and going == 1 else asked


def run_chromosomes(order, one, asked, demo_may_add=True):
    """one(c) for each chromosome in `order`, started strictly in that order (each waits for its
    turn as well as for room), at most chromosomes_at_once(asked) at a time (re-read each time one
    would start); returns (their entries in `order`, the most that ran at once)."""
    gate = threading.Condition()
    state = {"now": 0, "most": 0, "turn": 0}
    limit = (lambda: chromosomes_at_once(asked)) if demo_may_add else (lambda: asked)

    def gated(i):
        with gate:
            while state["turn"] != i or state["now"] >= limit():
                gate.wait(LOAD_RECHECK_SECONDS)
            state["turn"] += 1
            state["now"] += 1
            state["most"] = max(state["most"], state["now"])
            gate.notify_all()  # the next in turn may start too, if there is room
        try:
            return one(order[i])
        finally:
            with gate:
                state["now"] -= 1
                gate.notify_all()
    workers = asked + 1 if demo_may_add and asked == 2 and DEMO_LOAD.exists() else asked
    with ThreadPoolExecutor(max_workers=workers) as pool:
        runs = list(pool.map(gated, range(len(order))))
    return runs, state["most"]


def parse_args(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--run", required=True)
    ap.add_argument("--target", required=True, help="patient VCF, bgzipped and indexed, one sample")
    ap.add_argument("--patient-id", required=True)
    ap.add_argument("--holdout-samples", required=True, help="from build_dr2_cohort.py")
    ap.add_argument("--reference-samples", required=True, help="from build_dr2_cohort.py")
    ap.add_argument("--phase3-dir", required=True, help="per-chromosome 1000G VCFs, chr<N>.vcf.gz")
    ap.add_argument("--panel-dir", default=None,
                    help="prebuilt reference: chr<N>.snv.vcf.gz, the phase3 panel filtered once to "
                         "biallelic SNVs with exact duplicates collapsed, ALL samples. Given, the "
                         "hold-out is excluded at Beagle read time (excludesamples=) instead of "
                         "rebuilding a reference VCF per chromosome per run")
    ap.add_argument("--map-dir", required=True)
    ap.add_argument("--typed-positions", required=True, help="chrom<TAB>pos, no header")
    ap.add_argument("--chromosomes", required=True)
    ap.add_argument("--seed", default=str(lib_run.DEFAULT_SEED),
                    help="fixed for every run and recorded (SCO-10); another only for the score "
                         "skill's second-seed check")
    ap.add_argument("--threads", default="4")
    ap.add_argument("--xmx", default="8g")
    ap.add_argument("--java-opt", action="append", default=[],
                    help="extra JVM option for Beagle, repeatable (e.g. -XX:+UseParallelGC). JVM "
                         "settings change speed, not results; Beagle's own parameters are not "
                         "exposed here")
    ap.add_argument("--out-dir", required=True)
    ap.add_argument("--work-dir", default=None, help="default <out-dir>/work")
    ap.add_argument("--keep-work", action="store_true", help="keep per-chromosome intermediates")
    ap.add_argument("--skip-existing", action="store_true")
    ap.add_argument("--parallel-chromosomes", type=int, default=None,
                    help="chromosomes in flight at once, each its own Beagle with --threads and "
                         "--xmx; largest first. Beagle's output depends on its nthreads, so keep "
                         "--threads fixed and change this instead")
    return ap.parse_args(argv)


def main():
    args = parse_args()
    # Where Beagle runs is the harness's choice, read from refdata/ and not an option here.
    try:
        args.backend = impute_remote.configured_backend()
    except (OSError, ValueError, KeyError, impute_remote.RemoteError) as exc:
        die(str(exc))
    if args.parallel_chromosomes is None:
        args.parallel_chromosomes = 2 if args.backend == "gb10" else 1
    if args.backend == "gb10":
        if args.threads != "6" or not args.panel_dir:
            die("gb10 requires --threads 6 and --panel-dir")
        if args.java_opt:
            die("gb10 JVM options are fixed by the gate")
        if args.parallel_chromosomes != 2:
            die("gb10 requires --parallel-chromosomes 2 to overlap download and compute")
        chroms = parse_chroms(args.chromosomes)
        if len(set(chroms)) != len(chroms) or any(c not in map(str, range(1, 23)) for c in chroms):
            die("gb10 chromosomes must be unique autosomes 1..22")
        args.xmx = "10g"  # The installed gate sets heap to threads + 4 GiB.
    if args.parallel_chromosomes < 1:
        die("--parallel-chromosomes must be at least 1")

    work_root = args.work_dir or os.path.join(args.out_dir, "work")

    # ---- pre-flight. All three checks are about leakage or identity; none is recoverable. ----
    for path in (args.target, args.typed_positions):
        if not os.path.exists(path):
            die(f"file not found: {path}")

    target_samples = vcf_samples(args.target)
    if len(target_samples) != 1:
        die(f"--target must contain exactly one sample, found {len(target_samples)}: "
            f"{', '.join(target_samples[:10])}")
    if target_samples[0] != args.patient_id:
        die(f"--patient-id {args.patient_id!r} does not match the sample in --target "
            f"({target_samples[0]!r})")

    holdout = read_sample_list(args.holdout_samples)
    reference = read_sample_list(args.reference_samples)
    both = sorted(set(holdout) & set(reference))
    if both:
        die(f"{len(both)} sample(s) appear in both lists -- they would impute themselves: "
            f"{', '.join(both[:10])}")
    if args.patient_id in set(holdout) | set(reference):
        die(f"--patient-id {args.patient_id!r} appears in a sample list; the patient must be in "
            f"neither the reference nor the hold-out set")

    if args.panel_dir:
        check_panel_split(args.panel_dir, parse_chroms(args.chromosomes), holdout, reference,
                          args.patient_id)

    os.makedirs(args.out_dir, exist_ok=True)
    os.makedirs(work_root, exist_ok=True)
    # The 302-sample Beagle output lives in its own subdirectory. match_variants.py globs
    # *.vcf.gz in --imputed-dir, so a cohort file sitting beside the patient file would be
    # read as a second dosage source -- 44 files instead of 22, and only lexical ordering
    # kept the right one winning.
    cohort_dir = os.path.join(args.out_dir, "cohort")
    os.makedirs(cohort_dir, exist_ok=True)

    # Fail before hours of compute rather than during them: a run that filled the disk mid-way
    # lost its whole score stage. The outputs come to roughly half the reference's size (the
    # 302-sample cohort VCF is most of it); ask for 0.6x of it free.
    ref_paths = [os.path.join(args.panel_dir, f"chr{c}.snv.vcf.gz") if args.panel_dir
                 else os.path.join(args.phase3_dir, f"chr{c}.vcf.gz")
                 for c in parse_chroms(args.chromosomes)]
    need = int(0.6 * sum(os.path.getsize(p) for p in ref_paths if os.path.exists(p)))
    free = shutil.disk_usage(args.out_dir).free
    if free < need:
        die(f"{free / 1e9:.1f} GB free under {args.out_dir}; these chromosomes need about "
            f"{need / 1e9:.1f} GB for their outputs. Free space first.")

    # The first chromosome the gb10 could not impute ends the stage: nothing else is sent or
    # waited for, nothing runs on this machine instead, and the tool exits non-zero below.
    gb10_failed = threading.Event()
    gb10_failure = {}
    gb10_lock = threading.Lock()

    java, version_line = None, None
    if args.backend == "local":
        java = find_java()
        beagle_probe = subprocess.run([java, "-jar", BEAGLE], capture_output=True, text=True)
        version_text = (beagle_probe.stdout + beagle_probe.stderr).strip()
        version_line = version_text.splitlines()[0] if version_text else None

    def one(c):
        src = os.path.join(args.phase3_dir, f"chr{c}.vcf.gz")
        gmap = os.path.join(args.map_dir, f"plink.chr{c}.GRCh37.map")
        patient_out = os.path.join(args.out_dir, f"chr{c}.vcf.gz")
        cohort_stem = os.path.join(cohort_dir, f"chr{c}.cohort")
        dr2_out = os.path.join(args.out_dir, f"chr{c}.dr2.tsv.gz")
        work = os.path.join(work_root, f"chr{c}")

        if not (os.path.exists(src) and os.path.exists(src + ".tbi")):
            print(f"chr{c}: SKIPPED_no_source", file=sys.stderr)
            return {"chrom": c, "status": "SKIPPED_no_source", "source": src}
        panel = os.path.join(args.panel_dir, f"chr{c}.snv.vcf.gz") if args.panel_dir else None
        if panel and not (os.path.exists(panel) and os.path.exists(panel + ".tbi")):
            print(f"chr{c}: SKIPPED_no_panel", file=sys.stderr)
            return {"chrom": c, "status": "SKIPPED_no_panel", "source": src}
        if not os.path.exists(gmap):
            print(f"chr{c}: SKIPPED_no_genetic_map", file=sys.stderr)
            return {"chrom": c, "status": "SKIPPED_no_genetic_map", "source": src, "map": gmap}
        if args.skip_existing and os.path.exists(patient_out):
            print(f"chr{c}: SKIPPED_output_exists", file=sys.stderr)
            return {"chrom": c, "status": "SKIPPED_output_exists", "source": src,
                    "map": gmap, "patient_output": patient_out}
        if gb10_failed.is_set():
            print(f"chr{c}: FAILED (not sent: the gb10 failed chr{gb10_failure['chrom']})",
                  file=sys.stderr)
            return {"chrom": c, "status": "FAILED", "failed_step": "gb10", "returncode": None,
                    "stderr_tail": f"not sent: the gb10 failed chr{gb10_failure['chrom']} "
                                   f"first ({gb10_failure['reason']})"}

        # Final names are only ever written by os.replace after every step has succeeded, so a
        # chr<N>.vcf.gz in --out-dir is always complete: downstream tools glob that directory,
        # and --skip-existing trusts it. A failure part-way (ENOSPC took a run down this way)
        # leaves nothing behind under a final name.
        patient_tmp = os.path.join(work, f"chr{c}.patient.vcf.gz")
        dr2_tmp = os.path.join(work, f"chr{c}.dr2.tsv.gz")
        entry = {"chrom": c, "source": src, "map": gmap,
                 "patient_output": patient_out, "cohort_output": cohort_stem + ".vcf.gz",
                 "dr2_table": dr2_out, "beagle_log": os.path.join(args.out_dir, f"chr{c}.beagle.log")}
        os.makedirs(work, exist_ok=True)
        ref_vcf = os.path.join(work, "ref.vcf.gz")
        hold_vcf = os.path.join(work, "hold.vcf.gz")
        merged_vcf = os.path.join(work, "merged.vcf.gz")
        t0 = time.time()
        _TLS.steps = {}
        markers = None
        cohort_index = None
        try:
            if args.panel_dir:
                # reference = the prebuilt all-sample panel; the hold-out is dropped from it at
                # read time by excludesamples=. Beagle applies excludesamples= to the target
                # too, so the hold-outs go into the target under HOLDOUT_PREFIX names and are
                # not excluded from it. Measured byte-identical to the per-run rebuild below.
                ref_vcf = os.path.join(args.panel_dir, f"chr{c}.snv.vcf.gz")
            else:
                # reference = everyone not held out, biallelic SNVs, duplicates collapsed.
                # -Oz1 (fast compression): Beagle reads it fine and it writes far faster than level 6.
                run_pipe("bcftools view (reference)",
                         ["bcftools", "view", "-S", args.reference_samples, "--force-samples",
                          "-m2", "-M2", "-v", "snps", src, "-Ou"],
                         ["bcftools", "norm", "-d", "exact", "-Oz1", "--threads", args.threads,
                          "-o", ref_vcf])
                run_step("bcftools index (reference)", ["bcftools", "index", "-f", "-t", ref_vcf])

            # hold-out = the DR2 cohort, masked to the patient's typed positions so it carries an
            # array's worth of sites, not full WGS. Unmasked hold-outs would not resemble the
            # patient and the DR2 measured over them would not describe the patient's imputation.
            # -R (index jumps), not -T (stream the whole 2,504-sample file): the same records,
            # byte for byte (checked on chr22), without parsing the chromosome end to end.
            # --regions-overlap pos gives -R exactly -T's rule (a record is kept only if its POS
            # is a typed position), not -R's default of any overlap.
            run_step("bcftools view (hold-out)",
                     ["bcftools", "view", "-S", args.holdout_samples, "--force-samples",
                      "-m2", "-M2", "-v", "snps", "-R", args.typed_positions,
                      "--regions-overlap", "pos", src,
                      "-Oz1", "--threads", args.threads, "-o", hold_vcf])
            run_step("bcftools index (hold-out)", ["bcftools", "index", "-f", "-t", hold_vcf])
            if args.panel_dir:
                renamed = os.path.join(work, "hold.renamed.vcf.gz")
                rename_map = os.path.join(work, "holdout_rename.txt")
                with open(rename_map, "w") as fh:
                    for sample in holdout:
                        fh.write(f"{sample}\t{HOLDOUT_PREFIX}{sample}\n")
                run_step("bcftools reheader (hold-out)",
                         ["bcftools", "reheader", "-s", rename_map, "-o", renamed, hold_vcf])
                run_step("bcftools index (hold-out renamed)",
                         ["bcftools", "index", "-f", "-t", renamed])
                hold_vcf = renamed

            # patient + hold-outs as ONE Beagle target: this is what makes DR2 a measurement.
            merge_chromosome_target(args.target, hold_vcf, c, merged_vcf, args.threads)
            entry["n_target_samples"] = len(vcf_samples(merged_vcf))

            # Keep chrom= explicit even though the merged target now contains only this
            # chromosome. The scoring parameters and sample order are unchanged.
            if args.backend == "gb10":
                if gb10_failed.is_set():
                    raise Failed("gb10", None, f"not sent: the gb10 failed "
                                               f"chr{gb10_failure['chrom']} first")
                try:
                    entry["gate"] = impute_remote.impute_chromosome(
                        args.run, c, merged_vcf, args.holdout_samples, args.seed,
                        cohort_stem + ".vcf.gz", cohort_stem + ".log", threads=args.threads,
                        stop=gb10_failed)
                except impute_remote.RemoteError as exc:
                    entry["gb10_cause"] = exc.cause
                    if exc.cause != "request":
                        with gb10_lock:
                            if not gb10_failed.is_set():
                                gb10_failure.update(chrom=c, cause=exc.cause, reason=str(exc))
                                gb10_failed.set()
                    raise Failed("gb10", None, str(exc)) from exc
                for step, measure in entry["gate"]["steps"].items():
                    _TLS.steps["gb10 " + step] = measure["seconds"]
                # The gb10's Beagle wrote its log where Beagle writes it here, beside the cohort
                # VCF. Its stdout, which chr<N>.beagle.log keeps on this machine, is not kept by
                # the gate, and the log carries the same lines, so chr<N>.beagle.log is built
                # from it.
                with open(cohort_stem + ".log") as fh:
                    beagle_text = fh.read()
                with open(entry["beagle_log"], "w") as fh:
                    fh.write("Beagle ran on the gb10, gate run %s; its log follows.\n\n%s"
                             % (entry["gate"]["run_id"], beagle_text))
                entry["beagle_version"] = next(iter(beagle_text.splitlines()), None)
            else:
                cmd = [java, f"-Xmx{args.xmx}", *args.java_opt, "-jar", BEAGLE,
                       f"gt={merged_vcf}", f"ref={ref_vcf}", f"map={gmap}",
                       f"out={cohort_stem}", f"chrom={c}", f"nthreads={args.threads}",
                       f"seed={args.seed}", "gp=true", "impute=true"]
                if args.panel_dir:
                    cmd.append(f"excludesamples={args.holdout_samples}")
                entry["beagle_command"] = " ".join(cmd)
                t_beagle = time.time()
                proc = subprocess.run(cmd, capture_output=True, text=True)
                timed("beagle", t_beagle)
                with open(entry["beagle_log"], "w") as fh:
                    fh.write(" ".join(cmd) + "\n\n" + proc.stdout + "\n" + proc.stderr)
                if proc.returncode != 0:
                    raise Failed("beagle", proc.returncode, proc.stderr)
                beagle_text = proc.stdout
            # Read the split back off Beagle's own log rather than trusting the arguments: the
            # reference it used must be exactly the reference list, the target exactly
            # patient + hold-outs.
            n_ref, n_study = beagle_sample_counts(beagle_text)
            entry["beagle_reference_samples"] = n_ref
            entry["beagle_study_samples"] = n_study
            if n_ref != len(reference) or n_study != 1 + len(holdout):
                raise Failed("beagle sample counts", 0,
                             f"Beagle reports {n_ref} reference and {n_study} study samples; "
                             f"expected {len(reference)} and {1 + len(holdout)}")
            # Nothing below reads the cohort index; it is for whoever opens the cohort file
            # later. So it runs alongside the patient extraction rather than ahead of it.
            t_cohort_index = time.time()
            cohort_index = subprocess.Popen(
                ["bcftools", "index", "-f", "-t", cohort_stem + ".vcf.gz"],
                stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)

            # The patient-only file takes the plain chr<N>.vcf.gz name: downstream tools read this
            # directory expecting one sample per file.
            # The patient is the first sample of the target (bcftools merge was given --target
            # first, and Beagle keeps target order), so its genotypes are column 10. Cutting
            # that column out of the text stream before bcftools sees it spares bcftools from
            # parsing 301 hold-out columns only to discard them: 58 s -> 13 s on chr22. The
            # same `bcftools view -s <patient>` still runs on the one-sample stream, so the
            # name is still checked (it fails if column 10 is anyone else) and INFO/AC,AN are
            # added exactly as before -- the body is byte-identical. Beagle's output declares
            # no ##contig, which bcftools needs to subset a stream, hence the sed.
            run_chain("bcftools view (patient)",
                      [["bgzip", "-dc", cohort_stem + ".vcf.gz"],
                       ["cut", "-f1-10"],
                       ["sed", f"/^#CHROM/i\\\n##contig=<ID={c}>"],
                       ["bcftools", "view", "-s", args.patient_id, "-Oz", "-o", patient_tmp]])
            run_step("bcftools index (patient)", ["bcftools", "index", "-f", "-t", patient_tmp])

            # DR2/IMP -- estimated across the whole Beagle target. INFO is site-level, so the
            # patient file carries the cohort's DR2 and IMP unchanged (bcftools view -s only
            # adds AC/AN); reading them off the one-sample file gives the identical table
            # without parsing 302 sample columns again (10 s -> 1.5 s on chr22).
            query_gzip("bcftools query (dr2)",
                       ["bcftools", "query", "-f",
                        "%CHROM\t%POS\t%REF\t%ALT\t%INFO/DR2\t%INFO/IMP\n",
                        patient_tmp],
                       dr2_tmp)

            err = cohort_index.communicate()[1]
            timed("bcftools index (cohort, concurrent)", t_cohort_index)
            if cohort_index.returncode != 0:
                raise Failed("bcftools index (cohort)", cohort_index.returncode,
                             (err or b"").decode(errors="replace"))

            markers = n_markers(cohort_stem + ".vcf.gz")
            os.replace(dr2_tmp, dr2_out)
            os.replace(patient_tmp + ".tbi", patient_out + ".tbi")
            os.replace(patient_tmp, patient_out)
            entry.update({"status": "ok", "returncode": 0, "n_markers": markers})
        except (Failed, OSError) as exc:
            if isinstance(exc, Failed):
                entry.update({"status": "FAILED", "failed_step": exc.step,
                              "returncode": exc.returncode, "stderr_tail": exc.stderr[-2000:]})
            else:
                entry.update({"status": "FAILED", "failed_step": "filesystem",
                              "returncode": None, "stderr_tail": str(exc)})
        finally:
            if cohort_index is not None and cohort_index.poll() is None:
                cohort_index.wait()
        if entry.get("status") != "ok":
            # The cohort output of a failed chromosome may be cut short; it is not evidence of
            # anything, and leaving it would look like a finished run's output.
            for path in (cohort_stem + ".vcf.gz", cohort_stem + ".vcf.gz.tbi"):
                if os.path.exists(path):
                    os.remove(path)
        entry["seconds"] = round(time.time() - t0, 1)
        entry["step_seconds"] = dict(_TLS.steps)
        if not args.keep_work:
            shutil.rmtree(work, ignore_errors=True)
        print(f"chr{c}: {entry['status']} ({entry['seconds']}s, "
              f"{markers if markers is not None else 'n/a'} markers)", file=sys.stderr)
        return entry

    chroms = parse_chroms(args.chromosomes)
    # Imputation is most of the score stage. Its start goes in the ledger too: the live page
    # shows a stage from the ledger's first line for it, so without this it would show the score
    # stage only once imputation ended, half an hour on.
    lib_run.ledger_append(args.run, {
        "kind": "facts", "stage": "score", "tool": "impute_joint.py",
        "event": "imputation started", "chromosomes": chroms, "backend": args.backend,
    })
    t_all = time.time()
    if args.parallel_chromosomes == 1:
        runs, at_once = [one(c) for c in chroms], 1
    else:
        # Largest first, so the last chromosomes to start are the short ones and the pool does
        # not end on one long straggler. Each worker runs one whole chromosome at a time. The
        # gb10 route keeps exactly two in flight; a local demo run alone takes a third.
        def size(c):
            path = os.path.join(args.phase3_dir, f"chr{c}.vcf.gz")
            return os.path.getsize(path) if os.path.exists(path) else 0
        order = sorted(chroms, key=size, reverse=True)
        ran, at_once = run_chromosomes(order, one, args.parallel_chromosomes,
                                       demo_may_add=args.backend == "local")
        done = dict(zip(order, ran))
        runs = [done[c] for c in chroms]
    wall_seconds = round(time.time() - t_all, 1)

    result = {
        "tool": "impute_joint.py",
        "backend": args.backend,
        "generated_utc": lib_run.utcnow(),
        "run": args.run,
        "operation": "joint panel-based imputation: patient + hold-out cohort in one Beagle run, "
                     "so DR2 is measured natively over the run that produced the patient dosages. "
                     "NOT reference-AF mean-imputation.",
        "dr2_provenance": "DR2/IMP come from the same Beagle output as the patient dosages; "
                          "nothing is transplanted from a separate run",
        "beagle_version": version_line if java else next(
            (r["beagle_version"] for r in runs if r.get("beagle_version")), None),
        "java": lib_run.tool_version([java, "-version"]) if java else None,
        "java_path": java,
        "bcftools": lib_run.tool_version(["bcftools", "--version"]),
        "patient_id": args.patient_id,
        "target": args.target,
        "holdout_samples": args.holdout_samples,
        "n_holdout_samples": len(holdout),
        "reference_samples": args.reference_samples,
        "n_reference_samples": len(reference),
        "typed_positions": args.typed_positions,
        "phase3_dir": args.phase3_dir,
        "panel_dir": args.panel_dir,
        "reference_route": ("prebuilt all-sample panel, hold-out excluded at read time "
                            "(excludesamples=), hold-outs renamed " + HOLDOUT_PREFIX + "* in the target")
                           if args.panel_dir else "per-run rebuild from phase3 (bcftools view -S)",
        "map_dir": args.map_dir,
        "chromosomes": args.chromosomes,
        "seed": args.seed,
        "seed_is_the_default": args.seed == str(lib_run.DEFAULT_SEED),
        "preflight": {
            "target_single_sample": True,
            "target_sample_is_patient": True,
            "sample_lists_disjoint": True,
            "patient_in_neither_list": True,
        },
        "wall_seconds_all_chromosomes": wall_seconds,
        "parameters": {"threads": args.threads, "xmx": args.xmx, "gp": True, "impute": True,
                       "parallel_chromosomes": args.parallel_chromosomes,
                       "most_chromosomes_at_once": at_once,
                       "java_opts": args.java_opt,
                       "keep_work": args.keep_work, "work_dir": work_root},
        "per_chromosome": runs,
        "n_ok": sum(1 for r in runs if r.get("status") == "ok"),
        "n_failed": sum(1 for r in runs if r.get("status") == "FAILED"),
        "n_skipped": sum(1 for r in runs if str(r.get("status", "")).startswith("SKIPPED")),
    }
    if gb10_failure:
        result["gb10_failure"] = dict(gb10_failure)
    out = os.path.join(args.out_dir, "impute_joint.json")
    lib_run.emit(result, out)
    lib_run.ledger_append(args.run, {
        "kind": "artifact", "stage": "score", "tool": "impute_joint.py",
        "patient_id": args.patient_id, "seed": args.seed,
        "n_ok": result["n_ok"], "n_failed": result["n_failed"],
        "artifact": out,
    })
    if gb10_failure:
        die(f"Beagle on the gb10 failed chr{gb10_failure['chrom']}: {gb10_failure['reason']}. "
            "That is the environment failing, not a result: no other chromosome was sent, and "
            "nothing ran on this machine instead. The score skill says what to do.")


if __name__ == "__main__":
    main()
