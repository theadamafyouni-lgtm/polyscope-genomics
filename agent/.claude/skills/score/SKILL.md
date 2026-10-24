---
name: score
description: Stage 2. Match the model to the patient's build, resolve variants and strand, decide what each missing position means from the carried assay class, impute, apply the coverage gate, and compute the weighted sum. Never assumes absence equals reference (SCO-1..SCO-10).
---

# score

**Input:** the normalized file **and the provenance the door carried with it**. Score does **not**
re-derive the assay class — read `source_assay_class` from `runs/<RUN_ID>/door/provenance.json`
(`SCO-5`). That is the key name, not the bare `assay_class`, which exists only in the door's
`classification.json`; `interpret_request.py:64` reads the same field out of the same file the same
way. Read the provenance, never the file extension: a 23andMe txt converted to a VCF looks like a
VCF and has array semantics.

## 1. Build: match the model to the patient, do not lift over (`SCO-2`)

The PGS Catalog harmonizes to both GRCh37 and GRCh38 — a sweep of all 5,450 scores found
5,450/5,450 available in both — so **download the model in the patient's build** and liftover
never arises. `SCO-3`'s fallback survives only for custom non-catalog files; if you ever reach for
it, note that liftover does worse than drop variants: ~11 Mb of the genome is inverted between the
builds, and for a **palindromic SNP in an inverted region the reverse-complemented base matches a
valid forward-strand nucleotide, so the error escapes standard detection** — silent and
uncountable, unlike a dropped variant.

## 2. The core judgment: what does a missing position mean here? (`SCO-5`, `SCO-6`)

Read the kind of file first; what absence means **follows from that**. Read `source_assay_class`
from `runs/<RUN_ID>/door/provenance.json` and apply the rule for the class you actually find. The
door made that call; this stage neither re-derives it nor assumes which class it will be.

> **genotyping-array → impute. Never score-as-reference.** There is no positive hom-ref evidence anywhere in
> an array file. A position is either not on the chip (never interrogated) or an on-chip no-call.
> Assigning dosage 0 would inject the downward missing-SNP bias, and it is the `JC-7` error.

The other classes, and do not apply an array's rule to any of them: **gVCF** — score-as-reference
only inside a hom-ref block with `DP≥10` and `GQ≥20` present, impute low-confidence blocks and
gaps. **WGS** — score from its own calls where they cover the model, impute where they do not
(*A whole genome's own calls*, below); score-as-reference only where the file itself called the
reference.
**WES** — decide on-target vs off-target first (needs the capture BED), then coverage. In every
case the deciding quantity is **per-site DP from a coverage track, never genome-average depth**: a
30× WGS delivered as a plain variant-only VCF licenses assume-reference at no depth at all.

`GQ≥20` is a genotype-filtering convention, not a threshold traceable to a primary PRS source —
a tunable default, and labelled as one.

### A whole genome's own calls: is imputation needed?

A whole genome read the person's DNA everywhere it could, and a file that says where it called
the reference -- a Complete Genomics var file does, locus by locus -- carries positive evidence
for every position it called (`JC-7`). For such a file, measure what its own calls cover
**before** imputing anything; a plain variant-only VCF says nothing about where it called the
reference, so it imputes as below.

```bash
python3 tools/own_calls.py --run <RUN_ID> \
  --scorefile <the model file> --chr-col hm_chr --pos-col hm_pos \
  --effect-allele-col effect_allele --other-allele-col <as the header names it> \
  --effect-weight-col <as named> --calls runs/<RUN_ID>/door/normalized.vcf.gz \
  --cg-var <the person's file> --fasta refdata/fasta/GRCh37.fa.gz \
  --out-dir runs/<RUN_ID>/score/own_calls
```

Its `fraction_called` is the share of the model's autosomal weight the file called. Match and
measure those calls exactly as sections 4 and 5 do for imputed records, with
`--imputed-dir runs/<RUN_ID>/score/own_calls --typed-vcf runs/<RUN_ID>/score/own_calls/calls.vcf.gz`.
**If they pass the coverage gate** (0.90, and no unresolved locus at 5%), they are the person's
genotypes as read: score from them (section 6, `--imputed-dir runs/<RUN_ID>/score/own_calls`)
and do not impute. **If they do not**, impute as below. The decision is yours, and is recorded
either way in `missing_position_policy`, with the called share and the measured coverage.
Measured on one sample genome against PGS005218: 98% of the weight called in about a minute, where
imputing the same genome took 84 minutes and resolved 96%.

## 3. Impute (`SCO-7`, `SCO-10`)

**Imputation is not a last resort.** When the file supports it, impute rather than drop or assume.

Name which imputation you mean. This pipeline does **panel-based imputation** — inference from
this person's own haplotypes. It never does **reference-AF mean-imputation** (filling a genotype
with 2× the population frequency), which inserts a population average and presents it as this
person's dosage. Whether that counts as fabrication under `JC-2` is collision 2, flagged and
unresolved at spec level; this build sidesteps it by never doing it.

**Impute the person JOINTLY with a hold-out cohort, not alone.** Beagle's `DR2` is a per-variant
statistic estimated across the target samples, so one person imputed by themselves leaves it
nothing to vary over and it returns ~0 at almost every site. That is not a low-quality
measurement, it is **no measurement**, and the `SCO-8` gate below then reads unmeasurable
positions as failures. Measured on this build: solo imputation put ~68% of positions at r2~0.

Three steps.

**(a) The positions their file actually types.** The hold-out cohort is masked to these, so the
cohort is genotyped as if on the same chip rather than carrying full sequence. **Called
positions only**: a no-call is kept in the normalized VCF as a missing genotype, and if the
hold-outs kept their genotypes there, the DR2 at that site would measure a cohort that was
genotyped where this person was not, and overstate how well their value was recovered.

```bash
mkdir -p runs/<RUN_ID>/score
bcftools query -i 'GT!="mis"' -f '%CHROM\t%POS\n' runs/<RUN_ID>/door/normalized.vcf.gz \
  > runs/<RUN_ID>/score/patient_typed_positions.tsv
```

**(b) The cohort split.** `--super-pop` is **your ancestry judgment**, read from `inferred_group`
in `runs/<RUN_ID>/ancestry/ancestry.json` -- not a label the person gave you (`ANC-4`). Size has
no default and is yours to choose and record. **The seed is not yours to choose:** leave `--seed`
off, here and in (c), and every run uses the same fixed one, recorded as `seed` with
`seed_is_the_default: true`. Not the date, not anything that moves -- see *The seed* below.

The tool takes exactly one of the panel's own labels -- `AFR`, `AMR`, `EAS`, `EUR`, `SAS` -- and
refuses anything else, including a qualified one ("EUR (predominant ...)"). Your ancestry call
may be richer than that or in the HGDP+1kGP vocabulary the ancestry stage uses. `CSA` is broader
than phase3's `SAS`, so passing `SAS` for it is a cohort choice you make and record, not a
spelling fix; `MID` has no phase3 counterpart at all, so say that rather than picking a
neighbour silently.

```bash
python3 tools/build_dr2_cohort.py \
  --panel refdata/ancestry-ref/integrated_call_samples_v3.20130502.ALL.panel \
  --super-pop <your inferred group> --size <you choose> \
  --out-dir runs/<RUN_ID>/score/dr2
```

Hold-out and reference are disjoint by construction, so no sample imputes itself. **The batch size
is unjustified** -- 301 is what has been run, reached by widening from 100 until the answer
stabilised, not derived. It trades against DR2 precision. Say which you used and that it was
chosen, not derived.

**(c) Impute.** One Beagle run per chromosome over the patient plus the cohort, so the DR2 that
reaches the gate came out of the same run as the dosages. Nothing is transplanted from elsewhere.

```bash
python3 tools/impute_joint.py --run <RUN_ID> \
  --target runs/<RUN_ID>/door/normalized.vcf.gz --patient-id <sample name in that VCF> \
  --holdout-samples runs/<RUN_ID>/score/dr2/dr2_holdout.txt \
  --reference-samples runs/<RUN_ID>/score/dr2/dr2_reference.txt \
  --phase3-dir refdata/ancestry-ref/phase3 --map-dir refdata/maps \
  --typed-positions runs/<RUN_ID>/score/patient_typed_positions.tsv \
  --panel-dir refdata/panel-snv \
  --chromosomes 1-22 --threads 6 --xmx 6g --parallel-chromosomes 2 \
  --out-dir runs/<RUN_ID>/score/imputed_joint
```

`--panel-dir` is the reference: the phase3 panel filtered once to biallelic SNVs, all 2,504
samples. Beagle drops your hold-out from it as it reads it (`excludesamples=`), so the
reference it imputes against is exactly `dr2_reference.txt`, and the tool checks that against
Beagle's own sample counts on every chromosome. It gives byte-identical dosages and DR2 to
rebuilding the reference per run, which is what happens without `--panel-dir` and is much slower.
`--parallel-chromosomes 2` runs two chromosomes at once, each its own Beagle at `--threads`;
that too gives byte-identical output to one at a time, and `--xmx 6g` is what lets two fit.
On some runs the harness has Beagle run on another machine, the gb10, instead of this one. The
harness decides that, not you, and the command is the same either way. There, two chromosomes
are in flight but one Beagle job runs at a time, with the heap set by the gb10 and the thread
count at 6; each chromosome goes up alone, and the patient and hold-out sample order, seed, GP,
dosages and DR2 are the same as here. Nothing in this sandbox can run Beagle on those runs, so
there is nothing to fall back to. If Beagle there fails, see *If Beagle on the gb10 fails* below.

**`--threads` is a scoring parameter, like the seed.** Beagle's output depends on its thread
count as well as its seed: the same patient, cohort, seed and chromosome at 6 and at 8 threads
gave different dosages at ~4% of chr22's positions, by up to 1.27. Use 6, and record it with the
seed.

**Local execution is slow -- roughly four hours for the genome on an 8-core box** (chr22 alone takes about
4.5 minutes; running two chromosomes at a time cuts the total by about a quarter). Almost all of
it is Beagle itself: the hold-out co-imputation that makes DR2 a measurement costs that.
It is the accuracy-over-speed choice and it is expected; do not take a faster path that skips it. It
writes one patient-only VCF per chromosome into `--out-dir`, which is what every stage after this
reads, and the multi-sample cohort output into `--out-dir/cohort/`.

> ### You have to WAIT for it, and waiting is part of the stage
>
> The command runs for hours, which is longer than a single tool call can be. So background it --
> and then **keep polling until it is finished**. Do not report that imputation is running and end
> your turn. **A backgrounded job dies when your turn ends**, and the run is torn down with it: on
> 2026-08-05 exactly that happened, and a run that had done everything right through select lost
> the whole score stage six minutes into chr1.
>
> ```bash
> nohup python3 tools/impute_joint.py --run <RUN_ID> ... \
>   > runs/<RUN_ID>/score/impute_joint.log 2>&1 &
> ```
>
> Then poll. Each poll waits a bounded time and then returns, so each one fits inside a tool
> call. Give the Bash call a 600000 ms timeout (the default is 2 minutes); `timeout 540` makes
> the command itself give up first:
>
> ```bash
> timeout 540 bash -c 'until [ -e runs/<RUN_ID>/score/imputed_joint/impute_joint.json ]; do sleep 20; done'; \
>   ls runs/<RUN_ID>/score/imputed_joint/chr*.vcf.gz 2>/dev/null | wc -l
> ```
>
> A bare long `sleep` (`sleep 600; ls ...`) is refused by the Claude Code CLI on this box
> (2.1.293: "Foreground sleep is blocked"); the bounded `until` loop above is the form it runs.
>
> **It is done when `runs/<RUN_ID>/score/imputed_joint/impute_joint.json` exists** -- the tool
> writes it once, at the end, and it carries `n_ok`, `n_failed` and `n_skipped`. Read those before
> going on: a chromosome that failed is recorded there with its stderr rather than raised, so the
> file existing is not by itself success.
>
> Keep polling. A run of polls of nine minutes each is the expected shape of this stage, not a
> sign something is wrong. The largest chromosomes run first, so the count normally stays at 0
> for the first forty minutes or so on the local backend. To tell slow from dead, check the process, not the
> count: `pgrep -f impute_joint.py` and the tail of `runs/<RUN_ID>/score/impute_joint.log`.
> `chr<N>.beagle.log` is written only when that chromosome's Beagle finishes.
>
> **Every poll must return. Never block waiting for a file without a bound.** A command like
> `until [ -f .../chr4.vcf.gz ]; do sleep 900; done` with no `timeout` in front looks strictly
> better than repeated short polls, and it is what kills this stage. It blocks inside a single tool call, the call is cut
> off at the tool's own ceiling, and when the last such command dies your turn ends with it --
> the harness reads that as the run finishing and tears the sandbox down over the still-running
> job. On 2026-08-06 a run did precisely this: it armed four staged watchers, wrote "watchers
> armed, standing by for imputation to complete", and lost the stage at chr2. The same applies
> to `wait`, to an unbounded `while [ ! -f ... ]`, and to any single `sleep` long enough to
> outlast a tool call. The tool's ceiling is ten minutes; `timeout 540` keeps each poll under it.
>
> **The round-trips are not waste, and this is the trap.** Reasoning that repeated status checks
> cost round-trips without changing anything is correct about the checks and wrong about what
> they are for: they are the only thing keeping your turn alive, and your turn is the only thing
> keeping imputation alive. Twenty short calls to protect five hours of compute is the trade.
> Do not optimise them away.
>
> The one thing you must not do is treat "I started it" as "it is done".

**If Beagle on the gb10 fails.** On a run where Beagle runs on the gb10, the first chromosome it
could not impute stops the tool: it sends no other chromosome, runs nothing on this machine
instead, writes `impute_joint.json` with a `gb10_failure` entry giving the chromosome and the
reason, and exits non-zero. That is the environment failing, not a judgment, and none of
`score`, `refuse` or `stop` describes it. Do not run the tool again and do not go on with the
chromosomes that did come back. Record no judgment for this stage, write no `result.json`, say
why in plain words in your final message, giving the reason the tool printed, and end the run.
The harness ran the gb10: it records the run as an environment failure from its own record of
what came back, and files nothing as a result. A chromosome refused for your own request -- a
file outside your run, say -- is not that: it is failed without a `gb10_failure`, and is yours
to fix.

**The seed is a scoring parameter, not an implementation detail** (`SCO-10`). It draws the
hold-out cohort as well as Beagle's randomization, so another seed is another cohort and other
dosages. Stochastic pre-phasing moves an individual's percentile with no change to the genetic
model at all — up to ~9.19% of individuals shift by more than 10 percentile points under a
stochastic pipeline versus ~0.1% under a fixed-seed one. So:

- **One fixed seed, every run, by default.** The same file gives the same cohort and the same
  dosages again. That is repeatability; it is not evidence the answer is stable.
- **Near a line, check it with a second seed.** You decide when a result sits close enough to a
  line for the seed to matter: measured coverage near 0.90, a high-weight variant whose DR2 is
  near the bar, a locus near the 5% check. Then draw the cohort and impute again with `--seed 2`,
  into their own directories (`score/dr2_seed2`, `score/imputed_joint_seed2`), match and measure
  again, and compare the two answers at that line.
- **If the answer holds, say so**: the result is the default seed's, and it did not depend on
  the seed.
- **If it flips, the result is unstable, and that is what you report**, with the result and not
  after it: whether this model clears the line depends on the seed, which is not a property of
  the person's genome. The run's result stays the default seed's either way. **Never keep the
  seed that gives the nicer answer**, and never deliver the second seed's answer as the result.

Record both: the default seed's numbers are the run's, the second seed's are the check, and
`imputation` in your judgment says which seeds ran and whether the answer held.

**X, Y and MT are left out of coverage.** The staged panel is chr1–22, so a model's positions on
X, Y, the pseudoautosomal code XY and MT are never scored here, by any route. They are not
"assumed reference" either. `weight_coverage.py` leaves them out of both sides of the fraction, so
coverage is the share of the model's **autosomal** weight that was resolved, and §5's 5% check is
against that same autosomal total. It reports them under `left_out_of_coverage`: their rows,
their Σ|β| and its share of the model's whole weight. Say so rather than letting them vanish: the
score covers the autosomes, and that share of the model's weight is not in it. Where the share is
large, the score is a different instrument from the published one; judge whether it still
answers the person's question.

## 4. Match variants and resolve strand — here, not earlier (`SCO-4`)

```bash
python3 tools/match_variants.py --run <RUN_ID> \
  --scorefile <the model file you downloaded> \
  --chr-col hm_chr --pos-col hm_pos --effect-allele-col effect_allele \
  --other-allele-col <as the header names it> --effect-weight-col <as the header names it> \
  --imputed-dir runs/<RUN_ID>/score/imputed_joint \
  --typed-vcf runs/<RUN_ID>/door/normalized.vcf.gz \
  --palindromic-policy <you choose> --r2-bar 0.80 \
  --out-dir runs/<RUN_ID>/score
```

**Name the other-allele column the file actually fills.** Harmonized files can carry both
`other_allele` and `hm_inferOtherAllele`, and either can be empty in every row — in some models
`hm_inferOtherAllele` is blank throughout and `other_allele` holds the alleles. Pass an empty
column and the tool silently matches on the effect allele alone. Check which one has values
(the select stage's `scorefile_facts_<PGS>.json`, or `zcat | head`) and pass that one.

**Every flip decision is recorded.** A silent flip during normalization would erase provenance,
which is why strand is resolved at matching time rather than at conversion.

`--palindromic-policy` has no default. A/T and C/G markers are reverse complements of each other,
so alleles alone cannot fix orientation; resolving them properly needs the chip's own `.strand`
mapping file, which needs the chip generation — and a consumer export's chip generation is
typically **inferred from the header, not declared**, so check what the door actually established
before relying on it. Choose `exclude` unless you have a reason not to, and say what it
cost: excluded palindromic markers become unresolved weight, counted in the gate.

## 5. The coverage gate (`SCO-8`) — measured now, not estimated

```bash
python3 tools/weight_coverage.py measure --run <RUN_ID> \
  --scorefile <model file> --chr-col hm_chr --pos-col hm_pos \
  --effect-weight-col <as named> --dosage-tsv runs/<RUN_ID>/score/dosages.tsv \
  --r2-bar 0.80 --high-beta-fraction 0.05 \
  --out runs/<RUN_ID>/score/coverage_<PGS>.json
```

Two conditions, and a stop:

1. **Weighted coverage** — the fraction of the score's **weight** on positions you could not
   resolve, not the variant count, over the autosomes (X, Y and MT are in neither side, §3).
   Refuse if resolved Σ|β| < **0.90**. The count-based `--min_overlap 0.75` bar is explicitly
   rejected: count is the wrong variable.
2. **A named condition on high-β loci** — refuse if any single unresolved locus carries **≥5%** of
   total Σ|β|. **Reported individually, by locus, never summed into the fraction.** This is the
   case the aggregate is blind to: one unresolved dominant locus can swing this person's score by
   up to 2|β| even when the fraction looks fine. A locus is a position: the tool adds up the |β|
   of every row the model lists there (two alleles, or one listed twice), and `n_model_rows` says
   how many, so a locus can trip this on rows that would not one by one.
3. **Stop if the data is broken** — including the internal inconsistency in §6.

All three values are **design choices, chosen and defended, not derived** — no published number
exists. Say that when you use them. And the honest consequence, which is intended: a
stringent r² bar plus a 0.90 floor means an admixed patient, whose absent high-weight
positions are also the hardest to impute well, can trip a refusal. That is `FRAME-3`'s honest
limit made operational, and a correct refusal is a correct outcome.

## 6. Compute (`SCO-9`)

```bash
python3 tools/score_plink2.py --run <RUN_ID> \
  --score-input runs/<RUN_ID>/score/plink_score_input.tsv \
  --imputed-dir runs/<RUN_ID>/score/imputed_joint --out-dir runs/<RUN_ID>/score
```

**Dosage field: DS, auto-detected.** With no `--dosage-field` flag the tool picks the field per
target VCF — **`DS` when the VCF header declares `FORMAT/DS`, else `GP`**. Beagle always emits
`DS`, and plink2 *refuses* `dosage=GP` on a file that also carries `DS`, so scoring the Beagle
output with a fixed `GP` fails on every chromosome and returns `raw_score 0.0`. `--dosage-field`
still overrides when you need a specific field. The recorded `dosage_field_used` per chromosome
shows which was taken.

**Multiallelic records are excluded at import (`--import-max-alleles 2`).** Beagle emits ~30k
multiallelic dosage records per chromosome from the 1kg panel, and plink2 cannot parse a
multiallelic dosage — it aborts during `--vcf` import. The exclusion has to be an *import-time*
filter (`--import-max-alleles`); the post-import `--max-alleles` runs too late, after the dosage
parse that errors.

**The residual-missing rule, which must not be softened.** `no-mean-imputation` alone is not the
policy — turning mean-imputation off makes plink score a missing genotype as **zero dosage**,
which is absence-treated-as-reference, the very error the coverage architecture exists to prevent.
The real guarantee is architectural: the scorer only ever runs on a completed dosage set, so it
never faces a missing genotype. `no-mean-imputation` is a guard against the back door, not the
rule itself.

**Check `internal_consistency` in the output.** If plink used fewer variants than it was handed,
the pipeline is internally inconsistent and that is a **stop** — not a fill of any kind, not zero,
not mean.

**Check the model class.** Some catalog models are not weighted sums at all (dosage-per-genotype,
recessive, dominant, interaction, diplotype, haplotype). A weighted-sum computation cannot compute
those. `SEL-1` sources with no model-class filter, so the handoff can carry an uncomputable model;
the fork is unresolved, so surface it rather than resolving it silently.

## 7. Hand back to select if needed (`SEL-12`)

If the measured number is **materially worse** than the estimate select worked from, that is the
loop firing as designed, not a failure. Go back to `select`, take the next candidate down the
ranked list, and record why. Never re-score a model already scored; out of candidates → refuse.

## 8. Record

```bash
python3 tools/record.py --run <RUN_ID> --stage score --name score --judgment - <<'JSON'
{
  "decision": "...", "outcome": "score|refuse|stop|ask",
  "requirement_ids": ["SCO-6", "SCO-8", "..."],
  "model": "...", "raw_score": 0.0,
  "coverage": {"resolved_weight_fraction": 0.0, "measured_not_estimated": true,
               "high_beta_unresolved_loci": []},
  "missing_position_policy": "impute (array; SCO-6). No position scored as reference. | own calls (WGS: called share 0.98, measured coverage 0.98; no imputation)",
  "imputation": {"panel": "...", "seed": 1, "seed_is_the_default": true, "threads": 6,
                 "second_seed": null, "answer_held_across_seeds": null},
  "strand": {"flips_recorded": 0, "palindromic_policy": "..."},
  "reasons": ["..."],
  "evidence": [{"source": "runs/<RUN_ID>/score/...", "field": "..."}]
}
JSON
```
