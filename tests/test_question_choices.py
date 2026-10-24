"""Questions with a fixed set of answers carry them: the agent sends its choices with the
question (harness_client.py ask-person --option ...), and --other when a free-text answer makes
sense beside them. The question event carries both to the page, and the demo worker takes an
answer outside the choices only when 'other' was offered."""
import importlib.util
import io
import contextlib
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "harness"))
from harness import liverun  # noqa: E402

spec = importlib.util.spec_from_file_location("harness_client", REPO / "harness" / "sandbox_tools" / "harness_client.py")
client = importlib.util.module_from_spec(spec)
spec.loader.exec_module(client)


class ClientChoices(unittest.TestCase):
    def ask(self, *argv):
        sent = []
        with patch.object(client, "_send", sent.append), \
                patch.object(client, "_await", lambda mid, timeout: {"status": "answer", "text": "Yes"}), \
                patch.object(sys, "argv", ["harness_client.py", "ask-person", "--stage", "select",
                                           "--question", "May I go ahead?", "--why", "It decides the model.", *argv]), \
                contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()) as err:
            try:
                rc = client.main()
            except SystemExit as exc:
                rc = exc.code
        return rc, sent, err.getvalue()

    def test_choices_go_with_the_question_and_other_only_beside_them(self):
        rc, sent, _ = self.ask("--option", "Yes, go ahead", "--option", "No")
        self.assertEqual(rc, 0)
        self.assertEqual((sent[0]["options"], sent[0]["other"]), (["Yes, go ahead", "No"], False))
        rc, sent, _ = self.ask("--option", "Yes, go ahead", "--option", "No", "--other")
        self.assertEqual((rc, sent[0]["other"]), (0, True))
        rc, sent, _ = self.ask()                                   # the person's own words: no choices
        self.assertEqual((rc, sent[0]["options"], sent[0]["other"]), (0, [], False))
        for bad in (["--other"],                                   # 'other' beside nothing
                    ["--option", "Yes", "--option", "Yes"],        # the same choice twice
                    ["--option", "Yes", "--option", " "],          # an empty one
                    ["--option", "Yes"],                           # one is not a choice
                    [arg for i in range(9) for arg in ("--option", "Choice %d" % i)],
                    ["--option", "Yes", "--option", "x" * 121]):
            with self.subTest(bad=bad[:4]):
                rc, sent, err = self.ask(*bad)
                self.assertEqual((rc, sent), (5, []), err)
                self.assertIn("refusing to send", err)


class ClientPlainWords(unittest.TestCase):
    def test_no_internal_code_goes_to_the_person_in_a_question(self):
        ask = ClientChoices.ask
        for argv in (["--question", "May I go ahead, as INT-4 asks?"], ["--why", "SEL-12 needs it."],
                     ["--option", "Yes", "--option", "No (R1)"]):
            with self.subTest(argv=argv):
                rc, sent, err = ask(self, *argv)
                self.assertEqual((rc, sent), (5, []), err)
                self.assertIn("plain words", err)
        self.assertEqual(ask(self, "--question", "May I use model PGS005218 on GRCh37?")[0], 0)


class SamePattern(unittest.TestCase):
    def test_the_three_places_that_keep_codes_from_the_person_agree(self):
        sys.path.insert(0, str(REPO / "agent" / "tools"))
        import lib_run
        from demo_site import worker
        self.assertEqual(client.INTERNAL_CODE.pattern, lib_run.INTERNAL_CODE.pattern)
        self.assertEqual(worker.INTERNAL_CODE.pattern, lib_run.INTERNAL_CODE.pattern)


class EventChoices(unittest.TestCase):
    def test_the_question_event_carries_the_choices_and_whether_other_is_offered(self):
        with tempfile.TemporaryDirectory() as tmp:
            live = liverun.LiveRun(tmp, "run-example")
            Path(live.dir).mkdir(parents=True)
            seen = []
            for qid, options, other in (("q1", ["Yes", "No"], True), ("q2", ["Yes", "No"], False), ("q3", None, False)):
                threading.Timer(0.2, lambda q=qid: live._append(live.answers_path, {"kind": "answer", "question_id": q, "text": "Yes"})).start()
                live.ask(qid, "select", "May I?", "It matters.", options=options, other=other, timeout=5, poll=0.05,
                         on_emit=seen.append)
            self.assertEqual([(e.get("options"), e.get("other")) for e in seen],
                             [(["Yes", "No"], True), (["Yes", "No"], False), (None, None)])


if __name__ == "__main__":
    unittest.main()
