# How the agent runs

One agent, six skills (`ARCH-1`, `ARCH-2`, `ARCH-3`). This file is the flow. It is not a skill —
neither is the judgment core (`ARCH-4`), which is the contract every skill obeys and lives in
`JUDGMENT-CORE.md`. **Read that first.**

## The flow

```
                       ┌─ the door (§6.5) ─────────────────────────────┐
   person's file ─────►│ hygiene → classify → normalize → restore ALT  │
   "sample            └───────────────┬───────────────────────────────┘
       condition"                     │  normalized file + provenance
                                      ▼
                        ┌── ancestry (§11) ──┐
                        │ infer from genome; │
                        │ surface conflict   │
                        └─────────┬──────────┘
                                  ▼
     ┌──────────► 1. select (§7) ──────► 2. score (§8) ──────► 3. interpret (§9) ──► 4. report (§10)
     │                  │                     │                      │
     └── reselect ──────┘  (SEL-12)           │            reference built on request
        next candidate    materially worse ───┘
```

**The door is not a stage.** It runs before `select`, because `SEL-5` needs ancestry up front,
`ANC-1` infers ancestry from the patient's file, and `SEL-11`'s coverage estimate needs the assay
class to read presence correctly. All three need the file read and classified first (the old order was a
defect that was found and fixed — do not rebuild it).

`read-and-reason` runs twice: at the door on the patient's file, and inside `select` on each
scoring model file. Same capability, two inputs.

## Stage order and what each owns

| # | Skill | Owns | Must not |
|---|---|---|---|
| — | `read-and-reason` | assay class, vendor/chip, build, what absence means | read the build off a filename |
| — | `ancestry` | genetic ancestry from the genome; surfacing self-report conflict | let self-report drive the math |
| 1 | `select` | model choice, gates, the coverage floor | sort on variant count |
| 2 | `score` | build match, strand, missing-position decision, imputation, coverage gate, the sum | assume absence = reference |
| 3 | `interpret` | building the reference, placing the score | invent a distribution |
| 4 | `report` | the structured output, delivered with care | skip the explanation |

## Two rules that cut across the whole flow

**The math always uses the genome (`ANC-4`).** Self-report is a flag, not a signal. It never
drives model choice or reference choice and never adjusts confidence. The person is told when the
two disagree and may end the session; what they do not do is pick which ancestry the math runs on.

**Provenance travels, and the stages read it — never the file extension (`INTAKE-2`).** A 23andMe
txt converted to a VCF looks like a VCF and has array semantics. `score` reads what the door
carried forward (`SCO-5`); it does not re-derive the assay class.

## The select → score → reselect loop (`SEL-12`)

`SEL-11` selects on an *estimate* of coverage, because imputability is not knowable until
imputation runs. So a candidate can look acceptable at select and prove worse once scored.

- select takes the top-ranked candidate → score produces the real number → if it is **materially
  worse**, go back and take the next candidate down the ranked list.
- **You decide when to loop. There is no threshold** — a threshold here would be a rule engine,
  and `ARCH-5` says the agent judges.
- **Bounds:** never re-score a model already scored; out of candidates → refuse.
- **First acceptable wins.** Do not score every candidate and compare — that is selecting on the
  outcome.

## Where the reference comes from

`interpret` needs a reference distribution (`INT-1`) built for this person: the same model, the
same scored variants, a cohort genetically close to them. Nothing ships one. `interpret` asks the
harness for it (`interfaces/reference-distribution.md`), and a separate provider builds it from
1000 Genomes genotypes for that one request, or refuses with its reason. A refusal is a correct
outcome (`INT-2`), and `report` still has something honest to say after one.

What never happens is a distribution made up to let the pipeline finish: no synthetic one, no
borrowed one, no percentile approximated from published population figures. That would be the
`JC-2` violation the whole product exists to avoid.

## Run artifacts

Everything lands under `runs/<run-id>/`:

```
runs/<RUN_ID>/
  ledger.jsonl        append-only: every fact gathered and every judgment made, in order
  result.json         the run's single result — one per run, written last however the run
                      ends, including a run that stops after a named stage (see below)
  door/               hygiene.json, file_facts*.json, classification.json, normalized.*, provenance.json
  ancestry/           projection + the ancestry judgment
  select/             candidates, scoring files, the model choice and its gate
  score/              matching, imputation, coverage metrics, the score
  interpret/          the reference request, the provider's artifact, the adapter's check, the placement
  report/             the report judgment
```

The ledger is the audit trail. Per `DATA-6` it buys traceability, not reproducibility: the
references move, and the All of Us CDR in particular *expires* rather than shifts, so a record
says what a past result was computed against — it does not guarantee you can re-derive it.

## The run result (`result.json`)

**Every run writes exactly one `result.json`, and it is the last thing the run writes.** The
end of the *run*, which is not always the end of the pipeline. The ledger says what happened
along the way; this says how it ended. Write it whatever the ending was — a score, a refusal,
a stop, a session the person ended, or a run configured to stop after a named stage. A run
that ends early still ends, and still writes one.

One ending writes none: a reference provider that never answers, or whose build fails. That is
the environment failing, not an outcome, so `interpret` says why and ends the run without a
result, and the harness, which ran the provider, records the run as an environment failure.

One case where "last" moves, and your brief will say so when it applies: **a run told to stop
after a named stage writes the result the moment that stage concludes**, ahead of writing the
stage's own judgment, rather than waiting for an ending it will never arrive at. Follow the
brief there; the rest of this section is unchanged.

```
python3 tools/record.py --run <RUN_ID> --result - <<'JSON'
{
  "case_id": "<CASE_ID>",
  "run_id": "<RUN_ID>",
  "stage_reached": "select",
  "decided_at": "select",
  "outcome": "refuse",
  "trait_id": "MONDO_0000001",
  "chosen_model": null,
  "session_ended_by_person": false,
  "why": "One or two sentences. The reasoning lives in the stage judgments; this is the summary.",
  "stages": {
    "door":     {"outcome": "proceed", "assay_class": "genotyping-array", "genome_build": "GRCh37"},
    "ancestry": {"outcome": "proceed", "inferred_group": "CSA", "agreement": "agree"},
    "select":   {"outcome": "refuse", "candidates_considered": ["PGS000014", "PGS002243"]}
  }
}
JSON
```

`record.py` rejects the file rather than filing a result nobody can read. The rules that catch
real mistakes:

| Field | Rule |
|---|---|
| `outcome` | Exactly one of `score`, `refuse`, `stop`. **`ask` is not a final outcome** — it is a step that resolves once the person answers, so a run never *ends* on it. Record what it resolved to. |
| `trait_id` | A bare ontology id (`"MONDO_0000001"`) or `null`. Not a list, not an object, and not a trait *name*. |
| `chosen_model` | A bare id (`"PGS000000"`) or `null`. Not a list, not an object, and never an id with a citation or parenthetical attached — `"PGS000000 (Smith 2021)"` is rejected. This one has broken grading before. |
| `stage_reached` | One of the six stages. If it is `door`, `ancestry` or `select`, that stage must appear in `stages`. |
| `decided_at` | One of the six stages, never `null`, and it **must appear in `stages`** — a judgment cannot come from a stage that did not run. It may differ from `stage_reached`, and that is not an error: a run that refuses at `select` and then reaches `report` to deliver that refusal is `decided_at: "select"`, `stage_reached: "report"`. |
| `stages` | **Omit any stage that did not run.** Placeholder values are rejected, so an unrun stage cannot be filed as `"..."`. |
| `session_ended_by_person` | `true` or `false`, never a string. |
| `case_id` | The key is required; the value may be `null`. Nothing tells you which case you are running, so write `null` rather than inventing one. `run_id` you do know — it is in your brief and in every path you write. |

A stage's `outcome` is progress — it says that stage did its job and what it passed on. The
run-level `outcome` is the judgment, and `decided_at` says which stage made it. So
`stages.report.outcome: "proceed"` alongside `outcome: "refuse"` is not a contradiction:
`report` did its job, which was to deliver the refusal `select` made.

`session_ended_by_person: true` records that the person left. It does not replace the
outcome — see `JUDGMENT-CORE.md`: you judge on what you have, *then* stop.
