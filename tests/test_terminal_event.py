"""The one terminal live-run event a run ends with (Orchestrator._emit_terminal), for judgments
as _collect_judgments hands them over: in file-name order, which is not the order they were made.
The order they were made is the run's ledger, where tools/record.py appends each one as it is
recorded; and a valid result.json is how the run says it ended. Synthetic judgments only; no
agent, no pipeline."""
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "harness"))
from harness import orchestrator  # noqa: E402

RUN = "run-20261014-000000-abcdef"


class Live:
    dir = "/live/" + RUN

    def __init__(self):
        self.events = []

    def emit(self, kind, **fields):
        self.events.append(dict(fields, kind=kind))


class TerminalEventTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.o = orchestrator.Orchestrator.__new__(orchestrator.Orchestrator)
        self.o.prs_root, self.o.live, self.o.bare, self.o.stop_after = self.tmp.name, Live(), False, None
        self.o.run_id = RUN
        # Any caveat means the terminal event itself failed: fail the test with it.
        self.o.manifest = type("Manifest", (), {"caveat": lambda _self, text: self.fail(text), "data": {},
                                                "set": lambda _self, key, value: _self.data.__setitem__(key, value)})()
        self.clock = 1_800_000_000

    def judgment(self, stage, name, outcome, recorded=True, **fields):
        """A judgment file written now (each one a minute after the last), and recorded in the
        ledger as record.py records it, as listed."""
        artifact = os.path.join("runs", RUN, stage, name)
        path = Path(self.tmp.name) / artifact
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(dict(fields, outcome=outcome)))
        self.clock += 60
        os.utime(path, (self.clock, self.clock))
        if recorded:
            with open(Path(self.tmp.name) / "runs" / RUN / "ledger.jsonl", "a") as fh:
                fh.write(json.dumps({"kind": "facts", "stage": stage, "tool": "x.py"}) + "\n")
                fh.write(json.dumps({"kind": "judgment", "stage": stage, "outcome": outcome,
                                     "artifact": os.path.join(RUN, stage, name)}) + "\n")
        return dict(fields, stage=stage, artifact=artifact, outcome=outcome)

    def ending(self, judgments, rc=0, result=None, output=None):
        listed = sorted(judgments, key=lambda j: j["artifact"])  # as _collect_judgments lists them
        extra = ([result] if result is not None or output is not None else []) + ([output] if output is not None else [])
        self.o._emit_terminal(listed, rc, None, *extra)
        (event,) = self.o.live.events
        return event

    def result(self, outcome, decided_at, **fields):
        return {"valid": True, "state": "valid", "result": dict(fields, outcome=outcome, decided_at=decided_at)}

    def test_a_delivered_report_is_the_ending_though_earlier_models_were_refused_at_score(self):
        # Two models refused at score, the third scored, and the report delivered it.
        refused_first = self.judgment("score", "score_PGS000001.json", "refuse", model="PGS000001")
        refused_second = self.judgment("score", "score_PGS000002.json", "refuse", model="PGS000002")
        scored = self.judgment("score", "score.json", "score", model="PGS000003")
        interpret = self.judgment("interpret", "interpret.json", "score")
        report = self.judgment("report", "report.json", "score")
        event = self.ending([refused_first, refused_second, scored, interpret, report])
        self.assertEqual((event["kind"], event["outcome"]), ("run_completed", "score"))

    def test_a_report_that_refused_is_the_ending_with_its_own_words(self):
        scored = self.judgment("score", "score.json", "score")
        report = self.judgment("report", "report.json", "refuse", refusal_envelope={
            "headline": "We can't give you a score.", "detail": "The research does not cover you."})
        event = self.ending([scored, report])
        self.assertEqual((event["kind"], event["stage"], event["headline"]),
                         ("refusal", "report", "We can't give you a score."))

    def test_without_a_report_a_refusal_ends_the_run_only_if_it_is_its_stages_last_word(self):
        select = self.judgment("select", "model_choice.json", "proceed")
        refused = self.judgment("score", "score_PGS000001.json", "refuse")
        event = self.ending([select, refused])
        self.assertEqual((event["kind"], event["stage"]), ("refusal", "score"))
        self.assertIn("did not reach report", event["detail"])
        self.o.live.events.clear()
        # The next model scored, and the run stopped before report: not a refusal. Nor is it
        # completed: it recorded no ending, so it failed.
        scored = self.judgment("score", "score.json", "score")
        event = self.ending([select, refused, scored], rc=1)
        self.assertEqual((event["kind"], event["error"]), ("run_failed", "cut off before it recorded an ending: the agent exited 1"))

    def test_without_a_report_the_last_refusal_written_ends_the_run(self):
        ancestry = self.judgment("ancestry", "ancestry.json", "refuse")
        select = self.judgment("select", "model_choice.json", "stop")
        event = self.ending([ancestry, select])
        self.assertEqual((event["kind"], event["stage"], event["outcome"]), ("refusal", "select", "stop"))

    def test_the_order_is_the_one_the_ledger_recorded_not_the_files_times(self):
        """Touching or restoring an older refusal, or two files written in the same second, must
        not make it its stage's last word (review B9)."""
        for touch in ("newest", "same second"):
            with self.subTest(touch=touch):
                self.o.live.events.clear()
                select = self.judgment("select", "model_choice.json", "proceed")
                refused = self.judgment("score", "score_PGS000001.json", "refuse")
                scored = self.judgment("score", "score.json", "score")
                old = Path(self.tmp.name) / refused["artifact"]
                when = self.clock + 3600 if touch == "newest" else os.stat(Path(self.tmp.name) / scored["artifact"]).st_mtime
                os.utime(old, (when, when))
                event = self.ending([select, refused, scored], rc=1)
                self.assertEqual(event["kind"], "run_failed")  # not the touched refusal
                (Path(self.tmp.name) / "runs" / RUN / "ledger.jsonl").unlink()

    def test_a_judgment_the_ledger_never_recorded_is_never_a_stages_last_word(self):
        refused = self.judgment("score", "score_PGS000001.json", "refuse")
        scored = self.judgment("score", "score.json", "score")
        unrecorded = self.judgment("score", "score_PGS000009.json", "refuse", recorded=False)
        event = self.ending([refused, scored, unrecorded], rc=1)
        self.assertEqual(event["kind"], "run_failed")  # not the unrecorded refusal

    def test_a_valid_result_json_is_how_the_run_ended(self):
        # A report judgment written with the result summary's word for it (AGENT.md lets
        # stages.report.outcome be "proceed"): the run scored, as result.json says.
        scored = self.judgment("score", "score.json", "score")
        report = self.judgment("report", "report.json", "proceed")
        event = self.ending([scored, report], result=self.result("score", "score"))
        self.assertEqual((event["kind"], event["outcome"]), ("run_completed", "score"))
        # A refusal the result records at select, delivered by report with its own words.
        self.o.live.events.clear()
        select = self.judgment("select", "model_choice.json", "refuse")
        report = self.judgment("report", "report.json", "proceed", refusal_envelope={
            "headline": "No model fits your file well enough.", "detail": "Coverage was too low."})
        event = self.ending([select, report], result=self.result("refuse", "select"))
        self.assertEqual((event["kind"], event["stage"], event["outcome"], event["headline"]),
                         ("refusal", "report", "refuse", "No model fits your file well enough."))

    def test_judgments_that_disagree_with_a_valid_result_are_a_caveat_and_the_result_wins(self):
        caveats = []
        self.o.manifest = type("Manifest", (), {"caveat": lambda _self, text: caveats.append(text)})()
        scored = self.judgment("score", "score.json", "score")
        report = self.judgment("report", "report.json", "score")
        event = self.ending([scored, report], result=self.result("refuse", "score"))
        self.assertEqual((event["kind"], event["stage"], event["outcome"]), ("refusal", "score", "refuse"))
        (caveat,) = caveats
        self.assertIn("result.json", caveat)
        # Without a valid result, the judgments decide, as before.
        self.o.live.events.clear()
        event = self.ending([scored, report], result={"valid": False, "state": "absent", "result": None})
        self.assertEqual((event["kind"], event["outcome"]), ("run_completed", "score"))

    def test_a_run_cut_off_before_it_recorded_an_ending_failed_and_is_never_completed(self):
        """A run on 10-14: the agent exited 1 at score, its connection to Claude refused,
        with no report and no result. It ended as run_completed/stop, which the demo read as
        completed."""
        select = self.judgment("select", "model_choice.json", "proceed")
        absent = {"valid": False, "state": "absent", "result": None}
        said = ("...\nAPI Error: Couldn't connect through your proxy (ERR_PROXY_TUNNEL) — the proxy "
                "refused the tunnel: check its credentials and that it allows this host\n", "")
        event = self.ending([select], rc=1, result=absent, output=said)
        self.assertEqual(event["kind"], "run_failed")
        self.assertIn("API Error: Couldn't connect through your proxy", event["error"])
        self.assertEqual(self.o.manifest.data["cut_off"]["environment"], True)
        # Cut off with nothing printed: still an exit before any ending, and the environment's.
        self.o.live.events.clear()
        event = self.ending([select], rc=137, result=absent, output=("", ""))
        self.assertEqual(event["kind"], "run_failed")
        self.assertIn("exited 137", event["error"])
        # Ended of its own accord with no ending recorded: failed, but the agent's, not the environment's.
        self.o.live.events.clear()
        event = self.ending([select], rc=0, result=absent, output=("Done.", ""))
        self.assertEqual(event["kind"], "run_failed")
        self.assertEqual(self.o.manifest.data["cut_off"]["environment"], False)
        # A report delivered, or a refusal that is its stage's last word, is an ending.
        self.o.live.events.clear()
        report = self.judgment("report", "report.json", "score")
        self.assertEqual(self.ending([select, report], rc=1, result=absent, output=said)["kind"], "run_completed")


if __name__ == "__main__":
    unittest.main()
