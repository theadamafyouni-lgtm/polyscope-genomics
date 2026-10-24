"""record.py and the ancestry call. The agreement is the agent's judgment in one of four words
the skill and the page share: agree, partial, disagree or no-self-report (on 10-15 'partial' was
refused, and before that it hid a finished run's result; it is a real judgment). A partial or
full disagreement is shown to the person at the start of select (ANC-3), so surfaced_to_person
is true or false, never a note, and select's model choice is not recorded while the latest
ancestry call says a mismatch has not been shown: the page would hide the result."""
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


def call(**fields):
    return {"decision": "Closest to the EUR reference group.", "outcome": "proceed", "reasons": ["Example."],
            "evidence": [{"source": "runs/R/ancestry/projection.json"}], "inferred_group": "EUR",
            "used_for_math": "inferred (ANC-4)", **fields}


class AncestryAgreement(unittest.TestCase):
    def rejected(self, run=lambda: None):
        with contextlib.redirect_stderr(io.StringIO()) as err, self.assertRaises(SystemExit):
            run()
        return err.getvalue()

    def test_the_four_agreements_and_a_shown_flag_that_is_true_or_false(self):
        for agreement in ("agree", "partial", "disagree", "no-self-report"):
            record.validate_stage(call(agreement=agreement), "ancestry")
        # Recorded before select shows it (as on 10-08), and again once it has.
        record.validate_stage(call(agreement="partial", surfaced_to_person=False), "ancestry")
        record.validate_stage(call(agreement="disagree", surfaced_to_person=True), "ancestry")
        self.assertIn("'partial'", self.rejected(lambda: record.validate_stage(call(agreement="agrees"), "ancestry")))
        self.rejected(lambda: record.validate_stage(call(), "ancestry"))           # no agreement at all
        # 10-14: a note where the flag belongs, which the page cannot read.
        note = "pending -- surfaced at the start of select, before any model is picked (ANC-3)"
        self.assertIn("true or false", self.rejected(lambda: record.validate_stage(call(agreement="partial", surfaced_to_person=note), "ancestry")))
        # Not an ancestry call: a stop at ancestry, a note without one, another stage.
        record.validate_stage(call(outcome="stop", agreement="mixed"), "ancestry")
        record.validate_stage({"decision": "Asked.", "outcome": "proceed"}, "ancestry")
        record.validate_stage({"agreement": "mixed", "outcome": "proceed"}, "select")

    def test_a_model_is_not_chosen_while_the_latest_call_says_a_mismatch_was_not_shown(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(lib_run, "RUNS_DIR", tmp):
            run = "run-example"
            def ancestry(**fields):
                path = os.path.join(lib_run.run_dir(run, "ancestry"), "ancestry.json")
                Path(path).write_text(json.dumps(call(**fields)))
                lib_run.ledger_append(run, {"kind": "judgment", "stage": "ancestry", "outcome": "proceed",
                                            "artifact": os.path.relpath(path, lib_run.RUNS_DIR)})
            record.check_ancestry_shown(run)                       # no ancestry call: nothing to check
            ancestry(agreement="partial", surfaced_to_person=False)
            self.assertIn("surfaced_to_person", self.rejected(lambda: record.check_ancestry_shown(run)))
            ancestry(agreement="partial", surfaced_to_person=True)  # shown, and recorded again
            record.check_ancestry_shown(run)
            ancestry(agreement="agree")
            record.check_ancestry_shown(run)

    def test_a_person_who_answered_up_front_is_told_a_mismatch_in_the_report(self):
        # The demo's upload form: a partial or full mismatch the agent judges does not change the
        # result is told in the report (delivered.ancestry_note), not asked at select. Only where
        # the person answered up front: a benchmark run's person is shown it at select (ANC-3).
        record.validate_stage(call(agreement="partial", surfaced_to_person=False, surface_in_report=True), "ancestry")
        self.assertIn("surface_in_report", self.rejected(lambda: record.validate_stage(
            call(agreement="partial", surfaced_to_person=False, surface_in_report="later"), "ancestry")))
        with tempfile.TemporaryDirectory() as tmp, patch.object(lib_run, "RUNS_DIR", tmp):
            run = "run-example"
            path = os.path.join(lib_run.run_dir(run, "ancestry"), "ancestry.json")
            Path(path).write_text(json.dumps(call(agreement="partial", surfaced_to_person=False, surface_in_report=True)))
            lib_run.ledger_append(run, {"kind": "judgment", "stage": "ancestry", "outcome": "proceed",
                                        "artifact": os.path.relpath(path, lib_run.RUNS_DIR)})
            self.assertIn("surfaced_to_person", self.rejected(lambda: record.check_ancestry_shown(run)))
            Path(lib_run.run_dir(run), "intake.json").write_text(json.dumps({"condition": "Condition X", "consent": True}))
            record.check_ancestry_shown(run)

    def test_a_model_chosen_without_asking_is_recorded_so_only_where_they_answered_up_front(self):
        choice = {"decision": "Picked PGS005218.", "outcome": "proceed", "chosen_model": "PGS005218", "gate_type": "up-front"}
        with tempfile.TemporaryDirectory() as tmp, patch.object(lib_run, "RUNS_DIR", tmp):
            run = "run-example"
            os.makedirs(os.path.join(tmp, run))
            self.assertIn("up-front", self.rejected(lambda: record.check_gate(run, choice)))
            record.check_gate(run, dict(choice, gate_type="transparency"))     # asked, as a benchmark run is
            Path(tmp, run, "intake.json").write_text(json.dumps({"condition": "Condition X", "consent": True}))
            record.check_gate(run, choice)


if __name__ == "__main__":
    unittest.main()
