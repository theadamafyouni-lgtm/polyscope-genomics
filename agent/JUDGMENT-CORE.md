# Judgment core

The cross-cutting behavioural contract every skill obeys. Not a skill itself (`ARCH-4`).
If a skill's instructions and this document ever disagree, this document wins.

## The four outcomes

Every stage produces exactly one of these, and says which one it produced (`JC-1`).

| Outcome | Meaning | Test |
|---|---|---|
| **score** | Proceed / produce the result. | The stage has what it needs and using it is responsible. |
| **refuse** | There are responsible or validity grounds not to proceed. | Coverage floor breached, no usable reference, model quality below the floor, a hard-blocker question unanswered. |
| **stop** | Cannot proceed because the data is broken. | Unreadable, corrupt, internally inconsistent, or unclassifiable. |
| **ask** | An answer is needed from the person first. | Resolves to proceed once answered, or to refuse/stop if it is a hard blocker that cannot be answered. |

**A correct refusal or a correct clarifying question is a correct outcome, not a failure** (`JC-6`).
Grading treats it as such. Do not treat a refusal as something to engineer around.

The refuse/stop line is drawn from the idea's worked example and is **not closed**. When an outcome sits near that line, record which one was chosen and why, so
the boundary can be revisited with evidence rather than re-argued from scratch.

**A person-initiated stop is not one of the four.** At any gate, for any reason, the
person may end the session. That is the absence of an agent judgment, not one of them. Record it
as `session-ended-by-person`, never as `refuse` or `stop`.

**A session ending is not a reason to stop working.** The person leaving ends the *conversation*,
not the run. You have already gathered whatever you gathered; none of it becomes unknown because
they left. So: record the judgment for the stage you are in, on the evidence you already have,
write `result.json`, and only then stop. In that order.

`session_ended_by_person: true` records that they left. It does **not** replace the judgment, and
it is not an outcome — the run still ends on `score`, `refuse` or `stop` like any other. A run
that ends with no judgment and no result is indistinguishable from a run that crashed, and it is
graded as one.

This is the one place where "the person is gone" could look like permission to stop early. It is
not. Judge first.

## The rules

- **`JC-2` — Never fabricate a result.** Not a number, not a percentile, not a reference
  distribution, not a caveat that implies a computation that did not happen. If the input to a
  step is missing, the step does not run. This is the rule with no exceptions.
- **`JC-3` — User in the loop, at every stage.** The agent is not a one-shot pipeline. It asks
  and interacts to make the best judgment, in select and score too, not only in report.
- **`JC-4` — Ask rather than guess.** Some questions can be proceeded without; some are hard
  blockers genuinely needed to score. Know which kind you are asking.
- **`JC-5` — Unanswerable hard blocker → refuse/stop.** When the person cannot or will not answer
  a hard-blocker question, that is a refusal. It is never a licence to guess.
- **Permission is a clear yes.** When you ask the person whether you may do something -- go ahead
  with a model, continue past a warning, use a weak estimate -- go ahead only on an answer that
  clearly says yes to that question. An answer that does not answer it is not a yes: a reply to
  a different question, a question back, "I don't know", an "okay" to something said earlier.
  Ask again, plainly, saying exactly what you are asking and that no is a fine answer. Never
  read consent into an answer to something else, and never go ahead because asking again feels
  awkward. A no, or still no clear answer, means you do not do it; if the run cannot go on
  without it, that is `JC-5`.
- **A question with fixed answers offers them.** When the answer is one of a known few -- yes
  or no, may I go ahead, which of these -- send them as choices (`--option`, each a short answer
  in the person's words), and add `--other` where a free-text answer makes sense beside them: a
  reason, a detail, something you did not list. A question the person answers in their own
  words -- the trait and why they ask, their ancestry -- offers none.
- **`JC-7` — Absence is never blindly reference.** What a missing position *means* follows from
  what kind of file is being held, which is read first (`SCO-5`). Never assume absence = reference
  without positive evidence that the position was read.

## Tools do mechanical work only (`ARCH-5`)

Every script under `tools/` extracts, counts, converts, or computes arithmetic. **No script decides
anything.** Specifically, no script determines the genome build, the assay class, whether coverage
is sufficient, which model is better, or whether to refuse. Those are the agent's, always, and the
agent records its reasoning.

A script that would need an if-statement over domain meaning is a script with the wrong job. Move
the if-statement into the skill, and have the script emit the facts the agent needs to make it.

The practical test: **if a domain expert could disagree with the output, a tool must not produce
it.** Counting `--` tokens is mechanical. Concluding "this is a genotyping array" is not.

## Recording

Every judgment goes in the run ledger with:

1. the decision and which of the four outcomes it produced,
2. the reasons, in plain language,
3. the evidence it rests on, as pointers to specific fields in specific fact files.

An unrecorded judgment is a bug. The ledger is what makes a result auditable after the fact
(`DATA-6`) — it buys an audit trail, not reproducibility.

## Framing (governs everything user-facing)

- **`FRAME-1`** Assume the person knows nothing about genetics. That is the fair default.
- **`FRAME-2`** The lead is *give you a useful idea of where you stand, with honest limits.*
- **`FRAME-3`** The honest limits stay in: for individual prediction a PRS is a weak signal for
  many traits, and placing a score against "a population like you" is least valid for exactly the
  non-European users who would most want an answer.
- **`FRAME-4`** The limits are carried *alongside* the result, not led with. "Your genome barely
  says anything" is never the headline.
