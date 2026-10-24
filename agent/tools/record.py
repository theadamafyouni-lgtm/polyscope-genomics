#!/usr/bin/env python3
"""record.py -- store a judgment the agent made, with its reasons and its evidence.

The agent writes the judgment; this tool only validates the shape and files it.
It does not evaluate whether the judgment is correct -- it cannot, and must not try.

Two modes, one principle: check the shape, file it, never judge the content.

  --judgment   one stage's judgment, into runs/<RUN_ID>/<stage>/<name>.json
  --result     the run's single result, into runs/<RUN_ID>/result.json

What --judgment enforces (mechanically):
  * `outcome` is one of the six recognised values,
  * `reasons` is a non-empty list of non-empty strings,
  * `evidence` is a non-empty list, each item pointing at a fact file and a field path,
  * a refuse / stop / ask carries `requirement_ids` naming what drove it.

That last one is the point: a refusal has to cite the requirement it is enforcing, so
"the agent refused" is never an unexamined verdict (JC-6, and spec §16-G's open
refuse-vs-stop boundary stays reviewable).

What --result enforces is narrower and stricter, because result.json is read by a
grader rather than by a person, and a grader cannot see that a field is wrong -- only
that it is present. Every rule below exists because its absence has mis-graded a run:
an `ask` filed as a final outcome when the run had not actually ended, a `chosen_model`
carrying a citation or a parenthetical so the id no longer matched the key, a trait
recorded as a name instead of an ontology id, or a stage block still holding the
template's "...". A run writes exactly one result.json, and it is the last thing it
writes.

Usage:
  python3 tools/record.py --run RUN_ID --stage door --judgment path/to/judgment.json
  cat judgment.json | python3 tools/record.py --run RUN_ID --stage door --judgment -
  python3 tools/record.py --run RUN_ID --result path/to/result.json
  cat result.json | python3 tools/record.py --run RUN_ID --result -
"""
import argparse
import json
import math
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import lib_run  # noqa: E402

REQUIRE_IDS_FOR = {"refuse", "stop", "ask"}

# The six stages, in flow order. `stage_reached` names one of them.
STAGES = ["door", "ancestry", "select", "score", "interpret", "report"]

# A run ends on exactly one of these. `ask` is deliberately absent: it is a step that
# resolves once the person answers, so a run never ends on it. A result filed as `ask`
# is either a run that has not finished or one whose real outcome went unrecorded, and
# accepting it would let both grade as though they had completed.
FINAL_OUTCOMES = {"score", "refuse", "stop"}

# Shapes, not vocabularies -- this tool does not know which traits or models are real,
# and must not. A bare ontology id has no spaces; a trait NAME does. A bare model id is
# exactly PGS followed by six digits; "PGS000000 (Smith 2021)" is not, and that is the
# form that has broken grading before.
TRAIT_ID_RE = re.compile(r"[A-Za-z]+[_:][0-9]+")
MODEL_ID_RE = re.compile(r"PGS[0-9]{6}")

# What each stage block carries beyond `outcome`. A stage that did not run is omitted
# entirely; a stage that ran reports all of its fields.
STAGE_FIELDS = {
    "door": ["assay_class", "genome_build"],
    "ancestry": ["inferred_group", "agreement"],
    "select": ["candidates_considered"],
}

# Template text left where a value belongs. Kept deliberately short and unambiguous:
# a wider list would reject real domain values ("none" is a legitimate answer for
# `agreement` when the person gave no self-report) and a validator that rejects true
# statements is one people route around.
PLACEHOLDERS = {"...", "....", "…", "tbd", "t.b.d.", "todo", "n/a", "<...>", "xxx",
                "string", "example", "fill me in"}


# An ancestry call's agreement is one of these, the words its skill (section 6) and the page
# know: agree; partial, where the self-report matches the genome only in part (an admixed person
# naming two groups, say); disagree; no-self-report. A partial or full mismatch is shown to the
# person at the start of select (ANC-3): surfaced_to_person says whether it has been, true or
# false, and the call is recorded again once it has. Any other word, or a note where the flag
# belongs, hides the run's whole result on the page.
AGREEMENTS = ("agree", "partial", "disagree", "no-self-report")
MISMATCHES = ("partial", "disagree")


def fail(msg):
    print(f"record.py: REJECTED -- {msg}", file=sys.stderr)
    raise SystemExit(2)


def validate(j):
    if not isinstance(j, dict):
        fail("judgment must be a JSON object")
    for field in ("decision", "outcome", "reasons", "evidence"):
        if field not in j:
            fail(f"missing required field: {field}")
    if j["outcome"] not in lib_run.OUTCOMES:
        fail(f"outcome {j['outcome']!r} not one of {sorted(lib_run.OUTCOMES)}")
    if not isinstance(j["decision"], str) or not j["decision"].strip():
        fail("decision must be a non-empty string")
    if not isinstance(j["reasons"], list) or not j["reasons"]:
        fail("reasons must be a non-empty list")
    for r in j["reasons"]:
        if not isinstance(r, str) or not r.strip():
            fail("every reason must be a non-empty string")
    if not isinstance(j["evidence"], list) or not j["evidence"]:
        fail("evidence must be a non-empty list -- a judgment with no evidence is not recordable")
    for e in j["evidence"]:
        if not isinstance(e, dict) or "source" not in e:
            fail("every evidence item needs at least a 'source' (a fact file or artifact path)")
    if j["outcome"] in REQUIRE_IDS_FOR:
        ids = j.get("requirement_ids")
        if not isinstance(ids, list) or not ids:
            fail(f"outcome {j['outcome']!r} requires 'requirement_ids' naming what drove it "
                 f"(e.g. [\"SCO-8\", \"SEL-10\"])")
    return j


# The report's first layer: a few short sentences anyone can read in about 20 seconds.
SUMMARY_MAX_CHARS = 600
# What the person reads: the delivered report's words and a refusal's envelope.
PERSON_FIELDS = ("summary", "personalized_limit", "disclaimer", "comparison_group",
                 "model_choice", "ancestry_note", "doctor_note")
ENVELOPE_FIELDS = ("headline", "detail", "what_was_tried", "why_this_is_honest", "what_would_change_it")


# The summary's parts, in the order it says them: the trait by name; where the person lands, in
# one plain sentence with the range; one everyday comparison (a tendency, not a prediction); and
# that this is not their chance of getting the condition.
SUMMARY_PARTS = ("trait", "where", "comparison", "not_a_chance")
# Numbers that read as a risk: a percent sign, "N in M", "N times", odds.
RISK_NUMBER = re.compile(r"%|\b\d+(?:\.\d+)?\s+in\s+\d+\b|\b\d+(?:\.\d+)?\s*(?:x|times)\b|\bodds\b", re.IGNORECASE)
# Where the comparison group comes from, as the person reads it: one plain sentence. What a person
# never reads there: a link, a file's header line, a file or a path, a tool's name, an identifier.
GROUP_MAX_CHARS = 300
PLUMBING = re.compile(r"\S*://\S*|##\S*|[\w.+-]+\.(?:py|sh|vcf|gz|bgz|tbi|json|jsonl|tsv|csv|bed|bim|fam|pgen|pvar|psam|panel|fa|fasta|txt)\b"
                      r"|[\w.+-]+(?:/[\w.+-]+)+|\b[A-Za-z0-9]+_[A-Za-z0-9_]+\b")
# How many people the group has, in digits: "150 people".
GROUP_SIZE = re.compile(r"\b(\d[\d,]*)\s+(?:people|persons|individuals|participants|adults)\b", re.IGNORECASE)
NOT_A_CHANCE = re.compile(r"\bnot\b.*\b(?:chance|probability|likelihood|risk)\b", re.IGNORECASE | re.DOTALL)


def validate_report_words(j):
    """A scored report leads with a short plain summary, then the details with every limit in
    plain words; nothing the person reads names an internal code. The judgment's own decision,
    reasons and requirement_ids are the record's and keep their codes."""
    delivered = j.get("delivered") if isinstance(j.get("delivered"), dict) else {}
    envelope = j.get("refusal_envelope") if isinstance(j.get("refusal_envelope"), dict) else {}
    parts = delivered.get("summary_parts") if isinstance(delivered.get("summary_parts"), dict) else {}
    said = [(field, delivered.get(field)) for field in PERSON_FIELDS]
    said += [("summary_parts.%s" % part, parts.get(part)) for part in SUMMARY_PARTS]
    said += [("limits[%d]" % i, x) for i, x in enumerate(delivered.get("limits") or []) if isinstance(delivered.get("limits"), list)]
    said += [("refusal_envelope.%s" % field, envelope.get(field)) for field in ENVELOPE_FIELDS]
    for field, words in said:
        found = lib_run.INTERNAL_CODE.findall(words) if isinstance(words, str) else []
        if found:
            fail("%s names %s: an internal code, which the person never reads. Say what it means in "
                 "plain words instead." % (field, ", ".join(sorted(set(found)))))
    if j.get("outcome") != "score":
        return
    summary = delivered.get("summary")
    if not isinstance(summary, str) or not summary.strip():
        fail("a scored report delivers summary first: a few short sentences in plain words, then the details")
    if len(summary) > SUMMARY_MAX_CHARS:
        fail("summary is %d characters; it is the part anyone can read in 20 seconds (%d at most). "
             "The rest belongs in the details." % (len(summary), SUMMARY_MAX_CHARS))
    if not isinstance(delivered.get("summary_parts"), dict):
        fail("a scored report delivers summary_parts, the summary's parts in order: trait, where, "
             "comparison, not_a_chance; summary is them joined")
    for part in SUMMARY_PARTS:
        if not isinstance(parts.get(part), str) or not parts[part].strip():
            fail("summary_parts.%s is missing. The summary says, in this order: the trait by name (trait); "
                 "where the person lands, in one plain sentence with the range (where); one everyday "
                 "comparison, a tendency and not a prediction (comparison); and that this is not their "
                 "chance of getting the condition (not_a_chance)." % part)
    if " ".join(summary.split()) != " ".join(" ".join(parts[p] for p in SUMMARY_PARTS).split()):
        fail("summary is not summary_parts joined in that order: trait, where, comparison, not_a_chance")
    risky = sorted(set(RISK_NUMBER.findall(summary)))
    if risky:
        fail("the summary has %s: a number that reads as a risk. Say where they land, and the "
             "comparison, without one." % ", ".join(repr(r) for r in risky))
    placement = delivered.get("placement") if isinstance(delivered.get("placement"), dict) else {}
    bounds = placement.get("percentile_range")
    if isinstance(bounds, list) and len(bounds) == 2 and all(_finite(b) for b in bounds):
        words = lib_run.summary_words(*bounds)
        core = words["where"].replace(" people", "")
        if core.lower() not in " ".join(parts["where"].lower().split()):
            fail("summary_parts.where says where the person lands as %r, the range the report delivers "
                 "in whole numbers out of every 100 (rank_range.py's summary_words)" % words["where"])
        if re.search(r"percentile", parts["where"], re.IGNORECASE):
            fail("summary_parts.where says 'out of every 100'; percentiles belong in the details")
        comparison = " ".join(parts["comparison"].lower().split())
        missing = [w for w in words["comparison"] if w not in comparison]
        beyond = [w for _, w in lib_run.HEIGHT_BANDS if w in comparison and w not in words["allowed"]]
        if missing or beyond:
            fail("summary_parts.comparison must match how far from the middle the range is: say %s (both "
                 "ends of the range, as height), and not %s" % (" and ".join(repr(w) for w in words["comparison"]),
                                                                  ", ".join(repr(w) for w in beyond) or "words beyond them"))
    if not NOT_A_CHANCE.search(parts["not_a_chance"]):
        fail("summary_parts.not_a_chance says plainly that this is not their chance of getting the condition")
    limits = delivered.get("limits")
    if not (isinstance(limits, list) and limits and all(isinstance(x, str) and x.strip() for x in limits)):
        fail("a scored report delivers limits: every warning and caveat the run carried, each in "
             "plain words, as a list of sentences")
    group = delivered.get("comparison_group")
    if not isinstance(group, str) or not group.strip():
        fail("a scored report delivers comparison_group: one plain sentence saying who the person is "
             "compared with and where those people come from (how many, the study by its public name, "
             "how they were chosen)")
    if len(group) > GROUP_MAX_CHARS:
        fail("comparison_group is %d characters; it is one plain sentence (%d at most)" % (len(group), GROUP_MAX_CHARS))
    plumbing = sorted(set(PLUMBING.findall(group)))
    if plumbing:
        fail("comparison_group names %s: a link, a file, a path or a tool, which the person never reads. "
             "Say where the group comes from in plain words: how many people, the study by its public "
             "name, how they were chosen." % ", ".join(repr(x) for x in plumbing))
    if not GROUP_SIZE.search(group):
        fail("comparison_group says how many people the group has, in digits ('150 people')")


def _finite(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)


def validate_stage(j, stage):
    """What a stage's own call must carry for the page to show the run's result."""
    if stage == "interpret" and j.get("outcome") == "score" and not _finite(j.get("z_score")):
        fail("an interpretation that scores records z_score: the value rank_range.py wrote "
             "(z_score.value), where the score lies in the same comparison group, in the group's "
             "standard deviations")
    if stage == "report":
        validate_report_words(j)
    if stage == "report" and j.get("outcome") == "score":
        placement = (j.get("delivered") or {}).get("placement") if isinstance(j.get("delivered"), dict) else None
        if not (isinstance(placement, dict) and _finite(placement.get("z_score"))):
            fail("a scored report delivers placement.z_score, the z-score interpret recorded, rounded "
                 "to 2 decimals as the person reads it")
        if round(placement["z_score"], 2) != placement["z_score"]:
            fail("placement.z_score is said as the person reads it, rounded to 2 decimals: %s, not %r. "
                 "interpret keeps rank_range.py's own value." % (round(placement["z_score"], 2), placement["z_score"]))
    if stage != "ancestry" or j.get("outcome") != "proceed" or not {"inferred_group", "agreement"} & set(j):
        return
    if j.get("agreement") not in AGREEMENTS:
        fail(f"agreement {j.get('agreement')!r} is not one of {list(AGREEMENTS)}: 'partial' where the "
             f"self-report matches the genome only in part, 'disagree' where it does not, and say "
             f"how in agreement_explained.")
    if "surface_in_report" in j and not isinstance(j["surface_in_report"], bool):
        fail("surface_in_report is true or false: true where a person who answered up front is told "
             "the mismatch in the report (delivered.ancestry_note), not asked at select.")
    if j["agreement"] in MISMATCHES and "surfaced_to_person" in j and not isinstance(j["surfaced_to_person"], bool):
        fail("surfaced_to_person is true or false, not a note: false until select has shown the person "
             "the mismatch (ANC-3), then record the call again with true.")


def check_gate(run_id, j):
    """select's gate: transparency or decision where the person is asked; up-front only where
    they agreed on the demo's upload form (runs/<RUN_ID>/intake.json) and nobody asks them."""
    if j.get("gate_type") == "up-front" and _intake(run_id) is None:
        fail("gate_type 'up-front' is for a person who answered up front and agreed on the form "
             "(runs/<RUN_ID>/intake.json); this person is asked: a transparency or decision gate (select, 6)")


def check_intake_report(run_id, j):
    """A scored report for a person who answered up front (runs/<RUN_ID>/intake.json): nobody
    confirmed the model with them, so it says which scoring method was picked, why, and what the
    choice costs them (model_choice); it tells an ancestry mismatch the agent chose to tell in the
    report (ancestry_note); and it has the note for their doctor they asked for (doctor_note)."""
    answers = _intake(run_id)
    if answers is None or j.get("outcome") != "score":
        return
    delivered = j.get("delivered") if isinstance(j.get("delivered"), dict) else {}
    call, _ = _latest_ancestry(run_id)
    needed = {"model_choice": "which scoring method you picked, why, and what the choice costs them: "
                              "nobody confirmed it with them"}
    if call is not None and call.get("agreement") in MISMATCHES and _told_in_report(call, run_id) \
            and call.get("surfaced_to_person") is not True:
        needed["ancestry_note"] = ("what their DNA shows beside what they told you, and that the "
                                   "calculation uses their DNA: the ancestry call left it to the report")
    if answers.get("doctor_note") is True:
        needed["doctor_note"] = ("the short note they asked for, to take to their doctor: what was "
                                 "looked at, what the result does and does not say, no advice on treatment")
    for field, what in needed.items():
        words = delivered.get(field)
        if not isinstance(words, str) or not words.strip():
            fail("a scored report for a person who answered up front delivers %s: %s" % (field, what))
        plumbing = sorted(set(PLUMBING.findall(words)))
        if plumbing:
            fail("%s names %s: a link, a file, a path or a tool, which the person never reads. Say "
                 "it in plain words." % (field, ", ".join(repr(x) for x in plumbing)))


def check_group_size(run_id, j):
    """A scored report's comparison_group names the size of the group its range was counted in,
    once interpret has recorded that range (runs/<RUN_ID>/interpret/percentile_range.json)."""
    if j.get("outcome") != "score":
        return
    try:
        with open(os.path.join(lib_run.run_dir(run_id, create=False), "interpret", "percentile_range.json")) as fh:
            n = json.load(fh).get("n")
    except (OSError, ValueError, AttributeError):
        return
    if not isinstance(n, int) or isinstance(n, bool):
        return
    group = (j.get("delivered") or {}).get("comparison_group") if isinstance(j.get("delivered"), dict) else None
    sizes = [int(s.replace(",", "")) for s in GROUP_SIZE.findall(group if isinstance(group, str) else "")]
    if n not in sizes:
        fail("comparison_group says the group has %s; the range was counted in %d people "
             "(interpret/percentile_range.json). Say that number." % (
                 " or ".join("%d people" % s for s in sizes) or "no number of people", n))


def _latest_ancestry(run_id):
    """The latest ancestry call the ledger records, and where it is; (None, None) if none."""
    latest = None
    try:
        with open(os.path.join(lib_run.run_dir(run_id, create=False), "ledger.jsonl")) as fh:
            for line in fh:
                try:
                    entry = json.loads(line)
                except ValueError:
                    continue
                if (isinstance(entry, dict) and entry.get("kind") == "judgment" and entry.get("stage") == "ancestry"
                        and isinstance(entry.get("artifact"), str)):
                    latest = entry["artifact"]
    except OSError:
        return None, None
    if latest is None:
        return None, None
    try:
        with open(os.path.join(lib_run.RUNS_DIR, latest)) as fh:
            call = json.load(fh)
    except (OSError, ValueError):
        return None, None
    return (call, latest) if isinstance(call, dict) else (None, None)


def _intake(run_id):
    """The person's answers from the demo's upload form, runs/<RUN_ID>/intake.json; None where
    they were asked instead, as in every benchmark run."""
    try:
        with open(os.path.join(lib_run.run_dir(run_id, create=False), "intake.json")) as fh:
            answers = json.load(fh)
    except (OSError, ValueError):
        return None
    return answers if isinstance(answers, dict) else None


def _told_in_report(call, run_id):
    """A mismatch the agent judged does not change the result, for a person who answered up
    front: told in the report, not asked at select."""
    return call.get("surface_in_report") is True and _intake(run_id) is not None


def check_ancestry_shown(run_id):
    """Before select records a judgment: the latest ancestry call, if it found a partial or full
    mismatch, says it has been shown to the person, or for a person who answered up front that
    the report will tell it. Otherwise the page would hide the result."""
    call, latest = _latest_ancestry(run_id)
    if call is None or _told_in_report(call, run_id):
        return
    if call.get("agreement") in MISMATCHES and call.get("surfaced_to_person") is not True:
        fail(f"the latest ancestry call ({latest}) found a {call['agreement']!r} match and its "
             f"surfaced_to_person is not true. Show the person the mismatch now, at the start of "
             f"select (ANC-3), record the ancestry call again with surfaced_to_person true, then "
             f"record this.")


# ---------------------------------------------------------------------------
# result.json
# ---------------------------------------------------------------------------

def _is_placeholder(v):
    return isinstance(v, str) and (not v.strip() or v.strip().lower() in PLACEHOLDERS)


def _require_str(obj, key, where):
    v = obj.get(key)
    if not isinstance(v, str) or not v.strip():
        fail(f"{where}.{key} must be a non-empty string, got {type(v).__name__}: {v!r}")
    if _is_placeholder(v):
        fail(f"{where}.{key} is still the template placeholder {v!r}. Omit the block if "
             f"that stage did not run; fill it in if it did.")
    return v


def _bare_id(obj, key, pattern, example, why):
    """A bare id string, or null. Never a list, an object, or an id with text attached."""
    v = obj[key]
    if v is None:
        return None
    if not isinstance(v, str):
        fail(f"{key} must be a bare string like {example!r}, or null -- not a "
             f"{type(v).__name__}. {why}")
    if not pattern.fullmatch(v):
        fail(f"{key} must be exactly one bare id like {example!r}, or null. Got {v!r}. {why}")
    return v


def _model_list(block, name):
    v = block["candidates_considered"]
    if not isinstance(v, list):
        fail(f"stages.{name}.candidates_considered must be a list of bare PGS ids, got "
             f"{type(v).__name__}")
    for i, m in enumerate(v):
        if not isinstance(m, str) or not MODEL_ID_RE.fullmatch(m):
            fail(f"stages.{name}.candidates_considered[{i}] must be a bare id like "
                 f"'PGS000000'; got {m!r}")
    return v


def validate_result(r, run_id=None):
    """The shape of runs/<RUN_ID>/result.json. Raises SystemExit(2) on any violation.

    Mirrored by the harness, which re-validates the file it reads back rather than
    trusting that this ran. If you change a rule here, change it there too --
    harness/harness/orchestrator.py, validate_result().
    """
    if not isinstance(r, dict):
        fail("result must be a JSON object")

    for field in ("case_id", "run_id", "stage_reached", "decided_at", "outcome",
                  "trait_id", "chosen_model", "session_ended_by_person", "why", "stages"):
        if field not in r:
            fail(f"missing required field: {field}")

    # The key is required; a value is not. Nothing in the brief tells the agent which
    # case it is running -- the case id lives harness-side -- so demanding a non-empty
    # one would be demanding an invention, and JC-2 is the rule with no exceptions. It
    # stays mandatory-but-nullable so the agent states that it does not know rather than
    # omitting the field, and so a harness that later passes one in needs no change here.
    if r["case_id"] is not None:
        _require_str(r, "case_id", "result")
    rid = _require_str(r, "run_id", "result")
    if run_id is not None and rid != run_id:
        fail(f"run_id {rid!r} does not match --run {run_id!r}")

    if r["stage_reached"] not in STAGES:
        fail(f"stage_reached {r['stage_reached']!r} not one of {STAGES}")

    # Which stage made the judgment, which is not always the stage the run reached: a run
    # can refuse at `select` and still reach `report` to deliver that refusal. Where the
    # two differ is exactly what a grader of refusals wants, and `stage_reached` cannot
    # say it. So a `decided_at` that differs from `stage_reached` is not an error, and
    # nothing here should ever start requiring them to agree. Null is not one of the six.
    if r["decided_at"] not in STAGES:
        fail(f"decided_at {r['decided_at']!r} not one of {STAGES}")

    outcome = r["outcome"]
    if outcome == "ask":
        fail("outcome 'ask' is not a final outcome. An ask is a step that resolves once "
             "the person answers -- a run never ends on it. Record what it resolved to: "
             "score, refuse or stop. If the person left before answering, that is "
             "session_ended_by_person true, and the outcome is still the one the stage "
             "reached on the evidence it already had.")
    if outcome not in FINAL_OUTCOMES:
        fail(f"outcome {outcome!r} not one of {sorted(FINAL_OUTCOMES)}")

    _bare_id(r, "trait_id", TRAIT_ID_RE, "MONDO_0000001",
             "A trait name is not a trait id.")
    _bare_id(r, "chosen_model", MODEL_ID_RE, "PGS000000",
             "An id with a citation, a parenthetical or a score name appended is not a "
             "bare id, and is what has broken grading before.")

    if not isinstance(r["session_ended_by_person"], bool):
        fail("session_ended_by_person must be true or false, not "
             f"{type(r['session_ended_by_person']).__name__}")

    _require_str(r, "why", "result")

    if outcome == "score" and r["chosen_model"] is None:
        fail("outcome is 'score' but chosen_model is null -- a score is only a result "
             "alongside the model it was computed with")

    stages = r["stages"]
    if not isinstance(stages, dict):
        fail(f"stages must be an object keyed by stage name, got {type(stages).__name__}")
    for name in stages:
        if name not in STAGES:
            fail(f"stages has an unknown stage {name!r}; expected one of {STAGES}")
        block = stages[name]
        if not isinstance(block, dict):
            fail(f"stages.{name} must be an object, got {type(block).__name__}")
        oc = block.get("outcome")
        if oc not in lib_run.OUTCOMES:
            fail(f"stages.{name}.outcome {oc!r} not one of {sorted(lib_run.OUTCOMES)}")
        for field in STAGE_FIELDS.get(name, []):
            if field not in block:
                fail(f"stages.{name} is missing {field!r}. Omit the whole block if that "
                     f"stage did not run; do not file it with the field absent.")
            if field == "candidates_considered":
                _model_list(block, name)
            else:
                _require_str(block, field, f"stages.{name}")

    reached = r["stage_reached"]
    if reached in STAGE_FIELDS and reached not in stages:
        fail(f"stage_reached is {reached!r} but stages carries no {reached!r} block. The "
             f"stage the run reached has to report what it did there.")

    # Stricter than the rule above, and for all six stages rather than the three that
    # carry required fields: a judgment cannot have come from a stage that did not run.
    decided = r["decided_at"]
    if decided not in stages:
        fail(f"decided_at is {decided!r} but stages carries no {decided!r} block. The "
             f"stage that made the judgment is by definition one that ran.")

    return r


def _file_result(args, r):
    validate_result(r, run_id=args.run)
    r.setdefault("recorded_utc", lib_run.utcnow())
    path = os.path.join(lib_run.run_dir(args.run), "result.json")
    lib_run.write_json(path, r)
    lib_run.ledger_append(args.run, {
        "kind": "result",
        "stage_reached": r["stage_reached"],
        "outcome": r["outcome"],
        "trait_id": r["trait_id"],
        "chosen_model": r["chosen_model"],
        "session_ended_by_person": r["session_ended_by_person"],
        "artifact": os.path.relpath(path, lib_run.RUNS_DIR),
    })
    print(f"recorded result {r['outcome']} at {r['stage_reached']} "
          f"(model {r['chosen_model']})  -> {path}")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--run", required=True)
    ap.add_argument("--stage", choices=STAGES + ["session"],
                    help="required with --judgment; result.json carries stage_reached instead")
    ap.add_argument("--judgment", help="path to a JSON file, or - for stdin")
    ap.add_argument("--result", help="path to a JSON file, or - for stdin")
    ap.add_argument("--name", help="filename stem under runs/<run>/<stage>/ (default: judgment)")
    args = ap.parse_args()

    if bool(args.judgment) == bool(args.result):
        fail("pass exactly one of --judgment (one stage's judgment) or --result (the "
             "run's single result.json)")

    src = args.judgment or args.result
    raw = sys.stdin.read() if src == "-" else open(src).read()
    try:
        j = json.loads(raw)
    except json.JSONDecodeError as exc:
        fail(f"not valid JSON: {exc}")

    if args.result:
        return _file_result(args, j)

    if not args.stage:
        fail("--judgment requires --stage")

    validate(j)
    validate_stage(j, args.stage)
    if args.stage == "select":
        check_ancestry_shown(args.run)
        check_gate(args.run, j)
    if args.stage == "report":
        check_group_size(args.run, j)
        check_intake_report(args.run, j)
    j.setdefault("stage", args.stage)
    j.setdefault("recorded_utc", lib_run.utcnow())

    stem = args.name or "judgment"
    path = os.path.join(lib_run.run_dir(args.run, args.stage), f"{stem}.json")
    lib_run.write_json(path, j)
    lib_run.ledger_append(args.run, {
        "kind": "judgment",
        "stage": args.stage,
        "decision": j["decision"],
        "outcome": j["outcome"],
        "requirement_ids": j.get("requirement_ids", []),
        "artifact": os.path.relpath(path, lib_run.RUNS_DIR),
    })
    print(f"recorded {j['outcome']}: {j['decision']}  -> {path}")


if __name__ == "__main__":
    main()
