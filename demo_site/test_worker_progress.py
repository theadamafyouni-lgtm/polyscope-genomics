"""Progress regression cases use invented events and never launch a runner."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock

from demo_site.worker import Runs


class WorkerProgressTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="demo-progress-test-")
        self.addCleanup(temporary.cleanup)
        self.launcher = Mock(side_effect=AssertionError("must not launch a runner"))
        self.addCleanup(self.launcher.assert_not_called)
        self.runs = Runs(Path(temporary.name) / "private", launcher=self.launcher,
                         clock=lambda: 1000)
        self.addCleanup(self.runs.close)
        self.owner = "synthetic-progress-owner"
        self.run_id = "demo-" + "a" * 32
        self.runs.records[self.run_id] = {
            "run_id": self.run_id, "owner": self.owner,
            "status": "running", "started_at": 1000,
        }
        live = self.runs._path(self.run_id) / "live" / self.run_id
        live.mkdir(parents=True)
        self.session = live / "session.jsonl"

    def progress(self, events):
        self.session.write_text("".join(json.dumps(event) + "\n" for event in events))
        return self.runs.status(self.owner)["progress"]

    def test_description_precedence_and_nonblank_strings(self):
        descriptions = {
            "message": "Explicit message", "text": "Explicit text",
            "decision": "The input can be scored.",
            "note": "Checked score-file facts.", "tool": "inspect_scorefile",
        }
        for index, key in enumerate(descriptions):
            with self.subTest(first_available=key):
                event = {"kind": "stage_progress", "stage": "select", **descriptions}
                for preceding in list(descriptions)[:index]:
                    event[preceding] = " \n "
                self.assertEqual(self.progress([event])[0]["text"], descriptions[key])
        event = {
            "kind": "stage_progress", "stage": "select", "message": {},
            "text": [], "decision": False, "note": 7, "tool": "  read_facts  ",
        }
        self.assertEqual(self.progress([event])[0]["text"], "read_facts")

    def test_bookkeeping_notes_and_completion_decisions_preserve_metadata(self):
        events = [
            {"kind": "run_started", "ts": "2030-01-01T00:00:00Z"},
            {"kind": "stage_started", "stage": "door", "ts": "2030-01-01T00:00:01Z"},
            {"kind": "stage_completed", "stage": "door", "outcome": "proceed",
             "decision": "Recognized a supported genotype file.",
             "note": "This note loses to the decision.", "ts": "2030-01-01T00:00:02Z"},
            {"kind": "stage_progress", "stage": "door", "note": "Saved provenance.",
             "tool": "write_provenance", "ts": "2030-01-01T00:00:03Z"},
            {"kind": "question", "question_id": "synthetic-question", "stage": "select",
             "question": "Continue?"},
            {"kind": "run_completed", "stage": "report", "outcome": "score",
             "ts": "2030-01-01T00:00:04Z"},
        ]
        progress = self.progress(events)
        self.assertEqual([item["text"] for item in progress], [
            "Demo started", "door started", "Recognized a supported genotype file.",
            "Saved provenance.", "report completed",
        ])
        for event, item in zip((event for event in events if event["kind"] != "question"), progress):
            self.assertEqual({key: item[key] for key in ("kind", "stage", "outcome", "ts")},
                             {key: event.get(key) for key in ("kind", "stage", "outcome", "ts")})

    def test_empty_or_invalid_descriptions_use_readable_fallback(self):
        for stage in (None, "", " \n ", 42, {}):
            with self.subTest(stage=stage):
                event = {"kind": "stage_progress", "stage": stage,
                         "message": None, "text": "", "decision": [], "note": " ", "tool": {}}
                self.assertEqual(self.progress([event])[0]["text"], "Demo progress")
        self.assertEqual(self.progress([{"kind": "progress", "stage": " score "}])[0]["text"],
                         "score progress")


if __name__ == "__main__":
    unittest.main()
