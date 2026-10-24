"""The routine questions, answered on the upload form before a demo run starts: what the answers
may be (demo_site/intake.py), how the worker keeps them beside the run for its agent, and how the
harness hands them to the agent with the demo's brief. A run without them, as every benchmark
run is, has no answers file and the brief it always had."""
import base64
import json
import os
from pathlib import Path
import stat
import sys
import tempfile
import unittest
from unittest.mock import patch

from demo_site import intake, worker

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "harness"))
from harness import orchestrator  # noqa: E402

OWNER = "session-owner-abcdefghijklmnop"
ANSWERS = {"condition": "Condition X", "reason": "A rash keeps coming back and my brother has one too.",
           "ancestry": "Mixed heritage", "sex": "female", "diagnosed": "no", "doctor_note": True,
           "consent": True}


def encoded(value, ascii_only=True):
    return base64.b64encode(json.dumps(value, ensure_ascii=ascii_only).encode()).decode()


def upload(data=b"# synthetic genome\nrs1\t1\t101\tAA\n"):
    fh = tempfile.TemporaryFile()
    fh.write(data)
    fh.seek(0)
    return fh


class FakeProcess:
    returncode = None

    def poll(self):
        return self.returncode

    def kill(self):
        self.returncode = -9

    def wait(self, timeout=None):
        return self.returncode


class FormAnswers(unittest.TestCase):
    def test_the_answers_as_the_agent_gets_them(self):
        self.assertEqual(intake.check(ANSWERS), ANSWERS)
        # Left blank or unticked is left out; the person's own words are kept, trimmed.
        self.assertEqual(intake.check({"condition": "  Type 2 diabetes ", "reason": " ", "doctor_note": False,
                                       "consent": True}), {"condition": "Type 2 diabetes", "consent": True})
        self.assertEqual(intake.check({"condition": "Cond\x00ition\nX", "consent": True})["condition"],
                         "Condition\nX")
        self.assertEqual(intake.from_header(encoded(ANSWERS)), ANSWERS)
        words = {"condition": "Hautveränderung X", "ancestry": "家族のルーツはさまざま", "consent": True}
        self.assertEqual(intake.from_header(encoded(words, ascii_only=False)), words)

    def test_what_the_form_refuses(self):
        for bad, said in (({**ANSWERS, "consent": False}, "Tick the box"),
                          ({k: v for k, v in ANSWERS.items() if k != "consent"}, "Tick the box"),
                          ({**ANSWERS, "condition": " "}, "which condition"),
                          ({"consent": True}, "which condition"),
                          ({**ANSWERS, "condition": "x" * 201}, "200"),
                          ({**ANSWERS, "reason": "x" * 601}, "600"),
                          ({**ANSWERS, "ancestry": "x" * 301}, "300"),
                          ({**ANSWERS, "sex": "other"}, "form"),
                          ({**ANSWERS, "diagnosed": "maybe"}, "form"),
                          ({**ANSWERS, "doctor_note": "yes"}, "form"),
                          ({**ANSWERS, "condition": 5}, "form"),
                          ({**ANSWERS, "extra": "x"}, "form"),
                          (["Condition X"], "form")):
            with self.subTest(bad=str(bad)[:60]):
                with self.assertRaises(intake.IntakeError) as error:
                    intake.check(bad)
                self.assertIn(said, str(error.exception))
        for raw in (None, "", "not base64!", encoded("Condition X"), "A" * (intake.MAX_HEADER + 4),
                    base64.b64encode(b"\xff\xfe").decode()):
            with self.subTest(raw=str(raw)[:30]), self.assertRaises(intake.IntakeError):
                intake.from_header(raw)

    def test_the_longest_answers_fit_one_header_line_at_the_front_door(self):
        # nginx takes a request header line of at most 8 KiB (large_client_header_buffers 4 8k).
        longest = {"condition": "\U0001F9EC" * 200, "reason": '"' * 600, "ancestry": "祖" * 300,
                   "sex": "female", "diagnosed": "not-sure", "doctor_note": True, "consent": True}
        line = "%s: %s" % (intake.HEADER, encoded(longest, ascii_only=False))
        self.assertLessEqual(len(line), intake.MAX_HEADER + len(intake.HEADER) + 2)
        self.assertLess(len(line), 8192)
        self.assertEqual(intake.from_header(encoded(longest, ascii_only=False)), longest)


class WorkerKeepsThem(unittest.TestCase):
    def setUp(self):
        self.enterContext(patch.object(worker, "MIN_FREE_BYTES", 0))
        tmp = tempfile.TemporaryDirectory(prefix=".demo-intake-test-", dir=REPO)
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.runs = worker.Runs(self.root / "private", launcher=lambda *a: FakeProcess(), clock=lambda: 1000.0)
        self.addCleanup(self.runs.close)

    def child_command(self, root, run_id):
        with patch.object(os, "execve") as execute:
            worker._run_child([str(root), run_id, str(root / "genome.txt"), "-"])
        return execute.call_args.args[1]

    def test_the_answers_are_kept_beside_the_run_and_its_harness_is_told_where(self):
        status = self.runs.start(OWNER, upload(), intake=dict(ANSWERS))
        root = self.runs._path(status["run_id"])
        path = root / "intake.json"
        self.assertEqual(json.loads(path.read_text()), ANSWERS)
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        command = self.child_command(root, status["run_id"])
        self.assertEqual(command[command.index("--intake") + 1], str(path))
        # The command names the file, never what it says.
        self.assertFalse(any("Condition X" in part for part in command))

    def test_a_run_started_without_answers_has_none(self):
        status = self.runs.start(OWNER, upload())
        root = self.runs._path(status["run_id"])
        self.assertFalse((root / "intake.json").exists())
        self.assertNotIn("--intake", self.child_command(root, status["run_id"]))

    def test_the_answers_go_with_the_working_files_when_the_run_ends(self):
        status = self.runs.start(OWNER, upload(), intake=dict(ANSWERS))
        root = self.runs._path(status["run_id"])
        self.runs.end(OWNER)
        self.runs.join_cleanups()
        self.assertFalse((root / "intake.json").exists())

    def test_the_rpc_takes_the_answers_with_the_file_and_checks_them_again(self):
        server = object.__new__(worker.WorkerServer)
        started = []
        server.runs = type("Runs", (), {"start": lambda self, owner, file, intake: started.append(intake) or {"status": "running"}})()
        for payload in ({"op": "start", "owner": OWNER},
                        {"op": "start", "owner": OWNER, "intake": {**ANSWERS, "consent": False}},
                        {"op": "start", "owner": OWNER, "intake": "Condition X"}):
            file = upload()
            with self.subTest(payload=str(payload)[:70]), self.assertRaises(worker.WorkerError):
                server.dispatch(payload, file)
            self.assertTrue(file.closed)
        server.dispatch({"op": "start", "owner": OWNER, "intake": {**ANSWERS, "reason": " "}}, upload())
        self.assertEqual(started, [{k: v for k, v in ANSWERS.items() if k != "reason"}])


class HarnessHandsThemOver(unittest.TestCase):
    def test_a_demo_brief_says_where_the_answers_are_and_what_not_to_ask(self):
        brief = orchestrator.pipeline_brief("demo-1", intake=True)
        self.assertTrue(brief.startswith(orchestrator.PRS_BRIEF.format(run_id="demo-1")))
        added = brief[len(orchestrator.PRS_BRIEF.format(run_id="demo-1")):]
        self.assertIn("runs/demo-1/intake.json", added)
        for said in ("Never ask them to confirm the model", "surface_in_report", "model_choice",
                     "ancestry_note", "doctor_note", "follow_up_state", "`gate_type`: `up-front`"):
            self.assertIn(said, added)

    def test_a_benchmark_brief_is_the_one_it_always_was(self):
        self.assertEqual(orchestrator.pipeline_brief("run-1"), orchestrator.PRS_BRIEF.format(run_id="run-1"))
        self.assertEqual(orchestrator.pipeline_brief("run-1", stop_after="select"),
                         orchestrator.PRS_BRIEF.format(run_id="run-1") + orchestrator.stop_after_clause("select", "run-1"))

    def test_the_answers_are_staged_where_the_agent_reads_them(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "intake.json"
            source.write_text(json.dumps(ANSWERS))
            answers = orchestrator.read_intake(str(source))
            self.assertEqual(answers, ANSWERS)
            sandbox = Path(tmp) / "prs"
            orchestrator.write_intake(str(sandbox), "demo-1", answers)
            self.assertEqual(json.loads((sandbox / "runs" / "demo-1" / "intake.json").read_text()), ANSWERS)
            for bad in ({"consent": True}, {**ANSWERS, "consent": False}, ["x"]):
                source.write_text(json.dumps(bad))
                with self.subTest(bad=str(bad)[:40]), self.assertRaises(ValueError):
                    orchestrator.read_intake(str(source))

    def test_only_a_demo_run_takes_answers(self):
        with tempfile.TemporaryDirectory() as tmp, self.assertRaises(ValueError):
            source = Path(tmp) / "intake.json"
            source.write_text(json.dumps(ANSWERS))
            orchestrator.Orchestrator(run_id="run-1", intake=str(source))


if __name__ == "__main__":
    unittest.main()
