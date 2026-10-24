#!/usr/bin/env python3
"""rank_range.py -- where a score sits in its comparison group, as a range, from the counts.

Mechanical. It reads the placement counts reference_adapter.py wrote (n, n_below, n_equal,
n_above) and, with the agent's two choices -- how a tie counts, and the confidence level -- gives
the share of the comparison group scoring below this person, in percent: a point and a Wilson
score interval around it. Nothing is interpolated from quantiles; the counts are all it uses.

What the range covers is the sampling of the comparison group only: n people stand for a
population, and another n of the same kind would place the score a little differently. It
does NOT cover the imputation of the person's own score, the choice of the comparison group
(ancestry, k), the precision the scores were printed to, or the model's own error, so the real
uncertainty is wider. The output says so, for the report to say so.

It also gives the score's z-score in the same group: how many of the group's standard
deviations (sample sd, as the provider's distribution states it) the person's raw score lies
from the group's mean, from the group's own raw scores, with the mean, sd and n it used. A
second view of the same placement, with the range's limits and one more: it reads the group as
if its scores spread evenly about the mean.

Usage:
  python3 tools/rank_range.py --run <RUN_ID> \\
      --reference-response runs/<RUN_ID>/interpret/reference_response.json \\
      --ties half --level 0.95 --out runs/<RUN_ID>/interpret/percentile_range.json
"""
import argparse
import math
import os
from statistics import NormalDist, fmean, stdev
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import lib_run  # noqa: E402

TIES = {"below": 1.0, "half": 0.5, "above": 0.0}


def wilson(successes, n, level):
    """Wilson score interval for a proportion, as fractions; successes may be fractional."""
    z = NormalDist().inv_cdf(1 - (1 - level) / 2)
    p = successes / n
    denominator = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / denominator
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denominator
    return max(0.0, centre - half), min(1.0, centre + half)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--run")
    ap.add_argument("--reference-response", required=True, help="reference_adapter.py's output")
    ap.add_argument("--ties", required=True, choices=sorted(TIES),
                    help="no default: how a comparison score equal to the person's counts")
    ap.add_argument("--level", required=True, type=float, help="no default: e.g. 0.95")
    ap.add_argument("--out", required=True)
    args = ap.parse_args()
    if not 0 < args.level < 1:
        raise SystemExit("rank_range.py: --level is a probability between 0 and 1, e.g. 0.95")
    response = lib_run.read_json(args.reference_response) or {}
    placement = response.get("placement") or {}
    try:
        n, below, equal, above = (int(placement[k]) for k in ("n", "n_below", "n_equal", "n_above"))
    except (KeyError, TypeError, ValueError):
        raise SystemExit("rank_range.py: %s has no placement counts (n, n_below, n_equal, n_above)"
                         % args.reference_response)
    if n <= 0 or min(below, equal, above) < 0 or below + equal + above != n:
        raise SystemExit("rank_range.py: the placement counts do not add up: n=%s, below=%s, equal=%s, "
                         "above=%s" % (n, below, equal, above))
    scores = ((response.get("reference") or {}).get("distribution") or {}).get("raw_scores")
    x = placement.get("patient_raw_score")
    finite = lambda v: isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)
    if not (isinstance(scores, list) and len(scores) == n and all(map(finite, scores)) and finite(x)):
        raise SystemExit("rank_range.py: %s carries no comparison scores to measure against "
                         "(reference.distribution.raw_scores, %d of them, and placement.patient_raw_score)"
                         % (args.reference_response, n))
    sd = stdev(scores) if n > 1 else 0.0
    if not sd > 0:
        raise SystemExit("rank_range.py: the comparison group's scores do not differ (sd %r): there is "
                         "no spread to place a score in" % sd)
    mean = fmean(scores)
    successes = below + TIES[args.ties] * equal
    low, high = wilson(successes, n, args.level)
    result = {
        "tool": "rank_range.py",
        "generated_utc": lib_run.utcnow(),
        "reference_response": args.reference_response,
        "n": n, "n_below": below, "n_equal": equal, "n_above": above,
        "ties": args.ties, "level": args.level, "method": "Wilson score interval",
        "percentile_point": 100 * successes / n,
        "percentile_range": [100 * low, 100 * high],
        "covers": ("the sampling of the comparison group only: %d people standing for the "
                   "population they were drawn from" % n),
        "does_not_cover": [
            "the imputation of the person's own score",
            "the choice of the comparison group (ancestry, how many neighbours)",
            "the precision the scores were printed to, near a tie",
            "the scoring model's own error",
        ],
        "note": "The real uncertainty is wider than this range. A convention, chosen by the agent: "
                "how ties count and the level.",
        "summary_words": lib_run.summary_words(100 * low, 100 * high),
        "z_score": {
            "value": (x - mean) / sd, "patient_raw_score": x, "mean": mean, "sd": sd, "sd_ddof": 1, "n": n,
            "computed_from": "reference.distribution.raw_scores: the comparison group the range is counted in",
            "note": "How many of the group's standard deviations the score lies from the group's mean: "
                    "a position in this group, not a risk. It has the range's limits, and it reads the "
                    "group as if its scores spread evenly about the mean, which a skewed group does not; "
                    "the range, from the counts, is the placement.",
        },
    }
    lib_run.emit(result, args.out)
    if args.run:
        lib_run.ledger_append(args.run, {"kind": "facts", "stage": "interpret", "tool": "rank_range.py",
                                         "percentile_range": result["percentile_range"],
                                         "z_score": result["z_score"]["value"],
                                         "artifact": args.out})


if __name__ == "__main__":
    main()
