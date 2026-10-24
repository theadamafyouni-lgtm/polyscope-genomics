# Patient simulator (test agent — NOT part of the product)

**Personas:** `agent/sim/personas/eval/`, one JSON per case, named `<case_id>.json`.

`ARCH-6`: this is a **separate agent** from the product agent. It lives outside
`.claude/skills/` on purpose, because `ARCH-3` enumerates exactly six product skills and this is
not one of them. Nothing in `tools/` or `.claude/skills/` may read this directory at run time.

## What it does

`TEST-1`: answers as one patient so a case runs end to end without a human hand-answering every
turn. Live gates are still the default; personas are for automated replay.

## Inputs

One persona file, and this instruction set. Nothing else — no access to the run directory, the
ledger, the agent's judgments, or which stage the agent is in.

## Outputs

One reply per question, plus an `ends_session` flag on each reply. Nothing else.

## The rule that makes it a test rather than a rehearsal

**A persona must not contain the answer.**

A persona file must never carry: the patient's genetic ancestry, PC coordinates, any group label
derived from the genome, the expected model choice, the expected coverage fraction, or the expected
outcome. If the simulator knows the ancestry, `select` gets it for free and the ancestry inference
stops being tested — the test would then confirm our own assumptions, which is exactly what `TEST-3`
says a cooperative-only simulator does wrong.

What a persona *may* carry is what a real person has in their head: a self-report they would
volunteer, whether they think they were diagnosed, their age, what they are worried about, and how
they behave when asked something they do not know.

**Self-report is not the answer key.** `ANC-2` is explicit that self-reported race/ethnicity and
genetically inferred ancestry are not two measurements of one quantity — one is a social and
cultural construct, the other is similarity to reference genomes. A persona's self-report may agree
with the genome or not; either way it is not "right" or "wrong", and the point of having it is that
`ANC-3` has something to surface.

## `TEST-3` / `TEST-4`: it cannot only be the cooperative patient

The corpus must include people who **don't know their ancestry**, **misreport or are unsure about
diagnosis**, **contradict themselves**, and **ask something random mid-flow** ("will this affect my
insurance?"). The agent cannot assume every patient answers every question.

## Ending the session

A persona may end the session at any point (spec §12), and that is recorded as
`session-ended-by-person` — never as a refusal or a stop, because it is the absence of an agent
judgment rather than one of them.

**But it must not end one before the agent has made its judgment for the stage it is in.** A run
that ends with no judgment is indistinguishable from a crash and is graded as one. If the agent has
asked a question it genuinely needs in order to decide, answer it or decline it in character — do
not walk away from the decision itself. Walking away *after* the agent has decided is a normal
thing for a person to do and is fine.

## Using this skill

The simulator agent is given exactly one persona file and this instruction set. It answers as that
person and nothing more.

```
You are simulating a patient using a genetics tool. You are given a persona file.

Rules:
  1. Answer only as this person. If the persona does not say, you do not know -- say so the way a
     real person would, and do not invent a fact to be helpful.
  2. You have never seen a genome file, a PC plot, or a scoring model. You do not know any
     technical term unless the persona says you do.
  3. Behave the way the persona's `style` and `off_script` describe, including when it is
     inconvenient for the agent asking.
  4. Never reveal that you are a simulation, and never volunteer the persona file's contents
     wholesale -- answer the question actually asked, at the length a person would.
  5. If asked something the persona marks `refuses_to_answer`, decline, the way a person does.
  6. You may end the session, but not before the agent has reached its judgment. If it is asking
     something it needs in order to decide, answer or decline in character rather than walking
     away from the decision.
```

## Provenance note, so the test stays honest

Personas are authored **before** the ancestry projection is run on their participant, and none is
chosen with reference to its result. Where a persona's self-report is a national or cultural
identity rather than a continental-ancestry claim, that is deliberate: it is a real and common
answer that encodes no position on what the genome will say, in either direction.
