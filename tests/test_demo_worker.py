import io
import json
import os
from pathlib import Path
import shutil
import signal
import struct
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from demo_site import worker
from demo_site.uploads import UploadError, validate_upload

REPO = Path(__file__).resolve().parents[1]
OWNER = "session-owner-abcdefghijklmnop"
OTHER = "another-owner-abcdefghijklmnop"
GENOME = b"# synthetic genome\nrs1\t1\t101\tAA\n"
# The routine questions, answered on the upload form (demo_site/intake.py).
ANSWERS = {"condition": "Condition X", "consent": True}


def upload(data=GENOME):
    """An uploaded file as the worker gets it: open, at its start."""
    fh = tempfile.TemporaryFile()
    fh.write(data)
    fh.seek(0)
    return fh


class FakeProcess:
    def __init__(self):
        self.returncode = None

    def poll(self):
        return self.returncode

    def kill(self):
        self.returncode = -9

    def wait(self, timeout=None):
        return self.returncode


class GroupProcess:
    def __init__(self, process):
        self.process = process

    def poll(self):
        return self.process.poll()

    def wait(self, timeout=None):
        result = self.process.wait(timeout=timeout)
        if self.process.stderr:
            self.process.stderr.close()
        return result

    def kill(self):
        os.killpg(self.process.pid, signal.SIGKILL)


def uuid_hex():
    import uuid
    return uuid.uuid4().hex


class WorkerTests(unittest.TestCase):
    def setUp(self):
        # Free disk is the host's; only the test about it depends on it.
        self.enterContext(patch.object(worker, "MIN_FREE_BYTES", 0))
        self.tmp = tempfile.TemporaryDirectory(prefix=".demo-worker-test-", dir=REPO)
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.now = 1000.0
        self.fake = FakeProcess()
        self.runs = worker.Runs(self.root / "private", launcher=lambda *a: self.fake,
                                clock=lambda: self.now)
        self.addCleanup(self.runs.close)

    def test_one_run_owner_isolation_and_worker_lock(self):
        status = self.runs.start(OWNER, upload())
        self.assertEqual(status["status"], "running")
        self.assertEqual(self.runs.status(OTHER), {"status": "busy", "busy": True, "owned": False,
                                                   "full": True, "slots": 1, "retry_at": None})
        with self.assertRaises(worker.WorkerError) as error:
            self.runs.start(OTHER, upload())
        self.assertEqual(error.exception.code, "full")
        with self.assertRaises(worker.WorkerError) as error:
            self.runs.start(OWNER, upload())
        self.assertEqual(error.exception.code, "busy")
        with self.assertRaises(RuntimeError):
            worker.Runs(self.root / "private")
        self.runs.end(OTHER)
        self.assertEqual(self.runs.status(OWNER)["status"], "running")

    def slotted(self, **settings):
        runs = worker.Runs(self.root / "slots", launcher=lambda *a: FakeProcess(),
                           clock=lambda: self.now, failure_log=self.root / "failures.log", **settings)
        self.addCleanup(runs.close)
        return runs

    def test_as_many_runs_as_slots_then_full_with_when_to_come_back(self):
        runs = self.slotted(max_runs=2, typical_run=80 * 60, run_limit=240 * 60)
        first = runs.start(OWNER, upload())
        self.assertEqual((first["busy"], first["full"]), (True, False))
        self.assertEqual(runs.status(OTHER), {"status": "idle", "busy": False, "owned": False, "full": False})
        self.now += 600
        runs.start(OTHER, upload())
        third = "third-owner-abcdefghijklmnop"
        # Full: the first run started 10 minutes before the second, so it should end first.
        expected = {"status": "busy", "busy": True, "owned": False, "full": True, "slots": 2,
                    "retry_at": 1000 + 80 * 60}
        self.assertEqual(runs.status(third), expected)
        with self.assertRaises(worker.WorkerError) as error:
            runs.start(third, upload())
        self.assertEqual(error.exception.code, "full")
        # Past a run's usual length: ten minutes from now, at a time.
        self.now = 1000 + 90 * 60
        self.assertEqual(runs.status(third)["retry_at"], self.now + 600)
        # Never later than the time limit.
        self.now = 1000 + 235 * 60
        self.assertEqual(runs.status(third)["retry_at"], 1000 + 240 * 60)
        # A finished run frees its slot at once; its owner keeps the report, and sees full too.
        runs.processes[first["run_id"]].returncode = 0
        self.assertEqual(runs.status(third)["status"], "idle")
        mine = runs.status(OWNER)
        self.assertEqual((mine["status"], mine["busy"], mine["full"]), ("completed", False, False))
        runs.start(third, upload())
        mine = runs.status(OWNER)
        self.assertEqual((mine["status"], mine["busy"], mine["full"], mine["slots"]), ("completed", True, True, 2))

    def test_a_run_past_its_time_limit_is_stopped_and_frees_its_slot(self):
        runs = self.slotted(max_runs=1, run_limit=240 * 60)
        state = runs.start(OWNER, upload())
        process = runs.processes[state["run_id"]]
        self.assertEqual(runs.status(OTHER)["retry_at"], 1000 + 240 * 60)
        self.now += 240 * 60 - 1
        runs.sweep()
        self.assertEqual(runs.status(OWNER)["status"], "running")
        self.now += 1
        mine = runs.status(OWNER)
        self.assertEqual(process.returncode, -9)
        self.assertEqual(mine["status"], "failed")
        self.assertIn("time limit", mine["error"])
        self.assertEqual(runs.status(OTHER)["status"], "idle")
        record = json.loads((runs._path(state["run_id"]) / "record.json").read_text())
        self.assertEqual(record["why_failed"], "time limit: stopped after 240 minutes")
        (line,) = (self.root / "failures.log").read_text().splitlines()
        self.assertEqual(line.split("\t")[:4], [state["run_id"], "demo", "demo run",
                                                "time limit: stopped after 240 minutes"])
        # Kept for the hour every ended run gets, then purged.
        self.assertTrue(runs._path(state["run_id"]).exists())
        self.now += worker.RETENTION_SECONDS
        runs.sweep()
        self.assertFalse(runs._path(state["run_id"]).exists())

    def never_launched(self, runs, outcome=None, log=None):
        """A run whose harness exited 1 before its agent launched, leaving what the harness
        leaves: its manifest's outcome, and what it printed."""
        state = runs.start(OWNER, upload())
        run_id = state["run_id"]
        if outcome is not None:
            vault = runs._path(run_id) / "harness" / "vault" / "runs" / run_id
            vault.mkdir(parents=True)
            (vault / "manifest.json").write_text(json.dumps({"outcome": outcome}))
        if log is not None:
            (runs._path(run_id) / "harness.log").write_text(log)
        runs.processes[run_id].returncode = 1
        mine = runs.status(OWNER)
        (line,) = (self.root / "failures.log").read_text().splitlines()
        return mine, line.split("\t")[3], (self.root / "failures.log").read_text()

    def test_a_run_whose_sandbox_could_not_start_says_so_and_that_it_was_not_the_persons_file(self):
        # The gb10 after its 2026-10-20 reboot: bubblewrap may not make a user namespace, so the
        # harness's launch gate fails before step 1 and the harness exits 1.
        mine, why, _ = self.never_launched(self.slotted(), outcome={"launched": False, "failed_before_launch": {
            "type": "ProbeFailure",
            "detail": "probe process failed (rc=1): bwrap: setting up uid map: Permission denied\n"}})
        self.assertEqual(why, "did not launch: the sandbox could not start (bwrap: setting up uid map: Permission denied)")
        self.assertEqual(mine["status"], "failed")
        self.assertEqual(mine["error"], "The analysis could not start because of a problem on the demo's side, "
                                        "not with your file. Clear this run and try again later.")

    def test_a_run_that_never_launched_names_the_failure_and_never_quotes_its_detail(self):
        # A gate's detail can quote the sandbox's files; the log gets the kind of failure only.
        _, why, log = self.never_launched(self.slotted(), outcome={"launched": False, "failed_before_launch": {
            "type": "ProbeFailure", "detail": "answer-key strings are present in the prs sandbox, which no "
                                              "boundary reaches: [('runs/x/door/a.txt', 'SECRET-ANSWER-KEY')]"}})
        self.assertEqual(why, "did not launch: ProbeFailure")
        self.assertNotIn("SECRET-ANSWER-KEY", log)

    def test_a_harness_that_refused_to_start_says_its_own_reason(self):
        # Before any manifest: the harness's own one-line refusal, as it prints it.
        _, why, _ = self.never_launched(self.slotted(), log="run id      : x\nrun_harness.py: the claude CLI on "
                                        "this host does not run (No such file). Every agent would die at launch.\n")
        self.assertEqual(why, "did not launch: run_harness.py: the claude CLI on this host does not run "
                              "(No such file). Every agent would die at launch.")

    def test_run_settings_are_checked(self):
        for bad in ({"max_runs": 0}, {"max_runs": 1.5}, {"run_limit": 0}, {"typical_run": -60}):
            with self.assertRaises(ValueError):
                worker.Runs(self.root / "bad", **bad)

    def test_the_report_shows_the_judgment_each_stage_settled_on(self):
        run_id = self.runs.start(OWNER, upload())["run_id"]
        root = self.runs._path(run_id)
        recorded = [0]

        def judgment(stage, name, outcome, **fields):
            artifact = "runs/%s/%s/%s" % (run_id, stage, name)
            path = root / "harness" / "sandboxes" / run_id / "prs" / artifact
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(dict(fields, outcome=outcome)))
            # File times that say the opposite of the order made: only the ledger's order counts.
            os.utime(path, (1_900_000_000 - recorded[0], 1_900_000_000 - recorded[0]))
            recorded[0] += 1
            return dict(fields, stage=stage, artifact=artifact, outcome=outcome, recorded_seq=recorded[0])

        def bundle(made, result):
            vault = root / "harness" / "vault" / "runs" / run_id
            vault.mkdir(parents=True, exist_ok=True)
            # The manifest lists judgments in file-name order, as _collect_judgments does.
            (vault / "manifest.json").write_text(json.dumps({
                "stage_judgments": sorted(made, key=lambda j: j["artifact"]), "result": result}))
            # The report as the run's end keeps it, built here from each manifest in turn.
            return self.runs._report(self.runs.records[run_id], [])["bundle"]

        # In the order one run made them on 10-14: a model refused at score, a new choice, a score.
        made = [judgment("door", "classification.json", "proceed", assay_class="array"),
                judgment("door", "normalize.json", "proceed", strand="plus"),
                judgment("select", "model_choice.json", "proceed", chosen_model="PGS000001"),
                judgment("score", "score_PGS000001.json", "refuse", model="PGS000001"),
                judgment("select", "model_choice_final.json", "proceed", chosen_model="PGS000003"),
                judgment("score", "score.json", "score", model="PGS000003"),
                judgment("report", "report.json", "score")]
        settled = bundle(made, {"valid": True, "result": {"chosen_model": "PGS000003"}})
        name = lambda alias: settled[alias]["artifact"].rsplit("/", 1)[1]
        self.assertEqual((name("score"), settled["score"]["outcome"]), ("score.json", "score"))
        self.assertEqual(settled["model_choice"]["chosen_model"], "PGS000003")
        self.assertEqual(name("classification"), "classification.json")
        self.assertEqual(name("report"), "report.json")
        # No valid result: each stage's last word, so a model refused last is the score shown.
        made.append(judgment("score", "score_PGS000004.json", "refuse", model="PGS000004"))
        settled = bundle(made, {"valid": False})
        self.assertEqual((name("score"), settled["score"]["outcome"]), ("score_PGS000004.json", "refuse"))
        # One the ledger never recorded is never a stage's last word.
        made.append(dict(judgment("score", "score_PGS000005.json", "refuse", model="PGS000005"), recorded_seq=None))
        settled = bundle(made, {"valid": False})
        self.assertEqual(name("score"), "score_PGS000004.json")

    def test_the_report_carries_the_percentile_range_the_tool_computed(self):
        run_id = self.runs.start(OWNER, upload())["run_id"]
        root = self.runs._path(run_id)
        interpret = root / "harness" / "sandboxes" / run_id / "prs" / "runs" / run_id / "interpret"
        interpret.mkdir(parents=True)
        ranged = {"tool": "rank_range.py", "n": 150, "percentile_range": [64.33, 78.57]}
        (interpret / "percentile_range.json").write_text(json.dumps(ranged))
        vault = root / "harness" / "vault" / "runs" / run_id
        vault.mkdir(parents=True)
        (vault / "manifest.json").write_text(json.dumps({"stage_judgments": [], "result": {"valid": False}}))
        self.fake.returncode = 0
        self.assertEqual(self.runs.status(OWNER)["report"]["bundle"]["percentile_range"], ranged)

    def test_each_running_demo_is_told_how_many_demos_are_going(self):
        """For speedup 2: impute_joint.py runs a third chromosome only while its demo is alone."""
        runs = self.slotted(max_runs=2)
        first = runs.start(OWNER, upload())["run_id"]
        refdata = lambda run_id: runs._path(run_id) / "harness" / "sandboxes" / run_id / "prs" / "refdata"
        refdata(first).mkdir(parents=True)
        runs.sweep()
        self.assertEqual(json.loads((refdata(first) / "demo_load.json").read_text())["demo_runs_going"], 1)
        second = runs.start(OTHER, upload())["run_id"]
        runs.sweep()
        self.assertEqual(json.loads((refdata(first) / "demo_load.json").read_text())["demo_runs_going"], 2)
        self.assertFalse(refdata(second).exists())  # not staged yet: nothing written for it

    def door(self, runs, run_id, assay):
        """The door's classification of the run's file, as the agent records it."""
        door = runs._path(run_id) / "harness" / "sandboxes" / run_id / "prs" / "runs" / run_id / "door"
        door.mkdir(parents=True, exist_ok=True)
        (door / "classification.json").write_text(json.dumps({"outcome": "proceed", "assay_class": assay}))

    def finish(self, runs, owner, minutes, waits=(), reached="report", rc=0, assay="genotyping-array"):
        """A run started now that ends `minutes` later, its person taking each of `waits` minutes
        to answer one of its questions; it reached the end of the `reached` stage, and the door
        classified its file as `assay`."""
        iso = lambda at: time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(at))
        run_id = runs.start(owner, upload())["run_id"]
        if assay:
            self.door(runs, run_id, assay)
        live = runs._path(run_id) / "live" / run_id
        live.mkdir(parents=True, exist_ok=True)
        events = [{"kind": "run_started", "ts": iso(self.now)}]
        answers = []
        for i, wait in enumerate(waits):
            asked = self.now + 60 * (i + 1)
            events.append({"kind": "question", "question_id": "q%d" % i, "question": "Example?", "ts": iso(asked)})
            answers.append({"kind": "answer", "question_id": "q%d" % i, "text": "Yes.", "ts": iso(asked + 60 * wait)})
        events.append({"kind": "stage_completed", "stage": reached, "outcome": "proceed", "ts": iso(self.now + 60)})
        (live / "session.jsonl").write_text("".join(json.dumps(e) + "\n" for e in events))
        (live / "answers.jsonl").write_text("".join(json.dumps(a) + "\n" for a in answers))
        self.now += minutes * 60
        runs.processes[run_id].returncode = rc
        runs.sweep()
        return run_id

    def test_a_completed_run_that_reached_its_score_is_timed_without_its_persons_answers(self):
        runs = self.slotted(max_runs=3)
        mine = self.finish(runs, OWNER, 50, waits=(3, 2))
        times = [json.loads(line) for line in (runs.root / "run-times.jsonl").read_text().splitlines()]
        self.assertEqual(times, [{"run_id": mine, "ended_utc": "1970-01-01T01:06:40Z", "kind": "chip", "wall_seconds": 3000,
                                  "waiting_for_person_seconds": 300, "working_seconds": 2700}])
        # Not timed: a run that failed, and one that ended before its score (an early refusal says
        # nothing about how long an analysis takes).
        self.finish(runs, OTHER, 30, rc=1)
        self.finish(runs, "third-owner-abcdefghijklmnop", 8, reached="select")
        self.assertEqual(len((runs.root / "run-times.jsonl").read_text().splitlines()), 1)
        # A running run says how long it has waited for its person's answers.
        runs.end(OWNER)
        run_id = runs.start(OWNER, upload())["run_id"]
        live = runs._path(run_id) / "live" / run_id
        live.mkdir(parents=True, exist_ok=True)
        (live / "session.jsonl").write_text(json.dumps({"kind": "question", "question_id": "q", "question": "?",
                                                        "ts": "1970-01-01T01:40:00Z"}) + "\n")
        (live / "answers.jsonl").write_text(json.dumps({"kind": "answer", "question_id": "q", "text": "No.",
                                                        "ts": "1970-01-01T01:44:30Z"}) + "\n")
        self.assertEqual(runs.status(OWNER)["waited_seconds"], 270)

    def test_the_estimate_is_the_measured_runs_until_five_are_recorded_then_the_last_ten(self):
        bare = self.slotted()
        self.assertNotIn("estimates", bare.status(OWNER))  # nothing measured: no estimate
        bare.close()
        runs = worker.Runs(self.root / "estimates", launcher=lambda *a: FakeProcess(), clock=lambda: self.now,
                           max_runs=1, measured={"chip": [55 * 60, 65 * 60]})
        self.addCleanup(runs.close)
        self.assertEqual(runs.status(OWNER)["estimates"],
                         {"chip": {"low_minutes": 55, "median_minutes": 60, "high_minutes": 65, "runs": 2}})
        for minutes in (40, 41, 42, 43):
            self.finish(runs, OWNER, minutes)
            runs.end(OWNER)
        # Four recorded: the measured runs count too.
        self.assertEqual(runs.status(OWNER)["estimates"]["chip"],
                         {"low_minutes": 40, "median_minutes": 43, "high_minutes": 65, "runs": 6})
        self.finish(runs, OWNER, 44.5)
        runs.end(OWNER)
        self.assertEqual(runs.status(OWNER)["estimates"]["chip"],
                         {"low_minutes": 40, "median_minutes": 42, "high_minutes": 45, "runs": 5})
        for minutes in (50, 51, 52, 53, 54, 70):
            self.finish(runs, OWNER, minutes)
            runs.end(OWNER)
        # The last ten, and kept across a restart.
        runs.close()
        again = worker.Runs(self.root / "estimates", launcher=lambda *a: FakeProcess(), clock=lambda: self.now,
                            max_runs=1, measured={"chip": [55 * 60, 65 * 60]})
        self.addCleanup(again.close)
        self.assertEqual(again.status(OWNER)["estimates"]["chip"],
                         {"low_minutes": 41, "median_minutes": 51, "high_minutes": 70, "runs": 10})

    def test_a_whole_genome_is_timed_and_estimated_apart_from_a_chip_file(self):
        runs = self.slotted(max_runs=2, measured={"chip": [50 * 60]})
        (runs.root / "run-times.jsonl").write_text(json.dumps({"run_id": "old", "working_seconds": 9000}) + "\n")
        runs.close()
        runs = self.slotted(max_runs=2, measured={"chip": [50 * 60]})   # an old line without a kind counts for neither
        self.finish(runs, OWNER, 100, assay="WGS")
        runs.end(OWNER)
        chip = {"low_minutes": 50, "median_minutes": 50, "high_minutes": 50, "runs": 1}
        genome = {"low_minutes": 100, "median_minutes": 100, "high_minutes": 100, "runs": 1}
        self.assertEqual(runs.status(OWNER)["estimates"], {"chip": chip, "whole_genome": genome})
        # A running run: its own kind's estimate. A genotype table is a chip's file before the
        # door says so; what the door says decides; an exome, or a kind not known yet, has none.
        mine = runs.start(OWNER, upload())["run_id"]
        state = runs.status(OWNER)
        self.assertEqual((state["kind"], state["estimate"]), ("chip", chip))
        self.door(runs, mine, "whole-genome sequencing")
        state = runs.status(OWNER)
        self.assertEqual((state["kind"], state["estimate"]), ("whole_genome", genome))
        self.door(runs, mine, "WES (exome)")
        state = runs.status(OWNER)
        self.assertEqual((state["kind"], "estimate" in state), (None, False))

    def test_the_shipped_measured_run_times_are_read(self):
        """run-times-measured.json, the runs timed after the speedups, by the kind of file each
        read, is what the worker starts with."""
        times = worker.measured_run_times()
        self.assertTrue(times.get("chip"))
        self.assertTrue(set(times) <= {"chip", "whole_genome"})
        self.assertTrue(all(isinstance(t, (int, float)) and t > 0 for kind in times.values() for t in kind))
        self.assertEqual(worker.measured_run_times(self.root / "absent.json"), {})

    def test_a_full_demo_says_to_come_back_after_the_median_measured_run(self):
        runs = self.slotted(max_runs=1, typical_run=100 * 60, run_limit=240 * 60,
                            measured={"chip": [50 * 60, 60 * 60, 70 * 60], "whole_genome": [120 * 60]})
        mine = runs.start(OWNER, upload())["run_id"]
        self.assertEqual(runs.status(OTHER)["retry_at"], 1000 + 60 * 60)
        self.door(runs, mine, "WGS")            # its own kind's median
        self.assertEqual(runs.status(OTHER)["retry_at"], 1000 + 120 * 60)
        self.door(runs, mine, "genotyping-array")
        self.now = 1000 + 65 * 60  # past the median: ten minutes from now
        self.assertEqual(runs.status(OTHER)["retry_at"], self.now + 600)

    def test_a_question_with_choices_takes_one_of_them_or_free_text_only_when_offered(self):
        runs = self.slotted(max_runs=1)
        run_id = runs.start(OWNER, upload())["run_id"]
        live = runs._path(run_id) / "live" / run_id
        live.mkdir(parents=True, exist_ok=True)
        ask = lambda qid, other: {"kind": "question", "question_id": qid, "stage": "select", "question": "May I go ahead?",
                                  "why_it_matters": "It decides the model.", "options": ["Yes, go ahead", "No"], "other": other}
        (live / "session.jsonl").write_text(json.dumps(ask("q1", False)) + "\n")
        question = runs.status(OWNER)["question"]
        self.assertEqual((question["options"], question["other"]), (["Yes, go ahead", "No"], False))
        with self.assertRaises(worker.WorkerError) as refused:
            runs.answer(OWNER, "q1", "Maybe later")
        self.assertEqual(refused.exception.code, "invalid")
        self.assertEqual(runs.answer(OWNER, "q1", "Yes, go ahead")["status"], "running")
        with open(live / "session.jsonl", "a") as fh:
            fh.write(json.dumps(ask("q2", True)) + "\n")
        self.assertTrue(runs.status(OWNER)["question"]["other"])
        self.assertEqual(runs.answer(OWNER, "q2", "Only if it is quick")["status"], "running")

    def test_progress_notes_carry_no_internal_code(self):
        """The agent's notes are written for the record; a sentence naming an internal code is
        not shown, and a note that is nothing else shows the stage's plain label."""
        run_id = self.runs.start(OWNER, upload())["run_id"]
        live = self.runs._path(run_id) / "live" / run_id
        live.mkdir(parents=True, exist_ok=True)
        events = [{"kind": "stage_completed", "stage": "select", "outcome": "proceed",
                   "decision": "Chose PGS005218 (Rand 2025, PRS-CS). Next down the list for SEL-12: PGS002766."},
                  {"kind": "stage_completed", "stage": "ancestry", "outcome": "proceed", "decision": "Per ANC-3 and R1."}]
        (live / "session.jsonl").write_text("".join(json.dumps(e) + "\n" for e in events))
        texts = [p["text"] for p in self.runs.status(OWNER)["progress"]]
        self.assertEqual(texts, ["Chose PGS005218 (Rand 2025, PRS-CS).", "ancestry completed"])

    def test_a_finished_run_keeps_only_its_result_for_24_hours(self):
        """When a run ends its upload, the agent's sandbox and working files, and the person's
        typed answers are deleted; the result the page shows, report and comparison data and
        progress, is kept for 24 hours, then deleted. End deletes it at once."""
        runs = self.slotted(max_runs=1)
        mine = self.finish(runs, OWNER, 40, waits=(2,))
        ended = self.now
        runs.join_cleanups()
        path = runs._path(mine)
        self.assertEqual(sorted(p.name for p in path.iterdir()), ["record.json", "result.json"])
        state = runs.status(OWNER)
        self.assertEqual((state["status"], state["owned"], state["purge_at"]), ("completed", True, ended + 24 * 3600))
        self.assertTrue(state["progress"])
        self.assertIn("report", state)
        self.assertNotIn("Yes.", json.dumps(state))          # the person's typed answer is not kept
        self.now = ended + 24 * 3600 - 1
        self.assertEqual(runs.status(OWNER)["status"], "completed")
        self.now = ended + 24 * 3600
        self.assertEqual(runs.status(OWNER)["status"], "idle")
        self.assertFalse(path.exists())
        # End deletes a kept result at once; a failed run keeps its message the same way.
        failed = self.finish(runs, OWNER, 10, rc=1)
        runs.join_cleanups()
        self.assertEqual(sorted(p.name for p in runs._path(failed).iterdir()), ["record.json", "result.json"])
        self.assertIn("error", runs.status(OWNER))
        runs.end(OWNER)
        self.assertFalse(runs._path(failed).exists())

    def test_a_run_has_a_private_link_that_opens_it_until_its_result_is_deleted(self):
        runs = self.slotted(max_runs=2)
        mine = runs.start(OWNER, upload())["run_id"]
        link = runs.status(OWNER)["link"]
        self.assertRegex(link, r"^/r/[A-Za-z0-9_-]{43}$")
        self.assertNotIn("link", runs.status(OTHER))            # only its owner sees it
        token = link[3:]
        self.assertEqual(runs.link(token), {"owner": OWNER, "run_id": mine})
        for wrong in ("x" * 43, token[:-1] + ("A" if token[-1] != "A" else "B"), ""):
            with self.assertRaises(worker.WorkerError) as refused:
                runs.link(wrong)
            self.assertEqual(refused.exception.code, "not_found")
        # Finished: still opens it, until the result is deleted.
        runs.processes[mine].returncode = 0
        runs.sweep()
        self.assertEqual(runs.link(token)["run_id"], mine)
        self.now += 24 * 3600
        runs.sweep()
        with self.assertRaises(worker.WorkerError):
            runs.link(token)

    def test_generated_private_upload_and_invalid_upload_cleanup(self):
        with self.assertRaises(worker.WorkerError):
            self.runs.start(OWNER, upload(b"not a genome"))
        self.assertEqual(list((self.runs.root / "runs").iterdir()), [])
        state = self.runs.start(OWNER, upload())
        run = self.runs._path(state["run_id"])
        self.assertEqual((run / "genome.txt").stat().st_mode & 0o777, 0o600)
        self.assertTrue(json.loads((run / "record.json").read_text())["demo"])
        self.runs.end(OWNER)
        self.assertFalse(run.exists())

    def gated(self):
        """expand_upload held at a gate until the test opens it, as a whole genome holds it."""
        gate = threading.Event()
        real = worker.expand_upload

        def expand(source, target):
            while not gate.wait(0.01):
                target.write(b"#")  # As the copy goes on: refused once its run is ended.
            target.fh.seek(0)
            target.fh.truncate()
            return real(source, target)
        self.enterContext(patch.object(worker, "expand_upload", expand))
        self.enterContext(patch.object(worker, "QUICK_CHECK_SECONDS", 0.05))
        return gate

    def test_a_file_slow_to_check_is_its_runs_first_step(self):
        gate = self.gated()
        file = upload()
        state = self.runs.start(OWNER, file)
        run_id = state["run_id"]
        self.assertEqual((state["status"], state["progress"][-1]["text"]),
                         ("running", "Checking your file before the agent starts."))
        self.assertNotIn(run_id, self.runs.processes)
        with self.assertRaises(worker.WorkerError) as error:
            self.runs.start(OTHER, upload())
        self.assertEqual(error.exception.code, "full")  # The slot is taken while the file is checked.
        gate.set()
        self.runs.checks.get(run_id, (threading.Thread(),))[0].join(10)
        self.assertTrue(file.closed)
        state = self.runs.status(OWNER)
        self.assertNotIn("Checking your file before the agent starts.", [p["text"] for p in state["progress"]])
        self.assertIs(self.runs.processes[run_id], self.fake)
        self.assertEqual((self.runs._path(run_id) / "genome.txt").read_bytes(), GENOME)
        self.assertFalse((self.runs._path(run_id) / "genome.part").exists())
        self.assertNotIn("checking", json.loads((self.runs._path(run_id) / "record.json").read_text()))

    def test_end_demo_stops_a_check_at_once(self):
        self.gated()
        state = self.runs.start(OWNER, upload())
        thread = self.runs.checks[state["run_id"]][0]
        self.assertEqual(self.runs.end(OWNER)["status"], "idle")
        thread.join(5)
        self.assertFalse(thread.is_alive())
        self.assertFalse(self.runs._path(state["run_id"]).exists())
        self.assertEqual((self.runs.processes, self.runs.checks), ({}, {}))

    def test_a_file_refused_after_the_upload_was_answered_ends_its_run_with_the_reason(self):
        gate = self.gated()
        self.runs.failure_log = self.root / "failures.log"
        bad = GENOME + b"rs2\t1\tnot-a-position\tAA\n"
        with self.assertRaises(UploadError) as refused:
            validate_upload(bad)
        state = self.runs.start(OWNER, upload(bad))
        gate.set()
        self.runs.checks.get(state["run_id"], (threading.Thread(),))[0].join(10)
        state = self.runs.status(OWNER)
        self.assertEqual(state["status"], "failed")
        self.assertEqual(state["error"], str(refused.exception) + " Clear this run to upload another file.")
        self.assertEqual(self.runs.processes, {})
        self.assertFalse((self.runs._path(state["run_id"]) / "genome.part").exists())
        # The person's file, not a failure of the demo.
        self.assertFalse((self.root / "failures.log").exists())
        self.assertEqual(self.runs.status(OTHER)["status"], "idle")

    def test_no_run_starts_short_of_disk_space(self):
        file = upload()
        self.enterContext(patch.object(worker, "MIN_FREE_BYTES", 32 * 1024 ** 3))
        usage = shutil.disk_usage(self.root)._replace(free=worker.MIN_FREE_BYTES - 1)
        with patch.object(worker.shutil, "disk_usage", return_value=usage):
            with self.assertRaises(worker.WorkerError) as error:
                self.runs.start(OWNER, file)
        self.assertEqual(error.exception.code, "space")
        self.assertTrue(file.closed)
        self.assertEqual(list((self.runs.root / "runs").iterdir()), [])

    def test_no_run_starts_while_agents_cannot_be_sandboxed(self):
        # The gb10 after its 2026-10-20 reboot: its kernel keeps this account from making the user
        # namespace every agent's sandbox needs, so any run would end before step 1.
        said = ["bwrap: setting up uid map: Permission denied"]
        runs = worker.Runs(self.root / "paused", launcher=lambda *a: FakeProcess(), clock=lambda: self.now,
                           failure_log=self.root / "failures.log", sandbox_check=lambda: said[0])
        self.addCleanup(runs.close)
        self.assertEqual(runs.status(OWNER)["paused"], worker.PAUSED)
        file = upload()
        with self.assertRaises(worker.WorkerError) as error:
            runs.start(OWNER, file)
        self.assertEqual((error.exception.code, str(error.exception)), ("paused", worker.PAUSED))
        self.assertTrue(file.closed)
        self.assertEqual(list((runs.root / "runs").iterdir()), [])
        # One line in the failure log for the outage, however many rounds it lasts.
        runs.check_sandbox()
        runs.check_sandbox()
        (line,) = (self.root / "failures.log").read_text().splitlines()
        self.assertEqual(line.split("\t")[:4], ["-", "-", "sandbox", "agents cannot be sandboxed, so no analysis "
                                                "can start: bwrap: setting up uid map: Permission denied"])
        # Once they can again, runs start; a later outage is logged again.
        said[0] = None
        runs.check_sandbox()
        self.assertNotIn("paused", runs.status(OWNER))
        self.assertEqual(runs.start(OWNER, upload())["status"], "running")
        said[0] = "bwrap: setting up uid map: Permission denied"
        runs.check_sandbox()
        self.assertEqual(len((self.root / "failures.log").read_text().splitlines()), 2)

    def test_the_sandbox_check_makes_the_namespaces_the_harness_profile_asks_for(self):
        refused = subprocess.CompletedProcess([], 1, b"", b"bwrap: setting up uid map: Permission denied\n")
        with patch.object(worker.subprocess, "run", return_value=refused) as run:
            self.assertEqual(worker.sandbox_problem(), "bwrap: setting up uid map: Permission denied")
        argv = run.call_args.args[0]
        self.assertTrue(argv[0].endswith("bwrap"))
        sys.path.insert(0, str(REPO / "harness"))
        from harness import profile
        asked = {line for line in profile.build_profile(str(self.root / "s"), str(self.root / "s" / ".home"), 0)
                 .splitlines() if line.startswith("--unshare") or line in ("--die-with-parent", "--new-session")}
        self.assertEqual(asked, {"--unshare-user", "--unshare-pid", "--unshare-uts", "--unshare-ipc",
                                 "--die-with-parent", "--new-session"})
        self.assertLessEqual(asked, set(argv))
        with patch.object(worker.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, b"", b"")):
            self.assertIsNone(worker.sandbox_problem())
        with patch.object(worker.subprocess, "run", side_effect=subprocess.TimeoutExpired("bwrap", 10)):
            self.assertEqual(worker.sandbox_problem(), "bubblewrap did not answer within 10 seconds")
        with patch.object(worker.shutil, "which", return_value=None), \
                patch.object(worker.os.path, "exists", return_value=False):
            self.assertEqual(worker.sandbox_problem(), "bubblewrap is not installed")

    def test_the_worker_checks_the_sandbox_when_it_starts_and_on_every_round(self):
        made = {}

        class Runs:
            def __init__(self, *args, **settings):
                made.update(settings)

            def close(self):
                pass
        with patch.object(worker, "Runs", Runs), patch.object(worker, "WorkerServer") as server, \
                patch("demo_site.auth.PasswordService"), patch.object(worker.os, "geteuid", return_value=4321), \
                patch.object(sys, "argv", ["worker", "--state-dir", str(self.root / "p"),
                                           "--socket", str(self.root / "worker.sock")]):
            server.return_value.serve_forever.side_effect = KeyboardInterrupt
            worker.main()
        self.assertIs(made["sandbox_check"], worker.sandbox_problem)
        calls = []

        class Round:
            def sweep(self):
                calls.append("sweep")
                raise OSError("a purge failed")

            def check_sandbox(self):
                calls.append("sandbox")
        worker._tend(Round(), type("Auth", (), {"notice": lambda self: calls.append("notice")})())
        self.assertEqual(calls, ["sweep", "sandbox", "notice"])

    def test_purge_completed_failed_and_frozen_artifacts(self):
        for code in (0, 1):
            self.fake = FakeProcess()
            state = self.runs.start(OWNER, upload())
            root = self.runs._path(state["run_id"])
            for name in ("harness/sandboxes/test", "harness/vault/runs/test", "live/test"):
                target = root / name
                target.mkdir(parents=True)
                (target / "genome-copy").write_text("synthetic")
                target.chmod(0o555)
            self.fake.returncode = code
            self.runs.sweep()
            self.assertEqual(self.runs.status(OWNER)["status"], "completed" if code == 0 else "failed")
            self.now += worker.RETENTION_SECONDS - 1
            self.runs.sweep()
            self.assertTrue(root.exists())
            self.now += 1
            self.runs.sweep()
            self.assertFalse(root.exists())
        self.assertFalse((REPO / "results" / state["run_id"]).exists())

    def test_restart_ends_abandoned_as_failure_and_retains_finished(self):
        abandoned = self.runs.start(OWNER, upload())["run_id"]
        # Simulate the worker disappearing while its durable record remains.
        self.runs.processes.clear()
        self.runs.lock_file.close()
        restarted = worker.Runs(self.root / "private", launcher=lambda *a: FakeProcess(), clock=lambda: self.now)
        self.addCleanup(restarted.close)
        # Not dropped: ended as an environment failure its owner sees, kept for retention.
        self.assertTrue(restarted._path(abandoned).exists())
        self.assertEqual([r["run_id"] for r in restarted.interrupted], [abandoned])
        state = restarted.status(OWNER)
        self.assertEqual((state["status"], state["run_id"]), ("failed", abandoned))
        self.assertIn("error", state)
        record = json.loads((restarted._path(abandoned) / "record.json").read_text())
        self.assertEqual(record["environment_failure"], worker.INTERRUPTED)
        self.now += worker.RETENTION_SECONDS
        restarted.sweep()
        self.assertFalse(restarted._path(abandoned).exists())
        state = restarted.start(OWNER, upload())
        restarted.processes[state["run_id"]].returncode = 0
        restarted.sweep()
        restarted.close()
        again = worker.Runs(self.root / "private", clock=lambda: self.now)
        self.addCleanup(again.close)
        self.assertEqual(again.status(OWNER)["status"], "completed")

    def test_symlink_artifacts_are_never_read_or_removed_outside_run(self):
        state = self.runs.start(OWNER, upload())
        root = self.runs._path(state["run_id"])
        outside = self.root / "outside"
        outside.mkdir()
        (outside / "private.json").write_text('{"private":"must not escape"}')
        (root / "link").symlink_to(outside, target_is_directory=True)
        with self.assertRaises(OSError):
            worker._read_artifact(root, "link", "private.json")
        self.runs.end(OWNER)
        self.assertTrue((outside / "private.json").exists())

    def test_real_router_fake_agent_question_and_answer(self):
        gate = self.root / "gate.json"
        gate.write_text(json.dumps({"hard": ["SYNTHETIC_PRIVATE"],
            "provider_hard": ["SYNTHETIC_PROVIDER"], "deny_eval_run": "synthetic-private-run",
            "prior_run": "synthetic-prior-run"}))

        def launch(record, upload):
            return GroupProcess(subprocess.Popen([sys.executable,
                str(REPO / "tests/fixtures/demo_fake_harness.py"), str(upload.parent), record["run_id"]],
                env=dict(os.environ, PRS_HARNESS_TOKENS=str(gate)), start_new_session=True,
                stdout=subprocess.DEVNULL, stderr=subprocess.PIPE))

        self.runs.launcher = launch
        # The run's working files are read below, after it ends: their deletion then is held
        # here, and asked for (test_a_finished_run_keeps_only_its_result_for_24_hours deletes).
        cleaned = []
        self.runs._clean_working = cleaned.append
        state = self.runs.start(OWNER, upload())
        run_id = state["run_id"]
        deadline = time.monotonic() + 10
        while state["status"] == "running" and time.monotonic() < deadline:
            time.sleep(.05)
            state = self.runs.status(OWNER)
        self.assertEqual(state["status"], "waiting")
        self.assertEqual(state["question"]["text"], "How would you describe your ancestry?")
        qid = state["question"]["id"]
        with self.assertRaises(worker.WorkerError):
            self.runs.answer(OTHER, qid, "not my question")
        self.runs.answer(OWNER, qid, "My family is from the Levant.")
        with self.assertRaises(worker.WorkerError):
            self.runs.answer(OWNER, qid, "a duplicate")
        while self.runs.status(OWNER)["status"] in ("running", "waiting") and time.monotonic() < deadline:
            time.sleep(.05)
        final = self.runs.status(OWNER)
        self.assertEqual(final["status"], "completed")
        root = self.runs._path(run_id)
        self.assertEqual((root / "agent-received.txt").read_text().strip(), "My family is from the Levant.")
        manifest = json.loads((root / "harness/vault/runs" / run_id / "manifest.json").read_text())
        self.assertEqual(manifest["published"]["state"], "demo")
        self.assertIsNone(manifest["published"]["dest"])
        self.assertIn("report", final["report"]["bundle"])
        self.assertFalse((REPO / "results" / "adhoc" / run_id).exists())
        self.assertEqual(cleaned, [run_id])
        self.runs.end(OWNER)
        self.assertFalse(root.exists())

    def test_fixed_harness_command_default_local_imputation(self):
        group = self.root / "cgroup"
        group.mkdir()
        args = [str(self.root / "demo"), "demo-" + "a" * 32,
                str(self.root / "demo/genome.txt"), str(group)]
        with patch.object(os, "execve") as execute:
            worker._run_child(args)
        _, command, env = execute.call_args.args
        self.assertIn("--demo", command)
        self.assertEqual(command[command.index("--backend") + 1], "human")
        self.assertNotIn("--impute-backend", command)
        self.assertNotIn("--persona", command)
        self.assertEqual(env["PRS_DEMO_ROOT"], str(self.root / "demo/harness"))

    def test_socket_peer_and_strict_rpc(self):
        class Auth:
            def login(self, password, client):
                return {"ok": False, "retry_after": 1, "expires_at": 0}
        # The real handler and framing run without a bind: the review sandbox blocks
        # AF_UNIX send/bind as well as TCP. Peer credentials come from this transport double.
        server = object.__new__(worker.WorkerServer)
        server.runs, server.web_uid, server.password_service = self.runs, os.getuid(), Auth()
        class Transport:
            def __init__(self, value):
                raw = json.dumps(value).encode()
                self.input = io.BytesIO(struct.pack("!I", len(raw)) + raw)
                self.output = bytearray()
            def recv(self, size):
                return self.input.read(size)
            def sendall(self, data):
                self.output.extend(data)
            def settimeout(self, value):
                pass
            def getsockopt(self, *args):
                return struct.pack("3i", os.getpid(), os.getuid(), os.getgid())
        transport = Transport({"op": "status", "owner": OWNER})
        worker.WorkerHandler(transport, None, server)
        self.assertEqual(json.loads(transport.output[4:])["data"]["status"], "idle")
        for payload, file in (({"op": "password"}, None), ({"op": "start", "owner": OWNER, "command": "sh"}, upload()),
                              ({"op": "start", "owner": OWNER}, None), ({"op": "status", "owner": OWNER}, upload())):
            with self.assertRaises(worker.WorkerError):
                server.dispatch(payload, file)
            self.assertTrue(file is None or file.closed)
        server.web_uid = os.getuid() + 1
        transport = Transport({"op": "status", "owner": OWNER})
        worker.WorkerHandler(transport, None, server)
        self.assertEqual(json.loads(transport.output[4:])["code"], "forbidden")

    def test_unix_socket_smoke_when_permitted(self):
        try:
            server = worker.WorkerServer(self.root / "worker.sock", self.runs, os.getuid(), None)
        except PermissionError:
            self.skipTest("Review sandbox denies AF_UNIX bind; real framing/peer handler tested in memory.")
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            client = worker.WorkerClient(self.root / "worker.sock")
            response = client.request("status", owner=OWNER)
            self.assertEqual(response["status"], "idle")
            # The web is this same account: the socket's directory is private to it.
            self.assertEqual(self.root.stat().st_mode & 0o777, 0o700)
            # An upload goes beside its request as an open file, which the worker reads itself.
            with upload() as file:
                response = client.request("start", upload=file, owner=OWNER, intake=ANSWERS)
                self.assertFalse(file.closed)
            self.assertEqual(response["status"], "running")
            self.assertEqual((self.runs._path(response["run_id"]) / "genome.txt").read_bytes(), GENOME)
            client.request("end", owner=OWNER)
            with upload(b"not a genome") as file, self.assertRaises(worker.WorkerError) as error:
                client.request("start", upload=file, owner=OWNER, intake=ANSWERS)
            self.assertEqual(error.exception.code, "upload")
        finally:
            server.shutdown()
            server.server_close()

    def test_oversized_socket_frame_is_rejected_before_payload(self):
        class HeaderOnly:
            def settimeout(self, value):
                pass
            def recv(self, size):
                return struct.pack("!I", worker.MAX_MESSAGE + 1)
        with self.assertRaises(worker.WorkerError):
            worker._receive(HeaderOnly())

    def test_a_run_never_recorded_is_ended_too(self):
        run_id = "demo-" + "b" * 32
        (self.runs.root / "runs" / run_id).mkdir(mode=0o700)
        self.runs.close()
        restarted = worker.Runs(self.root / "private", clock=lambda: self.now)
        self.addCleanup(restarted.close)
        record = json.loads((restarted._path(run_id) / "record.json").read_text())
        self.assertEqual((record["status"], record["owner"], record["environment_failure"]),
                         ("failed", None, worker.INTERRUPTED))
        self.assertEqual(restarted.status(OWNER)["status"], "idle")

    def test_end_interrupted_refuses_beside_a_worker_and_never_deletes(self):
        abandoned = self.runs.start(OWNER, upload())["run_id"]
        command = [sys.executable, "-m", "demo_site.worker", "end-interrupted",
                   "--state-dir", str(self.root / "private")]
        busy = subprocess.run(command, cwd=REPO, capture_output=True, text=True, timeout=60)
        self.assertNotEqual(busy.returncode, 0)
        self.assertIn("Another demo worker owns this state directory", busy.stderr)
        self.runs.processes.clear()
        self.runs.close()
        done = subprocess.run(command, cwd=REPO, capture_output=True, text=True, timeout=60)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(done.stdout.strip(), "ended interrupted demo run " + abandoned)
        record = json.loads((self.runs._path(abandoned) / "record.json").read_text())
        self.assertEqual((record["status"], record["environment_failure"]), ("failed", worker.INTERRUPTED))

    def test_process_groups_stop_a_run_by_its_id_and_nothing_else(self):
        run_id = "demo-" + uuid_hex()
        groups = worker.ProcessGroups(self.root / "private" / "runs")
        stubborn = ("import signal, subprocess, sys, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); "
                    "subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(120)', sys.argv[1]]); "
                    "time.sleep(120)")
        run = subprocess.Popen([sys.executable, "-c", stubborn, run_id], start_new_session=True)
        bystander = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)", "demo-" + uuid_hex()])
        self.addCleanup(bystander.kill)
        deadline = time.monotonic() + 10
        while len(groups.members(run_id)) < 2 and time.monotonic() < deadline:
            time.sleep(.05)
        self.assertEqual(len(groups.members(run_id)), 2)
        self.assertEqual(groups.create(run_id), "-")
        groups.stop(run_id)
        run.wait(timeout=10)
        self.assertEqual(groups.members(run_id), [])
        self.assertIsNone(bystander.poll())

    def test_without_root_the_web_is_this_account(self):
        with patch.object(worker.os, "geteuid", return_value=4321), \
                patch.object(sys, "argv", ["worker", "--web-uid", str(os.getuid() + 1)]):
            with self.assertRaises(SystemExit):
                worker.main()
        args = type("Args", (), {"cgroup_root": None, "state_dir": str(self.root / "private")})
        with patch.object(worker.os, "geteuid", return_value=4321):
            self.assertIsInstance(worker._groups(args), worker.ProcessGroups)

    def test_failed_process_cleanup_keeps_slot_and_artifacts(self):
        class Groups:
            def stop(self, run_id):
                raise RuntimeError("synthetic cgroup could not be stopped")
        state = self.runs.start(OWNER, upload())
        self.runs.cgroups = Groups()
        self.fake.returncode = 0
        with self.assertRaises(RuntimeError):
            self.runs.sweep()
        self.assertEqual([r["run_id"] for r in self.runs._running()], [state["run_id"]])
        self.assertTrue(self.runs._path(state["run_id"]).exists())
        self.runs.cgroups = None


if __name__ == "__main__":
    unittest.main()
