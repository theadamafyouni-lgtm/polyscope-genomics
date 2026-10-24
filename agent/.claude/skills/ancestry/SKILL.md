---
name: ancestry
description: Infer the person's genetic ancestry from their genome, take their self-report, and surface any disagreement between the two without silently resolving it. Shared by select and interpret (ANC-1..ANC-6). Run after the door and before select, because select needs ancestry up front to choose a model.
---

# ancestry

Shared by `select` (which model fits this person) and `interpret` (which reference cohort
to build). Runs after the door: `ANC-5` holds only once absence is resolved per assay
class, so the file must be classified first.

## The rule that governs this skill

**The math always uses the genome (`ANC-4`).** Self-report never drives model choice, never
drives reference choice, never adjusts confidence. It is a flag, not a signal, and the two
are never combined into one number.

The person is asked, is shown any mismatch, and may end the session. What they do not
do is pick which ancestry the math runs on. That choice would be consent in name only.

## 1. Ask for the self-report

Ask plainly. Say it is optional and will not change the computation, only what you tell
them and whether a mismatch gets flagged. **"I don't know" is a complete answer** and must
not derail anything.

## 2. Build the reference

**Build a PCA basis every run, and do not use the one already sitting in your clone.**
`refdata/ancestry-ref/hgdp_basis/` is a complete prebuilt basis and it *is* staged. It was
built with parameters that are not the ones you are about to choose, so projecting against
it is not the computation this stage asks for. Build your own, and say in your judgment
that you did.

`refdata/ancestry-ref/build/` and `build_p3chr1/` are a different matter and are removed
from the clone on purpose: they were derived from a patient's own positions, and staging
them would hand you a finished projection.

Build from the HGDP + 1000 Genomes panel at
`refdata/ancestry-ref/hgdp_1kgp/GRCh37_HGDP+1kGP_ALL`. It is a plink2 pgen fileset, passed
as a stem with no extension, already one genome-wide fileset over all autosomes. Write
everything under `runs/<RUN_ID>/ancestry/`; the refdata clone is read-only and a write
there fails.

The panel carries **six groups: AFR, AMR, CSA, EAS, EUR, MID.** Note **CSA**, not SAS: it
is the wider group, so a South Asian person lands in CSA. Read the group names off
`reference_build.json` rather than assuming a vocabulary.

> **Never restrict the reference to the patient's own positions.** Not with `bcftools -R`,
> not with `-T`, not with any site list derived from their file.
>
> `ANC-6`'s coverage check reads `n_markers_shared_with_reference /
> marker_set.reference_pruned_markers`. Build the reference from the patient's markers and
> that ratio is 1.0 by construction, so the check reads clean at exactly the moment it
> should fire.
>
> If you inherit such a reference anyway: report the projection's coverage as
> **unestablished**, do not apply the calibration in §3 below, and do not report the ratio.

You choose the parameters and you record them. There are no defaults:

```bash
python3 tools/ancestry.py build-ref \
  --ref-pfile refdata/ancestry-ref/hgdp_1kgp/GRCh37_HGDP+1kGP_ALL \
  --panel refdata/ancestry-ref/hgdp_1kgp.panel \
  --maf <you choose> --geno <you choose> \
  --prune-window <you choose> --prune-step <you choose> --prune-r2 <you choose> \
  --pcs <you choose> --exclude-palindromic <yes|no> \
  --out-dir runs/<RUN_ID>/ancestry/ref
```

`--exclude-palindromic yes` is the choice to beat: A/T and C/G markers cannot be
strand-resolved from alleles alone, and an unresolved flip moves the query in PC space.

**Record the marker count after pruning.** It is the denominator of the coverage check.

**Which chromosomes you include bounds every distance you later quote.** The panel is
genome-wide and `build-ref` passes `--autosome`. If you narrow it, say so rather than
implying the genome.

**Expect coverage to look low.** A genome-wide pruned basis runs to a few hundred thousand
markers and a consumer array carries a fraction of them, so a shared-marker ratio in the
low tens of percent is the honest figure for an array. A ratio near 1.0 is the warning
sign, not the good one.

**A reference build is not reproducible across runs.** `build-ref` writes fixed filenames
with no version guard, so two runs on the same person will not produce the same
coordinates. State your group call with its uncertainty, not as a coordinate that would
survive a rerun.

## 3. Project

```bash
python3 tools/ancestry.py project --run <RUN_ID> \
  --ref-dir runs/<RUN_ID>/ancestry/ref \
  --patient-vcf runs/<RUN_ID>/door/normalized.vcf.gz \
  --pcs 20 --k-neighbours 50 \
  --out runs/<RUN_ID>/ancestry/projection.json
```

**`ANC-6`: read `patient_pcs_adp_corrected`.** Never substitute
`patient_pcs_simple_projection` or `distance_to_centroid_simple_projection`, in the group
call or in the report. Simple projection contracts the query toward the centre of the
reference cloud, worse as coverage falls and worst for admixed genomes.

**The corrected coordinate's precision is unquantified.** That bounds the coordinate, not
your conclusion. Coverage is `n_markers_shared_with_reference` over
`marker_set.reference_pruned_markers`, and no floor is enforced anywhere, so this check is
yours to make:

| coverage | what you may say |
|---|---|
| above ~50% | the correction is expected-good by extrapolation, not validated |
| ~50% | the correction is decisive; this is the highest coverage anyone has measured |
| near ~14% | quote distances as approximate; do not lean on small gaps between groups |
| below ~4% | say the projection is unreliable rather than reporting a coordinate |

The artifact carries `shrinkage_profile_measured_on_reference` and
`projection_shrinkage_ratio_for_this_query`. **Read the artifact's own `shrinkage_note`
before quoting either.** The ratio is `||simple|| / ||ADP||` for this person, two
estimators compared with no ground truth; it cannot tell you which one is off, or in which
direction, and it is not a measurement of the bias.

If you characterise this limitation to the person, say **admixed**, not non-European.

## 4. Judge the group yourself

The tool gives distances, never a label. Read:

- `distance_to_superpopulations[*].distance_to_centroid_adp` — distance to each group's centre
- `patient_distance_in_sd_units` — that distance against the group's own spread, which is
  what says whether the person is typical of it or sitting between groups
- `nearest_reference_samples.by_superpopulation` and `.by_population` — the local
  neighbourhood, which catches what a centroid distance hides
- `n_markers_shared_with_reference` — how much data the projection actually had

Then **state the group and your uncertainty.** A confident continental call from an array is
well supported: continental assignment is data-cheap, and a consumer array carries ~600k
markers. **Sub-continental resolution is not** — it needs roughly 100× more information —
so do not claim it.

**Do not invent a posterior cutoff.** There is no trained random forest here, so no
posterior and no `OTH` bin. Say what the distances are and what you conclude from them.

`reference_build.json` states what the panel actually carried. Read it rather than this
file if the two ever disagree.

## 5. When self-report and projection disagree (`ANC-2`, `ANC-3`)

**This is the ~1-in-8 path, not an edge case**, and it concentrates in admixed people — the
same people the product already serves worst.

**They are not two measurements of one quantity.** Self-reported race or ethnicity is a
social and cultural construct; inferred ancestry is similarity to reference genomes. A
mismatch is not an error in one of them to be corrected, and "your test says otherwise" is
both the wrong model and wrong.

- **Surface it at the start of `select`, before any model is picked** (`ANC-3`). Not after.
  Not in the report.
- **A match in part is its own answer.** An admixed person who names two groups, with a genome
  closest to one of them, is `partial`: not `agree`, not `disagree`. Say how in
  `agreement_explained`. It is surfaced like a disagreement.
- **Record it twice.** Here, with `surfaced_to_person: false` for `partial` or `disagree`; then,
  once `select` has shown the person, record the same call again with `surfaced_to_person: true`.
  It is `true` or `false`, never a note: `select` cannot record its model choice until the
  latest call says a mismatch has been shown, and the page shows no result without it.
- **Never silently pick one** (`ANC-2`).
- **It is not a hard blocker.** Continue on the genome with the mismatch shown.
- Explain *why* the genome is what the math uses: scoring models are keyed to genetic
  ancestry, so matching on the genome is what makes a model applicable at all. That is a
  property of the models, not a correction to the person.
- The person may end the session. Record it as `session-ended-by-person` — **not** a
  refusal and **not** a stop.

## 6. Record it

```bash
python3 tools/record.py --run <RUN_ID> --stage ancestry --name ancestry --judgment - <<'JSON'
{
  "decision": "...group, with uncertainty...",
  "outcome": "proceed",
  "inferred_group": "...", "self_reported": "...", "agreement": "agree|partial|disagree|no-self-report",
  "agreement_explained": "...",
  "distances_cited": {...},
  "used_for_math": "inferred (ANC-4)",
  "surfaced_to_person": false,
  "reasons": ["..."],
  "evidence": [{"source": "runs/<RUN_ID>/ancestry/projection.json", "field": "..."}]
}
JSON
```
