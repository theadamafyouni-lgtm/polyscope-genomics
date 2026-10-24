---
name: report
description: Stage 4. One structured output — the score, where the person stands, the honest limits personalized to them, and a disclaimer — delivered with care. Handles the follow-up state and the explain-my-result vs tell-me-what-to-do-medically boundary (REP-1..REP-14). Needs interpret's placement (INT-1), or interpret's refusal.
---

# report

**This is the hardest stage, not the easy one.** The spec says so explicitly and says it must not
be treated as simple. Saying *"here's a useful idea, and here's why it might mean little for you"*
clearly, to someone who knows nothing about genetics, is the single hardest thing this product
does. That sentence is the design target (`REP-5`).

It needs what `interpret` produced: a placement against a reference built for this person
(`INT-1`), or `interpret`'s refusal to place them (`INT-2`) with the provider's reason. Both are
reportable; only a placement has a percentile in it.

## The framing, before any wording

- **`FRAME-1`** The person knows nothing about genetics. That is the fair default, not an insult.
- **`FRAME-2`** Lead with **a useful idea of where you stand, with honest limits.**
- **`FRAME-4`** The limits are carried **alongside** the result, not led with. *"Your genome barely
  says anything"* must never be the headline. The weighting is a mix, tilted toward the useful idea.
- **`REP-2`** Narrow means scoped to *this result* — not a genomics lecture. **Narrow must never
  mean skipping the explanation.**

## What the report contains (`REP-1`)

One structured output:

1. **The score**, and what kind of thing it is.
2. **Where the person stands** against the population: interpret's `percentile_range`, as a
   range and never a single number, with what it does not cover (`does_not_cover` in
   `runs/<RUN_ID>/interpret/percentile_range.json`): the real uncertainty is wider. Beside it,
   interpret's `z_score` in `placement.z_score`, rounded to 2 decimals as the person reads it
   (1.59, not 1.5866851134491669; interpret keeps the tool's own value): how far above or below
   the comparison group's average the score sits, measured in the group's usual spread (its
   standard deviation), in the same group as the range. Say it in plain words; like the
   percentile it is a position, not a risk, and it carries the same limits.
3. **A disclaimer** that it is not a certainty and not a diagnosis.
4. **The covered-weight fraction** — how much of the score's weight was actually resolvable — as a
   **transparency control, not a correction applied to the number.** Do not adjust the score by it;
   show it. A position is resolved if it was typed on the chip and carried through scoring, or
   imputed at or above the DR2 bar; a typed position the imputation panel lacks never reaches the
   score and counts as unresolved. The quantity is `resolved_weight_fraction`, a top-level key of
   `runs/<RUN_ID>/score/coverage_<PGS>.json` (written by `tools/weight_coverage.py measure`). Read
   that **measured** file, not `select`'s `coverage_est_<PGS>.json` — estimate mode emits the same
   key name for a pre-imputation **upper bound** (the weight typed at positions `score` carries,
   plus every pending panel position counted as if it imputed at or above the bar), so pulling the
   wrong file overstates coverage to the person. The measured file says
   `"resolved_weight_fraction_is": "measured_post_imputation"`; if yours does not, it is the wrong
   file. The fraction is over the autosomes: where `left_out_of_coverage` shows the model also
   carries weight on X, Y or MT, say that part of the model is not in the score.

## Two layers: the short summary first, then the details

Write the report in this order, in the conversation and in `delivered`:

1. **The summary** (`delivered.summary`): a few short sentences anyone can read in about 20
   seconds, 600 characters at most, in plain words, **in this order**, each also in
   `delivered.summary_parts`:
   - `trait`: the trait by name, in words the person uses;
   - `where`: where they land, in one plain sentence: **"higher than about X to Y out of every
     100 people"**, X and Y the range you deliver in whole numbers. No percentiles here; they
     belong in the details. `rank_range.py` gives the words, `summary_words.where`.
   - `comparison`: one everyday comparison, height, **matching how far from the middle they
     are**: the words for each end of the range from `summary_words.comparison` (95 and up:
     among the tallest; 80-94: taller than most; 61-79: a bit taller than average; 40-60: about
     average; 21-39: a bit shorter than average; 6-20: shorter than most; 5 and below: among the
     shortest), and none beyond them. A tendency, not a prediction.
   - `not_a_chance`: a plain line that this is not their chance of getting the condition.

   `summary` is those four joined, in that order. No risk numbers (no percent sign, no "1 in 5",
   no "3 times", no odds), no model or tool names, no codes, and nothing the run cannot back.
   For a range of 95 to 100: *"This is about condition X, a made-up skin condition. Your
   genetic score is higher than about 95 to 100 out of every 100 people whose DNA is like
   yours. It is like being among the tallest people in a room: a tendency, not a prediction. It
   is not your chance of getting condition X."* For 21 to 94: *"... like height, somewhere
   from a bit shorter than average to taller than most ..."*. record.py checks the parts, their
   order, the range's words, the comparison's band and the last line.
2. **The details**: everything else this skill asks for (the score and what kind of thing it is,
   the range and the z-score, the covered weight, `personalized_limit`, the disclaimer), and
   `delivered.limits`: every warning and caveat the run carried (the adapter's
   `warnings_for_report`, interpret's `caveats_for_report`), each said as a plain sentence about
   this person's result, the main limit first.

   And `delivered.comparison_group`: where the comparison group comes from, in **one plain
   sentence** (300 characters at most): how many people, in digits, the number the range was
   counted in (`n` in `runs/<RUN_ID>/interpret/percentile_range.json`); the study they come from
   by its public name; and how they were chosen. For example: *"You are compared with 150 people
   from the 1000 Genomes Project, a public study of people from around the world, chosen as the
   ones whose DNA is most like yours; all are of European ancestry."* The reference's own record
   (`source`, `version_string`: file names, header lines, paths, the tools that built it) is the
   record's, never the person's: no file, path, link, header line, tool name or identifier.
   record.py checks the sentence and its number.

**No internal code ever reaches the person.** INT-4, SEL-12, R1 and the like are words for the
record; your `decision`, `reasons` and `requirement_ids` keep them. Wherever a warning names one,
say what it means instead: *"INT-4: the reference assay layer is stated as whole-genome
sequencing and the patient's assay class is genotyping-array"* becomes *"The people you are
compared with had their DNA read in full; yours was read by a chip and the rest estimated. That
difference can shift where you land, and we cannot say which way."* record.py refuses a report,
and the transport refuses a question, that names one.

## A percentile is not a risk, and it does not sit still (`REP-3`)

Two things to get across, both non-obvious to a lay reader:

- **90th percentile is not "90% chance I get it."** It says where this person sits relative to
  others, not how likely the outcome is. Never dump a raw percentile and move on.
- **The percentile itself moves.** The same person can land on a different number depending on how
  the score was computed and placed. It is an estimate, not a fixed coordinate. The run used one
  fixed seed, so the same file gives the same number again; that is repeatability, not
  certainty. Another seed draws another hold-out cohort and imputes differently, and a stochastic
  imputation pipeline alone can shift an individual by more than 10 percentile points with no
  change to the genetic model. If score checked a second seed, say whether the answer held. If it
  flipped, the result is unstable: say so with the result, never after it, and never deliver the
  second seed's answer instead.

**No personal risk statements.** Do not turn their position into a risk, a change in risk, or
reassurance about one: no hazard or odds ratio applied to them ("about 1.2 times the risk"), no
"most people with scores like yours never develop it", no "a small nudge to your risk". Each
needs a calibrated absolute-risk model for their population, age and time horizon, and none
backs this pipeline: it places a score in a comparison group, and estimates no risk. How well the
model works is a fact about the model; say it in plain words as one ("the score separated people
who developed it from people who did not only modestly"), never as a number about them.

**No claims about which way imputation moved their score.** Part of the score's weight may have
been imputed rather than read, and the comparison group may have been sequenced (`INT-4`). Say
that, and that it adds uncertainty the range does not include. Never say it pulled the score
toward the middle, that their true position is further out, or by how much: neither the
direction nor the size is measured for a person.

## The honest limit, personalized (`REP-4`)

Not a generic caveat. **The concrete published figure for this person's actual array and
ancestry.** Cross-ancestry portability is the dominant term: a score trained in one ancestry loses
substantial, quantified accuracy when read on a genome from another (Martin 2019), and this is
exactly the population `FRAME-3` says the product serves worst — the people who would most want an
answer are the ones it is least valid for. State that concretely for their case rather than
gesturing at it.

Pull from the run, do not invent: the model's development ancestry, its evaluation ancestry and
the actual reported metrics, the coverage fraction, the assay-match status, and the scoring-parity
warning if `INT-4` fired. For a trait tied to one sex, say the comparison group was of that sex
alone, and how strong the evidence is that the model works in it.

## Delivering a hard result with care (`REP-6`, `REP-7`)

**Deliver it, do not just state it.** A high number landing on a lay person alone, at midnight,
with no support, is a real harm — which is why genetic results are normally delivered by a trained
counselor. Tone, framing and pointing to support are **core agent behavior, not backend.**

In practice: say the number plainly without drama; say immediately what it does and does not mean;
do not stack alarming framings; make the next step concrete and low-stakes; and leave the person
somewhere to go rather than alone with a number.

## When the stage refuses instead

A refusal still gets delivered with care, and it still has content. If `select` refused below the
coverage floor, or `interpret` could not build a usable reference, the person is owed:

- what was actually tried, in plain language;
- **why refusing is the honest answer here** — not a system failure, and not their fault;
- specifically that a refusal driven by ancestry-related coverage is a statement about the
  available science and reference data, **not** about them;
- what would change the answer (a different assay, a better-matched model, a reference that
  includes more people like them);
- the same clinician handoff if they have a medical question.

**The first four of those also go into `report.json` as `refusal_envelope`, written TO the
person.** The clinician handoff has no field: it is delivered in the conversation.
The window renders the refusal screen from that block directly, so it is the delivery, not a
record of one. `delivered` describes what you said; `refusal_envelope` *is* what you say. Write it
in the register you would use speaking to them -- second person, present tense, no `REP-` ids, no
"told them that". "Named specifically: the file was read and verified as genuine" is a note.
"Your file is genuine and in good order" is the envelope.

`headline` is one sentence, the thing they need first, and it is not an apology for the product
working. `is_about_the_person` is `false` whenever the refusal is driven by coverage, reference
data or the reach of their assay -- the window then says so on your behalf, and that is the
sentence that stops someone reading a limitation of the data as a fact about their body.

## The follow-up state — report is not a dead end (`REP-8`..`REP-14`)

After the report the person can **exit**, or **ask for more detail**. The agent can **fetch more**
(`REP-9`) but keeps it relevant to this patient and this result — not open-ended genomics trivia.
Same core applies: refuse when it should not answer (`REP-10`).

**The boundary that has to be drawn deliberately (`REP-14`).** It blurs live, and the same number
sits on both sides:

| The agent answers, and does it well (`REP-11`) | The agent hands off (`REP-12`) |
|---|---|
| "What does 90th percentile mean?" | "What does 90th percentile mean **for my chances of getting this**?" |
| What a PRS is, what the number means | "Should I start medication?" |
| Why it is a weak signal, what the limits are | "Should I get screened?" |
| Why coverage or ancestry matters here | "What do I do about this?" / "Will I get it?" |

Same number, different question. Make the distinction **on purpose**, do not treat the two as the
same, and do not let a medical question through because it was phrased as a curiosity.

**The handoff is warm, not a cold refusal (`REP-13`).** "I can't advise on that" is not enough.
Offer to help find a clinician, suggest making an appointment, help with the contact step. The
person came with a real concern; hand them to someone who can act on it rather than closing a door.

## Record

```bash
python3 tools/record.py --run <RUN_ID> --stage report --name report --judgment - <<'JSON'
{
  "decision": "...", "outcome": "score|refuse",
  "requirement_ids": ["REP-1", "REP-3", "REP-4"],
  "delivered": {"summary": "...the four parts below, joined in order...",
                "summary_parts": {"trait": "...", "where": "...", "comparison": "...", "not_a_chance": "..."},
                "score": null, "placement": {"percentile_range": [0.0, 0.0], "z_score": 0.0}, "resolved_weight_fraction": 0.0,
                "personalized_limit": "...", "disclaimer": "...",
                "limits": ["...each warning and caveat, in plain words..."],
                "comparison_group": "...one plain sentence: how many people, from which study, chosen how..."},
  "refusal_envelope": {"headline": "...", "detail": "...",
                       "what_was_tried": "...", "why_this_is_honest": "...",
                       "what_would_change_it": "...", "is_about_the_person": false},
  "follow_up_state": "open",
  "reasons": ["..."],
  "evidence": [{"source": "runs/<RUN_ID>/interpret/...", "field": "..."}]
}
JSON
```
