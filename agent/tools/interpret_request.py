#!/usr/bin/env python3
"""interpret_request.py -- assemble the INT-1 request from what the run already produced.

Mechanical: it copies fields from artifacts the earlier stages wrote. It computes nothing new and
decides nothing. Its only real job is making sure the ancestry grouping handed to interpret is the
SAME quantity ANC-1 produced (INT-5), rather than something re-derived here.

Contract: interfaces/reference-distribution.md

Usage:
  python3 tools/interpret_request.py --run <RUN_ID> \
      --score runs/<RUN_ID>/score/raw_score.json \
      --coverage runs/<RUN_ID>/score/coverage_PGS0XXXXX.json \
      --ancestry runs/<RUN_ID>/ancestry/ancestry.json \
      --provenance runs/<RUN_ID>/door/provenance.json \
      --model-download runs/<RUN_ID>/select/dl_PGS0XXXXX.json \
      --out runs/<RUN_ID>/interpret/request.json
"""
import argparse
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import lib_run  # noqa: E402

# A bare ontology id, as record.py holds result.json's trait_id to.
TRAIT_ID_RE = re.compile(r"[A-Za-z]+[_:][0-9]+")


def bare_trait_id(given, trait_efo):
    """The trait's bare ontology id, as the contract's example request carries it.

    --trait-id when given. Otherwise the model's catalog record, whose trait_efo is a list of
    {"id", "label", "description", "url"} objects: the request takes the id out of it rather
    than carrying the whole list. A model mapped to several traits does not say which one the
    person asked about, so that takes --trait-id."""
    if given:
        if not TRAIT_ID_RE.fullmatch(given):
            raise SystemExit(
                f"interpret_request.py: --trait-id {given!r} is not a bare ontology id "
                f"(e.g. MONDO_0000001). Pass the id alone.")
        return given
    if trait_efo is None or isinstance(trait_efo, str):
        entries = [trait_efo] if trait_efo else []
    else:
        entries = [e.get("id") if isinstance(e, dict) else e for e in trait_efo]
    if not entries:
        return None
    if len(entries) > 1:
        raise SystemExit(
            f"interpret_request.py: the model's catalog record maps it to {len(entries)} "
            f"traits ({', '.join(map(str, entries))}). Pass --trait-id with the one the "
            f"person asked about.")
    if not isinstance(entries[0], str) or not TRAIT_ID_RE.fullmatch(entries[0]):
        raise SystemExit(
            f"interpret_request.py: the model's catalog record gives the trait id as "
            f"{entries[0]!r}, which is not a bare ontology id. Pass --trait-id.")
    return entries[0]


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--run", required=True)
    ap.add_argument("--score", required=True)
    ap.add_argument("--coverage", required=True)
    ap.add_argument("--ancestry", required=True)
    ap.add_argument("--provenance", required=True)
    ap.add_argument("--model-download", required=True)
    ap.add_argument("--trait-id")
    ap.add_argument("--trait-label")
    ap.add_argument("--comparison-sex", choices=["female", "male"],
                    help="only for a sex-specific trait whose model was developed and validated "
                         "in that sex (select/SKILL.md): the provider builds the comparison group "
                         "from reference samples recorded as that sex")
    ap.add_argument("--out", required=True)
    args = ap.parse_args()

    score = lib_run.read_json(args.score)
    cov = lib_run.read_json(args.coverage)
    anc = lib_run.read_json(args.ancestry)
    prov = lib_run.read_json(args.provenance)
    dl = lib_run.read_json(args.model_download)
    rest = dl.get("rest_record", {})
    # The file the raw score was computed from, as score_plink2.py recorded it. The provider
    # scores the reference cohort from this same file, so it travels with its hash and with
    # plink's own counts for the patient -- the parity claim is checked against these.
    # The hash is the one score_plink2.py took when it scored, not one taken now: the file
    # at that path may have been rebuilt since, and the request would then carry a hash of
    # bytes the raw score never came from.
    score_input = score.get("score_input")
    score_input_sha256 = score.get("score_input_sha256")
    if score_input and not score_input_sha256:
        raise SystemExit(
            f"interpret_request.py: {args.score} records no score_input_sha256, so which "
            f"bytes the raw score came from is not recorded. Re-run tools/score_plink2.py, "
            f"which records it, and build the request from its raw_score.json.")
    if score_input and os.path.exists(score_input):
        now = lib_run.sha256_file(score_input)
        if now != score_input_sha256:
            raise SystemExit(
                f"interpret_request.py: {score_input} has changed since the raw score was "
                f"computed from it (sha256 then {score_input_sha256}, now {now}). The "
                f"reference would be scored from a different file than the patient was. "
                f"Re-run tools/score_plink2.py on the current file, then build the request.")

    request = {
        "generated_utc": lib_run.utcnow(),
        "contract": "interfaces/reference-distribution.md",
        "run": args.run,
        "trait": {"id": bare_trait_id(args.trait_id, rest.get("trait_efo")),
                  "label": args.trait_label or rest.get("trait_reported")},
        "model": {
            "pgs_id": dl.get("pgs_id"),
            "hmPOS_build": dl.get("build_requested"),
            "resolved_url": dl.get("resolved_url"),
            "sha256": dl.get("sha256"),
            "header_verbatim": dl.get("file_header_verbatim"),
            "catalog_release_date": dl.get("catalog_release_date"),
            "license": rest.get("license"),
        },
        "patient": {
            "raw_score": score.get("raw_score"),
            "assay_class": prov.get("source_assay_class"),
            "vendor": prov.get("vendor"),
            "chip_generation": prov.get("chip_generation"),
            "genome_build": prov.get("declared_build"),
            # INT-5: the SAME quantity ANC-1 produced, copied, not re-derived
            "ancestry_grouping": {
                "group": anc.get("inferred_group"),
                "method": (anc.get("evidence") or [{}])[0].get("source"),
                "deviations": "see OPEN_QUESTIONS Q3",
                "source_judgment": args.ancestry,
            },
            "resolved_positions_manifest": os.path.join(
                os.path.dirname(args.coverage).replace("select", "score"), "dosages.tsv"),
            "score_input": score_input,
            "score_input_sha256": score_input_sha256,
            "n_variants_in_score_input": score.get("n_variants_in_score_input"),
            "resolved_weight_fraction": cov.get("resolved_weight_fraction"),
            "n_variants_in_score": score.get("n_variants_used_by_plink"),
            # Which plink2 build scored the patient, so the provider can say whether the
            # cohort was scored by the same one.
            "plink2_version": score.get("plink2_version"),
            "r2_bar_used": cov.get("r2_bar"),
        },
        # A same-sex comparison group, where the agent judged the trait needs one; null for
        # a group of both sexes. The provider builds it from the panel's recorded sex, and the
        # adapter refuses a reference whose group is not the one asked for.
        "comparison_group": {"sex": args.comparison_sex} if args.comparison_sex else None,
        "what_the_provider_must_return": [
            "usable + reason (INT-2)", "source", "version_string (DATA-6/R6)",
            "ancestry_grouping_definition (INT-5)", "cohort_construction_rule (§17.2 fork)",
            "n_participants", "too_few_threshold_used", "assay_layer (INT-4)",
            "scoring_parity (R1)", "distribution, with raw_scores",
        ],
    }
    lib_run.emit(request, args.out)
    lib_run.ledger_append(args.run, {"kind": "artifact", "stage": "interpret",
                                     "tool": "interpret_request.py", "artifact": args.out})


if __name__ == "__main__":
    main()
