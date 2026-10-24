#!/usr/bin/env python3
"""reference_adapter.py -- the INT-1 boundary. Validates a provider's reference distribution,
or refuses.

This is the stub the build brief calls for, and the important thing about it is what it does NOT
do: there is no fallback, no synthetic distribution, no "approximate" mode, and no default
percentile. If the reference artifact is absent or invalid, interpret is blocked and says so.
Inventing a distribution to make the pipeline run would be the JC-2 violation the whole product
exists to avoid.

Contract: interfaces/reference-distribution.md

Exit codes:
  0  reference present and valid; `placement` carries the counts
  3  reference_unavailable  -- no artifact supplied or file missing
  4  reference_invalid      -- artifact present but fails the contract, contradicts itself, is
                               for a different model or score input than the request, holds
                               raw scores with missing genotypes scored as zero dosage, or lacks
                               the allele counts that would show it does not; also
                               request_missing / request_invalid when the request itself cannot
                               be checked against
  5  reference_unusable     -- provider itself reports usable=false (INT-2 refusal, a CORRECT outcome)

On a valid reference it also COUNTS: how many reference raw scores fall below, equal to and above
the patient's raw score. It does not turn those counts into a percentile. That takes a convention
-- how ties and the patient's own position are counted -- and the convention is the agent's to
choose and to state, not a fact this tool can supply.

Usage:
  python3 tools/reference_adapter.py --run <RUN_ID> \
      --request runs/<RUN_ID>/interpret/request.json \
      [--reference path/to/provider_artifact.json] \
      --out runs/<RUN_ID>/interpret/reference_response.json
"""
import argparse
import json
import math
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import lib_run  # noqa: E402

REQUIRED = [
    "usable", "reason", "source", "version_string", "ancestry_grouping_definition",
    "cohort_construction_rule", "n_participants", "too_few_threshold_used",
    "assay_layer", "scoring_parity", "distribution",
]
REQUIRED_QUANTILES = ["p01", "p05", "p10", "p25", "p50", "p75", "p90", "p95", "p99"]
PARITY = {"same-resolved-set", "full-model-set", "other"}


def blocked(run, out, code, status, detail, extra=None):
    payload = {
        "tool": "reference_adapter.py",
        "generated_utc": lib_run.utcnow(),
        "status": status,
        "detail": detail,
        "interpret_can_proceed": False,
        "contract": "interfaces/reference-distribution.md",
        "no_fallback_note": "There is deliberately no fallback path. INT-1's reference is built, "
                            "not looked up, and a synthesised distribution would be a fabricated "
                            "result under JC-2.",
    }
    if extra:
        payload.update(extra)
    lib_run.emit(payload, out)
    if run:
        lib_run.ledger_append(run, {"kind": "blocked", "stage": "interpret",
                                    "tool": "reference_adapter.py", "status": status,
                                    "artifact": out})
    raise SystemExit(code)


def _finite(x):
    """A real number, not a bool and not NaN or infinite."""
    return (isinstance(x, (int, float)) and not isinstance(x, bool)
            and math.isfinite(x))


def _count(x):
    """An integer, not a bool and not a float that happens to be whole."""
    return isinstance(x, int) and not isinstance(x, bool)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--run")
    ap.add_argument("--request", required=True)
    ap.add_argument("--reference")
    ap.add_argument("--out", required=True)
    args = ap.parse_args()

    if not os.path.exists(args.request):
        blocked(args.run, args.out, 4, "request_missing",
                f"no interpret request at {args.request}")
    request = lib_run.read_json(args.request)

    if not args.reference:
        blocked(args.run, args.out, 3, "reference_unavailable",
                "No reference distribution artifact supplied: no provider answered this request "
                "(or its answer was not collected). Nothing is placed without one.",
                {"request_echo": request})
    if not os.path.exists(args.reference):
        blocked(args.run, args.out, 3, "reference_unavailable",
                f"reference artifact not found at {args.reference}", {"request_echo": request})

    try:
        ref = json.loads(open(args.reference).read())
    except json.JSONDecodeError as exc:
        blocked(args.run, args.out, 4, "reference_invalid", f"not valid JSON: {exc}")
    if not isinstance(ref, dict):
        blocked(args.run, args.out, 4, "reference_invalid", "the artifact is not a JSON object")

    missing = [k for k in REQUIRED if k not in ref]
    if missing:
        blocked(args.run, args.out, 4, "reference_invalid",
                f"missing required contract fields: {missing}")

    # Shape checks that hold for a refusal too: a refusal for a different model is not this
    # request's refusal, and a `usable` that is not a real boolean is not a statement at all.
    if not isinstance(ref["usable"], bool):
        blocked(args.run, args.out, 4, "reference_invalid",
                f"usable must be true or false; got {ref['usable']!r}")
    if not isinstance(ref["reason"], str) or not ref["reason"].strip():
        blocked(args.run, args.out, 4, "reference_invalid",
                f"reason must be a non-empty string; got {ref['reason']!r}")
    want_model = request.get("model") or {}
    got_model = ref.get("model") if isinstance(ref.get("model"), dict) else {}
    for key in ("pgs_id", "sha256"):
        if not want_model.get(key) or got_model.get(key) != want_model.get(key):
            blocked(args.run, args.out, 4, "reference_invalid",
                    f"artifact model.{key} is {got_model.get(key)!r} but the request's is "
                    f"{want_model.get(key)!r}; a reference built for another scoring file is "
                    f"not a reference for this score")

    # A same-sex comparison group asked for (a sex-specific trait), or a group of both sexes: a
    # reference built for the other is not this request's reference, nor its refusal.
    want_sex = (request.get("comparison_group") or {}).get("sex")
    if ref.get("comparison_group_sex") != want_sex:
        blocked(args.run, args.out, 4, "reference_invalid",
                f"the request asked for a comparison group of {want_sex or 'both sexes'} and the "
                f"artifact's comparison group is of {ref.get('comparison_group_sex') or 'both sexes'}")

    if ref.get("usable") is False:
        blocked(args.run, args.out, 5, "reference_unusable",
                f"provider reports no usable reference could be built: {ref.get('reason')}. "
                f"Per INT-2 this is a correct refusal, not a failure.",
                {"provider_reason": ref.get("reason"),
                 "n_participants": ref.get("n_participants"),
                 "too_few_threshold_used": ref.get("too_few_threshold_used")})

    dist = ref.get("distribution")
    if not isinstance(dist, dict):
        blocked(args.run, args.out, 4, "reference_invalid", "distribution must be an object")
    missing_q = [q for q in REQUIRED_QUANTILES if q not in dist]
    if missing_q:
        blocked(args.run, args.out, 4, "reference_invalid",
                f"distribution lacks quantiles {missing_q}")
    qs = [dist[q] for q in REQUIRED_QUANTILES]
    if not all(_finite(v) for v in qs):
        blocked(args.run, args.out, 4, "reference_invalid",
                f"distribution quantiles must be finite numbers; got {qs}")
    if any(b < a for a, b in zip(qs, qs[1:])):
        blocked(args.run, args.out, 4, "reference_invalid",
                f"distribution quantiles decrease somewhere along p01..p99: {qs}")
    n = dist.get("n")
    if not (isinstance(n, int) and not isinstance(n, bool) and n > 0):
        blocked(args.run, args.out, 4, "reference_invalid",
                f"distribution.n must be a positive integer; got {n!r}")
    # Every reference score, so the patient can be placed by counting rather than by reading
    # between nine quantiles -- which would be the approximate percentile interpret forbids.
    raw = dist.get("raw_scores")
    if not isinstance(raw, list) or len(raw) != n or not all(_finite(v) for v in raw):
        blocked(args.run, args.out, 4, "reference_invalid",
                f"distribution.raw_scores must be a list of {n} finite numbers, one per "
                f"reference participant")
    ids = dist.get("raw_score_sample_ids")
    if (not isinstance(ids, list) or len(ids) != n
            or not all(isinstance(s_, str) and s_ for s_ in ids) or len(set(ids)) != n):
        blocked(args.run, args.out, 4, "reference_invalid",
                f"distribution.raw_score_sample_ids must name {n} distinct reference "
                f"participants, one per raw score")
    if ref.get("n_participants") != n:
        blocked(args.run, args.out, 4, "reference_invalid",
                f"n_participants is {ref.get('n_participants')!r} but the distribution has "
                f"{n} raw scores")
    # The summary must describe these scores. Each quantile is checked against the order
    # statistics that bracket it under every standard quantile definition (Hyndman & Fan
    # types 1-9): for p = q/100 it lies between the ceil(pn)-1 th and the floor(pn)+2 th
    # smallest score. No definition is chosen, so none is imposed on the provider. The
    # tolerance is floating-point rounding in an interpolation, nothing more.
    srt = sorted(raw)

    def within(v, lo, hi):
        return (lo <= v <= hi or math.isclose(v, lo, rel_tol=1e-9, abs_tol=1e-12)
                or math.isclose(v, hi, rel_tol=1e-9, abs_tol=1e-12))

    for qname in REQUIRED_QUANTILES:
        pct = int(qname[1:])
        lo = max(0, -(-pct * n // 100) - 2)
        hi = min(n - 1, pct * n // 100 + 1)
        if not within(dist[qname], srt[lo], srt[hi]):
            blocked(args.run, args.out, 4, "reference_invalid",
                    f"distribution.{qname} is {dist[qname]!r}, outside [{srt[lo]!r}, "
                    f"{srt[hi]!r}], where any quantile definition puts it for these raw "
                    f"scores; the summary does not describe the scores it ships with")
    for key, val in (("min", srt[0]), ("max", srt[-1])):
        if dist.get(key) is not None and dist[key] != val:
            blocked(args.run, args.out, 4, "reference_invalid",
                    f"distribution.{key} is {dist[key]!r} but the raw scores' {key} is {val!r}")
    if ref.get("scoring_parity") not in PARITY:
        blocked(args.run, args.out, 4, "reference_invalid",
                f"scoring_parity must be one of {sorted(PARITY)}; got {ref.get('scoring_parity')!r}")
    patient = request.get("patient") or {}
    detail = ref.get("scoring_parity_detail")
    detail = detail if isinstance(detail, dict) else {}
    want_sha = patient.get("score_input_sha256")
    got_sha = detail.get("score_input_sha256")
    # Whatever the label: a cohort the artifact says was scored from a score input other
    # than the request's is a reference for some other score, as a different model file is.
    if got_sha is not None and got_sha != want_sha:
        blocked(args.run, args.out, 4, "reference_invalid",
                f"the artifact says the cohort was scored from a score input with sha256 "
                f"{got_sha!r}; the request's is {want_sha!r}. A reference scored from a "
                f"different file is not a reference for this score")
    # With no-mean-imputation a missing genotype call is scored as zero dosage. A cohort
    # member with fewer than two alleles counted at every variant plink2 used therefore has
    # a raw score with filled values in it -- absence scored as a genotype -- whatever the
    # label says. The two counts are the only evidence that this did not happen, so both are
    # required: an artifact without them, or with either in another shape, shows nothing
    # about missing calls and is refused rather than read as having none.
    ac = detail.get("reference_allele_ct_min_max")
    used_ref = detail.get("n_variants_plink2_used_for_reference")
    if not (_count(used_ref) and used_ref > 0):
        blocked(args.run, args.out, 4, "reference_invalid",
                f"scoring_parity_detail.n_variants_plink2_used_for_reference must be a positive "
                f"integer, the variants plink2 used for the cohort; got {used_ref!r}. Without "
                f"it nothing shows whether missing cohort genotypes were scored as zero dosage")
    if not (isinstance(ac, list) and len(ac) == 2 and all(_count(v) for v in ac)):
        blocked(args.run, args.out, 4, "reference_invalid",
                f"scoring_parity_detail.reference_allele_ct_min_max must be two integers, the "
                f"least and greatest ALLELE_CT across the cohort; got {ac!r}. Without it "
                f"nothing shows whether missing cohort genotypes were scored as zero dosage")
    if ac != [2 * used_ref, 2 * used_ref]:
        blocked(args.run, args.out, 4, "reference_invalid",
                f"reference ALLELE_CT runs {ac[0]} to {ac[1]} where {2 * used_ref} means every "
                f"call present at the {used_ref} variants plink2 used: some cohort genotypes "
                f"were missing and were scored as zero dosage, so those raw scores contain "
                f"filled values")
    if ref.get("scoring_parity") == "same-resolved-set":
        # The claim is that the cohort was scored from the patient's own score input and that
        # plink2 used every one of its variants on both sides. The hash and the counts are
        # how that is checked rather than taken, so each count must be present and an
        # integer: a null, a float or a boolean that happens to compare equal is not one.
        # The allele counts above hold for this label too.
        if not want_sha or got_sha != want_sha:
            blocked(args.run, args.out, 4, "reference_invalid",
                    f"scoring_parity is same-resolved-set but the artifact's score input "
                    f"sha256 is {got_sha!r} and the request's is {want_sha!r}")
        n_in = patient.get("n_variants_in_score_input")
        counts = {
            "request patient.n_variants_in_score (used for the patient)":
                patient.get("n_variants_in_score"),
            "scoring_parity_detail.n_variants_in_score_input":
                detail.get("n_variants_in_score_input"),
            "scoring_parity_detail.n_variants_plink2_used_for_reference": used_ref,
        }
        bad = {k: v for k, v in counts.items() if not _count(v) or v != n_in}
        not_scored = detail.get("n_score_input_variants_not_scored_in_reference")
        if (not _count(n_in) or n_in < 1 or bad
                or not _count(not_scored) or not_scored != 0):
            blocked(args.run, args.out, 4, "reference_invalid",
                    f"scoring_parity is same-resolved-set, which needs every count to equal "
                    f"the request's n_variants_in_score_input ({n_in!r}) and no score-input "
                    f"variant left unscored in the reference; got {bad or 'equal counts'} and "
                    f"n_score_input_variants_not_scored_in_reference={not_scored!r}")

    x = patient.get("raw_score")
    if not _finite(x):
        blocked(args.run, args.out, 4, "request_invalid",
                f"the request's patient.raw_score is {x!r}; there is nothing to count against")
    placement = {
        "patient_raw_score": x,
        "n": n,
        "n_below": sum(1 for v in raw if v < x),
        "n_equal": sum(1 for v in raw if v == x),
        "n_above": sum(1 for v in raw if v > x),
        "compared_against": "reference.distribution.raw_scores",
        "note": "Counts only, by exact comparison of the two numbers as given. No percentile "
                "is computed here: turning these counts into one takes a convention for ties "
                "and for the patient's own position, and that is the agent's to choose and to "
                "state. The comparison is exact; the inputs are rounded. The patient's "
                "raw_score is what the request carries (score_plink2.py sums plink2's "
                "per-chromosome SCORE1_SUM values, each as plink2 printed it), and each "
                "reference value is what the artifact carries (build_reference.py copies "
                "plink2's per-sample SCORE1_SUM as printed). The two sides are rounded "
                "differently, so a reference score within that rounding of the patient's "
                "could fall on either side of it, and scores counted in n_equal may differ "
                "beyond that precision.",
    }

    warnings = []
    # assay_layer may be a bare string or the object form the provider uses. Whether the
    # reference's assay layer is the patient's is a judgment, and only the provider's own
    # stated assay_matched is taken as one. Two strings that differ are not evidence of
    # different layers ("genotyping array" and "genotyping-array"), and two that are equal
    # are not proof of the same one, so this tool never compares them.
    layer = ref.get("assay_layer")
    patient_assay = (request.get("patient") or {}).get("assay_class")
    if isinstance(layer, dict):
        layer_desc = layer.get("reference") or "unstated"
        matched = layer.get("assay_matched")
    else:
        layer_desc, matched = layer, None
    if matched is False:
        warnings.append(
            f"INT-4: the provider states the reference assay layer ({layer_desc}) does not "
            f"match the patient's assay class {patient_assay!r}. The R1 "
            f"imputed/partial-vs-complete bias is baked into any placement from this pairing "
            f"and the report must say so.")
    elif matched is not True:
        warnings.append(
            f"INT-4: the reference assay layer is stated as {layer_desc!r} and the patient's "
            f"assay class is {patient_assay!r}. The provider does not state whether they are "
            f"the same layer, and this tool does not judge it from the wording. If they are "
            f"not, the R1 imputed/partial-vs-complete bias is baked into the placement and "
            f"the report must say so.")
    if ref.get("scoring_parity") == "other":
        warnings.append(
            "scoring_parity is other: the provider states "
            f"{detail.get('statement')!r}. The reference "
            "and the patient were not scored over the same variant set; the report must say "
            "so.")
    if ref.get("scoring_parity") == "full-model-set":
        warnings.append(
            "scoring_parity is full-model-set: reference individuals were scored over all model "
            "positions while the patient was scored over the resolved subset. The percentile "
            "carries the R1 bias; the report must state it.")

    payload = {
        "tool": "reference_adapter.py",
        "generated_utc": lib_run.utcnow(),
        "status": "reference_valid",
        "interpret_can_proceed": True,
        "warnings_for_report": warnings,
        "placement": placement,
        "reference": ref,
        "request_echo": request,
        "version_capture": {
            "source": ref.get("source"),
            "version_string": ref.get("version_string"),
            "ancestry_grouping_definition": ref.get("ancestry_grouping_definition"),
            "note": "DATA-6: recorded, not pinned. For All of Us this buys an audit trail, not "
                    "reproducibility -- only three CDRs stay live and v3-v6 cannot be migrated to, "
                    "so the reference expires rather than moves.",
        },
    }
    lib_run.emit(payload, args.out)
    if args.run:
        lib_run.ledger_append(args.run, {"kind": "facts", "stage": "interpret",
                                         "tool": "reference_adapter.py",
                                         "status": "reference_valid", "artifact": args.out})


if __name__ == "__main__":
    main()
