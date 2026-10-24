"""The harness side of the live-run contract.

Contract: ../frontend/interfaces/live-run.md ("Live-run interface -- the harness / window
contract"). Two append-only JSONL files in a run directory, polled. No ports, no sockets, no
startup ordering.

    <RUNS_ROOT>/<run_id>/
        upload/<filename>    the window writes this, once, before starting
        session.jsonl        harness -> window
        answers.jsonl        window -> harness

The reason this module matters beyond plumbing: it is what makes the persona backend and the
human backend the same thing. A persona satisfies the contract by appending exactly the same
`answer` line the browser appends. The orchestrator routes a question out and waits for an
answer in, and never learns which side produced it.

Two places where the frontend's own code diverges from its written contract. Each is
handled here, and each is recorded in the manifest so the divergence is visible rather than
absorbed:

  (a) session.jsonl is not harness-only. The window mirrors answers and session_end into it
      and writes its own run_started. Answers are therefore read from answers.jsonl only.
  (b) stage_completed carries "proceed"; run_completed must carry exactly "score" or the
      window never routes to the result view.
"""
import json
import os
import re
import time

SAFE_RUN_ID = re.compile(r"^[A-Za-z0-9._-]+$")
RUNS_ROOT_ENV = "PRS_RUNS_ROOT"

STAGES = ["door", "ancestry", "select", "score", "interpret", "report"]


def utcnow():
    # Second resolution, UTC, trailing Z -- matching the window exactly. Collisions are
    # routine and expected; line order is the only total order. Nothing here sorts by ts.
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


class LiveRunError(Exception):
    pass


class LiveRun:
    """One run directory, from the harness side."""

    # Malformed lines seen anywhere, so corruption reaches the manifest rather than being
    # silently skipped. Class-level: one run has one live-run directory.
    _corrupt = []

    def __init__(self, runs_root, run_id):
        if not SAFE_RUN_ID.match(run_id):
            raise LiveRunError("run_id %r fails the window's own validator %s"
                               % (run_id, SAFE_RUN_ID.pattern))
        self.runs_root = os.path.abspath(runs_root)
        self.run_id = run_id
        self.dir = os.path.join(self.runs_root, run_id)
        self.session_path = os.path.join(self.dir, "session.jsonl")
        self.answers_path = os.path.join(self.dir, "answers.jsonl")

    # -- reading ---------------------------------------------------------
    @staticmethod
    def _read_jsonl(path):
        """Read whole lines only, and tell a partial write apart from corruption.

        A partially flushed FINAL line is normal while the other side is mid-write: stop
        there and pick it up next poll. A malformed line anywhere EARLIER is corruption, and
        stopping on it would hide every answer after it -- turning a lost answer into a
        recorded timeout, which reads as the person having said nothing. Those are skipped
        and surfaced in the manifest instead.
        """
        out = []
        if not os.path.exists(path):
            return out
        try:
            with open(path, "r") as fh:
                lines = fh.readlines()
        except OSError:
            return out
        for i, line in enumerate(lines):
            if not line.endswith("\n"):
                # The last line is still being written. Normal; pick it up next poll.
                break
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                # A malformed line that is NOT the last one is corruption, not a partial
                # write. Stopping here would hide every answer after it and turn a lost
                # answer into a recorded timeout, so it is skipped and surfaced instead.
                LiveRun._corrupt.append({"path": path, "line_number": i + 1,
                                         "text": line[:200]})
                continue
        return out

    def answer_events(self):
        return self._read_jsonl(self.answers_path)

    # -- writing ---------------------------------------------------------
    def _append(self, path, event):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        event.setdefault("ts", utcnow())
        # One write() of one complete line. Both sides read line-at-a-time and stop on a
        # partial line, so a buffered partial write would stall the other side's poll loop.
        with open(path, "a") as fh:
            fh.write(json.dumps(event, sort_keys=False) + "\n")
            fh.flush()
            os.fsync(fh.fileno())
        return event

    def emit(self, kind, **fields):
        """Append a harness event to session.jsonl."""
        ev = {"kind": kind}
        ev.update(fields)
        return self._append(self.session_path, ev)

    def emit_answer(self, question_id, text, skipped=False):
        """Append an answer to answers.jsonl.

        Used by the PERSONA backend. The browser writes the identical shape. Exactly five
        keys -- the window constructs precisely these and nothing else.
        """
        return self._append(self.answers_path, {
            "kind": "answer",
            "question_id": question_id,
            "text": text,
            "skipped": bool(skipped),
        })

    def emit_session_end(self, at_gate, note=""):
        return self._append(self.answers_path, {
            "kind": "session_end",
            "outcome": "session-ended-by-person",
            "at_gate": at_gate,
            "note": note,
        })

    # -- state -----------------------------------------------------------
    def ended_by_person(self):
        for a in self.answer_events():
            if a.get("kind") == "session_end":
                return a
        return None

    # -- the one interaction that blocks ---------------------------------
    def ask(self, question_id, stage, question, why_it_matters, hard_blocker=False,
            options=None, timeout=900, poll=0.4, on_emit=None, other=False):
        """Emit one question and block until it is answered or the person leaves.

        Returns (status, payload):
            ("answer",      {...})  an answer arrived
            ("session_end", {...})  the person ended the session -- spec 12, NOT an outcome
            ("timeout",     None)   nobody answered in time

        The frontend's reference implementation collapses the last two into a single None
        and its caller prints "person ended the session (or timed out)", unable to tell them
        apart. They are different events with different obligations -- one is the person
        exercising a right, the other is the harness failing -- so they are kept distinct
        here.

        Only ONE question may be outstanding at a time. This is a hard requirement, not a
        convention: the window's state reducer does a bare `pending = e` on every question
        event, so a second question silently replaces the first on screen and answering the
        first never clears the second. The blocking shape of this call is what enforces it.
        """
        if not why_it_matters or not str(why_it_matters).strip():
            # FRAME-1: the person is assumed to know nothing about genetics, and a bare
            # question with no stated reason is what makes people guess. The contract marks
            # this required; refusing here rather than defaulting keeps that true.
            raise LiveRunError("why_it_matters is required on every question (FRAME-1)")

        already = {a.get("question_id") for a in self.answer_events()
                   if a.get("kind") == "answer"}
        if question_id in already:
            raise LiveRunError("question_id %r already answered in this run" % question_id)

        ev = {"kind": "question", "question_id": question_id, "stage": stage,
              "question": question, "why_it_matters": why_it_matters,
              "hard_blocker": bool(hard_blocker)}
        if options:
            # The choices, and whether a free-text answer is taken beside them.
            ev["options"] = list(options)
            ev["other"] = bool(other)
        self._append(self.session_path, ev)
        if on_emit:
            on_emit(ev)

        deadline = time.time() + timeout
        while time.time() < deadline:
            for a in self.answer_events():
                if a.get("kind") == "session_end":
                    return "session_end", a
                if a.get("kind") == "answer" and a.get("question_id") == question_id:
                    return "answer", a
            time.sleep(poll)
        return "timeout", None
