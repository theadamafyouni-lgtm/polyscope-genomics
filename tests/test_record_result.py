"""record.py and what a result carries for the person: the z-score from the same comparison
group as the range (rank_range.py), in interpret's call and in the delivered report; and the
report in two layers, a short plain-words summary first and then the details, with no internal
code (INT-4, SEL-12, R1...) anywhere the person reads."""
import contextlib
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "agent" / "tools"))
import lib_run  # noqa: E402
import record  # noqa: E402


def interpret(**fields):
    return {"decision": "Placed.", "outcome": "score", "reasons": ["Example."],
            "evidence": [{"source": "runs/R/interpret/percentile_range.json"}],
            "percentile_range": [20.77, 93.85], "z_score": 0.25, **fields}


# The summary, in its order: the trait by name; where the person lands, with the range; one
# everyday comparison; and that this is not their chance of getting it.
PARTS = {"trait": "This is about condition X, a made-up skin condition.",
         "where": "Your genetic score is higher than about 21 to 94 out of every 100 people whose DNA is like yours.",
         "comparison": "Like height, somewhere from a bit shorter than average to taller than most: a tendency, not a prediction.",
         "not_a_chance": "It is not your chance of getting condition X."}
SUMMARY = " ".join(PARTS[k] for k in ("trait", "where", "comparison", "not_a_chance"))
LIMITS = ["The comparison group is all European ancestry; yours is partly from outside Europe, so part of where "
          "you land may reflect ancestry rather than your skin.",
          "Most of the score's weight was estimated from nearby positions, not read directly from your file."]


# Where the comparison group comes from, in one plain sentence.
GROUP = ("You are compared with 150 people from the 1000 Genomes Project, a public study of people from "
         "around the world, chosen as the ones whose DNA is most like yours; all are of European ancestry.")


def report(delivered=None, **placement):
    return {"decision": "Delivered; INT-4 and R1 carried as limits.", "outcome": "score", "reasons": ["REP-1."],
            "requirement_ids": ["REP-1", "REP-3"], "evidence": [{"source": "runs/R/interpret/interpret.json"}],
            "delivered": dict({"summary": SUMMARY, "summary_parts": dict(PARTS), "limits": list(LIMITS),
                               "comparison_group": GROUP,
                               "personalized_limit": "It cannot say whether you will need treatment.",
                               "disclaimer": "Not a diagnosis.",
                               "placement": dict({"percentile_range": [21, 94], "z_score": 0.25}, **placement)},
                              **(delivered or {}))}


class ResultFields(unittest.TestCase):
    def rejected(self, run):
        with contextlib.redirect_stderr(io.StringIO()) as err, self.assertRaises(SystemExit):
            run()
        return err.getvalue()

    def test_a_scored_interpretation_and_report_carry_the_z_score(self):
        record.validate_stage(interpret(), "interpret")
        record.validate_stage(report(), "report")
        without = interpret(); del without["z_score"]
        self.assertIn("rank_range.py", self.rejected(lambda: record.validate_stage(without, "interpret")))
        self.rejected(lambda: record.validate_stage(interpret(z_score="0.25"), "interpret"))
        self.rejected(lambda: record.validate_stage(interpret(z_score=float("nan")), "interpret"))
        self.assertIn("z_score", self.rejected(lambda: record.validate_stage(report(z_score=None), "report")))
        # No score, no z-score: a refusal at interpret or report carries none.
        record.validate_stage({"decision": "No group.", "outcome": "refuse"}, "interpret")
        record.validate_stage({"decision": "Refused.", "outcome": "refuse"}, "report")


    def test_the_report_delivers_the_z_score_rounded_to_two_decimals(self):
        # As the person reads it: 1.59, not interpret's 1.5866851134491669.
        for said in (1.59, -0.4, 2.0, 0):
            with self.subTest(said=said):
                record.validate_stage(report(z_score=said), "report")
        for unrounded in (1.5866851134491669, 0.251, -2.0049):
            with self.subTest(unrounded=unrounded):
                self.assertIn("2 decimals", self.rejected(lambda: record.validate_stage(report(z_score=unrounded), "report")))
        # interpret keeps rank_range.py's own value, unrounded.
        record.validate_stage(interpret(z_score=1.5866851134491669), "interpret")


    def test_the_report_says_where_the_comparison_group_comes_from_in_plain_words(self):
        record.validate_stage(report(), "report")
        self.assertIn("comparison_group", self.rejected(lambda: record.validate_stage(report({"comparison_group": None}), "report")))
        self.assertIn("comparison_group", self.rejected(lambda: record.validate_stage(report({"comparison_group": " "}), "report")))
        # The provider's own record of it, as the page showed it on 10-15: files, paths, a header, a tool.
        raw = ("reference genotypes: ref_1kg_PGS005218.vcf.gz (2504 samples; header ##source=1000GenomesPhase3Pipeline, "
               "##fileDate=20150218); cohort selected in the PCA basis built by ancestry.py build-ref from "
               "refdata/ancestry-ref/hgdp_1kgp/GRCh37_HGDP+1kGP_ALL, labelled from refdata/ancestry-ref/hgdp_1kgp.panel")
        said = self.rejected(lambda: record.validate_stage(report({"comparison_group": raw}), "report"))
        for found in ("ref_1kg_PGS005218.vcf.gz", "##", "ancestry.py", "refdata/ancestry-ref"):
            self.assertIn(found, said)
        for words in ("150 people from the 1000 Genomes Project, from ftp://ftp.1000genomes.ebi.ac.uk/vol1.",
                      "150 people chosen by ancestry.py as nearest to you.",
                      "People from the 1000 Genomes Project whose DNA is most like yours.",   # how many?
                      "150 people from the 1000 Genomes Project (INT-4).",
                      GROUP * 3):
            with self.subTest(words=words[:60]):
                self.assertIn("comparison_group", self.rejected(lambda: record.validate_stage(report({"comparison_group": words}), "report")))
        # A refusal has no group to describe.
        record.validate_stage({"decision": "Refused.", "outcome": "refuse"}, "report")

    def test_a_report_for_a_person_who_answered_up_front_explains_the_choice_and_what_they_asked_for(self):
        # The demo's upload form (runs/<RUN_ID>/intake.json): no model was confirmed with them, so the
        # report says which method was picked, why, and what it costs them; an ancestry mismatch the
        # agent chose to tell in the report is told there; and a note for their doctor if they asked.
        choice = ("I picked a 2025 score for condition X built from over a million people of European ancestry, "
                  "because it was tested in people like you with early signs of it. It was built mostly in "
                  "European ancestry, so it says less for the part of yours from outside Europe.")
        ancestry_note = ("You told us your family is of mixed heritage; your DNA is closest to the European reference "
                         "group, with a clear part from outside Europe. The calculation uses your DNA.")
        note = ("I looked at a genetic score for condition X. It places me higher than most people whose DNA is "
                "like mine. It is not a diagnosis and says nothing about treatment.")
        with tempfile.TemporaryDirectory() as tmp, patch.object(lib_run, "RUNS_DIR", tmp):
            run = "run-1"
            record.check_intake_report(run, report())                   # a benchmark run: nothing more
            os.makedirs(os.path.join(tmp, run))
            answers = {"condition": "Condition X", "consent": True}
            Path(tmp, run, "intake.json").write_text(json.dumps(answers))
            self.assertIn("model_choice", self.rejected(lambda: record.check_intake_report(run, report())))
            for bad in (" ", "Picked PGS005218 by tools/select.py from scores_PGS005218.txt.gz."):
                with self.subTest(bad=bad):
                    self.assertIn("model_choice", self.rejected(lambda: record.check_intake_report(run, report({"model_choice": bad}))))
            record.check_intake_report(run, report({"model_choice": choice}))
            record.check_intake_report(run, {"decision": "Refused.", "outcome": "refuse"})   # nothing was picked
            # An ancestry mismatch the agent chose to tell in the report.
            path = os.path.join(lib_run.run_dir(run, "ancestry"), "ancestry.json")
            Path(path).write_text(json.dumps({"agreement": "partial", "surfaced_to_person": False, "surface_in_report": True}))
            lib_run.ledger_append(run, {"kind": "judgment", "stage": "ancestry", "outcome": "proceed",
                                        "artifact": os.path.relpath(path, lib_run.RUNS_DIR)})
            self.assertIn("ancestry_note", self.rejected(lambda: record.check_intake_report(run, report({"model_choice": choice}))))
            record.check_intake_report(run, report({"model_choice": choice, "ancestry_note": ancestry_note}))
            # A note for their doctor, when they asked for one.
            Path(tmp, run, "intake.json").write_text(json.dumps({**answers, "doctor_note": True}))
            both = {"model_choice": choice, "ancestry_note": ancestry_note}
            self.assertIn("doctor_note", self.rejected(lambda: record.check_intake_report(run, report(both))))
            record.check_intake_report(run, report({**both, "doctor_note": note}))
        # Nothing the person reads names an internal code, wherever it is.
        for field in ("model_choice", "ancestry_note", "doctor_note"):
            with self.subTest(field=field):
                self.assertIn("INT-4", self.rejected(lambda: record.validate_stage(report({field: "Because of INT-4."}), "report")))

    def test_the_group_sentence_names_the_size_of_the_group_the_range_was_counted_in(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(lib_run, "RUNS_DIR", tmp):
            run = "run-1"
            record.check_group_size(run, report())                 # no range recorded yet: nothing to check
            os.makedirs(os.path.join(tmp, run, "interpret"))
            with open(os.path.join(tmp, run, "interpret", "percentile_range.json"), "w") as fh:
                json.dump({"tool": "rank_range.py", "n": 150, "n_below": 147}, fh)
            record.check_group_size(run, report())
            said = self.rejected(lambda: record.check_group_size(run, report({"comparison_group": GROUP.replace("150", "2,504")})))
            self.assertIn("150", said)
            record.check_group_size(run, {"decision": "Refused.", "outcome": "refuse"})

    def test_a_scored_report_leads_with_a_short_plain_summary_then_the_details(self):
        record.validate_stage(report(), "report")   # codes in its own decision and reasons are the record's
        self.assertIn("summary", self.rejected(lambda: record.validate_stage(report({"summary": None}), "report")))
        self.assertIn("summary", self.rejected(lambda: record.validate_stage(report({"summary": " "}), "report")))
        self.assertIn("20 seconds", self.rejected(lambda: record.validate_stage(report({"summary": SUMMARY * 3}), "report")))

    def test_the_summary_says_trait_where_comparison_and_not_a_chance_in_that_order(self):
        def parts(**changed):
            new = dict(PARTS, **changed)
            return {"summary_parts": new, "summary": " ".join(new[k] for k in ("trait", "where", "comparison", "not_a_chance"))}
        self.assertIn("summary_parts", self.rejected(lambda: record.validate_stage(report({"summary_parts": None}), "report")))
        for part in PARTS:
            with self.subTest(part=part):
                self.assertIn(part, self.rejected(lambda: record.validate_stage(report(parts(**{part: " "})), "report")))
        backwards = dict(parts(), summary=" ".join(reversed(SUMMARY.split(". "))))
        self.assertIn("in that order", self.rejected(lambda: record.validate_stage(report(backwards), "report")))
        # Where they land: "higher than about X to Y out of every 100 people", X and Y the delivered
        # range in whole numbers (21-94 here); percentiles stay in the details.
        self.assertIn("out of every 100", self.rejected(lambda: record.validate_stage(report(parts(where="Your score is high in the group.")), "report")))
        self.assertIn("out of every 100", self.rejected(lambda: record.validate_stage(report(parts(where="Higher than about 20 to 94 out of every 100 people.")), "report")))
        self.assertIn("percentile", self.rejected(lambda: record.validate_stage(report(parts(
            where="Higher than about 21 to 94 out of every 100 people: the 21st to 94th percentile.")), "report")))
        record.validate_stage(report(parts(where="Your score is higher than about 21 to 94 out of every 100 people like you.")), "report")
        # The comparison matches how far from the middle they are: both ends of the range, and
        # nothing beyond them.
        self.assertIn("taller than most", self.rejected(lambda: record.validate_stage(report(parts(
            comparison="It is like being a bit taller than average: a tendency, not a prediction.")), "report")))
        self.assertIn("among the tallest", self.rejected(lambda: record.validate_stage(report(parts(
            comparison="From a bit shorter than average to taller than most, maybe among the tallest: a tendency.")), "report")))
        top = {"percentile_range": [95, 100]}
        tall = dict(PARTS, where="Your genetic score is higher than about 95 to 100 out of every 100 people like you.",
                    comparison="It is like being among the tallest people in a room: a tendency, not a prediction.")
        joined = " ".join(tall[k] for k in ("trait", "where", "comparison", "not_a_chance"))
        record.validate_stage(report({"summary_parts": tall, "summary": joined}, **top), "report")
        short = dict(tall, comparison="It is like being a bit taller than average: a tendency, not a prediction.")
        self.assertIn("among the tallest", self.rejected(lambda: record.validate_stage(report(
            {"summary_parts": short, "summary": " ".join(short[k] for k in ("trait", "where", "comparison", "not_a_chance"))}, **top), "report")))
        # The last line says this is not their chance; no number reads as a risk.
        self.assertIn("chance", self.rejected(lambda: record.validate_stage(report(parts(not_a_chance="Talk to your doctor.")), "report")))
        for risky in ("It is like a 20% higher risk.", "About 1 in 5 people like you get it.",
                      "It is like being 3 times as likely.", "The odds are a little higher."):
            with self.subTest(risky=risky):
                self.assertIn("risk", self.rejected(lambda: record.validate_stage(report(parts(comparison=risky)), "report")))
        for limits in (None, [], ["Fine.", ""], ["Fine.", 3]):
            with self.subTest(limits=limits):
                self.assertIn("limits", self.rejected(lambda: record.validate_stage(report({"limits": limits}), "report")))

    def test_no_internal_code_reaches_the_person(self):
        for code in ("INT-4", "SEL-12", "ANC-3", "REP-1", "JC-2", "FRAME-1", "SCO-8", "DATA-6", "ARCH-5", "INTAKE-3", "R1"):
            for field in ("summary", "personalized_limit", "disclaimer"):
                with self.subTest(code=code, field=field):
                    said = self.rejected(lambda: record.validate_stage(report({field: "The score is limited (%s)." % code}), "report"))
                    self.assertIn(code, said)
                    self.assertIn("plain words", said)
            self.rejected(lambda: record.validate_stage(report({"limits": LIMITS + ["%s: the assay layers differ." % code]}), "report"))
        refusal = {"decision": "Refused.", "outcome": "refuse", "refusal_envelope": {
            "headline": "We could not place your score.", "detail": "No comparison group fits you (INT-2).",
            "what_was_tried": "...", "why_this_is_honest": "...", "what_would_change_it": "...", "is_about_the_person": False}}
        self.assertIn("INT-2", self.rejected(lambda: record.validate_stage(refusal, "report")))
        # Names the person may meet in their own records are not internal codes.
        names = dict(PARTS, trait="This is about condition X (MONDO_0000001), scored with model PGS000001 on GRCh37.",
                     comparison="Like height, from a bit shorter than average to taller than most (UTF-8, SHA-256, DR2 0.8, COVID-19).")
        record.validate_stage(report({"summary_parts": names, "summary": " ".join(names[k] for k in ("trait", "where", "comparison", "not_a_chance"))}), "report")


if __name__ == "__main__":
    unittest.main()
