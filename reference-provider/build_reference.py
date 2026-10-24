#!/usr/bin/env python3
"""
build_reference.py -- reference distribution provider for the INT-1 interface.

Builds a PRS reference distribution for ONE patient and ONE scoring model by
scoring a genetically-similar slice of a reference panel with the patient's own
resolved score input, and emits the JSON artifact described in CONTRACT.md.

This is a separate provider. It does not import from, read from, or write into
the agent repo. The agent consumes the artifact it did not produce.

Steps:
  1. Join the resolved dosage rows to the harmonized scoring file, for the model's
     header, the typed/imputed split and an optional sum-of-|beta| regression guard,
     and compare that join with the patient's own score input.
  2. Select the k nearest reference samples in PC space, from the basis samples that
     have genotypes in the reference VCF.
  3. Score them with plink2 from the patient's own score input, by the recipe
     score_plink2.py scored the patient with plus --snps-only just-acgt, and count what
     plink2 actually used.
  4. Emit the artifact, with every cohort member's raw score.
"""

import argparse
import collections
import datetime
import gzip
import hashlib
import json
import math
import os
import re
import subprocess
import sys

import numpy as np

# The scoring file's own #genome_build header says hg38. That describes the
# AUTHORS' original build. The hm_chr / hm_pos columns are GRCh37, per the
# #HmPOS_build header. We join on hm_chr / hm_pos and ignore chr_name /
# chr_position and #genome_build entirely.
HM_CHR_COL = "hm_chr"
HM_POS_COL = "hm_pos"
EA_COL = "effect_allele"
# Bound from --effect-weight-col at startup. Only six columns are universal across PGS
# Catalog scoring files -- effect_allele, hm_chr, hm_pos, hm_rsID, hm_source and
# hm_inferOtherAllele -- and the weight column is not one of them, which is why
# match_variants.py and weight_coverage.py both take it as an argument too.
WEIGHT_COL = None

QUANTILES = [1, 5, 10, 25, 50, 75, 90, 95, 99]

# The distance curve is reported in full, ALWAYS, independent of --k. Choosing a
# point on this curve -- the cohort boundary -- is the agent's decision, not this
# tool's. --k governs only which cohort actually gets scored in step 3.
CURVE_RANKS = [1, 10, 25, 50, 100, 200, 300, 500, 750, 1000]
COHORT_SNAPSHOT_KS = [50, 300, 1000]
# rank 1 is included because the d1-vs-d300 contrast is what separates "this
# patient sits at the centre of the cloud" from "this patient sits inside a
# cluster", and cohort_fit_note reports that separation. A claim in the artifact
# must carry the measurement it rests on.
BASELINE_RANKS = [1, 10, 50, 100, 300, 500, 1000]
# k values at which superpopulation purity is reported. why_this_k rests on where
# composition breaks down, so the breakdown is measured across a grid rather than
# read off the three cohort snapshots.
PURITY_KS = [50, 100, 150, 200, 250, 300, 350, 400, 500, 750, 1000]
# The log-log slope is read over a multiplicative window and its peak is scanned,
# not sampled at CURVE_RANKS -- reading a peak off ten sampled ranks would put it
# wherever the grid falls rather than where the curve puts it.
SLOPE_WINDOW = 1.6
SLOPE_SCAN_MAX = 1200

# There are no verdict thresholds. cohort_fit_note used to pick "centrality effect
# present" / "borderline" / "no centrality effect detected" against fixed percentile
# cutoffs (5, 15 and 10), and that verdict decided whether a disclosure was made at all.
# Where to draw those lines is a judgment an expert could make differently, and a tool
# only extracts; the measurements are reported, and every comparative sentence is
# emitted only when the inequality it states holds.


# ----------------------------------------------------------------------------
# small helpers
# ----------------------------------------------------------------------------

# Each reference sample's recorded sex, staged by the harness from the 1000 Genomes panel; read
# only when the request asks for a same-sex comparison group.
SEX_LABELS = "sample_sex.panel"


def die(msg):
    """Hard stop. Used only for conditions that make the artifact untrustworthy.

    A refusal (INT-2, too few similar samples) is NOT one of these -- that path
    writes a valid artifact and exits 0.
    """
    sys.stderr.write("\nFATAL: %s\n" % msg)
    sys.exit(1)


def sha256_of(path):
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def variant_key(chrom, pos):
    """Normalise a coordinate to a single joinable string.

    int(pos) collapses any leading-zero / whitespace difference between the two
    files, so the join cannot silently miss on formatting.
    """
    return "%s:%d" % (str(chrom).strip(), int(str(pos).strip()))


# ----------------------------------------------------------------------------
# STEP 1 -- regenerate the score input
# ----------------------------------------------------------------------------

def read_resolved_positions(dosages_path):
    """Return {variant_key: source} for status == 'resolved' rows only.

    Keeps `source` (typed/imputed) so the imputation disclosure in step 4 is
    computed from the data rather than asserted.
    """
    resolved = {}
    n_rows = 0
    status_counts = collections.Counter()

    with open(dosages_path, "r") as fh:
        header = fh.readline().rstrip("\n").split("\t")
        try:
            i_chrom = header.index("chrom")
            i_pos = header.index("pos")
            i_source = header.index("source")
            i_status = header.index("status")
        except ValueError:
            die("dosages file header missing an expected column; got %r" % (header,))

        for line in fh:
            f = line.rstrip("\n").split("\t")
            n_rows += 1
            status = f[i_status]
            status_counts[status] += 1
            if status != "resolved":
                continue
            key = variant_key(f[i_chrom], f[i_pos])
            if key in resolved:
                die("duplicate resolved position %s in dosages file -- the join key "
                    "is not unique and the weight sum would double-count" % key)
            resolved[key] = f[i_source]

    return resolved, n_rows, status_counts


def build_score_input(scoring_gz, resolved, out_path, expected_abs_beta,
                      expected_imputed_abs_beta):
    """Join the resolved positions to the scoring file and emit 'ID EA BETA'.

    The effect_weight string is copied through VERBATIM -- no float round-trip --
    so the emitted file carries exactly the authors' precision. The verification
    sum is computed separately in float via math.fsum.
    """
    abs_betas = []
    abs_betas_by_source = {"typed": [], "imputed": []}
    n_by_source = collections.Counter()
    matched_keys = set()
    n_scoring_rows = 0

    os.makedirs(os.path.dirname(os.path.abspath(out_path)), exist_ok=True)

    meta_header = {}

    with gzip.open(scoring_gz, "rt") as fh, open(out_path, "w") as out:
        # READ the '#' preamble, do not skip it. pgs_id, the trait and the harmonized
        # build all live there, and the artifact has to describe THIS file rather than
        # assert a model chosen when the tool was written.
        header = None
        for line in fh:
            if line.startswith("#"):
                s = line.lstrip("#").strip()
                if "=" in s:
                    k, v = s.split("=", 1)
                    meta_header[k.strip()] = v.strip()
                continue
            header = line.rstrip("\n").split("\t")
            break
        if header is None:
            die("scoring file has no column header line")

        for col in (HM_CHR_COL, HM_POS_COL, EA_COL, WEIGHT_COL):
            if col not in header:
                die("scoring file header lacks required column %r; got %r" % (col, header))
        i_chr = header.index(HM_CHR_COL)
        i_pos = header.index(HM_POS_COL)
        i_ea = header.index(EA_COL)
        i_w = header.index(WEIGHT_COL)

        out.write("ID EA BETA\n")
        for line in fh:
            f = line.rstrip("\n").split("\t")
            n_scoring_rows += 1
            hc, hp = f[i_chr], f[i_pos]
            if not hc or not hp or hc == "NA" or hp == "NA":
                continue
            key = variant_key(hc, hp)
            src = resolved.get(key)
            if src is None:
                continue
            if key in matched_keys:
                die("scoring file has a duplicate hm_chr:hm_pos at %s -- the join "
                    "is not 1:1 and the weight sum would double-count" % key)
            matched_keys.add(key)

            w_str = f[i_w]
            ea = f[i_ea]
            out.write("%s %s %s\n" % (key, ea, w_str))

            aw = abs(float(w_str))
            abs_betas.append(aw)
            n_by_source[src] += 1
            if src in abs_betas_by_source:
                abs_betas_by_source[src].append(aw)
            else:
                die("dosages row for %s has an unrecognised source %r; the "
                    "typed/imputed split behind the R1 disclosure would be "
                    "incomplete" % (key, src))

    # fsum, not sum(): 1.09M additions accumulate enough float error to move a
    # 4-decimal comparison.
    total_abs_beta = math.fsum(abs_betas)
    typed_abs = math.fsum(abs_betas_by_source["typed"])
    imputed_abs = math.fsum(abs_betas_by_source["imputed"])

    unmatched = set(resolved) - matched_keys

    stats = {
        "scoring_file_header": meta_header,
        "n_variants_emitted": len(matched_keys),
        "n_scoring_file_rows": n_scoring_rows,
        "n_resolved_positions": len(resolved),
        "n_resolved_not_in_scoring_file": len(unmatched),
        "n_variants_typed": n_by_source["typed"],
        "n_variants_imputed": n_by_source["imputed"],
        "sum_abs_beta": total_abs_beta,
        "sum_abs_beta_typed": typed_abs,
        "sum_abs_beta_imputed": imputed_abs,
        "imputed_weight_fraction": (imputed_abs / total_abs_beta) if total_abs_beta else None,
    }

    # The 19.0372 check verifies the TOTAL. It cannot detect an error in the
    # typed/imputed split, and that split is the number the R1 disclosure
    # reports. So it gets its own cross-check against the value supplied for it.
    # Non-fatal: only the total was mandated as a stop condition. But a
    # disagreement is printed loudly and recorded in the artifact rather than
    # quietly overwritten by the expected value.
    stats["expected_sum_abs_beta_imputed"] = expected_imputed_abs_beta
    if expected_imputed_abs_beta is None:
        stats["imputed_split_verification"] = "not checked (no expected value supplied)"
    elif round(imputed_abs, 4) == round(float(expected_imputed_abs_beta), 4):
        stats["imputed_split_verification"] = "agrees with the supplied value"
    else:
        stats["imputed_split_verification"] = (
            "DISAGREES: computed %.4f, supplied %.4f. The artifact reports the "
            "COMPUTED value." % (imputed_abs, float(expected_imputed_abs_beta)))
        sys.stderr.write(
            "\nWARNING: imputed |beta| cross-check disagrees\n"
            "  computed from the data : %.4f\n"
            "  supplied as expected   : %.4f\n"
            "  The artifact will report the computed value, not the supplied one.\n"
            % (imputed_abs, float(expected_imputed_abs_beta)))

    # VERIFICATION, when a target is supplied. This is a REGRESSION GUARD, not a
    # property of the scoring file: build_score_input joins the scoring file against
    # the RESOLVED SET, so the sum changes whenever the resolved set changes. A value
    # therefore belongs to one run of one patient and model, and there is no correct
    # default. Supplied and mismatched -> stop, the join is wrong. Not supplied -> the
    # computed value is recorded and reported as unchecked, never quietly asserted.
    got = round(total_abs_beta, 4)
    stats["sum_abs_beta_rounded"] = got

    if expected_abs_beta is None:
        stats["expected_sum_abs_beta"] = None
        stats["verification_passed"] = None
        stats["verification_note"] = (
            "NOT verified: no --expected-abs-beta supplied, so nothing checked that the "
            "join reproduced a prior run. The computed value is recorded. Pass %.4f on a "
            "rerun of this same patient and model to make the guard meaningful." % got)
        sys.stderr.write(
            "\nNOTE: step 1 join was NOT verified -- no --expected-abs-beta supplied.\n"
            "  computed sum of |BETA| : %.4f\n"
            "  Pass that value on a rerun of the same patient and model to catch a\n"
            "  changed join. It is a regression guard, not a constant.\n" % got)
        return stats

    want = round(float(expected_abs_beta), 4)
    stats["expected_sum_abs_beta"] = want
    stats["verification_passed"] = (got == want)
    if got != want:
        sys.stderr.write(
            "\nSTEP 1 VERIFICATION FAILED\n"
            "  sum of |BETA| over emitted file : %.4f\n"
            "  expected                        : %.4f\n"
            "  difference                      : %+.6f\n"
            "  rows emitted                    : %d\n"
            "  resolved positions              : %d\n"
            "  resolved not found in scoring   : %d\n"
            % (got, want, total_abs_beta - want, len(matched_keys),
               len(resolved), len(unmatched))
        )
        die("join is wrong -- refusing to proceed with a mismatched variant set")

    return stats


# ----------------------------------------------------------------------------
# STEP 2 -- select the reference cohort
# ----------------------------------------------------------------------------

def load_eigenvec(path, n_pcs):
    """Return (iids, coords[n_samples, n_pcs])."""
    iids = []
    rows = []
    with open(path, "r") as fh:
        header = fh.readline().lstrip("#").rstrip("\n").split("\t")
        # tolerate FID/IID or IID-only layouts
        if header[0] == "FID":
            i_iid, first_pc = 1, 2
        else:
            i_iid, first_pc = 0, 1
        n_avail = len(header) - first_pc
        if n_avail < n_pcs:
            die("eigenvec has %d PCs but the projection used %d" % (n_avail, n_pcs))
        for line in fh:
            f = line.rstrip("\n").split("\t")
            iids.append(f[i_iid])
            rows.append([float(x) for x in f[first_pc:first_pc + n_pcs]])
    return iids, np.asarray(rows, dtype=float)


def load_eigenval(path, n_pcs):
    """Eigenvalues of the reference PCA.

    Not used in any distance. Loaded so ancestry_grouping_definition can report how
    unevenly the variance is spread across the PCs it weights equally -- a property
    of the distance rule this provider is built on, and one a reader needs in order
    to know how much to make of which samples land either side of the k-th
    neighbour.
    """
    try:
        with open(path, "r") as fh:
            vals = [float(x) for x in fh if x.strip()]
    except (IOError, OSError, ValueError):
        return None
    return vals[:n_pcs] or None


def counts_text(counts):
    """{'EUR': 250, 'AMR': 12} -> 'EUR 250, AMR 12', largest first."""
    return ", ".join("%s %d" % (g, n) for g, n in
                     sorted(counts.items(), key=lambda kv: (-kv[1], kv[0])))


def load_sample_labels(path):
    """Return {sample: (pop, super_pop)} from the PCA basis's own sample_labels.json.

    These are the labels ancestry.py wrote beside the basis and counted the agent's
    nearest_reference_samples with, so they are the only labels under which the
    self-check can reproduce that result. A sample with a null label is left out
    here and caught by the every-sample-labelled check in main().
    """
    try:
        with open(path, "r") as fh:
            raw = json.load(fh)
    except (OSError, ValueError) as exc:
        die("sample labels unreadable at %s: %s" % (path, exc))
    labels = {}
    for s, v in raw.items():
        v = v or {}
        if v.get("pop") is not None and v.get("super_pop") is not None:
            labels[s] = (v["pop"], v["super_pop"])
    return labels


def load_sex_labels(path):
    """{sample: "female" | "male"} from a tab-separated panel whose header names a `sample`
    column and a `sex` or `gender` one -- the 1000 Genomes panel records it as gender. A sample
    with anything else there has no recorded sex, and is left out of a same-sex group."""
    try:
        with open(path) as fh:
            header = [h.strip().lower() for h in fh.readline().rstrip("\n").split("\t")]
            column = next((header.index(h) for h in ("sex", "gender") if h in header), None)
            if "sample" not in header or column is None:
                die("%s names no sample and sex (or gender) columns" % path)
            sexes = {}
            for line in fh:
                f = line.rstrip("\n").split("\t")
                if len(f) > column and f[column].strip().lower() in ("female", "male"):
                    sexes[f[header.index("sample")]] = f[column].strip().lower()
    except OSError as exc:
        die("sex labels unreadable at %s: %s" % (path, exc))
    return sexes


def same_sex_pool(iids, keep, wanted, sexes):
    """The candidate pool cut to the samples recorded as `wanted`: their indices into iids, and
    what the cut left out, for the artifact to state."""
    same = [i for i in keep if sexes.get(iids[i]) == wanted]
    other = sum(1 for i in keep if sexes.get(iids[i]) not in (None, wanted))
    return same, {"comparison_group_sex": wanted, "n_before": len(keep), "n_of_that_sex": len(same),
                  "n_excluded_other_sex": other,
                  "n_excluded_without_a_recorded_sex": len(keep) - len(same) - other}


def vcf_header_facts(path):
    """What the reference VCF says about itself, read off its own header.

    The ##fileformat/##fileDate/##source/##reference lines, the FORMAT fields it
    declares, and its samples in file order. Nothing is inferred: a line the header
    does not carry is absent here.
    """
    opener = gzip.open if path.endswith(".gz") else open
    meta, fmt = collections.OrderedDict(), []
    with opener(path, "rt") as fh:
        for line in fh:
            if line.startswith("##"):
                s = line[2:].rstrip("\n")
                k, _, v = s.partition("=")
                if k in ("fileformat", "fileDate", "source", "reference") and k not in meta:
                    meta[k] = v
                elif k == "FORMAT" and "ID=" in v:
                    fmt.append(v.split("ID=", 1)[1].split(",", 1)[0])
                continue
            if line.startswith("#CHROM"):
                return {"header_lines": meta, "format_ids": fmt,
                        "samples": line.rstrip("\n").split("\t")[9:]}
            break
    die("reference VCF %s has no #CHROM header line" % path)


def counts_for(iids, labels, idx):
    """Population and superpopulation counts for a set of row indices."""
    pop = collections.Counter()
    sup = collections.Counter()
    for i in idx:
        p, s = labels.get(iids[i], ("UNKNOWN", "UNKNOWN"))
        pop[p] += 1
        sup[s] += 1
    return dict(pop), dict(sup)


def neighbour_selfcheck(iids, coords, labels, patient_vecs, projection):
    """Reproduce the agent's own recorded k=50 nearest-neighbour result.

    Purpose: confirm our coordinates live in the SAME decomposition as the
    eigenvec -- same axis order, sign and scale. If neither patient vector
    reproduces the agent's result, the spaces do not match and every distance
    downstream is meaningless. Stop hard rather than reconcile.
    """
    recorded = projection.get("nearest_reference_samples") or {}
    k_rec = recorded.get("k")
    want_pop = recorded.get("by_population")
    want_sup = recorded.get("by_superpopulation")
    if not (k_rec and want_pop):
        return {"performed": False,
                "note": "projection.json carries no nearest_reference_samples block to check against"}

    results = {}
    for name, vec in patient_vecs.items():
        d = np.linalg.norm(coords - vec, axis=1)
        order = np.argsort(d, kind="stable")[:k_rec]
        got_pop, got_sup = counts_for(iids, labels, order)
        # A tie straddling the k-th boundary would make the comparison
        # order-dependent rather than wrong. Detect and say so.
        ds = np.sort(d, kind="stable")
        boundary_tie = bool(k_rec < len(ds) and ds[k_rec - 1] == ds[k_rec])
        results[name] = {
            "matches": (got_pop == want_pop and
                        (want_sup is None or got_sup == want_sup)),
            "by_population": got_pop,
            "by_superpopulation": got_sup,
            "boundary_tie_at_k": boundary_tie,
        }

    any_match = any(r["matches"] for r in results.values())
    return {
        "performed": True,
        "k_checked": k_rec,
        "recorded_by_population": want_pop,
        "recorded_by_superpopulation": want_sup,
        "per_vector": results,
        "any_vector_reproduces_agent_result": any_match,
    }


def distance_curve(d_sorted, n_available):
    """Distance to the r-th nearest neighbour across CURVE_RANKS, 1-indexed.

    Reported regardless of --k. The shape of this curve is what says how well the
    panel fits the patient; a single k hides it.
    """
    return collections.OrderedDict(
        (r, float(d_sorted[r - 1])) for r in CURVE_RANKS if r <= n_available)


def cohort_snapshots(iids, labels, order, n_available):
    """Population / superpopulation composition at several candidate cohort sizes."""
    out = collections.OrderedDict()
    for kk in COHORT_SNAPSHOT_KS:
        if kk > n_available:
            continue
        pop, sup = counts_for(iids, labels, order[:kk])
        out[kk] = {"population_counts": pop, "superpopulation_counts": sup}
    return out


def reference_baseline(coords, ranks, chunk=128):
    """For every reference sample, its distance to its OWN r-th nearest neighbour
    within the reference set, excluding itself.

    This is the scale the patient's curve is read against: what "close" looks like
    for someone who genuinely belongs to this panel. Without it the patient's
    distances are bare numbers with no reference frame.

    Computed by direct chunked subtraction rather than the Gram-matrix identity,
    which loses precision at small distances -- and small distances are exactly
    what the low ranks measure.
    """
    n = coords.shape[0]
    out = {r: np.empty(n, dtype=float) for r in ranks}
    for start in range(0, n, chunk):
        stop = min(start + chunk, n)
        block = np.linalg.norm(
            coords[start:stop, None, :] - coords[None, :, :], axis=2)
        for i in range(start, stop):
            block[i - start, i] = np.inf   # exclude self
        block.sort(axis=1)
        for r in ranks:
            if r <= n - 1:
                out[r][start:stop] = block[:, r - 1]
            else:
                out[r][start:stop] = np.nan
    return out


def percentile_of(value, arr):
    """Where `value` falls in `arr`, as a percentile. 0 = closer than everyone."""
    finite = arr[np.isfinite(arr)]
    if not len(finite):
        return None
    return float(100.0 * np.mean(finite < value))


def _log_log_slope(d_sorted, r, n_available, window=SLOPE_WINDOW):
    """d(log distance)/d(log rank) at rank r, over a multiplicative window."""
    lo, hi = max(2, int(r / window)), min(n_available, int(r * window))
    if hi <= lo or d_sorted[lo - 1] <= 0:
        return None
    return float((math.log(d_sorted[hi - 1]) - math.log(d_sorted[lo - 1]))
                 / (math.log(hi) - math.log(lo)))


def curve_diagnostics(d_sorted, sup_sorted, n_available, k):
    """Everything why_this_k rests on: where the distance curve says the local
    neighbourhood runs out, and where composition says the same.

    The local log-log slope is the elbow test. A uniformly filled d-dimensional
    cloud gives a constant 1/d, so the only "elbow" available to read is a
    departure from constant -- and the rank where that departure peaks is the rank
    at which the curve is climbing fastest, i.e. where the local neighbourhood is
    being exhausted. Scanned rather than sampled, because reading the peak off ten
    curve ranks would put it wherever the sampling grid happens to fall.
    """
    out = collections.OrderedDict()

    slope = collections.OrderedDict()
    for r in CURVE_RANKS:
        s = _log_log_slope(d_sorted, r, n_available)
        if s is not None:
            slope["d%d" % r] = s
    out["log_log_slope"] = slope
    out["log_log_slope_definition"] = (
        "d(log distance)/d(log rank), over a %.1fx window either side of each rank. "
        "Constant means a uniformly filled cloud with no boundary; the value is "
        "1/(local dimension). A peak marks the rank where distance is climbing "
        "fastest, which is where the local neighbourhood runs out."
        % SLOPE_WINDOW)

    # Locate the peak by a fine scan, and report how wide it is. A narrow band is a
    # boundary you can read; a band spanning most of the curve is not.
    scan_hi = min(n_available - 1, SLOPE_SCAN_MAX)
    scanned = [(r, _log_log_slope(d_sorted, r, n_available))
               for r in range(5, scan_hi)]
    scanned = [(r, s) for r, s in scanned if s is not None]
    if scanned:
        peak_r, peak_s = max(scanned, key=lambda rs: rs[1])
        band = [r for r, s in scanned if s >= 0.95 * peak_s]
        out["slope_peak"] = {
            "rank": peak_r,
            "slope": peak_s,
            "band_within_95pct_of_peak": [min(band), max(band)],
            "scan_range": [5, scan_hi],
            "note": ("The rank at which the distance curve climbs fastest, and the "
                     "range of ranks within 5%% of that peak. Scanned over ranks "
                     "5-%d; a peak at the scan edge would be an edge effect and not "
                     "a feature." % scan_hi),
        }

    # Superpopulation purity across k, and the last k at which the cohort is a
    # single group. This is the axis k was actually chosen on.
    purity = collections.OrderedDict()
    for kk in PURITY_KS:
        if kk > n_available:
            continue
        c = collections.Counter(sup_sorted[:kk])
        top, n_top = c.most_common(1)[0]
        purity[kk] = {"largest_superpopulation": top, "n": int(n_top),
                      "pct": round(100.0 * n_top / kk, 1),
                      "n_other": int(kk - n_top)}
    out["superpopulation_purity_by_k"] = purity
    first_other = next((i + 1 for i, g in enumerate(sup_sorted) if g != sup_sorted[0]),
                       None)
    out["superpopulation_at_rank_1"] = sup_sorted[0] if sup_sorted else None
    out["last_k_single_superpopulation"] = (first_other - 1) if first_other else n_available
    out["first_rank_of_a_second_superpopulation"] = first_other

    # Boundary degeneracy. If the k-th and (k+1)-th neighbours are separated by far
    # less than the typical local gap, then which samples land inside the cohort is
    # a property of where an integer k cuts the ordering, not of the patient.
    if 1 <= k < n_available:
        lo, hi = max(0, k - 50), min(n_available, k + 50)
        gaps = np.diff(d_sorted[lo:hi])
        out["boundary_degeneracy"] = {
            "d_k": float(d_sorted[k - 1]),
            "d_k_plus_1": float(d_sorted[k]),
            "gap_at_boundary": float(d_sorted[k] - d_sorted[k - 1]),
            "median_gap_within_50_ranks": float(np.median(gaps)) if len(gaps) else None,
            "note": ("A boundary gap at or below the local median gap means cohort "
                     "membership at the margin is set by the choice of an integer k, "
                     "not by any separation in the data."),
        }
    return out


def metric_sensitivity(coords, patient_vec, order, k, sup_arr, src, eigenvals):
    """How much of the neighbour ordering is decided by PCs that carry little
    variance, and does the cohort's superpopulation mixture survive weighting them
    down?

    This exists because the artifact reports a cohort composition, and a reader
    will take a count like "AFR 3" for a statement about the patient unless told
    what produced it. Euclidean distance over plink eigenvectors weights every PC
    equally -- the columns are all normalised to the same variance -- so PCs that
    hold a few percent of the eigenvalue mass get the same say as the ones that
    separate continental groups. Whether that matters is measurable, so it is
    measured rather than left as a caveat.
    """
    if not eigenvals or len(eigenvals) != coords.shape[1]:
        return None
    ev = np.asarray(eigenvals, dtype=float)
    total = float(ev.sum())
    if total <= 0:
        return None

    # How many leading PCs it takes to hold TOP_SHARE of the eigenvalue mass.
    TOP_SHARE = 0.85
    cum = np.cumsum(ev) / total
    n_top = int(np.searchsorted(cum, TOP_SHARE) + 1)

    sq = (coords - patient_vec) ** 2
    tail_share = sq[:, n_top:].sum(axis=1) / sq.sum(axis=1)
    sel = order[:k]

    out = collections.OrderedDict()
    out["definition"] = (
        "Distances here are Euclidean over %d plink2 eigenvector columns, which are "
        "all normalised to the same variance, so every PC carries equal weight in "
        "the ordering regardless of how much variance it explains."
        % coords.shape[1])
    out["n_leading_pcs_holding_%d_pct_of_eigenvalue_mass" % int(100 * TOP_SHARE)] = n_top
    out["leading_pc_eigenvalue_share"] = round(float(cum[n_top - 1]), 4)
    out["trailing_pc_share_of_squared_distance_panel_wide"] = round(
        float(np.median(tail_share)), 4)
    out["trailing_pc_share_of_squared_distance_within_cohort"] = round(
        float(np.median(tail_share[sel])), 4)
    out["share_note"] = (
        "Median share of squared distance contributed by PC%d and above, panel-wide and "
        "within the selected cohort; the leading PCs contribute the rest%s."
        % (n_top + 1, _share_comparison(out, n_top)))

    # Does the mixture survive weighting PCs by the variance they actually explain?
    dw = np.linalg.norm((coords - patient_vec) * np.sqrt(ev), axis=1)
    sel_w = np.argsort(dw, kind="stable")[:k]
    counts_w = collections.Counter(sup_arr[sel_w])
    off_src = [i for i in sel if sup_arr[i] != src]
    retained = [i for i in off_src if i in set(sel_w)]
    out["eigenvalue_weighted_recheck"] = {
        "definition": ("the same k nearest neighbours, with each PC scaled by the "
                       "square root of its eigenvalue so the ordering is weighted by "
                       "the variance each PC explains"),
        "superpopulation_counts": dict(counts_w),
        "n_outside_source_group_under_equal_weighting": len(off_src),
        "n_of_those_still_selected_under_eigenvalue_weighting": len(retained),
        "note": ("A count that survives both weightings is a fact about the patient's "
                 "position. One that does not is a fact about the metric."),
    }
    return out


def select_cohort(iids, coords, labels, patient_vec, k, min_cohort, max_kth_distance,
                  eigenvals=None):
    d = np.linalg.norm(coords - patient_vec, axis=1)
    order = np.argsort(d, kind="stable")
    n_available = len(iids)
    n_selected = min(k, n_available)
    sel = order[:n_selected]
    sel_d = d[sel]

    def rank_distance(r):
        """Distance to the r-th nearest neighbour, 1-indexed. None if r > n."""
        return float(d[order[r - 1]]) if r <= n_available else None

    pop_counts, sup_counts = counts_for(iids, labels, sel)

    d_sorted = d[order]
    sup_sorted = [labels.get(iids[i], ("UNKNOWN", "UNKNOWN"))[1] for i in order]
    info = {
        "n_available": n_available,
        "n_selected": int(n_selected),
        "k_requested": k,
        "neighbour_distances": {
            "d1": rank_distance(1),
            "d10": rank_distance(10),
            "d50": rank_distance(50),
            "dk": float(sel_d[-1]) if n_selected else None,
            "k_for_dk": int(n_selected),
        },
        # reported independent of --k; the boundary is the agent's call
        "distance_curve": distance_curve(d_sorted, n_available),
        "curve_diagnostics": curve_diagnostics(d_sorted, sup_sorted, n_available,
                                               int(n_selected)),
        "cohort_snapshots": cohort_snapshots(iids, labels, order, n_available),
        "population_counts": pop_counts,
        "superpopulation_counts": sup_counts,
        "selected_samples": [iids[i] for i in sel],
        "selected_distances": [float(x) for x in sel_d],
    }

    # The group the cohort is drawn from -- a property of the SELECTED SET, not a
    # label for the patient. It exists so the fit assessment reads the patient's
    # curve against the samples the cohort is actually made of, and so the metric
    # check below knows which members are the marginal ones.
    info["cohort_source_group"] = (max(sup_counts, key=sup_counts.get)
                                   if sup_counts else None)
    sup_arr = np.array([labels.get(i, ("UNKNOWN", "UNKNOWN"))[1] for i in iids])
    info["metric_sensitivity"] = metric_sensitivity(
        coords, patient_vec, order, int(n_selected), sup_arr,
        info["cohort_source_group"], eigenvals)

    # INT-2 refusal conditions. Both are correct outcomes, not errors.
    #
    # The reason string must name the ACTUAL cause. A cohort can fall short of
    # --min-cohort for two quite different reasons, and conflating them would
    # put a false claim about the reference panel into a clinical artifact:
    #   (a) the panel genuinely holds fewer samples than the threshold, or
    #   (b) the panel is large but --k asked for fewer than --min-cohort.
    # Only (a) is "too few similar participants exist".
    refusals = []
    if n_selected < min_cohort:
        if n_available < min_cohort:
            refusals.append(
                "the reference panel holds only %d samples in total, below the "
                "--min-cohort threshold of %d; too few participants to form a "
                "stable distribution" % (n_available, min_cohort))
        else:
            refusals.append(
                "cohort of %d is below the --min-cohort threshold of %d because "
                "--k was set to %d. %d reference samples were available; this is a "
                "configuration shortfall, NOT a finding that too few genetically "
                "similar participants exist"
                % (n_selected, min_cohort, k, n_available))
    dk = info["neighbour_distances"]["dk"]
    if max_kth_distance is not None and dk is not None and dk > max_kth_distance:
        refusals.append(
            "distance to the %dth nearest neighbour is %.6f, beyond the "
            "--max-kth-distance limit of %.6f; the nearest available reference "
            "samples are not genetically similar enough to this patient"
            % (n_selected, dk, max_kth_distance))
    info["refusals"] = refusals
    return info


# ----------------------------------------------------------------------------
# STEP 3 -- score the cohort with plink2
# ----------------------------------------------------------------------------

def run_plink2(plink2, argv, label):
    """Run plink2, echo the interesting log lines, die on failure."""
    cmd = [plink2] + argv
    print("    $ %s" % " ".join(cmd))
    proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                          universal_newlines=True)
    if proc.returncode != 0:
        sys.stderr.write(proc.stdout[-4000:] if proc.stdout else "")
        die("plink2 failed during %s (exit %d)" % (label, proc.returncode))
    return proc.stdout or ""


def read_score_input(path):
    """The patient's own plink score input: a header line, then ID, EA, BETA.

    Read exactly as plink2 is told to read it (`1 2 3 header`), whitespace split, so
    a tab-separated file and a space-separated one parse the same.
    -> ([ids in file order], {id: effect allele}, {id: |beta|})
    """
    ids, ea, abs_beta = [], {}, {}
    with open(path, "r") as fh:
        if not fh.readline().split():
            die("score input %s has no header line" % path)
        for line in fh:
            f = line.split()
            if not f:
                continue
            if len(f) < 3:
                die("score input %s has a row with fewer than 3 columns: %r" % (path, line))
            ids.append(f[0])
            ea[f[0]] = f[1]
            abs_beta[f[0]] = abs(float(f[2]))
    return ids, ea, abs_beta


def compare_score_inputs(rejoined_path, patient_ea):
    """Step 1's join against the patient's own score input, by variant ID.

    Counts only. A differing effect allele at the same ID is what a strand flip made
    at matching looks like: the join copies the scoring file's allele verbatim, the
    score input carries the allele actually used.
    """
    rj_ea = {}
    with open(rejoined_path, "r") as fh:
        fh.readline()
        for line in fh:
            f = line.split()
            if f:
                rj_ea[f[0]] = f[1]
    both = set(rj_ea) & set(patient_ea)
    return {
        "n_in_step1_join": len(rj_ea),
        "n_in_patient_score_input": len(patient_ea),
        "n_in_both": len(both),
        "n_only_in_step1_join": len(set(rj_ea) - both),
        "n_only_in_patient_score_input": len(set(patient_ea) - both),
        "n_in_both_with_a_different_effect_allele": sum(
            1 for k in both if rj_ea[k] != patient_ea[k]),
        "note": ("Step 1 joins the dosages file's resolved rows to the scoring file and is "
                 "what the typed/imputed split and the |beta| sums are computed over. The "
                 "cohort is scored from the patient's score input, not from this join, so "
                 "any difference between the two sets is reported here rather than "
                 "reconciled."),
    }


def score_cohort(plink2, ref_vcf, score_input, keep_path, sample_ids, out_prefix):
    """Score the cohort by the recipe score_plink2.py scores the patient with.

    Same score input file, same variant IDs (@:#), the same duplicate and multiallelic
    exclusions at import, the same `--score ... 1 2 3 header cols=+scoresums,+denom
    no-mean-imputation`. The additions are --keep, which selects the cohort; list-variants,
    which makes plink2 name the variants it actually used so the parity count is plink2's own
    rather than an inference; and --snps-only just-acgt. The patient's dosages come from an
    SNV-only imputation panel, but this panel is cut from phase3 at the model's positions and
    can carry an indel where the patient has an SNV. Both then get the same @:# ID and
    --rm-dup drops both, leaving the SNV unscored for the cohort: 21 of 1,093,407 on one dev
    run. plink2 applies --snps-only at load, before --rm-dup, so the SNV survives.
    """
    with open(keep_path, "w") as fh:
        fh.write("#IID\n")
        for s in sample_ids:
            fh.write("%s\n" % s)

    log = run_plink2(plink2, ["--vcf", ref_vcf,
                              "--snps-only", "just-acgt",
                              "--set-all-var-ids", "@:#", "--rm-dup", "exclude-all",
                              "--import-max-alleles", "2",
                              "--keep", keep_path,
                              "--score", score_input, "1", "2", "3", "header",
                              "cols=+scoresums,+denom", "no-mean-imputation",
                              "list-variants",
                              "--out", out_prefix], "scoring")

    # plink2's own count of what it actually used -- authoritative, not inferred
    n_processed = None
    for line in log.splitlines():
        s = line.strip()
        if s.startswith("--score:") and "variants processed" in s:
            for tok in s.split():
                if tok.isdigit():
                    n_processed = int(tok)
                    break
    # plink2's own account of what it imported, dropped and used. Column 0 only: the
    # indented lines are the echo of the options, and "--x: Nk variants ..." is progress.
    # plink2 wraps a long warning onto unindented continuation lines, which are rejoined.
    log_lines, prev = [], None
    for ln in log.splitlines():
        if ln.startswith(("--vcf:", "--score:", "--rm-dup", "Note:", "Warning")):
            prev = None if re.match(r"--\S+: \d+k variants ", ln) else ln.strip()
            if prev:
                log_lines.append(prev)
        elif prev and prev.startswith("Warning") and ln.strip() and not ln.startswith(" "):
            log_lines[-1] = prev = prev + " " + ln.strip()
        else:
            prev = None
    used = []
    if os.path.exists(out_prefix + ".sscore.vars"):
        with open(out_prefix + ".sscore.vars") as fh:
            used = [ln.strip() for ln in fh if ln.strip()]
    return log, n_processed, used, log_lines


def read_sscore(path):
    """-> (iids, {column: np.array})"""
    iids, cols, data = [], None, collections.defaultdict(list)
    with open(path, "r") as fh:
        header = fh.readline().lstrip("#").rstrip("\n").split("\t")
        cols = {n: i for i, n in enumerate(header)}
        if "IID" not in cols:
            die("%s has no IID column; got %r" % (path, header))
        for line in fh:
            f = line.rstrip("\n").split("\t")
            iids.append(f[cols["IID"]])
            for name, i in cols.items():
                if name == "IID":
                    continue
                try:
                    data[name].append(float(f[i]))
                except ValueError:
                    pass
    return iids, {k: np.asarray(v) for k, v in data.items() if len(v) == len(iids)}


# ----------------------------------------------------------------------------
# STEP 4 -- emit the artifact
# ----------------------------------------------------------------------------

def summarise(scores):
    if scores is None or not len(scores):
        return dict([("p%02d" % q, None) for q in QUANTILES] +
                    [("mean", None), ("sd", None), ("min", None),
                     ("max", None), ("n", 0)])
    out = collections.OrderedDict()
    for q in QUANTILES:
        out["p%02d" % q] = float(np.percentile(scores, q))
    out["mean"] = float(np.mean(scores))
    out["sd"] = float(np.std(scores, ddof=1)) if len(scores) > 1 else None
    out["min"] = float(np.min(scores))
    out["max"] = float(np.max(scores))
    out["n"] = int(len(scores))
    return out


def _fit_assessment(cohort):
    """The centrality and curve-shape measurements, as prose that asserts only what the
    numbers show.

    It reaches no verdict. An earlier version picked one against fixed cutoffs, and before
    that one fixed argument had numbers substituted into it; both put a conclusion in the
    tool. Each comparative clause here is emitted only when the inequality it states holds
    in this run's numbers.
    """
    cen = cohort["centrality"]
    curve = cohort["distance_curve"]
    pvb = cohort["patient_vs_baseline"]
    panel_base = cohort["reference_baseline"]
    by_sup = cohort["reference_baseline_by_superpopulation"]
    pct_by_sup = cohort["patient_vs_baseline_by_superpopulation"]
    shape = cohort["curve_shape"]
    src = cohort["cohort_source_group"]

    pct_central = cen["pct_of_panel_closer_to_centroid"]
    pct_central_src = cen.get("pct_of_source_group_closer_to_centroid")
    src_n = cohort["superpopulation_counts"].get(src, 0)
    shape_src = shape["by_superpopulation"].get(src, {})
    shape_pct = shape_src.get("patient_percentile")

    def rel(a, b):
        return "below" if a < b else ("above" if a > b else "equal to")

    # --- part 1: what is measured, and centrality -----------------------------
    parts = [
        "Two things can make a selected cohort look closer than it should: the patient "
        "may sit near the centre of the reference cloud, where everything is near, or "
        "inside a dense cluster, where the closeness is real. The measurements below bear "
        "on which; this tool reports them and reaches no verdict.",
        "Centrality: the patient's distance to the panel centroid is %.6f, %s the panel "
        "median of %.6f (5th percentile %.6f, 95th %.6f). %.1f%% of the %d "
        "reference samples are closer to the centroid than the patient%s."
        % (cen["patient_distance_to_centroid"],
           rel(cen["patient_distance_to_centroid"],
               cen["panel_median_distance_to_centroid"]),
           cen["panel_median_distance_to_centroid"],
           cen["panel_p5_distance_to_centroid"],
           cen["panel_p95_distance_to_centroid"],
           pct_central, cohort["n_available"],
           ("" if pct_central_src is None else
            ", and %.1f%% of %s -- the group supplying %d of the %d selected "
            "neighbours -- are" % (pct_central_src, src, src_n, cohort["n_selected"]))),
    ]

    # --- part 2: the rank profile, and the baseline that makes it readable -----
    d1_meds = {g: by_sup[g]["d1"] for g in by_sup}
    tightest = min(d1_meds, key=d1_meds.get)
    loosest = max(d1_meds, key=d1_meds.get)
    p1 = pvb.get(1, {}).get("percentile_vs_panel")
    p300 = pvb.get(300, {}).get("percentile_vs_panel")
    src_pct = pct_by_sup.get(src, {})
    s1_, s300 = src_pct.get("d1"), src_pct.get("d300")

    flattening = p1 is not None and p300 is not None and p300 < p1
    mixture = (panel_base[1]["median"] < by_sup[src]["d1"]
               and panel_base[300]["median"] > by_sup[src]["d300"])
    src_drift_smaller = (flattening and s1_ is not None and s300 is not None
                         and abs(s1_ - s300) < abs(p1 - p300))

    if p1 is not None and p300 is not None:
        txt = ("Rank profile, against the whole panel: the patient runs from the %.1fth "
               "percentile at d1 (distance %.6f) to the %.1fth at d300 (%.6f)."
               % (p1, curve.get(1), p300, curve.get(300)))
        if flattening:
            txt += " The percentile falls from d1 to d300."
        txt += (" Across groups the median d1 spans %.6f (%s) to %.6f (%s); the "
                "all-panel median d1 of %.6f is %s %s's %.6f and its median d300 of "
                "%.6f is %s %s's %.6f."
                % (d1_meds[tightest], tightest, d1_meds[loosest], loosest,
                   panel_base[1]["median"], rel(panel_base[1]["median"], by_sup[src]["d1"]),
                   src, by_sup[src]["d1"],
                   panel_base[300]["median"],
                   rel(panel_base[300]["median"], by_sup[src]["d300"]),
                   src, by_sup[src]["d300"]))
        if mixture:
            txt += (" A sample at %s's own median distances would therefore rank above "
                    "the all-panel median at d1 and below it at d300." % src)
        parts.append(txt)

    have = [(r, src_pct["d%d" % r]) for r in BASELINE_RANKS
            if src_pct.get("d%d" % r) is not None]
    prof = ", ".join("%.1fth at d%d" % (v, r) for r, v in have)
    if s1_ is not None and s300 is not None and p1 is not None and p300 is not None:
        tight_r, tight_v = min(have, key=lambda rv: rv[1])
        txt = ("Against %s alone -- the baseline the cohort is drawn from -- the patient "
               "is the %.1fth percentile at d1 and the %.1fth at d300, a move of %.1f "
               "points where the all-panel baseline showed %.1f%s. Full profile: %s. "
               "The patient's lowest percentile against %s is the %.1fth, at d%d."
               % (src, s1_, s300, abs(s1_ - s300), abs(p1 - p300),
                  ", so the drift is smaller against the group than against the panel"
                  if src_drift_smaller else "",
                  prof, src, tight_v, tight_r))
        if tight_r < 300 and s300 > tight_v:
            txt += " After that rank the percentile rises again, to the %.1fth at d300." % s300
        parts.append(txt)
    else:
        parts.append("Against %s alone the profile is: %s." % (src, prof))

    # --- part 3: the scale-free statistic -------------------------------------
    if shape.get("patient") is not None and shape_pct is not None:
        parts.append(
            "Scale-free statistic, %s -- a group being uniformly denser or sparser than "
            "the panel does not move it: the patient's is %.3f, %s the %s median of %.3f; "
            "%.1f%% of %s members have a smaller value than the patient's."
            % (shape["definition"], shape["patient"],
               rel(shape["patient"], shape_src.get("median")), src,
               shape_src.get("median"), shape_pct, src))
    else:
        parts.append("The scale-free statistic (%s) was not computed for %s."
                     % (shape["definition"], src))

    # --- part 4: guard the no-label stance ------------------------------------
    parts.append(
        "Naming %s here is a statement about which reference samples the cohort is "
        "built from, not a group assignment for the patient.%s See "
        "ancestry_grouping_definition.why_no_label."
        % (src, " It is named because the patient's percentile drifts less across ranks "
                "against %s than against the all-panel baseline." % src
           if src_drift_smaller else ""))

    return " ".join(parts)


def _why_this_k(cohort, args):
    """What the curve and composition measurements say about the k the provider chose.

    It does not say why k was chosen. That reason is the provider's and is recorded
    verbatim from --k-rationale beside this text; the previous version asserted "k was
    chosen on COHORT COMPOSITION" in every branch, which this tool cannot know. Every
    comparative clause is emitted only when the inequality it states holds in this
    run's numbers.
    """
    diag = cohort["curve_diagnostics"]
    src = cohort["cohort_source_group"]
    k = cohort["k_requested"]
    n_sel = cohort["n_selected"]
    n_src = cohort["superpopulation_counts"].get(src, 0)
    purity = diag["superpopulation_purity_by_k"]
    slope = diag["log_log_slope"]
    peak = diag.get("slope_peak")
    bd = diag.get("boundary_degeneracy") or {}
    pure_k = diag["last_k_single_superpopulation"]
    first_other = diag["first_rank_of_a_second_superpopulation"]
    first_sup = diag.get("superpopulation_at_rank_1")

    traj = ", ".join("%.1f%% at k=%d" % (purity[kk]["pct"], kk)
                     for kk in (350, 500, 1000) if kk in purity)

    parts = ["What the measurements show about k=%d. Why k was chosen is the "
             "provider's statement, recorded in k_rationale_stated_by_provider; this "
             "text does not supply one." % k]
    band = None
    if peak:
        band = peak["band_within_95pct_of_peak"]
        at_edge = peak["rank"] in (peak["scan_range"][0], peak["scan_range"][1] - 1)
        d10 = slope.get("d10")
        parts.append(
            "The distance curve's log-log slope %s a peak of %.3f at rank %d%s. Ranks "
            "%d-%d are within 5%% of that peak. Whether the patient's position compresses "
            "this curve is not judged here; cohort_fit_note carries the centrality and "
            "curve-shape measurements that bear on it."
            % (("rises from %.3f at d10 to" % d10) if d10 is not None and d10 < peak["slope"]
               else "reaches",
               peak["slope"], peak["rank"],
               " -- at the edge of the scanned range" if at_edge else
               ", the rank where distance climbs fastest in the scanned range",
               band[0], band[1]))
    else:
        parts.append("No slope peak could be located in the distance curve.")

    if first_other is None:
        parts.append("Every measured rank is %s; no second superpopulation appears."
                     % first_sup)
    else:
        txt = ("The cohort is entirely %s through k=%d and the first neighbour from a "
               "second superpopulation arrives at rank %d." % (first_sup, pure_k, first_other))
        if band is not None:
            if band[0] <= first_other <= band[1]:
                txt += (" That rank falls inside the slope band, so the two independent "
                        "readings -- one from distances, one from panel labels -- put "
                        "the edge in the same place.")
            else:
                txt += (" That rank falls outside the slope band (%d-%d), so the two "
                        "readings do not put the edge in the same place." % tuple(band))
        parts.append(txt)

    parts.append(
        "At k=%d the cohort is %d/%d (%.1f%%) %s; the largest superpopulation's share is "
        "%s." % (k, n_src, n_sel, 100.0 * n_src / n_sel, src, traj or "not measured"))

    if first_other is not None and k > pure_k:
        parts.append(
            "k=%d is %d ranks beyond the last single-superpopulation rank (%d), so the "
            "cohort admits %d sample(s) from outside %s; cutting at %d instead would rest "
            "the distribution on %d scores rather than %d. Which way to make that trade "
            "is not something these measurements decide."
            % (k, k - pure_k, pure_k, n_sel - n_src, src, pure_k, pure_k, n_sel))
    else:
        parts.append("k=%d is within the single-superpopulation range." % k)
    if band is not None:
        if k > band[1]:
            parts.append("It is %d ranks beyond the top of the slope band (%d)."
                         % (k - band[1], band[1]))
        elif k < band[0]:
            parts.append("It is %d ranks short of the bottom of the slope band (%d)."
                         % (band[0] - k, band[0]))
        else:
            parts.append("It falls inside the slope band.")
    parts.append("The cohort is %.1fx the --min-cohort floor of %d."
                 % (float(n_sel) / args.min_cohort, args.min_cohort))

    mg = bd.get("median_gap_within_50_ranks")
    if mg is not None and bd["gap_at_boundary"] <= mg:
        parts.append(
            "The boundary is near-degenerate: d%d is %.6f and d%d is %.6f, a gap of "
            "%.2e against a local median gap of %.2e, so which samples fall just inside "
            "the cohort is set by the choice of an integer k rather than by any "
            "separation in the data. Read selected_cohort_superpopulation_counts with "
            "that in mind."
            % (n_sel, bd["d_k"], n_sel + 1, bd["d_k_plus_1"], bd["gap_at_boundary"], mg))

    return " ".join(parts)


def _share_comparison(ms, n_top):
    """The clause comparing the trailing PCs' share of squared distance inside the cohort
    with panel-wide, worded as the two numbers actually order, or nothing if equal."""
    w = ms["trailing_pc_share_of_squared_distance_within_cohort"]
    p = ms["trailing_pc_share_of_squared_distance_panel_wide"]
    if w == p:
        return ""
    return (", so PC%d and above carry %s of the squared distance inside the cohort than "
            "across the panel" % (n_top + 1, "more" if w > p else "less"))


def _d1_spread(by_sup):
    """max/min of the per-group median d1 -- the ratio baseline_frame_note quotes."""
    d1 = [v["d1"] for v in by_sup.values() if v.get("d1")]
    return (max(d1) / min(d1)) if d1 else float("nan")


def build_artifact(args, projection, request, s1, cohort, parity, score_stats,
                   scoring_sha256, plink2_version):
    usable = not cohort["refusals"]
    reason = ("a usable reference distribution was built"
              if usable else "; ".join(cohort["refusals"]))

    rb = projection.get("reference_build") or {}
    nd = cohort["neighbour_distances"]
    curve = cohort["distance_curve"]

    assessment = _fit_assessment(cohort)
    first_other = cohort["curve_diagnostics"]["first_rank_of_a_second_superpopulation"]
    anc_group = (((request.get("patient") or {}).get("ancestry_grouping") or {})
                 .get("group"))

    eigenvals = cohort.get("eigenvals")
    if not (eigenvals and eigenvals[-1]):
        eigenvals = None
    ms = cohort.get("metric_sensitivity") if eigenvals else None
    ms_n_top = None
    if ms:
        ms_n_top = next((v for k_, v in ms.items()
                         if k_.startswith("n_leading_pcs_holding_")), None)
    if ms_n_top is None:
        ms = None

    art = collections.OrderedDict()
    art["usable"] = usable
    art["reason"] = reason
    # The sex the comparison group was cut to, as the request asked; null for both sexes.
    art["comparison_group_sex"] = (request.get("comparison_group") or {}).get("sex")
    art["generated_utc"] = datetime.datetime.now(
        datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    art["provider"] = "build_reference.py (independent reference distribution provider)"

    # Read from the scoring file's own '#' header. NEVER asserted: a hardcoded model id
    # would silently mislabel this artifact the moment a different scoring file was
    # passed, and a confidently wrong label on a clinical artifact is worse than a
    # missing one. A None here means the header did not carry the field.
    hdr = s1.get("scoring_file_header") or {}
    art["trait"] = {
        "id": hdr.get("trait_efo"),
        "label": hdr.get("trait_reported"),
        "mapped": hdr.get("trait_mapped"),
        "read_from": "the scoring file's own # header, not asserted by this tool",
    }
    art["model"] = {
        "pgs_id": hdr.get("pgs_id"),
        "pgs_name": hdr.get("pgs_name"),
        "hmPOS_build": hdr.get("HmPOS_build"),
        "hmPOS_date": hdr.get("HmPOS_date"),
        "genome_build_declared": hdr.get("genome_build"),
        "variants_number_declared": hdr.get("variants_number"),
        "weight_type": hdr.get("weight_type"),
        "sha256": scoring_sha256,
        "coordinate_columns_used": "%s / %s" % (HM_CHR_COL, HM_POS_COL),
        "effect_weight_column_used": args.effect_weight_col,
        "note": "#genome_build describes the authors' ORIGINAL build and is often "
                "different from the coordinates used here. The hm_chr / hm_pos columns "
                "this tool reads are in #HmPOS_build. Both are recorded above so the "
                "difference is visible rather than resolved silently.",
    }

    # Read off the inputs, never written in. The previous strings named one panel
    # release and one sample count, and would have gone on naming them whatever VCF
    # and basis were passed.
    vh = cohort["ref_vcf"]
    hl = vh["header_lines"]
    pool = cohort["candidate_pool"]
    art["source"] = (
        "reference genotypes: %s (%d samples; header ##source=%s, ##fileDate=%s); "
        "cohort selected in the PCA basis built by %s from %s, labelled from %s"
        % (os.path.basename(args.ref_vcf), len(vh["samples"]),
           hl.get("source", "not stated"), hl.get("fileDate", "not stated"),
           rb.get("tool"), rb.get("reference_source"), rb.get("panel_file")))
    art["source_detail"] = {
        "reference_vcf": os.path.basename(args.ref_vcf),
        "reference_vcf_header_lines": hl,
        "reference_vcf_n_samples": len(vh["samples"]),
        "reference_vcf_build_record": cohort.get("ref_vcf_provenance"),
        "pca_basis_reference_build": rb,
        "candidate_pool": pool,
    }
    art["version_string"] = ("; ".join("##%s=%s" % (k_, hl[k_]) for k_ in
                                       ("source", "fileDate", "reference") if k_ in hl)
                             or None)
    art["version_string_note"] = (
        "The reference VCF's own ##source, ##fileDate and ##reference header lines, "
        "verbatim. This tool supplies no release identifier of its own; null means the "
        "header carried none of them.")

    art["ancestry_grouping_definition"] = {
        "grouping": "none -- continuous position in PC space, no discrete label",
        "method": ("%s-PC projection of the patient into the PCA basis %s built from %s "
                   "(%s markers after pruning, %s basis samples), then k-nearest-neighbour "
                   "selection by Euclidean distance in that %s-PC space, over the "
                   "candidate pool in cohort_construction_rule.candidate_pool. No "
                   "population label is assigned to the patient by this provider."
                   % (projection.get("pcs_used"), rb.get("tool"),
                      rb.get("reference_source"), rb.get("n_markers_after_pruning"),
                      rb.get("n_reference_samples"), projection.get("pcs_used"))),
        "pc_vector_used": ("patient_pcs_adp_corrected" if args.pc_source == "adp"
                           else "patient_pcs_simple_projection"),
        "pcs_used": projection.get("pcs_used"),
        "distance_metric_note": (
            ("Euclidean distance over %d PCs weights every PC equally: the plink2 "
             "eigenvector columns are all normalised to the same variance, whatever "
             "share of the variance each PC explains. The %d leading PCs hold %.0f%% of "
             "the eigenvalue mass, and PC1's eigenvalue is %.0fx PC%d's. PC%d and above "
             "carry a median %.1f%% of the squared distance within the selected cohort, "
             "against %.1f%% panel-wide%s."
             % (projection.get("pcs_used"), ms_n_top, 100.0 * ms["leading_pc_eigenvalue_share"],
                eigenvals[0] / eigenvals[-1], projection.get("pcs_used"),
                ms_n_top + 1,
                100.0 * ms["trailing_pc_share_of_squared_distance_within_cohort"],
                100.0 * ms["trailing_pc_share_of_squared_distance_panel_wide"],
                _share_comparison(ms, ms_n_top)))
            if ms else
            ("Euclidean distance over %d PCs weights every PC equally. The reference "
             "eigenvalues were not available to quantify how unevenly the variance is "
             "spread across them." % (projection.get("pcs_used") or 0))),
        "cohort_composition_note": (
            ("The selected cohort is a single superpopulation: %s."
             % counts_text(cohort["superpopulation_counts"]))
            if len(cohort["superpopulation_counts"]) <= 1 else
            ("The selected cohort is not superpopulation-homogeneous: %s. That is "
             "what a distance rule produces, not a defect in it -- the rule takes "
             "whoever is nearest and never promised a single group. Re-running the "
             "same k with each PC weighted by the variance it explains gives %s, and "
             "%d of the %d members from outside %s survive. A count that survives both "
             "weightings is a fact about where the patient sits; one that does not is a "
             "fact about how the distance is defined. See distance_metric_note."
             % (counts_text(cohort["superpopulation_counts"]),
                counts_text(ms["eigenvalue_weighted_recheck"]["superpopulation_counts"]),
                ms["eigenvalue_weighted_recheck"]["n_of_those_still_selected_under_eigenvalue_weighting"],
                ms["eigenvalue_weighted_recheck"]["n_outside_source_group_under_equal_weighting"],
                cohort["cohort_source_group"]))
            if ms else
            ("The selected cohort is not superpopulation-homogeneous: %s. That is "
             "what a distance rule produces, not a defect in it -- the rule takes "
             "whoever is nearest and never promised a single group. The full "
             "composition is reported in cohort_construction_rule; nothing about it "
             "is inferred here." % counts_text(cohort["superpopulation_counts"]))),
        "metric_sensitivity": ms,
        "why_no_label": (
            ("The upstream ancestry stage assigned a group for this patient (%r), and "
             "the interpret request carried it here. This provider declines to use it. "
             % anc_group if anc_group else
             "The interpret request carries no upstream group for this patient. ") +
            "Selection is by distance in PC space alone: no sample is included or "
            "excluded by its panel label, and this artifact attaches no label to the "
            "patient. The contract's INT-5 / 17.2 fork permits a distance rule in "
            "place of a discrete label; this is that. The two do not produce the same "
            "cohort -- a label rule draws from every panel member carrying the label "
            "however far away they sit, while this rule takes the nearest %d and lets "
            "the labels fall where they may -- which is exactly why the resulting "
            "neighbour counts must not be read back as a label.%s The panel's labels "
            "can still enter through the choice of k, if k was sized on cohort "
            "composition; whether it was is the provider's statement in "
            "cohort_construction_rule.k_rationale_stated_by_provider. That would set a "
            "boundary, not a label: no sample is included or excluded by its label, no "
            "label is attached to the patient, and the selection rule remains distance."
            % (cohort["n_selected"],
               (" Members from outside the largest group enter only in the outer part "
                "of the cohort -- the first at rank %d of %d -- so moving k changes the "
                "mixture; see cohort_construction_rule.why_this_k and "
                "metric_sensitivity." % (first_other, cohort["n_selected"]))
               if first_other is not None and first_other <= cohort["n_selected"]
               else "")),
        "k": cohort["k_requested"],
        "selected_cohort_superpopulation_counts": cohort["superpopulation_counts"],
        "versioned_record_note": (
            "INT-5 requires the grouping to travel with the version, and "
            "reference_adapter.py's version_capture records this block as that "
            "grouping. A distance rule is not fully described by its method string: "
            "the same rule at a different k, or over a differently weighted PC space, "
            "selects a different cohort and yields a different percentile. k, the "
            "resulting composition, and the weighting convention are therefore "
            "recorded here rather than only in cohort_construction_rule, so the "
            "versioned record is sufficient on its own."),
        "upstream_deviation_note_verbatim": rb.get("deviation_note"),
    }

    art["cohort_construction_rule"] = {
        "rule": ("k-nearest neighbours in %s-PC space by Euclidean distance, k=%d, "
                 "selected from the %d samples of the candidate pool"
                 % (projection.get("pcs_used"), cohort["k_requested"],
                    cohort["n_available"])),
        "candidate_pool": pool,
        "k": cohort["k_requested"],
        "min_cohort_threshold": args.min_cohort,
        "max_kth_distance_threshold": args.max_kth_distance,
        "k_rationale_stated_by_provider": args.k_rationale,
        "why_this_k": _why_this_k(cohort, args),
        "neighbour_distances": {"d1": nd["d1"], "d10": nd["d10"],
                                "d50": nd["d50"],
                                "d%d" % nd["k_for_dk"]: nd["dk"]},
        "distance_curve": curve,
        "distance_curve_diagnostics": cohort["curve_diagnostics"],
        "cohort_composition_at_candidate_k": cohort["cohort_snapshots"],
        "selected_cohort_population_counts": cohort["population_counts"],
        "selected_cohort_superpopulation_counts": cohort["superpopulation_counts"],
    }

    art["n_participants"] = score_stats.get("n_scored", 0)
    art["too_few_threshold_used"] = args.min_cohort
    art["too_few_threshold_note"] = (
        ("With %d samples in the candidate pool and k=%d, this count-based threshold "
         "cannot bind; it fires only on misconfiguration. "
         % (cohort["n_available"], cohort["k_requested"])
         if cohort["n_available"] >= args.min_cohort and
         cohort["k_requested"] >= args.min_cohort else "") +
        "The refusal lever for similarity is --max-kth-distance, set to %s for this "
        "run." % (args.max_kth_distance if args.max_kth_distance is not None
                  else "no check"))

    # The patient side is read off the request and the dosages file; the reference side
    # is the provider's own statement, because no VCF field says what assay produced it.
    # Neither is written in here, which is what the previous fixed strings did.
    rp = request.get("patient") or {}
    patient_assay = rp.get("assay_class")
    art["assay_layer"] = {
        "reference": args.reference_assay,
        "reference_stated_by": "the provider, via --reference-assay",
        "reference_vcf_format_fields": vh["format_ids"],
        "patient": {
            "assay_class": patient_assay,
            "vendor": rp.get("vendor"),
            "chip_generation": rp.get("chip_generation"),
            "n_resolved_variants_typed": s1["n_variants_typed"],
            "n_resolved_variants_imputed": s1["n_variants_imputed"],
            "read_from": "the request's patient block, and the source column of the "
                         "dosages file",
        },
        # Not decided here. One side is the provider's free text and the other the
        # request's class token; whether they name the same assay layer is a judgment,
        # and comparing the strings would make it by spelling.
        "assay_matched": None,
        "assay_matched_note": ("not decided by this tool; reference_adapter.py takes only "
                               "a provider's own true/false here as a statement"),
        "statement": (
            "The reference assay is stated by the provider as %r; the patient's assay "
            "class, from the request, is %r. INT-4 asks for a reference on the patient's "
            "assay layer because a score from one layer placed against another inherits "
            "the R1 imputed/partial-vs-complete bias. Whether these are the same layer is "
            "not judged here, and nothing is corrected. See also "
            "imputation_shrinkage_note." % (args.reference_assay, patient_assay)),
    }

    # same-resolved-set is a claim that the cohort was scored over exactly the variants
    # the patient's own score used. It is made only when every count that could show
    # otherwise agrees; anything short of that is `other`, with the counts.
    art["scoring_parity"] = parity["label"]
    art["scoring_parity_detail"] = {
        "statement": parity["statement"],
        "score_input": os.path.basename(args.score_input),
        "score_input_sha256": parity["score_input_sha256"],
        "score_input_sha256_in_request": parity["score_input_sha256_in_request"],
        "scoring_recipe": parity["recipe"],
        "n_variants_in_score_input": parity["n_given"],
        "n_variants_plink2_used_for_reference": parity["n_reference_used"],
        "n_variants_patient_scoring_used": parity["n_patient_used"],
        "n_variants_in_patient_score_input_per_request": parity["n_patient_given"],
        "plink2_version_patient_per_request": parity["plink2_patient"],
        "n_score_input_variants_not_scored_in_reference": parity["n_not_scored"],
        "sum_abs_beta_not_scored_in_reference": parity["sum_abs_beta_not_scored"],
        "not_scored_examples": parity["not_scored_examples"],
        "plink2_log_lines": parity["log_lines"],
        "reference_allele_ct_min_max": parity["allele_ct_min_max"],
        "step1_join_vs_score_input": s1.get("vs_score_input"),
        "n_model_variants": s1["n_scoring_file_rows"],
        "n_patient_resolved": s1["n_resolved_positions"],
        "sum_abs_beta": round(s1["sum_abs_beta"], 6),
        "sum_abs_beta_verified_against": s1["expected_sum_abs_beta"],
        "scoring_tool": plink2_version,
        "score_convention": (
            "distribution reports SCORE1_SUM, the dosage-weighted sum of effect "
            "weights (the conventional PRS raw score). The mean-of-alleles form "
            "(SCORE1_AVG) is reported alongside as distribution_score_avg so a "
            "convention mismatch with the patient's raw_score is detectable rather "
            "than silent."),
    }

    art["multiallelic_handling"] = {
        "decision_rule": (
            "The patient's rule, applied unchanged: variant IDs set to chrom:pos, every "
            "duplicated ID removed (--rm-dup exclude-all) and records with more than two "
            "alleles excluded at import (--import-max-alleles 2), as score_plink2.py "
            "does for the patient. A position the reference holds more than one record "
            "at is therefore not scored for the cohort, and it is counted in "
            "scoring_parity_detail rather than resolved here."),
        "plink2_log_lines": parity["log_lines"],
    }

    imputed_any = bool(s1["sum_abs_beta_imputed"])
    art["imputation_shrinkage_note"] = {
        "statement": (
            ("%.4f of the patient's %.4f resolved |beta| (%.0f%%) comes from imputed "
             "rather than directly typed positions. Imputed dosages are expected values "
             "from the imputation, not observed genotypes. Whether that moves this "
             "patient's placement, in which direction and by how much, depends on how the "
             "reference genotypes were produced (assay_layer) and is not measured here."
             % (s1["sum_abs_beta_imputed"], s1["sum_abs_beta"],
                100.0 * s1["imputed_weight_fraction"]))
            if imputed_any else
            "None of the patient's resolved |beta| comes from imputed positions."),
        "sum_abs_beta_total": round(s1["sum_abs_beta"], 4),
        "sum_abs_beta_imputed": round(s1["sum_abs_beta_imputed"], 4),
        "sum_abs_beta_typed": round(s1["sum_abs_beta_typed"], 4),
        "imputed_weight_fraction": (round(s1["imputed_weight_fraction"], 4)
                                    if s1["imputed_weight_fraction"] is not None else None),
        "n_variants_imputed": s1["n_variants_imputed"],
        "n_variants_typed": s1["n_variants_typed"],
        "direction": "not measured" if imputed_any else "none: nothing was imputed",
        "magnitude": "not measured" if imputed_any else "none",
        "status": "This is a disclosure, not a correction. No adjustment was applied.",
    }

    cen = cohort["centrality"]
    src = cohort["cohort_source_group"]

    art["cohort_fit_note"] = {
        "finding": ("not decided by this tool: the measurements are below, and no cutoff "
                    "turns them into a verdict"),
        "statement": (
            "%.1f%% of the %d reference samples in the candidate pool are closer to the "
            "panel centroid than the patient's PC coordinates. The patient's distance to "
            "the centroid is %.6f, %s the panel median of %.6f (5th percentile %.6f). "
            "Whether that position makes the selected neighbours closer than the patient's "
            "genetic neighbourhood warrants is not decided here; "
            "shrinkage_vs_fit_assessment sets out the measurements that bear on it."
            % (cen["pct_of_panel_closer_to_centroid"], cohort["n_available"],
               cen["patient_distance_to_centroid"],
               ("below" if cen["patient_distance_to_centroid"]
                < cen["panel_median_distance_to_centroid"] else
                "above" if cen["patient_distance_to_centroid"]
                > cen["panel_median_distance_to_centroid"] else "equal to"),
               cen["panel_median_distance_to_centroid"],
               cen["panel_p5_distance_to_centroid"])),
        "shrinkage_vs_fit_assessment": assessment,
        "centrality_measurement": cen,
        "curve_shape": cohort["curve_shape"],
        "distance_curve": curve,
        "panel_baseline": cohort["reference_baseline"],
        "panel_baseline_by_superpopulation": cohort["reference_baseline_by_superpopulation"],
        "panel_baseline_definition": (
            "For each reference sample, the distance to its OWN r-th nearest "
            "neighbour within the panel, excluding itself. Median and 5th/95th "
            "percentile across all %d samples. This is what 'close' looks like for "
            "someone who genuinely belongs to this panel."
            % cohort["n_available"]),
        "patient_vs_baseline": cohort["patient_vs_baseline"],
        "patient_vs_baseline_by_superpopulation": cohort["patient_vs_baseline_by_superpopulation"],
        "baseline_frame_note": (
            "The all-panel percentiles and the per-superpopulation percentiles "
            "answer different questions and can disagree sharply. The pool mixes "
            "groups whose own median d1 differs by %.2fx between the tightest and the "
            "loosest, so a patient near one group can drift across the all-panel "
            "percentiles without anything about that patient changing. Both are "
            "reported; the all-panel figures are what any such drift shows up in."
            % _d1_spread(cohort["reference_baseline_by_superpopulation"])),
        "cohort_source_group": src,
        "cohort_source_group_definition": (
            "The largest superpopulation among the selected neighbours (%s, %d of "
            "%d). It names which reference samples the cohort is built from, so the "
            "measurements can be read against that group's own baseline. It is NOT a "
            "group assignment for the patient -- see "
            "ancestry_grouping_definition.why_no_label."
            % (src, cohort["superpopulation_counts"].get(src, 0),
               cohort["n_selected"])),
        "direction": "not measured",
        "magnitude": "not measured",
        "status": ("A report of measurements, not a correction and not a verdict. No "
                   "adjustment was applied to the patient's coordinates or to the cohort."),
        "combined_effect_with_imputation": (
            "Not measured. This note concerns cohort selection and "
            "imputation_shrinkage_note the patient's score; whether either moves the "
            "placement, and how they combine, is not measured here."),
    }

    art["distribution"] = summarise(score_stats.get("scores_sum"))
    # Every cohort member's score, not only nine quantiles: the agent places the patient
    # by counting against these, and a quantile table would leave it to interpolate.
    if score_stats.get("scores_sum") is not None:
        art["distribution"]["raw_scores"] = [float(x) for x in score_stats["scores_sum"]]
        art["distribution"]["raw_score_sample_ids"] = list(score_stats["iids_scored"])
    art["distribution_score_avg"] = summarise(score_stats.get("scores_avg"))
    art["distribution_note"] = (
        "Raw scores from the patient's own score input. distribution is SCORE1_SUM, "
        "with each cohort member's score in raw_scores and their reference sample id at "
        "the same index of raw_score_sample_ids; distribution_score_avg is SCORE1_AVG."
        if usable else
        "No distribution was built. The provider refused under INT-2; every "
        "quantile is null rather than zero, because a zero would be a fabricated "
        "value. See `reason`.")

    art["self_check_pc_space"] = cohort.get("selfcheck")
    return art


# ----------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(
        description="Build a PRS reference distribution artifact (INT-1 provider).")
    ap.add_argument("--inputs", default="inputs", help="input directory")
    ap.add_argument("--workdir", default="work", help="intermediate files")
    ap.add_argument("--out", default="reference_distribution.json", help="artifact path")
    ap.add_argument("--dosages", required=True,
                    help="dosages.tsv from match_variants.py (chrom/pos/source/status)")
    ap.add_argument("--scoring", required=True,
                    help="the PGS Catalog scoring file (gzipped) in the patient's build")
    ap.add_argument("--ref-vcf", required=True,
                    help="reference panel VCF the cohort is scored from")
    ap.add_argument("--score-input", required=True,
                    help="the patient's own plink score input (ID EA BETA), the file "
                         "score_plink2.py scored the patient with. The cohort is scored "
                         "from this file and no other.")
    ap.add_argument("--request", required=True,
                    help="the INT-1 request.json. The patient's assay, ancestry group, "
                         "and score-input counts and hash are read from it.")
    ap.add_argument("--reference-assay", required=True,
                    help="what assay produced the reference genotypes, in your words. "
                         "No VCF field says this, so it is your statement and is "
                         "recorded as such.")
    ap.add_argument("--effect-weight-col", required=True,
                    help="the weight column, read off the scoring file's own header. "
                         "Not assumed: only six columns are universal across PGS Catalog "
                         "files and this is not one of them.")
    ap.add_argument("--k", type=int, required=True,
                    help="number of nearest reference samples to select. No default: the "
                         "cohort size is a judgment about how much of the neighbourhood "
                         "to include, and it is not derived from anything.")
    ap.add_argument("--min-cohort", type=int, required=True,
                    help="refuse below this cohort size. No default, for the same reason.")
    ap.add_argument("--k-rationale", default=None,
                    help="why you chose --k (and --max-kth-distance, if you set it), in "
                         "your words. Recorded verbatim; the tool's own text reports "
                         "measurements and never states a reason for k.")
    ap.add_argument("--max-kth-distance", type=float, default=None,
                    help="refuse if the kth neighbour is further than this "
                         "(default: no check)")
    ap.add_argument("--pc-source", choices=["adp", "simple"], default="adp",
                    help="which patient PC vector from projection.json (default adp)")
    ap.add_argument("--expected-abs-beta", type=float, default=None,
                    help="OPTIONAL regression guard: sum of |BETA| over the join, taken "
                         "from a prior run of THIS patient and model. No default -- the "
                         "value belongs to a run, not to the tool. Omitted: not checked, "
                         "and the artifact says so.")
    ap.add_argument("--expected-imputed-abs-beta", type=float, default=None,
                    help="OPTIONAL cross-check on the imputed share of sum |BETA|. "
                         "Reported, never enforced.")
    ap.add_argument("--plink2", default="./plink2", help="path to the plink2 binary")
    args = ap.parse_args()

    # k <= 0 would make order[:k] select n_available+k samples (k<0) or an empty
    # cohort whose distances are None (k==0), which then crashes the summary
    # print. Neither is a meaningful request; reject at the boundary.
    global WEIGHT_COL
    WEIGHT_COL = args.effect_weight_col

    if args.k < 1:
        die("--k must be >= 1 (got %d)" % args.k)
    if args.min_cohort < 1:
        die("--min-cohort must be >= 1 (got %d)" % args.min_cohort)

    I = lambda name: os.path.join(args.inputs, name)
    dosages = args.dosages
    scoring = args.scoring
    projection_path = I("projection.json")
    eigenvec = I("ref_pca.eigenvec")
    eigenval = I("ref_pca.eigenval")
    labels_path = I("sample_labels.json")
    rejoined = os.path.join(args.workdir, "step1_join_score_input.tsv")

    for p in (dosages, scoring, projection_path, eigenvec, labels_path,
              args.score_input, args.request, args.ref_vcf):
        if not os.path.exists(p):
            die("required input missing: %s" % p)

    with open(projection_path) as fh:
        projection = json.load(fh)
    with open(args.request) as fh:
        request = json.load(fh)

    # ---- STEP 1 ----------------------------------------------------------
    print("=" * 72)
    print("STEP 1  join the resolved set to the scoring file")
    print("=" * 72)
    resolved, n_dosage_rows, status_counts = read_resolved_positions(dosages)
    print("  dosage data rows            : %d" % n_dosage_rows)
    for st, n in sorted(status_counts.items()):
        print("    %-16s        : %d" % (st, n))

    s1 = build_score_input(scoring, resolved, rejoined, args.expected_abs_beta,
                           args.expected_imputed_abs_beta)
    given_ids, given_ea, given_abs_beta = read_score_input(args.score_input)
    s1["vs_score_input"] = compare_score_inputs(rejoined, given_ea)
    print("  scoring file rows           : %d" % s1["n_scoring_file_rows"])
    print("  variants emitted            : %d  (typed %d, imputed %d)"
          % (s1["n_variants_emitted"], s1["n_variants_typed"], s1["n_variants_imputed"]))
    print("  resolved not in scoring file: %d" % s1["n_resolved_not_in_scoring_file"])
    if s1["expected_sum_abs_beta"] is None:
        print("  sum |BETA|                  : %.4f   (NOT verified, no target supplied)"
              % s1["sum_abs_beta"])
    else:
        print("  sum |BETA|                  : %.4f   (expected %.4f)  OK"
              % (s1["sum_abs_beta"], s1["expected_sum_abs_beta"]))
    print("    of which typed positions  : %.4f" % s1["sum_abs_beta_typed"])
    print("    of which imputed positions: %.4f  (%.1f%%)  [%s]"
          % (s1["sum_abs_beta_imputed"], 100.0 * (s1["imputed_weight_fraction"] or 0.0),
             s1["imputed_split_verification"]))
    vs = s1["vs_score_input"]
    print("  patient's score input       : %d variants (%d in both, %d only in the "
          "join, %d only in the score input, %d with a different effect allele)"
          % (vs["n_in_patient_score_input"], vs["n_in_both"], vs["n_only_in_step1_join"],
             vs["n_only_in_patient_score_input"],
             vs["n_in_both_with_a_different_effect_allele"]))

    # ---- STEP 2 ----------------------------------------------------------
    print()
    print("=" * 72)
    print("STEP 2  select the reference cohort")
    print("=" * 72)
    n_pcs = projection.get("pcs_used")
    if not isinstance(n_pcs, int) or n_pcs < 1:
        die("projection.json has no usable pcs_used value")

    vec_field = {"adp": "patient_pcs_adp_corrected",
                 "simple": "patient_pcs_simple_projection"}
    patient_vecs = {}
    for name, field in vec_field.items():
        v = projection.get(field)
        if v is None:
            die("projection.json lacks %s" % field)
        if len(v) < n_pcs:
            die("%s has %d values but pcs_used is %d" % (field, len(v), n_pcs))
        patient_vecs[name] = np.asarray(v[:n_pcs], dtype=float)

    iids, coords = load_eigenvec(eigenvec, n_pcs)
    eigenvals = load_eigenval(eigenval, n_pcs)
    labels = load_sample_labels(labels_path)
    print("  basis samples               : %d" % len(iids))
    print("  PCs used                    : %d" % n_pcs)
    missing_labels = [s for s in iids if s not in labels]
    if missing_labels:
        die("%d eigenvec samples have no label in %s (e.g. %s)"
            % (len(missing_labels), labels_path, missing_labels[:3]))

    expected_n = (projection.get("reference_build") or {}).get("n_reference_samples")
    if expected_n is not None and len(iids) != expected_n:
        die("eigenvec has %d samples but projection.json records %d for this build"
            % (len(iids), expected_n))

    # self-check: are we in the same PC space the agent used?
    check = neighbour_selfcheck(iids, coords, labels, patient_vecs, projection)
    if check.get("performed"):
        print("  self-check vs agent's recorded k=%d nearest:" % check["k_checked"])
        print("    recorded : %s" % json.dumps(check["recorded_by_population"], sort_keys=True))
        for name in ("adp", "simple"):
            r = check["per_vector"][name]
            print("    %-7s: %s  -> %s%s"
                  % (name, json.dumps(r["by_population"], sort_keys=True),
                     "MATCH" if r["matches"] else "no match",
                     "  [tie at the k-th boundary]" if r["boundary_tie_at_k"] else ""))
        if not check["any_vector_reproduces_agent_result"]:
            die("neither patient vector reproduces the agent's recorded k=%d "
                "neighbour result. The eigenvec and the projection are not in the "
                "same PC space, so every distance below would be meaningless. "
                "Stopping rather than reconciling." % check["k_checked"])
        if not check["per_vector"][args.pc_source]["matches"]:
            print("    NOTE: --pc-source %s does NOT reproduce the agent's result "
                  "(the other vector does). Proceeding as instructed; this is "
                  "recorded in the artifact." % args.pc_source)

    # The candidate pool. The self-check above ran over every basis sample, because
    # that is the set the agent's neighbours were counted over. A cohort member must
    # also be scorable, so the pool is the basis samples whose genotypes are in the
    # reference VCF -- a consequence of which genotypes exist, recorded in full so it
    # is stated rather than hidden.
    vh = vcf_header_facts(args.ref_vcf)
    in_vcf = set(vh["samples"])
    keep = [i for i, smp in enumerate(iids) if smp in in_vcf]
    dropped = [i for i, smp in enumerate(iids) if smp not in in_vcf]
    d_pop, d_sup = counts_for(iids, labels, dropped)
    pool = collections.OrderedDict([
        ("definition", "the PCA basis samples (ref_pca.eigenvec) whose genotypes are "
                       "present in the --ref-vcf header; a basis sample with no "
                       "genotypes cannot be scored, so it cannot be a cohort member"),
        ("n_basis_samples", len(iids)),
        ("n_ref_vcf_samples", len(in_vcf)),
        ("n_available", len(keep)),
        ("n_basis_samples_without_genotypes", len(dropped)),
        ("basis_samples_without_genotypes_by_superpopulation", d_sup),
        ("basis_samples_without_genotypes_by_population", d_pop),
        ("n_ref_vcf_samples_not_in_basis", len(in_vcf - set(iids))),
        ("self_check_ran_on", "every basis sample, before this restriction"),
    ])
    # What the restriction did to THIS patient's neighbourhood, not only to the pool: of
    # the k basis samples nearest the patient over the whole basis, how many have no
    # genotypes and so cannot be cohort members, and how far the k-th nearest sits with
    # and without them. Without this a reader can only reconstruct it by comparing
    # self_check_pc_space with the selected cohort's counts.
    if args.k >= 1 and len(iids):
        d_full = np.linalg.norm(coords - patient_vecs[args.pc_source], axis=1)
        order_full = np.argsort(d_full, kind="stable")
        kk = min(args.k, len(iids))
        dropped_set = set(dropped)
        near_dropped = [i for i in order_full[:kk] if i in dropped_set]
        nd_pop, nd_sup = counts_for(iids, labels, near_dropped)
        pool["patient_neighbourhood_before_restriction"] = collections.OrderedDict([
            ("k", kk),
            ("n_of_the_k_nearest_basis_samples_without_genotypes", len(near_dropped)),
            ("those_by_superpopulation", nd_sup),
            ("those_by_population", nd_pop),
            ("d_k_over_every_basis_sample", float(d_full[order_full[kk - 1]])),
            ("note", "the k basis samples nearest the patient, by the same PC vector and "
                     "distance as the cohort, over every basis sample before the pool "
                     "restriction; d_k_over_the_candidate_pool is the same distance "
                     "after it"),
        ])
    print("  candidate pool              : %d of %d basis samples have genotypes in "
          "the reference VCF (%d VCF samples)" % (len(keep), len(iids), len(in_vcf)))
    if not keep:
        die("no basis sample has genotypes in %s; there is nothing to score"
            % args.ref_vcf)
    # A same-sex comparison group, where the request asks for one: the agent judged the trait
    # sex-specific and its model developed and validated in that sex. Not a choice here: the
    # pool is cut to the samples the panel records as that sex, and the artifact says so.
    wanted = (request.get("comparison_group") or {}).get("sex")
    if wanted is not None:
        if wanted not in ("female", "male"):
            die("the request's comparison_group.sex is %r; it is female or male" % (wanted,))
        sex_path = I(SEX_LABELS)
        if not os.path.exists(sex_path):
            die("the request asks for a %s comparison group, and %s, which records each "
                "reference sample's sex, is not here; a group of both sexes is not what was "
                "asked for" % (wanted, sex_path))
        keep, same = same_sex_pool(iids, keep, wanted, load_sex_labels(sex_path))
        same["sex_labels"] = SEX_LABELS
        pool["same_sex"] = same
        print("  same-sex group               : %d of %d recorded as %s"
              % (same["n_of_that_sex"], same["n_before"], wanted))
        if not keep:
            die("no sample in the candidate pool is recorded as %s" % wanted)
    iids = [iids[i] for i in keep]
    coords = coords[keep]

    cohort = select_cohort(iids, coords, labels, patient_vecs[args.pc_source],
                           args.k, args.min_cohort, args.max_kth_distance,
                           eigenvals=eigenvals)
    if "patient_neighbourhood_before_restriction" in pool:
        pool["patient_neighbourhood_before_restriction"]["d_k_over_the_candidate_pool"] = (
            cohort["neighbour_distances"]["dk"])
        pool["patient_neighbourhood_before_restriction"]["k_for_d_k_over_the_candidate_pool"] = (
            cohort["neighbour_distances"]["k_for_dk"])
    cohort["candidate_pool"] = pool
    cohort["ref_vcf"] = vh
    prov_path = args.ref_vcf + ".provenance.json"
    if os.path.exists(prov_path):
        with open(prov_path) as fh:
            pr = json.load(fh)
        cohort["ref_vcf_provenance"] = {k_: pr.get(k_) for k_ in (
            "tool", "generated_utc", "phase3_dir", "scoring_file_sha256", "chromosomes",
            "n_records_written", "n_positions_not_in_panel", "samples_in_output")}
    nd = cohort["neighbour_distances"]
    print("  pc-source                   : %s" % args.pc_source)
    print("  cohort scored in step 3     : %d  (--k %d, available %d)"
          % (cohort["n_selected"], cohort["k_requested"], cohort["n_available"]))

    # --- the distance curve, and the baseline that gives it scale -----------
    print()
    print("  DISTANCE CURVE -- reported in full regardless of --k.")
    print("  Baseline: each reference sample's distance to its OWN r-th nearest")
    print("  neighbour within the panel (median, and 5th-95th percentile).")
    print()
    base = reference_baseline(coords, BASELINE_RANKS)
    base_stats = {}
    for r in BASELINE_RANKS:
        a = base[r][np.isfinite(base[r])]
        base_stats[r] = {
            "median": float(np.median(a)),
            "p5": float(np.percentile(a, 5)),
            "p95": float(np.percentile(a, 95)),
        }
    cohort["reference_baseline"] = base_stats

    # Per-superpopulation baselines, AND the patient's percentile against each.
    # The all-panel median mixes groups whose own neighbour distances differ by
    # nearly 2x, and that mixing alone drifts a patient's all-panel percentile
    # across ranks with nothing about the patient changing. Separating that
    # artifact from a real centrality effect is the whole job of cohort_fit_note,
    # so the per-group percentiles -- not just the medians -- belong in the
    # artifact. Emitting all five groups is deliberate: the assessment can then
    # cite the group the cohort is drawn from without this tool ever choosing a
    # label for the patient.
    sup_arr = np.array([labels[i][1] for i in iids])
    base_by_sup = collections.OrderedDict()
    pct_by_sup = collections.OrderedDict()
    for g in sorted(set(sup_arr)):
        m = sup_arr == g
        base_by_sup[g] = collections.OrderedDict(
            ("d%d" % r, float(np.median(base[r][m][np.isfinite(base[r][m])])))
            for r in BASELINE_RANKS)
        pct_by_sup[g] = collections.OrderedDict(
            ("d%d" % r, percentile_of(cohort["distance_curve"][r], base[r][m]))
            for r in BASELINE_RANKS if r in cohort["distance_curve"])
    cohort["reference_baseline_by_superpopulation"] = base_by_sup
    cohort["patient_vs_baseline_by_superpopulation"] = pct_by_sup

    # Scale-free curve shape: how much wider the neighbourhood is at rank 300 than
    # at rank 1. A group being denser or sparser than the panel cannot move this,
    # which is what makes it the cleanest test of whether the patient's curve is
    # genuinely flatter than that of the people the cohort is drawn from.
    SHAPE_LO, SHAPE_HI = 1, 300
    if SHAPE_HI in cohort["distance_curve"] and SHAPE_HI in base:
        shape_pat = (cohort["distance_curve"][SHAPE_HI]
                     / cohort["distance_curve"][SHAPE_LO])
        shape_ref = base[SHAPE_HI] / base[SHAPE_LO]
        cohort["curve_shape"] = {
            "definition": ("d%d / d%d -- the width of the neighbourhood at rank %d "
                           "relative to rank %d, which is scale-free"
                           % (SHAPE_HI, SHAPE_LO, SHAPE_HI, SHAPE_LO)),
            "patient": float(shape_pat),
            "panel_median": float(np.median(shape_ref)),
            "patient_percentile_vs_panel": percentile_of(shape_pat, shape_ref),
            "by_superpopulation": collections.OrderedDict(
                (g, {"median": float(np.median(shape_ref[sup_arr == g])),
                     "patient_percentile": percentile_of(shape_pat,
                                                         shape_ref[sup_arr == g])})
                for g in sorted(set(sup_arr))),
        }
    else:
        cohort["curve_shape"] = {
            "definition": "d%d / d%d" % (SHAPE_HI, SHAPE_LO),
            "patient": None, "panel_median": None,
            "patient_percentile_vs_panel": None,
            "by_superpopulation": {},
            "note": "not computed -- fewer than %d reference samples available"
                    % SHAPE_HI,
        }

    print("    rank        patient    panel median    panel p5-p95        patient pctile")
    print("    " + "-" * 74)
    patient_vs_baseline = {}
    for r, dist in cohort["distance_curve"].items():
        if r in base_stats:
            b = base_stats[r]
            pct = percentile_of(dist, base[r])
            patient_vs_baseline[r] = {"patient": dist, "percentile_vs_panel": pct}
            print("    d%-9d %-10.6f %-15.6f %-19s %.1f%%"
                  % (r, dist, b["median"],
                     "%.6f - %.6f" % (b["p5"], b["p95"]), pct))
        else:
            print("    d%-9d %-10.6f %-15s %-19s %s"
                  % (r, dist, "-", "-", "-"))
    cohort["patient_vs_baseline"] = patient_vs_baseline

    # Centrality: is the patient sitting at the middle of the cloud? This is what
    # distinguishes "well matched" from "projection-shrunk", and it feeds
    # cohort_fit_note. Reported against the panel AND against the group the cohort
    # is drawn from, because the panel-wide figure alone cannot tell an unusually
    # central patient from one sitting normally inside a group that is itself
    # nearer the centre than average.
    centroid = coords.mean(axis=0)
    r_ref = np.linalg.norm(coords - centroid, axis=1)
    r_pat = float(np.linalg.norm(patient_vecs[args.pc_source] - centroid))
    src_group = cohort["cohort_source_group"]
    cohort["centrality"] = {
        "patient_distance_to_centroid": r_pat,
        "pct_of_panel_closer_to_centroid": float(100.0 * np.mean(r_ref < r_pat)),
        "cohort_source_group": src_group,
        "pct_of_source_group_closer_to_centroid": (
            float(100.0 * np.mean(r_ref[sup_arr == src_group] < r_pat))
            if src_group is not None and (sup_arr == src_group).any() else None),
        "source_group_median_distance_to_centroid": (
            float(np.median(r_ref[sup_arr == src_group]))
            if src_group is not None and (sup_arr == src_group).any() else None),
        "panel_median_distance_to_centroid": float(np.median(r_ref)),
        "panel_p5_distance_to_centroid": float(np.percentile(r_ref, 5)),
        "panel_p95_distance_to_centroid": float(np.percentile(r_ref, 95)),
        "patient_distance_to_centroid_other_vector": float(np.linalg.norm(
            patient_vecs["simple" if args.pc_source == "adp" else "adp"] - centroid)),
        "recorded_projection_shrinkage_ratio": projection.get(
            "projection_shrinkage_ratio_for_this_query"),
        "recorded_projection_shrinkage_ratio_note": (
            "Copied from projection.json for provenance. It is NOT read as evidence "
            "of a centrality effect here, and nothing in this artifact is derived "
            "from it. Upstream states it compares two estimators with no ground "
            "truth and so cannot identify which is biased or in which direction; "
            "the direction question is settled by the measurements in this block "
            "instead. Note also that the vector used here (%s) is the corrected "
            "estimator, not the one the ANC-6 shrinkage claim is about."
            % ("patient_pcs_adp_corrected" if args.pc_source == "adp"
               else "patient_pcs_simple_projection")),
    }
    cohort["eigenvals"] = eigenvals
    cohort["selfcheck"] = check
    print()
    print("    patient pctile = %% of reference samples whose own r-th neighbour is")
    print("    CLOSER than the patient's. 50%% means the patient sits like a typical")
    print("    panel member at that rank; high means the panel fits them worse.")
    print("    (Patient distances are over %d samples, each reference sample's own"
          % cohort["n_available"])
    print("    over the other %d -- a negligible asymmetry, noted for honesty.)"
          % (cohort["n_available"] - 1))

    # The same profile against the group the cohort is drawn from. The all-panel
    # column above mixes groups of very different density and drifts on its own;
    # this column is the patient against the group the cohort is mostly made of.
    src_g = cohort["cohort_source_group"]
    if src_g:
        print()
        print("    vs %s alone (%d of the %d selected neighbours) -- the baseline the"
              % (src_g, cohort["superpopulation_counts"].get(src_g, 0),
                 cohort["n_selected"]))
        print("    cohort is drawn from:")
        print("      " + "  ".join(
            "d%d %.1f%%" % (r, pct_by_sup[src_g]["d%d" % r])
            for r in BASELINE_RANKS if pct_by_sup[src_g].get("d%d" % r) is not None))
        cs = cohort["curve_shape"]
        if cs.get("patient") is not None:
            print("      curve shape d300/d1 %.3f vs %s median %.3f  -> %.1f%% of %s"
                  % (cs["patient"], src_g,
                     cs["by_superpopulation"][src_g]["median"],
                     cs["by_superpopulation"][src_g]["patient_percentile"], src_g))
        print("      centrality: %.1f%% of the panel and %.1f%% of %s are closer to"
              % (cohort["centrality"]["pct_of_panel_closer_to_centroid"],
                 cohort["centrality"]["pct_of_source_group_closer_to_centroid"],
                 src_g))
        print("                  the centroid than the patient")

    # --- composition at candidate cohort boundaries -------------------------
    print()
    print("  COHORT COMPOSITION at candidate boundaries:")
    for kk, snap in cohort["cohort_snapshots"].items():
        print("    k=%-5d superpop %s" % (kk, json.dumps(snap["superpopulation_counts"], sort_keys=True)))
        print("            pop      %s" % json.dumps(snap["population_counts"], sort_keys=True))
    print()
    print("  Cohort actually scored (--k %d): superpop %s"
          % (cohort["k_requested"], json.dumps(cohort["superpopulation_counts"], sort_keys=True)))
    print("                                  pop      %s"
          % json.dumps(cohort["population_counts"], sort_keys=True))
    print("  d1=%.6f  d10=%.6f  d50=%.6f  d%d=%.6f"
          % (nd["d1"], nd["d10"], nd["d50"], nd["k_for_dk"], nd["dk"]))
    if cohort["refusals"]:
        print("  INT-2 REFUSAL               : %s" % cohort["refusals"][0])

    # ---- STEP 3 ----------------------------------------------------------
    scoring_sha256 = sha256_of(scoring)
    plink2_version = run_plink2(args.plink2, ["--version"], "version check").strip()
    score_stats = {}
    rp = request.get("patient") or {}
    parity = {
        "label": None, "statement": None,
        "score_input_sha256": sha256_of(args.score_input),
        "score_input_sha256_in_request": rp.get("score_input_sha256"),
        "recipe": None,
        "n_given": len(given_ids),
        "n_reference_used": None,
        "n_patient_used": rp.get("n_variants_in_score"),
        "n_patient_given": rp.get("n_variants_in_score_input"),
        "plink2_patient": rp.get("plink2_version"),
        "n_not_scored": None, "sum_abs_beta_not_scored": None,
        "not_scored_examples": [], "log_lines": [], "allele_ct_min_max": None,
    }

    if cohort["refusals"]:
        # INT-2 refusal. Do not score: there is no defensible cohort. Still write
        # a complete, valid artifact and exit 0 -- refusing IS the correct outcome.
        print()
        print("=" * 72)
        print("STEP 3  SKIPPED -- INT-2 refusal, no cohort to score")
        print("=" * 72)
        parity["statement"] = "nothing was scored: INT-2 refusal, see reason"
    else:
        print()
        print("=" * 72)
        print("STEP 3  score the cohort from the patient's own score input")
        print("=" * 72)
        keep_path = os.path.join(args.workdir, "cohort.keep")
        out_prefix = os.path.join(args.workdir, "cohort_score")
        _log, n_processed, used, log_lines = score_cohort(
            args.plink2, args.ref_vcf, args.score_input, keep_path,
            cohort["selected_samples"], out_prefix)
        iids_scored, cols = read_sscore(out_prefix + ".sscore")

        used_set = set(used)
        not_scored = [v for v in given_ids if v not in used_set]
        n_given = len(given_ids)
        ac = cols.get("ALLELE_CT", cols.get("NALLELE_CT"))
        ac_mm = [int(ac.min()), int(ac.max())] if ac is not None and len(ac) else None
        parity.update({
            "recipe": ("plink2 --vcf <ref-vcf> --snps-only just-acgt --set-all-var-ids @:# "
                       "--rm-dup exclude-all "
                       "--import-max-alleles 2 --keep <cohort> --score <score-input> 1 2 3 "
                       "header cols=+scoresums,+denom no-mean-imputation list-variants"),
            "n_reference_used": n_processed,
            "n_not_scored": len(not_scored),
            "sum_abs_beta_not_scored": math.fsum(given_abs_beta[v] for v in not_scored),
            "not_scored_examples": not_scored[:10],
            "log_lines": log_lines,
            "allele_ct_min_max": ac_mm,
        })

        # Each condition is one way the two scores could cover different variants.
        # same-resolved-set needs all of them; a failed one is named in the statement.
        sha_req = parity["score_input_sha256_in_request"]
        n_pu, n_pg = parity["n_patient_used"], parity["n_patient_given"]
        pv_here = (plink2_version.splitlines() or [""])[0].strip()
        pv_patient = (str(parity["plink2_patient"]).strip()
                      if parity["plink2_patient"] is not None else None)
        checks = [
            (pv_patient == pv_here,
             "the patient was scored by %r, per the request, and the cohort by %r"
             % (pv_patient, pv_here)),
            (sha_req is not None and sha_req == parity["score_input_sha256"],
             "the score input's sha256 %s is not the one the request names (%s)"
             % (parity["score_input_sha256"], sha_req)),
            (n_pg == n_given,
             "the request says the patient's score input held %s variants; this one "
             "holds %d" % (n_pg, n_given)),
            (n_pu == n_given,
             "the patient's own scoring used %s of the %d variants" % (n_pu, n_given)),
            (not not_scored and n_processed == n_given,
             "plink2 used %s of the %d variants for the reference cohort"
             % (n_processed, n_given)),
            (ac_mm is not None and n_processed is not None
             and ac_mm[0] == ac_mm[1] == 2 * n_processed,
             "reference ALLELE_CT runs %s to %s where every call present at the %s "
             "variants plink2 used would give %s, so some cohort genotypes were missing "
             "and no-mean-imputation scored them as zero dosage"
             % (ac_mm and ac_mm[0], ac_mm and ac_mm[1], n_processed,
                2 * n_processed if n_processed is not None else None)),
        ]
        failed = [msg for ok, msg in checks if not ok]
        if not failed:
            parity["label"] = "same-resolved-set"
            parity["statement"] = (
                "Reference individuals were scored from the patient's own score input, "
                "by the recipe score_plink2.py scored the patient with plus --snps-only "
                "just-acgt (so a panel indel cannot remove the SNV at its position) and the "
                "same plink2 build, and every one of its %d variants was used for the patient "
                "and for every cohort member." % n_given)
        else:
            parity["label"] = "other"
            parity["statement"] = (
                "Not the same resolved set: %s. The cohort was scored from the patient's "
                "own score input by the patient's recipe plus --snps-only just-acgt; the "
                "counts are in this block."
                % "; ".join(failed))

        print("  PARITY (the patient's score input vs what plink2 used for the cohort)")
        print("    variants in score input   : %d" % n_given)
        print("    used for the reference    : %s" % n_processed)
        print("    not scored in reference   : %d  (|beta| %.6e)"
              % (len(not_scored), parity["sum_abs_beta_not_scored"]))
        if not_scored:
            print("      e.g. %s" % ", ".join(not_scored[:5]))
        print("    used for the patient      : %s of %s  (from the request)"
              % (n_pu, n_pg))
        for ln in log_lines:
            print("    plink2: %s" % ln)
        if ac_mm:
            print("  ALLELE_CT per sample        : min %d, max %d (2 x variants used = %s)"
                  % (ac_mm[0], ac_mm[1],
                     2 * n_processed if n_processed is not None else "?"))
        print("  scoring_parity              : %s" % parity["label"])
        for msg in failed:
            print("    - %s" % msg)
        print("  samples scored              : %d (requested %d)"
              % (len(iids_scored), cohort["n_selected"]))
        if len(iids_scored) != cohort["n_selected"]:
            print("  WARNING: plink2 scored a different number of samples than selected")

        score_stats = {
            "n_processed": n_processed,
            "n_scored": len(iids_scored),
            "iids_scored": iids_scored,
            "scores_sum": cols.get("SCORE1_SUM"),
            "scores_avg": cols.get("SCORE1_AVG"),
        }

    # ---- STEP 4 ----------------------------------------------------------
    print()
    print("=" * 72)
    print("STEP 4  emit the artifact")
    print("=" * 72)
    art = build_artifact(args, projection, request, s1, cohort, parity, score_stats,
                         scoring_sha256, plink2_version)
    with open(args.out, "w") as fh:
        json.dump(art, fh, indent=2)
        fh.write("\n")

    print("  usable                      : %s" % art["usable"])
    print("  reason                      : %s" % art["reason"])
    print("  n_participants              : %s" % art["n_participants"])
    d = art["distribution"]
    if d["n"]:
        print("  DISTRIBUTION (SCORE1_SUM, %d participants):" % d["n"])
        for q in QUANTILES:
            print("    p%-4d %+.6f" % (q, d["p%02d" % q]))
        print("    mean  %+.6f    sd %.6f" % (d["mean"], d["sd"]))
        print("    min   %+.6f    max %+.6f" % (d["min"], d["max"]))
    else:
        print("  DISTRIBUTION                : null (refusal; quantiles are null, "
              "not zero)")
    print("  artifact                    : %s" % args.out)


if __name__ == "__main__":
    main()
