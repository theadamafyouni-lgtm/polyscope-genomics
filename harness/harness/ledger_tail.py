"""Tail the PRS agent's own ledger and translate it into live-run events.

The agent records every stage into `runs/<run_id>/ledger.jsonl` inside its sandbox, but the
orchestrator launches that agent once and blocks on a single subprocess call until it
returns -- so it never observes a stage boundary and emits nothing between `run_started` and
the end, which is why a 61-minute run showed the window an empty progress list with a
35-minute silence between judgments.

This module is MECHANICAL. It translates lines that exist, in file order, and nothing else:
it never infers a stage the ledger did not name, never synthesises an outcome, never emits a
refusal. `outcome` is passed through verbatim rather than mapped onto the window's
vocabulary, because a translated outcome is the harness making a judgment on the agent's
behalf -- the one thing the rest of this harness refuses to do. No fraction is emitted on
progress: the ledger has no notion of one, and a made-up number would be fabrication in the
one place a person is watching.

Depends on liverun for STAGES and for the LiveRun it writes through. It knows nothing about
the orchestrator.
"""
import json
import os
import sys
import threading
import time

from . import liverun

# Emitted on every translated event, so a reader can tell a tailer's mechanical translation
# apart from an event the harness deliberately decided to write.
SOURCE = "ledger"


class LedgerTailer:
    """One agent ledger, followed while the run is in flight."""

    def __init__(self, live, ledger_path, poll_seconds=1.0):
        self.live = live
        self.ledger_path = ledger_path
        self.poll_seconds = poll_seconds

        self._stop = threading.Event()
        self._thread = None

        # Byte offset of the first byte NOT yet translated. Always left before a partial
        # trailing line, never inside one.
        self._offset = 0
        self._announced = set()
        self._unknown_reported = set()

        self.lines_read = 0
        self.events_emitted = 0
        self.unknown_stage_lines = 0
        self.unparseable_lines = 0
        self.ignored_kind_lines = 0
        self.ledger_found = False
        self.resets = 0

    # -- lifecycle -------------------------------------------------------
    def start(self):
        """Spawn the follower, as the orchestrator spawns its router thread."""
        if self._thread is not None:
            return self._thread
        self._thread = threading.Thread(target=self._follow_forever, daemon=True)
        self._thread.start()
        return self._thread

    def stop(self, timeout=10):
        """Signal the thread, join it, then drain once more.

        The final drain is the point of this method. The agent writes its closing `report`
        judgment and exits, and the orchestrator's join happens immediately afterwards -- so
        every line written since the last poll tick would be lost on a plain stop. A tailer
        that drops the closing judgment is worse than no tailer at all, because the window
        then shows a run that ran and never concluded.
        """
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=timeout)
        drained = self._drain()
        return {"final_drain_lines": drained, "stats": self.stats()}

    def stats(self):
        """Enough to tell a tailer that never found its ledger from a run with no stages.

        Those two produce an identical (empty) progress list in the window, and only
        `ledger_found` distinguishes a plumbing failure from an honest silence.
        """
        return {
            "ledger_path": self.ledger_path,
            "ledger_found": self.ledger_found,
            "lines_read": self.lines_read,
            "events_emitted": self.events_emitted,
            "unknown_stage_lines": self.unknown_stage_lines,
            "unparseable_lines": self.unparseable_lines,
            "ignored_kind_lines": self.ignored_kind_lines,
            "unknown_stage_labels": sorted(str(s) for s in self._unknown_reported),
            "bytes_consumed": self._offset,
            "file_resets": self.resets,
            "stages_open": sorted(self._announced),
        }

    # -- reading ---------------------------------------------------------
    def _follow_forever(self):
        # The ledger does not exist until the agent's first tool call writes it, which is
        # minutes in. Polling for it is the normal case, not an error.
        while not self._stop.is_set():
            self._drain()
            time.sleep(self.poll_seconds)

    def _drain(self):
        """Translate every COMPLETE line after the offset. Returns how many were read."""
        try:
            size = os.path.getsize(self.ledger_path)
        except OSError:
            return 0
        self.ledger_found = True

        if size < self._offset:
            # The file was replaced, not appended to. Anything before this is a different
            # file's content and the offset means nothing against it.
            sys.stderr.write(
                "ledger_tail: %s shrank from %d to %d bytes -- file replaced, "
                "restarting from 0\n" % (self.ledger_path, self._offset, size))
            self._offset = 0
            self.resets += 1
        if size == self._offset:
            return 0

        try:
            with open(self.ledger_path, "rb") as fh:
                fh.seek(self._offset)
                chunk = fh.read(size - self._offset)
        except OSError:
            return 0

        cut = chunk.rfind(b"\n")
        if cut < 0:
            # Nothing but a partial line so far. Leave the offset where it is so the line is
            # picked up whole next pass.
            return 0
        complete, self._offset = chunk[:cut + 1], self._offset + cut + 1

        n = 0
        for raw in complete.decode("utf-8", "replace").splitlines():
            raw = raw.strip()
            if not raw:
                continue
            n += 1
            self.lines_read += 1
            try:
                line = json.loads(raw)
            except ValueError:
                # Counted and skipped, never raised: one bad line must not stop the window
                # learning about every stage after it.
                self.unparseable_lines += 1
                continue
            if not isinstance(line, dict):
                self.unparseable_lines += 1
                continue
            self._translate(line)
        return n

    # -- translation -----------------------------------------------------
    def _translate(self, line):
        stage = line.get("stage")
        if stage not in liverun.STAGES:
            self.unknown_stage_lines += 1
            if stage not in self._unknown_reported:
                # Once per distinct label. Silently dropping a stage the window has no slot
                # for is how a mismatch between the skills and STAGES stays invisible.
                self._unknown_reported.add(stage)
                sys.stderr.write("ledger_tail: ledger stage %r is not in liverun.STAGES; "
                                 "lines for it are not emitted\n" % (stage,))
            return

        kind = line.get("kind")
        if kind not in ("facts", "judgment", "artifact"):
            self.ignored_kind_lines += 1
            return

        ts = line.get("ts")
        if stage not in self._announced:
            self._announced.add(stage)
            self._emit("stage_started", ts, stage=stage)

        if kind == "facts":
            fields = {"stage": stage}
            if line.get("tool"):
                fields["tool"] = line["tool"]
            if line.get("artifact"):
                fields["note"] = os.path.basename(line["artifact"])
            self._emit("stage_progress", ts, **fields)
        elif kind == "artifact":
            fields = {"stage": stage}
            if line.get("artifact"):
                fields["note"] = os.path.basename(line["artifact"])
            self._emit("stage_progress", ts, **fields)
        else:
            fields = {"stage": stage}
            for key in ("outcome", "decision", "artifact", "requirement_ids"):
                if key in line:
                    # outcome included: passed through exactly as the agent wrote it.
                    fields[key] = line[key]
            self._emit("stage_completed", ts, **fields)
            # The observed stage sequence re-enters -- select, score, select, score -- so a
            # completed stage is un-announced here and a later line for it emits a fresh
            # stage_started. The window is built to show a stage being returned to.
            self._announced.discard(stage)

    def _emit(self, kind, ts, **fields):
        # The LEDGER's timestamp, not the moment the tailer noticed the line. A drain can lag
        # a write by a poll interval or, on the final drain, by the whole tail of the run --
        # and the gap between two judgments is the number this exists to make visible.
        # LiveRun._append setdefaults ts, so passing it here is enough.
        if ts:
            fields["ts"] = ts
        fields["source"] = SOURCE
        self.live.emit(kind, **fields)
        self.events_emitted += 1
        return kind
