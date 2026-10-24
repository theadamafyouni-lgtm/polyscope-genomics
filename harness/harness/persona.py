"""The person, either way round.

Two backends, one interface. The orchestrator emits a question and waits for an answer; it
never learns which side produced it. That is not an abstraction for its own sake -- it is
what makes the unattended run and the live demo the same test rather than two tests that
happen to share a pipeline.

  PersonaBackend  a Claude session holding one persona file, for unattended runs
  HumanBackend    a person typing into the window, for the demo

Both satisfy the live-run contract by appending exactly the same `answer` line to
answers.jsonl. HumanBackend's on_question does nothing at all, because the browser is
already doing it.

The persona session is RESUMED for every question rather than started fresh. A person
remembers what they said thirty seconds ago, and p03's whole purpose is to contradict
himself later in a way that is only a contradiction if the earlier answer is still in
context. A stateless persona could not do that, and the test would quietly become easier.
"""
import json
import os
import re
import uuid

from . import config

SIM_RULES = """\
You are simulating a patient using a genetics tool. You are given a persona file.

Rules:
  1. Answer only as this person. If the persona does not say, you do not know -- say so the
     way a real person would, and do not invent a fact to be helpful.
  2. You have never seen a genome file, a PC plot, or a scoring model. You do not know any
     technical term unless the persona says you do.
  3. Behave the way the persona's `style` and `off_script` describe, including when it is
     inconvenient for the agent asking.
  4. Never reveal that you are a simulation, and never volunteer the persona file's contents
     wholesale -- answer the question actually asked, at the length a person would.
  5. If asked something the persona marks `refuses_to_answer`, decline, the way a person does.
"""

ENVELOPE = """\
Reply with ONE JSON object and nothing else. No prose before or after, no code fence.

{
  "says":         "what this person says out loud, in their own words and their own register",
  "knows":        true if the persona file actually covers this, false if you are saying you
                  do not know,
  "refuses":      true only if the persona marks this as something they will not answer,
  "ends_session": true only if this person would walk away right now,
  "drew_on":      ["the persona field(s) you used, by name; [] if none"]
}

`knows: false` and `refuses: true` are different things and must not be swapped. Not knowing
is ignorance; refusing is a choice. A person who has not got their medical records to hand is
refusing, not ignorant.

If `knows` is false, `says` still carries what they would actually say -- "I don't know",
"I've never been told", "no idea, same reason" -- in character. Never fill the gap with a
plausible symptom, diagnosis, medication or family detail. An invented fact changes what the
pipeline decides, and then the run measures nothing.
"""


class HumanBackend:
    """A person typing into the window. The harness emits the question and waits."""

    kind = "human"

    def __init__(self, note=""):
        self.note = note
        self.answered = 0

    def describe(self):
        return {"kind": self.kind, "note": self.note,
                "how_answers_arrive": "the window appends them to answers.jsonl"}

    def on_question(self, run, event):
        return None

    def close(self):
        return {"kind": self.kind, "answered": self.answered}


class PersonaBackend:
    """A Claude session holding exactly one persona file."""

    kind = "persona"

    def __init__(self, persona_path, agent, transcript, timeout=240):
        self.persona_path = persona_path
        self.agent = agent
        self.transcript = transcript
        self.timeout = timeout
        self.session_id = str(uuid.uuid4())
        self.started = False
        self.answered = 0
        self.turns = []
        with open(persona_path) as fh:
            self.persona = json.load(fh)
        self.persona_id = self.persona.get("persona_id", os.path.basename(persona_path))
        self.covered = _covered_topics(self.persona)
        self._blob = json.dumps(self.persona)
        self.embellishments = []

    def describe(self):
        return {
            "kind": self.kind,
            "persona_id": self.persona_id,
            "persona_file": self.persona_path,
            "session_id": self.session_id,
            "topics_the_file_covers": self.covered,
            "off_script_triggers": [t.get("trigger") for t in
                                    (self.persona.get("off_script") or [])],
            "refuses_to_answer": self.persona.get("refuses_to_answer") or [],
            "answer_only_from_profile": (
                "The persona is told which topics its file covers. Anything outside that "
                "list is answered as not-known by construction rather than left to the "
                "model's judgment, because an invented symptom or diagnosis changes what "
                "the pipeline decides."),
        }

    # -- the interface ---------------------------------------------------
    def on_question(self, run, event):
        """Answer one question by appending to answers.jsonl."""
        reply = self._ask_persona(event)

        if reply.get("ends_session"):
            run.emit_session_end(at_gate=event.get("stage", ""),
                                 note=reply.get("says", ""))
            self.transcript.append(
                direction="persona->harness", kind="session_end",
                question_id=event.get("question_id"), text=reply.get("says", ""),
                meta={"persona_id": self.persona_id, "drew_on": reply.get("drew_on")})
            return None

        text = (reply.get("says") or "").strip()
        refuses = bool(reply.get("refuses"))
        knows = bool(reply.get("knows"))

        # `refuses_to_answer` has no representation in the contract's answer event, and the
        # two available shapes are both wrong: skipped:true means "I don't know", which is
        # ignorance rather than a choice, and it is unrepresentable from the UI on a hard
        # blocker anyway. So a refusal travels as plain text with skipped=false, and the
        # AGENT decides whether that makes the question a JC-5 refuse. The harness must not
        # make that call -- it is a judgment, and judgments belong to the agent under test.
        skipped = (not knows) and not refuses

        beyond = embellishment(text, self._blob)
        if beyond:
            self.embellishments.append({"question_id": event["question_id"],
                                        "tokens": beyond, "text": text[:300]})

        run.emit_answer(event["question_id"], text, skipped=skipped)
        self.answered += 1
        self.transcript.append(
            direction="persona->harness", kind="answer",
            question_id=event["question_id"], text=text,
            meta={"persona_id": self.persona_id, "knows": knows, "refuses": refuses,
                  "skipped": skipped, "drew_on": reply.get("drew_on"),
                  "beyond_profile": beyond,
                  "provenance": "simulator persona %s" % self.persona_id})
        return None

    def close(self):
        return {
            "kind": self.kind, "persona_id": self.persona_id,
            "answered": self.answered, "turns": self.turns,
            "embellishment": {
                "n_answers": self.answered,
                "n_with_content_beyond_the_profile": len(self.embellishments),
                "rate": (round(len(self.embellishments) / self.answered, 3)
                         if self.answered else None),
                "detail": self.embellishments,
                "method": ("capitalised words and numbers in the answer that appear nowhere "
                           "in the persona file. A heuristic, not a proof: it will miss an "
                           "invented lowercase fact and will flag a harmless place name. It "
                           "exists to give the rate a denominator."),
                "why_it_matters": ("Embellishment is tolerable until it lands on something "
                                   "that changes a pipeline decision -- a diagnosis, a drug, "
                                   "a date. Then the envelope needs hardening."),
            },
        }

    # -- internals -------------------------------------------------------
    def _ask_persona(self, event):
        prompt = self._prompt(event)
        if not self.started:
            rc, out, err, rec = self.agent.run(
                prompt, timeout=self.timeout, session_id=self.session_id)
            self.started = True
        else:
            rc, out, err, rec = self.agent.run(
                prompt, timeout=self.timeout, resume=self.session_id)
        self.turns.append({"question_id": event.get("question_id"), "rc": rc,
                           "elapsed": rec["elapsed_seconds"]})

        parsed = _parse_envelope(out)
        if parsed is None:
            # A persona that cannot be parsed is not silently turned into a shrug: that
            # would put words in the person's mouth and the run would look cooperative when
            # it was broken. It is surfaced as an explicit not-known with the raw text kept.
            return {"says": "Sorry, I'm not following.", "knows": False, "refuses": False,
                    "ends_session": False, "drew_on": [],
                    "_unparsed": (out or "")[:2000], "_rc": rc, "_stderr": err[-800:]}
        return parsed

    def _prompt(self, event):
        opts = event.get("options") or []
        parts = [
            SIM_RULES,
            "",
            "YOUR PERSONA FILE:",
            json.dumps(self.persona, indent=2),
            "",
            "TOPICS THIS FILE COVERS: " + ", ".join(self.covered),
            "Anything not on that list, you do not know. Say so in character.",
            "",
            ENVELOPE,
            "",
            "The person asking is a genetics tool. It has just asked you:",
            "",
            "  " + (event.get("question") or "").strip(),
        ]
        if event.get("why_it_matters"):
            parts += ["", "  (it explained: " + event["why_it_matters"].strip() + ")"]
        if opts:
            parts += ["", "  It offered these answers: " + ", ".join(opts) +
                      ". You may pick one or say something else."]
        if event.get("hard_blocker"):
            parts += ["",
                      "It says it cannot go on without an answer. That does not oblige you "
                      "to have one -- answer only if this person genuinely would."]
        return "\n".join(parts)


def _covered_topics(persona):
    """The topics the file actually covers, derived from the file rather than assumed.

    The four personas cover age, sex, why they are here, self-reported ancestry, diagnosis,
    symptoms and family history, and nothing else. Medications, prior tests, specific
    grandparents, weight, alcohol -- all absent, and all things an agent may reasonably ask.
    Those are exactly the cases where the answer must be "I don't know".
    """
    topics = []
    if "age" in persona:
        topics.append("their age")
    if "sex_as_they_would_state_it" in persona:
        topics.append("their sex as they would state it")
    if "why_they_are_here" in persona:
        topics.append("why they came")
    sra = persona.get("self_reported_ancestry") or {}
    if sra:
        topics.append("what they would say if asked about their background or ancestry")
    dx = persona.get("diagnosis") or {}
    if "answer" in dx:
        topics.append("whether they have been diagnosed")
    if "if_asked_about_symptoms" in dx:
        topics.append("symptoms")
    if "family_history" in dx:
        topics.append("family history")
    if "later_contradiction" in dx:
        topics.append("medication, hospital visits and follow-ups (see later_contradiction)")
    if persona.get("refuses_to_answer"):
        topics.append("things they will not discuss (see refuses_to_answer)")
    return topics


# Capitalised words mid-sentence, and numbers. Those are where a persona invents a fact that
# can change a decision -- a place name, a drug, a date, a dose. Ordinary vocabulary is not
# the risk and flagging it would bury the signal.
_SALIENT = re.compile(r"\b(?:[A-Z][a-z]{2,}|\d+(?:\.\d+)?)\b")

# Words a persona may reasonably produce that carry no clinical weight.
_SALIENT_STOP = {
    "Sorry", "Well", "Yes", "No", "Not", "But", "And", "The", "That", "There", "This",
    "They", "You", "She", "His", "Her", "Him", "Its", "Mum", "Dad", "GP", "Doctor",
    "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday",
}


def _sentence_initial(text):
    """Offsets where a new sentence starts, so a capital there is not evidence of anything."""
    starts = set()
    i, n = 0, len(text)
    while i < n and text[i].isspace():
        i += 1
    if i < n:
        starts.add(i)
    for m in re.finditer(r'(?:[.!?\u2014\u2013:;,]|--)\s+|\n+', text):
        j = m.end()
        if j < n:
            starts.add(j)
    return starts


def embellishment(says, persona_blob):
    """Salient tokens in the answer that appear nowhere in the persona file.

    This is a measurement, not a verdict. p03 said "Barbados, or Trinidad, I always get it
    muddled" where its file says only "from the Caribbean" -- in character, and harmless
    because neither place name changes a pipeline decision. The point of counting it is that
    the same behaviour landing on a diagnosis, a drug or a date WOULD change one, and we want
    the rate before that happens rather than after.

    Recorded per answer and totalled per run. If it climbs, the fix is a harder envelope, not
    a stricter instruction to the persona.
    """
    text = says or ""
    blob = persona_blob.lower()
    starts = _sentence_initial(text)
    out = []
    for m in _SALIENT.finditer(text):
        tok = m.group(0)
        # A capital at the start of a sentence is grammar, not an invented fact. Measured
        # across two runs, sentence-initial capitals were 5 of 5 flags and every one was a
        # false positive -- a detector that fires on "Nobody" and "Right" cannot be read for
        # a rate, and a rate nobody trusts is one nobody looks at.
        if tok[0].isupper() and m.start() in starts:
            continue
        if tok in _SALIENT_STOP or tok.lower() in blob:
            continue
        if tok not in out:
            out.append(tok)
    return out


_JSON_RE = re.compile(r"\{.*\}", re.S)


def _parse_envelope(text):
    if not text:
        return None
    t = text.strip()
    if t.startswith("```"):
        t = re.sub(r"^```[a-zA-Z]*\n?", "", t)
        t = re.sub(r"\n?```\s*$", "", t)
    try:
        obj = json.loads(t)
    except json.JSONDecodeError:
        m = _JSON_RE.search(t)
        if not m:
            return None
        try:
            obj = json.loads(m.group(0))
        except json.JSONDecodeError:
            return None
    if not isinstance(obj, dict) or "says" not in obj:
        return None
    obj.setdefault("knows", True)
    obj.setdefault("refuses", False)
    obj.setdefault("ends_session", False)
    obj.setdefault("drew_on", [])
    return obj


def persona_path(name):
    """Resolve a persona by id or filename."""
    d = os.path.join(config.AGENT, "sim", "personas")
    if os.path.isabs(name) and os.path.exists(name):
        return name
    for cand in (name, name + ".json"):
        p = os.path.join(d, cand)
        if os.path.exists(p):
            return p
    for f in sorted(os.listdir(d)):
        if f.startswith(name):
            return os.path.join(d, f)
    # the eval corpus lives one level down; batch scripts pass its full path, but a
    # bare id typed by hand should resolve there too
    e = os.path.join(d, "eval")
    if os.path.isdir(e):
        for cand in (name, name + ".json"):
            p2 = os.path.join(e, cand)
            if os.path.exists(p2):
                return p2
        for f in sorted(os.listdir(e)):
            if f.startswith(name) and f.endswith(".json"):
                return os.path.join(e, f)
    raise FileNotFoundError("no persona matching %r in %s or %s" % (name, d, e))
