---
name: interpret
description: Stage 3. Place the raw score against an ancestry-matched, assay-matched population reference so the number means something. Refuses when a usable reference cannot be built (INT-1..INT-5).
---

# interpret

Place the raw score against an ancestry-matched population reference so the number means
something.

## The one thing to understand about this stage

**The reference is built, not looked up (`INT-1`).** No per-trait PRS percentile
distribution exists to query anywhere. It is constructed for this person, by running *the
same score* across a cohort genetically similar to them.

So `INT-2`'s refusal is **"we cannot build a usable one"** — too few sufficiently similar
participants for a stable distribution — not "no matched reference exists."

**Never substitute for it.** No synthetic distribution, no "approximate" percentile, no
borrowing a distribution from another trait or another cohort, no reasoning from published
population means. A raw score with no reference is not a weak result, it is *not a result*,
and presenting it as one is fabrication (`JC-2`).

## 1. Build the request

```bash
python3 tools/interpret_request.py --run <RUN_ID> \
  --score runs/<RUN_ID>/score/raw_score.json \
  --coverage runs/<RUN_ID>/score/coverage_<PGS>.json \
  --ancestry runs/<RUN_ID>/ancestry/ancestry.json \
  --provenance runs/<RUN_ID>/door/provenance.json \
  --model-download runs/<RUN_ID>/select/dl_<PGS>.json \
  --out runs/<RUN_ID>/interpret/request.json
```

If `select` chose a comparison group of one sex (its choice's `sex.comparison_group_sex`), add
`--comparison-sex <female|male>`: the provider then builds the group only from reference samples
recorded as that sex, and the response's `comparison_group_sex` says so. Never ask for one
`select` did not choose.

The ancestry grouping in the request is **the same quantity `ANC-1` produced** (`INT-5`). It
flows `ANC-1` → interpret → `DATA-6`, which versions it. **Do not re-derive or re-label it
here.**

The request also carries the file your raw score was computed from
(`runs/<RUN_ID>/score/plink_score_input.tsv`, as `score_plink2.py` recorded it) with the hash
`score_plink2.py` took when it scored, plink's own counts and its version. The provider scores
the reference cohort from that same file. If the file has changed since you scored,
`interpret_request.py` stops and says so: re-run `score_plink2.py` on the current file first,
because a reference scored from different bytes is not a reference for your raw score. Every
path in the request must lie inside `runs/<RUN_ID>/`; the harness refuses one that does not.

## 2. Ask the provider, and collect the answer

Building a reference can take hours, longer than one command is allowed to run. So send the
request without waiting:

```bash
python3 comms/harness_client.py request-reference --no-wait \
  --request runs/<RUN_ID>/interpret/request.json
```

It prints a request id (`r-...`) at once. Collect the reply with calls that each fit inside one
command, and repeat while the exit code is 6. **Give each of these Bash calls a 600000 ms
timeout** (the default is 2 minutes, which kills the call before `--timeout 540` returns, and
then you never see exit 6); `--timeout 540` makes the command itself return first:

```bash
python3 comms/harness_client.py await-reference --id <that id> --timeout 540
```

A run of these, nine minutes each, is the expected shape of this step while a reference is
built -- it can take hours. Do not replace it with a bare `sleep` or an unbounded wait.

You can still ask the person questions while it runs. Send one reference request at a time.

| client exit | what it means | what you do |
|---|---|---|
| 6 | still running | run `await-reference` again |
| 0 | the provider answered; the artifact's path is printed | pass that path to the adapter below |
| 3 | no usable reference could be built, or none was supplied | if a path is printed after the message, pass it to the adapter, which records the refusal; if not, the provider sent nothing back: see *If the provider fails* below |
| 5 | the harness did not hand the request over, or replied with something unexpected; the reason is printed | not a reference outcome. If it says a request is already being built, it prints that request's id: collect that one with `await-reference --id <it>`. If the reason says a path is not inside your run, the request named a file it should not have — rebuild it from this run's own files. If it says an input is ambiguous, your run holds more than one file the provider would take for it, and the reason lists them: keep the one this run used under that name, rename the others, and send the request again. Both are yours to fix, not the provider failing. Otherwise the provider was not reached or did not finish: see *If the provider fails* below, with the printed reason |
| 4 | only from the blocking form (without `--no-wait`): nobody replied in time | the request is still being built and its id is printed; collect it with `await-reference --id <it>` rather than sending another |

Then validate what came back. **This is the only thing that reads the artifact for you:**

```bash
python3 tools/reference_adapter.py --run <RUN_ID> \
  --request runs/<RUN_ID>/interpret/request.json \
  --reference <the path the client printed> \
  --out runs/<RUN_ID>/interpret/reference_response.json
```

| adapter exit | what it means | what you do |
|---|---|---|
| 0 | a usable reference was built, for this model and this score input | go to §3 |
| 5 | the provider could not build a usable one | **`INT-2`'s refusal, a correct outcome** (`JC-6`). Record `refuse` with the provider's reason and the participant count, and hand to `report`, which still has something honest to say |
| 4 | the artifact fails the contract, contradicts itself, or is not for this request: a different model or scoring file, a different score input from yours, counts that do not support the parity it claims, reference scores with missing genotypes filled as zero dosage, or malformed numbers | **Use no number from the artifact.** The provider's build failed: see *If the provider fails* below, with the adapter's detail. This is not the provider refusing and not `stop` |
| 3 | no artifact was supplied | the provider sent nothing back: see *If the provider fails* below. **Do not record `stop`** — the data is not broken |

**If the provider fails.** A provider that did not finish in its own time, sent nothing back or
sent an artifact that fails the contract, and a request the harness could not hand over for a
reason other than your own request or files (a path outside your run, an ambiguous input): each
is the environment failing, not a judgment, and none of `score`, `refuse` or `stop` describes it.
It is not `INT-2`'s refusal either, which is the provider answering, with its reason, that no
usable group can be built for this person. So do not guess an outcome. Record no judgment for
this stage, write no `result.json`, and use no number from any artifact. Say why in plain words in
your final message, giving the reason the client or the adapter printed, and end the run. The
harness ran the provider: it records the run as an environment failure from its own record of
what came back, and files nothing as a result.

A provider that is still building has not failed. Keep collecting while the client exits 6: a run
that ends before the reply arrives is filed on what it recorded, as your result.

## 3. Place the score, and carry the caveats forward

`reference_response.json` carries `placement`: of the `n` reference participants' raw scores,
how many are below (`n_below`), equal to (`n_equal`) and above (`n_above`) this person's raw
score. They are counted by exact comparison, with no interpolation — but of rounded numbers:
plink2 prints each score to a fixed precision, and your raw score is a sum of per-chromosome
printed values, so a reference score within that rounding of yours could sit on either side
(`placement.note`). Say where the person stands from the counts — for example "higher than
`n_below` of the `n` people in the comparison group" — and if a count is close to a boundary
that matters, say that it rests on rounded scores.

**The percentile is a range, never a single number.** Work it out from the counts:

```bash
python3 tools/rank_range.py --run <RUN_ID> \
  --reference-response runs/<RUN_ID>/interpret/reference_response.json \
  --ties <below|half|above> --level <you choose> \
  --out runs/<RUN_ID>/interpret/percentile_range.json
```

How a tie counts and the level are conventions you choose and state, not measurements. Record
`percentile_range` as the tool wrote it, and `z_score`, its `z_score.value`: how many of the
same group's standard deviations the score lies from the group's mean. It is a second view of the
same placement with the same limits, and it reads the group as if its scores spread evenly about
the mean, so the range, from the counts, stays the placement. Read its `covers` and `does_not_cover` and carry them to
report: the range is the sampling of the comparison group only. It does not include the
imputation of the person's score, the choice of the group, or the model's own error, so the
real uncertainty is wider than the range, and the report says so. **Never read a position off
the quantiles by interpolating between them** — that is the approximate percentile this stage
forbids.

Then read `warnings_for_report` and **carry every warning into report.** They are not
decoration, and report says each in plain words: the codes in them stay in the record, never in
front of the person:

- **`INT-4`, assay match.** An array-derived score placed against a WGS-derived reference
  bakes the imputed-versus-complete bias straight into the percentile, and its direction and size
  for this person are not measured: never say which way it moved their score. The adapter does not
  decide whether the layers match from how they are worded; unless the provider stated it,
  the warning gives both descriptions and the judgment is yours. If they do not match, the
  report says so.
- **`scoring_parity`.** `same-resolved-set` means the cohort was scored from your own score
  input and plink used every one of its variants on both sides. Anything else — `other` with
  the provider's counts, or `full-model-set` — means the two scores cover different variants,
  the placement is biased, and the report says so.
- **`INT-5`, the membership rule.** A cohort is a set and needs either a discrete label or a
  distance cutoff, while the math treats ancestry as continuous. That fork is unresolved.
  **Name whichever rule the provider used in the report**, rather than hiding it.

## 4. Keep `SEL-11`'s floor and `INT-2`'s refusal separate

They are different failures, and they call for different things being said to the person, so
never describe one as the other:

- **the select floor is uncertainty** — not enough of the score's weight is resolvable, a
  property of the model plus the file
- **`INT-2`'s refusal is placement** — nothing valid to compare the score against, a
  property of the reference cohort

## 5. Record

```bash
python3 tools/record.py --run <RUN_ID> --stage interpret --name interpret --judgment - <<'JSON'
{
  "decision": "...", "outcome": "score|refuse|stop|ask",
  "requirement_ids": ["INT-1", "INT-2", "INT-4"],
  "placement": {"n": 0, "n_below": 0, "n_equal": 0, "n_above": 0},
  "percentile_range": [0.0, 0.0],
  "percentile_range_convention": {"ties": "...", "level": 0.95, "method": "Wilson score interval"},
  "z_score": 0.0,
  "reference": {"source": "...", "version_string": "...", "n": 0,
                "assay_layer": "...", "scoring_parity": "..."},
  "caveats_for_report": ["..."],
  "reasons": ["..."],
  "evidence": [{"source": "runs/<RUN_ID>/interpret/reference_response.json", "field": "..."}]
}
JSON
```
