"""Local-only worker. Its RPC has no command, path or flag parameter.

Root-owned with a delegated cgroup where root is available (the VM); otherwise it runs as the
web's own account and finds a run's processes by its run id (ProcessGroups)."""
import argparse
from datetime import datetime, timezone
import fcntl
import hmac
import json
import math
import os
from pathlib import Path
import re
import shutil
import secrets
import signal
import socket
import socketserver
import stat
import statistics
import struct
import subprocess
import sys
import threading
import time
import uuid

from . import failures
from . import intake as form
from .uploads import MAX_EXPANDED_BYTES, MAX_UPLOAD_BYTES, UploadError, expand_upload

REPO = Path(__file__).resolve().parents[1]
# Requests are small: an upload comes as an open file beside its request, never inside it.
# Answers carry a run's report, its artifacts each up to MAX_ARTIFACT.
MAX_MESSAGE = 32 * 1024 * 1024
MAX_ARTIFACT = 4 * 1024 * 1024
# A new demo needs room for its genome expanded, the genome's copy in the agent's sandbox and
# what the run writes (a finished sample A run held 8.6 GB of its own files), beside another run
# still writing as much; below this free space none starts.
MIN_FREE_BYTES = 2 * MAX_EXPANDED_BYTES + 16 * 1024 * 1024 * 1024
# A file that can be checked in this long is checked before the upload is answered: refused,
# it is refused as an upload, and no run is left behind.
QUICK_CHECK_SECONDS = 10
# When a run ends, its upload, the agent's sandbox and working files, and its person's typed
# answers are deleted; the result its page shows (the report with its comparison data, and the
# progress) is kept in result.json for RETENTION_SECONDS after the end, then deleted too. End
# deletes it at once.
RETENTION_SECONDS = 24 * 3600
KEPT = ("record.json", "result.json")
# How long an analysis takes, for the page's estimate and for when a full demo should have a slot
# again. Each completed run that reached the end of its score stage is timed into RUN_TIMES, beside
# the runs and outliving them: from its upload to its end, less the time it waited for its
# person's answers (questions add time of their own). The estimate is the median and range of the
# last RECENT_RUNS; until ENOUGH_RUNS are timed, the runs measured after the speedups
# (MEASURED_RUN_TIMES) count beside them. A run that ended before its score is not timed: an early
# refusal says nothing about how long an analysis takes.
# The requirement codes the specs and skills use (INT-4, SEL-12, R1...): words for the record,
# never for the person. The same pattern is in harness_client.py, demo_site/worker.py and
# lib_run.py (tests/test_question_choices.py holds them equal).
INTERNAL_CODE = re.compile(r"\b(?:INTAKE|INT|ANC|SEL|SCO|JC|REP|ARCH|DATA|FRAME)-\d+[a-z]?\b|\bR\d{1,2}\b")
RUN_TIMES = "run-times.jsonl"
RECENT_RUNS, ENOUGH_RUNS = 10, 5
# Each kind of file is timed and estimated apart: a chip's genotype file, or a whole genome. A
# run's kind is what the door judged its file to be (assay_class); before that, what the file's
# own format says where it says (a genotype table is a chip's; a Complete Genomics var file a
# whole genome's); an exome, or a kind not known, is neither.
KINDS = ("chip", "whole_genome")
FILE_KINDS = {".txt": "chip", ".tsv": "whole_genome"}
MEASURED_RUN_TIMES = Path(__file__).with_name("run-times-measured.json")
TIMED_STAGES = ("score", "interpret", "report")
OWNER_RE = re.compile(r"[A-Za-z0-9_-]{20,128}\Z")
RUN_RE = re.compile(r"demo-[0-9a-f]{32}\Z")
# What an interrupted run is ended with, for its owner to see until the retention sweep.
INTERRUPTED = "interrupted: the demo worker stopped while this run was in progress"
# What a run stopped at --run-limit-minutes is ended with.
TIME_LIMIT = "time limit: stopped after %d minutes"
# What a run that could not authenticate prints (the harness's ENVIRONMENT_FAILURE_MARKERS, and
# the expiry the CLI reports). Seeing one, the worker raises the flag that stops the watchdog
# restarting anything until a person has logged in again and removed it.
LOGIN_MARKERS = ("Failed to authenticate", "OAuth session expired", "Not logged in",
                 "OAuth access token has expired", "OAuth token has been revoked")
# What a run whose agent never launched is ended with: the harness stopped before step 1, which
# is the demo's own failure, never the person's file (_not_launched says why).
NOT_LAUNCHED = "did not launch: "
# bubblewrap's own words when it cannot make a sandbox ("bwrap: setting up uid map: Permission
# denied"), and the harness's own one-line refusals to start (run_harness.py's exits).
BWRAP_SAID = re.compile(r"\bbwrap: [^\n]{1,200}")
HARNESS_SAID = re.compile(r"(?m)^run_harness\.py: [^\n]{1,300}$")
# Every agent runs in a bubblewrap sandbox. While this account cannot make one -- the gb10 after
# its 2026-10-20 reboot, whose new kernel keeps unprivileged user namespaces from it -- no
# analysis can start: the worker looks when it starts and on every round of its upkeep, and
# meanwhile refuses new runs, and every status says so for the page to show before a file is
# chosen (sandbox_problem, Runs.check_sandbox).
PAUSED = ("The demo cannot start a new analysis right now because of a problem on its side. "
          "Please try again later.")
# The namespaces the harness's profile asks bubblewrap for (harness/harness/profile.py; the
# tests hold the two equal).
SANDBOX_CHECK = ("--ro-bind", "/", "/", "--dev", "/dev", "--proc", "/proc", "--unshare-user",
                 "--unshare-pid", "--unshare-uts", "--unshare-ipc", "--die-with-parent", "--new-session")


class WorkerError(Exception):
    def __init__(self, code, message):
        self.code = code
        super().__init__(message)


def _receive(sock):
    deadline = time.monotonic() + 30
    def read(count):
        data = bytearray()
        while len(data) < count:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise WorkerError("invalid", "Worker request timed out.")
            sock.settimeout(remaining)
            part = sock.recv(min(count - len(data), 65536))
            if not part:
                raise WorkerError("invalid", "Incomplete worker request.")
            data.extend(part)
        return bytes(data)
    size = struct.unpack("!I", read(4))[0]
    if size > MAX_MESSAGE:
        raise WorkerError("invalid", "Worker request is too large.")
    try:
        value = json.loads(read(size))
    except (ValueError, UnicodeError) as exc:
        raise WorkerError("invalid", "Invalid worker request.") from exc
    if not isinstance(value, dict):
        raise WorkerError("invalid", "Invalid worker request.")
    return value


def _send(sock, value):
    raw = json.dumps(value, separators=(",", ":")).encode()
    if len(raw) > MAX_MESSAGE:
        raise WorkerError("invalid", "Worker response is too large.")
    sock.sendall(struct.pack("!I", len(raw)) + raw)


def _receive_file(sock):
    """The one open file a "start" request carries right after it, as a binary file to read."""
    sock.settimeout(30)
    try:
        message, fds, flags, _ = socket.recv_fds(sock, 1, 1)
    except OSError as exc:
        raise WorkerError("invalid", "Incomplete worker request.") from exc
    if message != b"\0" or len(fds) != 1 or flags & socket.MSG_CTRUNC:
        for fd in fds:
            os.close(fd)
        raise WorkerError("invalid", "Invalid worker request.")
    return os.fdopen(fds[0], "rb")


class WorkerClient:
    def __init__(self, socket_path="/run/prs-demo/worker.sock"):
        self.socket_path = str(socket_path)

    def request(self, op, upload=None, **payload):
        """``upload``, for "start", is the uploaded file, open: it goes beside the request as a
        descriptor, so the worker reads it from disk however large it is."""
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
                sock.settimeout(45)
                sock.connect(self.socket_path)
                _send(sock, {"op": op, **payload})
                if upload is not None:
                    socket.send_fds(sock, [b"\0"], [upload.fileno()])
                response = _receive(sock)
        except (OSError, TimeoutError) as exc:
            raise WorkerError("unavailable", "The demo worker is unavailable.") from exc
        if not response.get("ok"):
            raise WorkerError(response.get("code", "unavailable"),
                              response.get("error", "The demo worker is unavailable."))
        return response["data"]


class _UntilEnded:
    """The expanded genome's file, which takes no more once its run is ended, so End demo
    stops a check of a whole genome at once instead of when it would have finished."""

    def __init__(self, fh, ended):
        self.fh, self.ended = fh, ended

    def write(self, data):
        if self.ended.is_set():
            raise OSError("the run was ended")
        return self.fh.write(data)


def _write_json(path, value):
    temporary = path.with_suffix(".tmp")
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "w") as fh:
        json.dump(value, fh)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(temporary, path)
    directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


def _read_artifact(root, *parts):
    """Never follow a sandbox-created symlink or read a special/oversized file."""
    fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for part in parts[:-1]:
            nxt = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = nxt
        leaf = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
        with os.fdopen(leaf, "rb") as fh:
            info = os.fstat(fh.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_ARTIFACT:
                raise ValueError("artifact is not a bounded regular file")
            data = fh.read(MAX_ARTIFACT + 1)
            if len(data) > MAX_ARTIFACT:
                raise ValueError("artifact too large")
            return data
    finally:
        os.close(fd)


def _json_artifact(root, *parts):
    try:
        return json.loads(_read_artifact(root, *parts))
    except (OSError, ValueError):
        return None


class Cgroups:
    """Cgroup membership survives setsid(), double-forks and a worker crash."""
    def __init__(self, root):
        self.root = Path(root)
        if not (self.root / "cgroup.controllers").is_file():
            raise RuntimeError("A delegated cgroup v2 directory is required.")

    def create(self, run_id):
        group = self.root / run_id
        group.mkdir(mode=0o700)
        if not (group / "cgroup.kill").exists():
            group.rmdir()
            raise RuntimeError("This kernel must support cgroup.kill.")
        return group

    def stop(self, run_id):
        group = self.root / run_id
        if not group.exists():
            return
        (group / "cgroup.kill").write_text("1")
        deadline = time.monotonic() + 10
        while "populated 1" in (group / "cgroup.events").read_text():
            if time.monotonic() > deadline:
                raise RuntimeError("Demo processes have not stopped; files are retained.")
            time.sleep(0.05)
        # The sandbox cannot write this cgroup tree, so it cannot add child groups.
        group.rmdir()


class ProcessGroups:
    """Cgroups' place where there is no root and so no delegated cgroup.

    A run is every process of this account whose command line names its run id: the launch
    child and the harness it becomes, and each bubblewrap, whose arguments name the run's
    sandbox. The id is 128 random bits, so nothing else names it. What runs inside a sandbox
    is in bubblewrap's pid namespace and dies with it (--die-with-parent); the harness's
    sandboxed agents and tools need nothing more. Stopping is SIGTERM, then SIGKILL, and an
    error, as Cgroups.stop gives one, if anything is still there."""

    def __init__(self, runs_dir):
        self.runs_dir = Path(runs_dir)

    def create(self, run_id):
        return "-"

    def members(self, run_id):
        needle, uid, me = run_id.encode(), os.getuid(), os.getpid()
        found = []
        for entry in os.scandir("/proc"):
            if not entry.name.isdigit() or int(entry.name) == me:
                continue
            try:
                if entry.stat().st_uid != uid:
                    continue
                with open(os.path.join(entry.path, "cmdline"), "rb") as fh:
                    if needle in fh.read():
                        found.append(int(entry.name))
            except OSError:
                continue
        return found

    def stop(self, run_id):
        for sig in (signal.SIGTERM, signal.SIGKILL):
            pids = self.members(run_id)
            if not pids:
                return
            for pid in pids:
                try:
                    os.kill(pid, sig)
                except ProcessLookupError:
                    pass
            deadline = time.monotonic() + 10
            while self.members(run_id) and time.monotonic() < deadline:
                time.sleep(0.05)
        if self.members(run_id):
            raise RuntimeError("Demo processes have not stopped; files are retained.")


def sandbox_problem():
    """None when this account can make the sandbox every agent runs in, else why not, in
    bubblewrap's own words: bubblewrap, found as the harness's shim finds it, making the
    namespaces the harness's profile asks for around /bin/true."""
    bwrap = shutil.which("bwrap") or "/usr/bin/bwrap"
    if not os.path.exists(bwrap):
        return "bubblewrap is not installed"
    try:
        done = subprocess.run([bwrap, *SANDBOX_CHECK, "--", "/bin/true"], stdin=subprocess.DEVNULL,
                              capture_output=True, timeout=10)
    except subprocess.TimeoutExpired:
        return "bubblewrap did not answer within 10 seconds"
    except OSError as exc:
        return "bubblewrap could not run: %s" % exc.strerror
    if done.returncode == 0:
        return None
    said = [line.strip() for line in done.stderr.decode("utf-8", "replace").splitlines() if line.strip()]
    return said[-1][:200] if said else "bubblewrap exited %d" % done.returncode


def _not_launched(outcome, log):
    """Why a run's agent never launched, or None: a sandbox that could not start, in
    bubblewrap's own words; else the kind of failure the harness recorded before launch, never
    its detail, which can quote the sandbox's files; else the harness's own refusal to start."""
    failed = outcome.get("failed_before_launch") if isinstance(outcome, dict) else None
    if isinstance(failed, dict):
        bwrap = BWRAP_SAID.search(str(failed.get("detail") or ""))
        if bwrap:
            return NOT_LAUNCHED + "the sandbox could not start (%s)" % bwrap.group(0).strip()
        kind = str(failed.get("type") or "")
        return NOT_LAUNCHED + (kind if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_.]{0,80}", kind)
                               else "the harness stopped before its agent")
    said = HARNESS_SAID.findall(log or "")
    return NOT_LAUNCHED + said[-1].strip() if said else None


def _plain(note):
    """An agent's note as the person may see it: without the sentences that name an internal
    code (the notes are written for the record). None when nothing is left."""
    kept = [s for s in re.split(r"(?<=[.!?])\s+", note.strip()) if s and not INTERNAL_CODE.search(s)]
    return " ".join(kept) or None


def _seconds(ts):
    """An event's time in Unix seconds, from either form events carry it in; None from neither."""
    if isinstance(ts, (int, float)) and not isinstance(ts, bool) and math.isfinite(ts):
        return float(ts)
    if isinstance(ts, str):
        try:
            at = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        except ValueError:
            return None
        return (at if at.tzinfo else at.replace(tzinfo=timezone.utc)).timestamp()
    return None


def measured_run_times(path=MEASURED_RUN_TIMES):
    """The working seconds of the runs measured after the speedups, by the kind of file each read."""
    try:
        runs = json.loads(Path(path).read_text()).get("runs", [])
    except (OSError, ValueError, AttributeError):
        return {}
    times = {}
    for run in runs:
        if (isinstance(run, dict) and run.get("kind") in KINDS
                and isinstance(run.get("working_seconds"), (int, float)) and run["working_seconds"] > 0):
            times.setdefault(run["kind"], []).append(run["working_seconds"])
    return times


class Runs:
    def __init__(self, root, *, cgroups=None, launcher=None, clock=time.time,
                 failure_log=None, login_flag=None, max_runs=1, run_limit=None, typical_run=None,
                 measured=(), sandbox_check=None):
        """max_runs demos run at once. A run going for run_limit seconds is stopped. A person waiting
        for a slot is told to come back after a run's median time (see RUN_TIMES); measured, the
        working seconds of runs timed after the speedups, stands in until enough are timed here,
        and typical_run, a run's usual length, when there is neither. sandbox_check, the worker's
        sandbox_problem, says whether agents can be sandboxed (check_sandbox); without one, as
        with an injected launcher, they are taken to be."""
        if not isinstance(max_runs, int) or max_runs < 1:
            raise ValueError("max_runs must be a whole number of at least 1")
        if any(value is not None and not value > 0 for value in (run_limit, typical_run)):
            raise ValueError("run_limit and typical_run are positive numbers of seconds")
        self.max_runs, self.run_limit, self.typical_run = max_runs, run_limit, typical_run
        self.measured = {kind: [float(seconds) for seconds in times]
                         for kind, times in dict(measured or {}).items() if kind in KINDS}
        self.root = Path(root).resolve()
        self.failure_log = failure_log
        self.login_flag = login_flag
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(self.root, 0o700)
        self.lock_file = (self.root / "worker.lock").open("a")
        try:
            fcntl.flock(self.lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            self.lock_file.close()
            raise RuntimeError("Another demo worker owns this state directory.") from exc
        self.cgroups = cgroups
        self.launcher = launcher or self._launch
        self.clock = clock
        self.lock = threading.RLock()
        self.records = {}
        self.processes = {}
        self.checks = {}  # run id -> (the thread checking its file, set when the run is ended)
        self.interrupted = []
        self.cleanups = {}  # run id -> the thread deleting its working files
        self.sandbox_check = sandbox_check
        self.sandbox_problem = None  # why agents cannot be sandboxed, as last checked
        self.root.joinpath("runs").mkdir(mode=0o700, exist_ok=True)
        self.run_times = self._read_run_times()
        self._recover()
        self.check_sandbox()

    def check_sandbox(self):
        """Whether agents can be sandboxed now (sandbox_check): looked at when the worker starts and
        on every round of its upkeep (_tend). While they cannot, no new run starts and every
        status says so (PAUSED); the round that finds they cannot puts one line in the failure
        log, and the one that finds they can again says so on stderr. Returns the problem."""
        if self.sandbox_check is None:
            return None
        problem = self.sandbox_check()
        with self.lock:
            was, self.sandbox_problem = self.sandbox_problem, problem
        if problem and not was and self.failure_log:
            failures.append(self.failure_log, "-", "-", "sandbox",
                            "agents cannot be sandboxed, so no analysis can start: " + problem,
                            time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(self.clock())))
        elif was and not problem:
            print("demo sandbox: agents can be sandboxed again", file=sys.stderr, flush=True)
        return problem

    def _path(self, run_id):
        if not RUN_RE.fullmatch(run_id):
            raise ValueError("invalid generated run id")
        return self.root / "runs" / run_id

    def _recover(self):
        # These are only this worker's generated directories, never benchmark runs. One that
        # was still going when the last worker stopped is not deleted: what is left of it is
        # stopped and it is ended as an environment failure its owner sees, then kept for the
        # normal retention time like any other finished run.
        for path in self.root.joinpath("runs").iterdir():
            if not RUN_RE.fullmatch(path.name) or path.is_symlink():
                raise RuntimeError("Unexpected entry in the private demo runs directory.")
            record = _json_artifact(path, "record.json")
            if record and record.get("status") in ("completed", "failed"):
                self.records[path.name] = record
                if not record.get("result_kept") and record.get("owner"):
                    self._keep_result(record, self._events(path.name))
                elif any(p.name not in KEPT for p in path.iterdir()):
                    self._clean_working(path.name)
            else:
                self.interrupted.append(self._end_interrupted(path.name, record))
        self.sweep()

    def _end_interrupted(self, run_id, record):
        if self.cgroups:
            self.cgroups.stop(run_id)
        if not isinstance(record, dict) or record.get("run_id") != run_id:
            # Created and never recorded: no owner can see it, but it is still ended, not dropped.
            record = {"demo": True, "run_id": run_id, "owner": None, "started_at": self.clock()}
        record.update(status="failed", ended_at=self.clock(), environment_failure=INTERRUPTED)
        self.records[run_id] = record
        self._persist(record)
        self._keep_result(record, self._events(run_id))
        self._log_failure(run_id, INTERRUPTED)
        return record

    def _log_failure(self, run_id, why):
        if self.failure_log:
            failures.append(self.failure_log, run_id, "demo", "demo run", why,
                            time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(self.clock())))

    def _why_failed(self, run_id, rc, events):
        """The harness's own classification where it made one, else what the run said last,
        else why its agent never launched (_not_launched)."""
        manifest = _json_artifact(self._path(run_id), "harness", "vault", "runs", run_id,
                                  "manifest.json") or {}
        why = (manifest.get("published") or {}).get("environment_failure")
        if not why:
            failed = next((e for e in reversed(events) if e.get("kind") == "run_failed"), None)
            why = failed.get("error") if failed else None
        outcome = manifest.get("outcome") or {}
        said = "\n".join(str(outcome.get(k) or "") for k in ("prs_stdout_tail", "prs_stderr_tail"))
        log = self._log_tail(run_id)
        return why or _not_launched(outcome, log) or "the harness exited %s" % rc, said + "\n" + log

    def _log_tail(self, run_id, size=65536):
        try:
            fd = os.open(self._path(run_id) / "harness.log", os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        except OSError:
            return ""
        with os.fdopen(fd, "rb") as fh:
            if not stat.S_ISREG(os.fstat(fh.fileno()).st_mode):
                return ""
            fh.seek(max(0, os.fstat(fh.fileno()).st_size - size))
            return fh.read(size).decode("utf-8", "replace")

    def _check_login(self, run_id, text):
        marker = next((m for m in LOGIN_MARKERS if m.lower() in text.lower()), None)
        if marker and self.login_flag:
            flag = Path(self.login_flag)
            _write_json(flag, {"run": run_id, "marker": marker, "at": time.strftime(
                "%Y-%m-%dT%H:%M:%SZ", time.gmtime(self.clock()))})
        return marker

    def _persist(self, record):
        _write_json(self._path(record["run_id"]) / "record.json", record)

    def _launch(self, record, upload):
        if self.cgroups is None:
            raise RuntimeError("A delegated cgroup v2 is required to launch demos.")
        run_id = record["run_id"]
        group = self.cgroups.create(run_id)
        run_root = self._path(run_id)
        # Only generated values appear in argv. No user string becomes a path or flag.
        argv = [sys.executable, "-m", "demo_site.worker", "_launch",
                str(run_root), run_id, str(upload), str(group or "-")]
        with (run_root / "harness.log").open("wb") as log:
            return subprocess.Popen(argv, cwd=REPO, stdin=subprocess.DEVNULL,
                                    stdout=log, stderr=subprocess.STDOUT,
                                    start_new_session=True)

    def _remove(self, run_id):
        check = self.checks.pop(run_id, None)
        if check:
            check[1].set()
        if self.cgroups:
            self.cgroups.stop(run_id)
        process = self.processes.pop(run_id, None)
        if process:
            # Tests inject a fake runner; real children were killed as one cgroup above.
            if process.poll() is None:
                process.kill()
            process.wait(timeout=10)
        cleaning = self.cleanups.pop(run_id, None)
        if cleaning:
            cleaning.join(300)  # its working files are being deleted: let that finish first
        path = self._path(run_id)
        if path.exists():
            # Frozen directories need write permission for unlink. Never chmod files:
            # reference data may be read-only hard links to the shared source.
            for current, dirs, _ in os.walk(path, followlinks=False):
                os.chmod(current, 0o700, follow_symlinks=False)
                for name in dirs:
                    target = Path(current) / name
                    if not target.is_symlink():
                        os.chmod(target, 0o700, follow_symlinks=False)
            shutil.rmtree(path)
        self.records.pop(run_id, None)

    def _stop_at_limit(self, run_id, process):
        """A run past its time limit is stopped, as End demo stops one, and ended as a failure
        its person sees; its files stay for the hour every ended run gets."""
        if self.cgroups:
            self.cgroups.stop(run_id)
        if process.poll() is None:
            process.kill()
        process.wait(timeout=10)
        record = self.records[run_id]
        why = TIME_LIMIT % round(self.run_limit / 60)
        record.update(status="failed", ended_at=self.clock(), why_failed=why, time_limit=True)
        self._persist(record)
        self._keep_result(record, self._events(run_id))
        del self.processes[run_id]
        self._log_failure(run_id, why)

    def sweep(self):
        with self.lock:
            for run_id, process in list(self.processes.items()):
                if (self.run_limit and process.poll() is None
                        and self.clock() - self.records[run_id]["started_at"] >= self.run_limit):
                    self._stop_at_limit(run_id, process)
                    continue
                rc = process.poll()
                if rc is not None:
                    process.wait(timeout=1)
                    record = self.records[run_id]
                    events = self._events(run_id)
                    failed = rc != 0 or any(e.get("kind") == "run_failed" for e in events)
                    # Kill any remaining tools before releasing the run's slot.
                    if self.cgroups:
                        self.cgroups.stop(run_id)
                    record.update(status="failed" if failed else "completed", ended_at=self.clock())
                    if failed:
                        why, said = self._why_failed(run_id, rc, events)
                        record["why_failed"] = why
                    elif any(e.get("kind") == "stage_completed" and e.get("stage") in TIMED_STAGES for e in events):
                        self._time_run(record, events)
                    self._persist(record)
                    self._keep_result(record, events)
                    del self.processes[run_id]
                    if failed:
                        self._log_failure(run_id, why)
                        self._check_login(run_id, why + "\n" + said)
            for run_id, record in list(self.records.items()):
                if record.get("ended_at", float("inf")) + RETENTION_SECONDS <= self.clock():
                    self._remove(run_id)
            going = len(self._running())
            for run_id in self.processes:
                self._tell_load(run_id, going)

    def _keep_result(self, record, events):
        """An ended run's result, as its page shows it, into result.json; then everything else
        in the run's directory is deleted, on a thread of its own: the upload, the sandbox, the
        working files, the typed answers."""
        run_id = record["run_id"]
        kept = {"progress": self._progress(record, events), "report": self._report(record, events),
                "kind": self._kind(record)}
        try:
            _write_json(self._path(run_id) / "result.json", kept)
        except OSError:
            return  # the run is being removed; nothing to keep
        record["result_kept"] = True
        self._persist(record)
        self._clean_working(run_id)

    def _clean_working(self, run_id):
        path = self._path(run_id)

        def clean():
            try:
                entries = [p for p in path.iterdir() if p.name not in KEPT]
            except OSError:
                return
            for entry in entries:
                try:
                    if entry.is_dir() and not entry.is_symlink():
                        for current, dirs, _ in os.walk(entry, followlinks=False):
                            os.chmod(current, 0o700, follow_symlinks=False)
                            for name in dirs:
                                target = Path(current) / name
                                if not target.is_symlink():
                                    os.chmod(target, 0o700, follow_symlinks=False)
                        shutil.rmtree(entry)
                    else:
                        entry.unlink()
                except OSError:
                    pass  # gone already, the run removed meanwhile

        thread = threading.Thread(target=clean, name="clean " + run_id, daemon=True)
        self.cleanups[run_id] = thread
        thread.start()

    def join_cleanups(self, timeout=60):
        for thread in list(self.cleanups.values()):
            thread.join(timeout)

    def link(self, token):
        """The run a private link opens, while it is going or its result is kept."""
        if isinstance(token, str) and 20 <= len(token) <= 64:
            with self.lock:
                self.sweep()
                for record in self.records.values():
                    mine = record.get("link_token")
                    if isinstance(mine, str) and hmac.compare_digest(mine.encode(), token.encode()) and record.get("owner"):
                        return {"owner": record["owner"], "run_id": record["run_id"]}
        raise WorkerError("not_found", "This link has expired or is not valid.")

    def _tell_load(self, run_id, going):
        """How many demos are going, written into a running demo's refdata/ (read-only inside
        its sandbox): impute_joint.py runs a third chromosome only while it is 1. Nothing is
        written before the harness has staged the sandbox."""
        refdata = self._path(run_id) / "harness" / "sandboxes" / run_id / "prs" / "refdata"
        if refdata.is_dir() and not refdata.is_symlink():
            try:
                _write_json(refdata / "demo_load.json", {"demo_runs_going": going, "written_utc": time.strftime(
                    "%Y-%m-%dT%H:%M:%SZ", time.gmtime(self.clock()))})
            except OSError:
                pass  # a run being torn down; the next sweep writes it again or not at all

    def _waited(self, run_id, events):
        """Seconds the run has waited for its person: from each question to its answer."""
        asked = {e.get("question_id"): _seconds(e.get("ts")) for e in events if e.get("kind") == "question"}
        waited = 0.0
        for answer in self._events(run_id, "answers.jsonl"):
            start, end = asked.get(answer.get("question_id")), _seconds(answer.get("ts"))
            if answer.get("kind") == "answer" and start is not None and end is not None and end > start:
                waited += end - start
        return waited

    def _time_run(self, record, events):
        wall = record["ended_at"] - record["started_at"]
        waited = min(wall, self._waited(record["run_id"], events))
        entry = {"run_id": record["run_id"], "ended_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(record["ended_at"])),
                 "kind": self._kind(record), "wall_seconds": round(wall), "waiting_for_person_seconds": round(waited),
                 "working_seconds": round(wall - waited)}
        try:
            fd = os.open(self.root / RUN_TIMES, os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW, 0o600)
            with os.fdopen(fd, "a") as fh:
                fh.write(json.dumps(entry) + "\n")
        except OSError:
            return  # an estimate a run short, never a run lost
        self.run_times.append((entry["kind"], entry["working_seconds"]))

    def _read_run_times(self):
        try:
            lines = (self.root / RUN_TIMES).read_text().splitlines()
        except OSError:
            return []
        times = []
        for line in lines:
            try:
                entry = json.loads(line)
                kind, seconds = entry.get("kind"), entry["working_seconds"]
            except (ValueError, KeyError, TypeError, AttributeError):
                continue
            if kind in KINDS and isinstance(seconds, (int, float)) and seconds > 0:
                times.append((kind, seconds))
        return times

    def _kind(self, record):
        """What kind of file the run reads: the door's judgment once it has made it, else the
        file's own format where that says; None for an exome or a kind not known."""
        run_id = record["run_id"]
        door = _json_artifact(self._path(run_id), "harness", "sandboxes", run_id, "prs", "runs", run_id,
                              "door", "classification.json")
        assay = door.get("assay_class") if isinstance(door, dict) else None
        if isinstance(assay, str) and assay.strip():
            assay = assay.lower()
            if re.search(r"exome|\bwes\b", assay):
                return None
            if re.search(r"\bwgs\b|whole[\s-]*genome|sequenc", assay):
                return "whole_genome"
            if re.search(r"array|chip|genotyp", assay):
                return "chip"
            return None
        return record.get("file_kind")

    def _estimates(self):
        """Each kind's estimate that something has measured."""
        return {kind: estimate for kind in KINDS if (estimate := self._estimate(kind))}

    def _estimate(self, kind):
        """How long an analysis of this kind of file takes, in whole minutes: the median, and the
        range of the runs it is from. None with nothing measured."""
        recorded = [seconds for k, seconds in self.run_times if k == kind]
        seconds = recorded[-RECENT_RUNS:] if len(recorded) >= ENOUGH_RUNS else self.measured.get(kind, []) + recorded
        if not seconds:
            return None
        return {"low_minutes": math.floor(min(seconds) / 60),
                "median_minutes": math.floor(statistics.median(seconds) / 60 + 0.5),
                "high_minutes": math.ceil(max(seconds) / 60), "runs": len(seconds)}

    def _events(self, run_id, filename="session.jsonl"):
        try:
            raw = _read_artifact(self._path(run_id), "live", run_id, filename)
        except (OSError, ValueError):
            return []
        events = []
        for line in raw.splitlines(keepends=True):
            if not line.endswith(b"\n"):
                break
            try:
                event = json.loads(line)
                if isinstance(event, dict):
                    events.append(event)
            except ValueError:
                continue
        return events

    def _append_answer(self, record, event):
        path = self._path(record["run_id"]) / "live" / record["run_id"]
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
        fd = os.open(path / "answers.jsonl", os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "a") as fh:
            event["ts"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(self.clock()))
            fh.write(json.dumps(event) + "\n")
            fh.flush()
            os.fsync(fh.fileno())

    def _owned(self, owner):
        owned = [r for r in self.records.values() if r["owner"] == owner]
        return max(owned, key=lambda r: r["started_at"]) if owned else None

    def _running(self):
        return [r for r in self.records.values() if r["status"] == "running"]

    def _retry_at(self, running):
        """When a slot should be free again: the earliest a running demo should end, from a
        run's usual length (a run already past it, ten minutes from now), never later than its
        time limit. None when neither is known."""
        now, ends = self.clock(), []
        estimates = self._estimates()
        for record in running:
            estimate = estimates.get(self._kind(record))
            usual = estimate["median_minutes"] * 60 if estimate else self.typical_run
            end = max(record["started_at"] + usual, now + 600) if usual else None
            if self.run_limit:
                limit = record["started_at"] + self.run_limit
                end = limit if end is None else min(end, limit)
            if end is not None:
                ends.append(max(end, now + 60))
        return math.ceil(min(ends)) if ends else None

    def _capacity(self, running):
        full = len(running) >= self.max_runs
        return {"full": True, "slots": self.max_runs, "retry_at": self._retry_at(running)} if full else {"full": False}

    def start(self, owner, upload, intake=None):
        """``upload`` is the uploaded file as received, open for reading; it is this run's to
        close. ``intake`` is the person's answers to the routine questions (demo_site/intake.py),
        kept beside the run in intake.json for its agent. The run takes its slot at once and its file is checked on a thread of its own,
        since a whole genome takes minutes and the lock must not wait on it. A file checked
        within QUICK_CHECK_SECONDS is answered here: refused, as an upload with its reason, and
        no run is left. One that takes longer is the run's first step, and if it is refused the
        run ends with the reason, for its person to read."""
        try:
            try:
                info = os.fstat(upload.fileno())
            except (OSError, AttributeError, ValueError) as exc:
                raise WorkerError("upload", "Choose a genome file to upload.") from exc
            if not stat.S_ISREG(info.st_mode) or not 0 < info.st_size <= MAX_UPLOAD_BYTES:
                raise WorkerError("upload", "The uploaded genome exceeds the size limit."
                                  if stat.S_ISREG(info.st_mode) and info.st_size else
                                  "Choose a genome file to upload.")
            with self.lock:
                self.sweep()
                mine = self._owned(owner)
                if mine and mine["status"] == "running":
                    raise WorkerError("busy", "End your current demo first.")
                if len(self._running()) >= self.max_runs:
                    raise WorkerError("full", "All demo slots are in use.")
                if self.sandbox_problem:
                    raise WorkerError("paused", PAUSED)
                if shutil.disk_usage(self.root).free < MIN_FREE_BYTES:
                    raise WorkerError("space", "The demo server is short of disk space. Please try again later.")
                previous = self._owned(owner)
                if previous:
                    self._remove(previous["run_id"])
                run_id = "demo-" + uuid.uuid4().hex
                self._path(run_id).mkdir(mode=0o700)
                record = {"demo": True, "run_id": run_id, "owner": owner,
                          "status": "running", "started_at": self.clock(), "checking": True,
                          "link_token": secrets.token_urlsafe(32)}
                self.records[run_id] = record
                ended = threading.Event()
                try:
                    self._persist(record)
                    if intake is not None:
                        _write_json(self._path(run_id) / "intake.json", intake)
                    staged = self._path(run_id) / "genome.part"
                    fd = os.open(staged, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
                    thread = threading.Thread(target=self._prepare, name="check " + run_id, daemon=True,
                                              args=(record, upload, os.fdopen(fd, "wb"), staged, ended))
                    self.checks[run_id] = (thread, ended)
                    thread.start()
                except BaseException:
                    self._remove(run_id)
                    raise
        except BaseException:
            upload.close()
            raise
        thread.join(QUICK_CHECK_SECONDS)
        with self.lock:
            if self.records.get(run_id) is record and record.get("upload_error"):
                self._remove(run_id)
                raise WorkerError("upload", record["upload_error"])
            return self.status(owner)

    def _prepare(self, record, upload, target, staged, ended):
        """The upload expanded into ``target``, the run's ``staged`` file, then the run launched;
        or the run refused. Both close."""
        run_id = record["run_id"]
        try:
            with upload, target:
                suffix = expand_upload(upload, _UntilEnded(target, ended))
        except UploadError as exc:
            return self._reject(record, str(exc), staged)
        except Exception:
            # The run was ended while its file was checked, or the disk failed under it.
            return self._reject(record, "The file could not be read. Please try again.", staged)
        with self.lock:
            self.checks.pop(run_id, None)
            if self.records.get(run_id) is not record or record["status"] != "running":
                return
            genome = self._path(run_id) / ("genome" + suffix)
            record["file_kind"] = FILE_KINDS.get(suffix)
            try:
                os.rename(staged, genome)
                record.pop("checking")
                self._persist(record)
                self.processes[run_id] = self.launcher(record, genome)
            except Exception:
                record.pop("checking", None)
                record.update(status="failed", ended_at=self.clock(), why_failed="the demo could not be started")
                self._persist(record)
                self._log_failure(run_id, record["why_failed"])

    def _reject(self, record, why, staged):
        """A file that is not a genome the demo reads ends its run with the reason. That is the
        person's file, not a failure of the demo, so the failure log has no line for it."""
        with self.lock:
            self.checks.pop(record["run_id"], None)
            try:
                staged.unlink()
            except OSError:
                pass
            if self.records.get(record["run_id"]) is not record or record["status"] != "running":
                return
            record.pop("checking", None)
            record.update(status="failed", ended_at=self.clock(), upload_error=why,
                          why_failed="upload not accepted: " + why)
            self._persist(record)

    def _settled(self, judgments, stage, model):
        """The judgment a stage settled on. A stage can record several -- a model refused at score
        before the next one scored, a first model choice and the final one, door's classification
        and its normalization -- and the manifest lists them in file-name order, which is not the
        order they were made. For select and score, those for the model the result names; for
        door, its classification; and of those, the one recorded last: the harness gives each its
        place in the run's ledger (recorded_seq), and one the ledger never recorded is never a
        stage's last word over one it did. File times are not that order."""
        mine = [j for j in judgments if j.get("stage") == stage]
        if model and stage in ("select", "score"):
            key = "chosen_model" if stage == "select" else "model"
            mine = [j for j in mine if j.get(key) == model] or mine
        if stage == "door":
            mine = [j for j in mine if "assay_class" in j] or mine
        if not mine:
            return None

        def recorded(item):
            number = item[1].get("recorded_seq")
            number = number if isinstance(number, int) and not isinstance(number, bool) else None
            return (number is not None, number or 0, item[0])
        return max(enumerate(mine), key=recorded)[1]

    def _report(self, record, events):
        run_id = record["run_id"]
        root = self._path(run_id)
        manifest = _json_artifact(root, "harness", "vault", "runs", run_id, "manifest.json") or {}
        aliases = {"door": "classification", "ancestry": "ancestry", "select": "model_choice",
                   "score": "score", "interpret": "interpret", "report": "report"}
        result_record = manifest.get("result") or {}
        result = result_record.get("result") if result_record.get("valid") else None
        model = (result or {}).get("chosen_model")
        judgments = [j for j in manifest.get("stage_judgments", []) if isinstance(j, dict)]
        bundle = {}
        for stage, alias in aliases.items():
            judgment = self._settled(judgments, stage, model)
            if judgment:
                bundle[alias] = judgment
        base = ("harness", "sandboxes", run_id, "prs", "runs", run_id)
        reference = _json_artifact(root, *base, "interpret", "reference_response.json")
        if reference:
            bundle["reference_response"] = reference
        # The percentile range as rank_range.py computed it from the counts, which the page
        # checks the interpretation and the delivered report against.
        ranged = _json_artifact(root, *base, "interpret", "percentile_range.json")
        if ranged:
            bundle["percentile_range"] = ranged
        if isinstance(model, str) and re.fullmatch(r"PGS[0-9]+", model):
            coverage = _json_artifact(root, *base, "score", "coverage_" + model + ".json")
            if coverage:
                bundle["coverage"] = coverage
        refusal = next((event for event in reversed(events) if event.get("kind") == "refusal"), None)
        return {"bundle": bundle, "result": result, "refusal": refusal}

    def status(self, owner):
        with self.lock:
            self.sweep()
            record = self._owned(owner)
            capacity = self._capacity(self._running())
            estimates = self._estimates()
            told = {"estimates": estimates} if estimates else {}
            if self.sandbox_problem:
                told["paused"] = PAUSED
            if record is None:
                return {"status": "busy" if capacity["full"] else "idle", "busy": capacity["full"],
                        "owned": False, **capacity, **told}
            run_id = record["run_id"]
            link = {"link": "/r/" + record["link_token"]} if record.get("link_token") else {}
            if record.get("result_kept") and record["status"] in ("completed", "failed"):
                kept = _json_artifact(self._path(run_id), "result.json") or {}
                state = {"status": record["status"], "busy": capacity["full"], "owned": True, "run_id": run_id,
                         "progress": kept.get("progress") or [], **capacity, **told, **link,
                         "kind": kept.get("kind"), "report": kept.get("report") or {},
                         "purge_at": record["ended_at"] + RETENTION_SECONDS}
                if estimates.get(state["kind"]):
                    state["estimate"] = estimates[state["kind"]]
                if record["status"] == "failed":
                    state["error"] = self._error(record)
                return state
            events = self._events(run_id)
            answered = {e.get("question_id") for e in self._events(run_id, "answers.jsonl")}
            pending = next((e for e in reversed(events)
                            if e.get("kind") == "question" and e.get("question_id") not in answered), None)
            state = {"status": record["status"], "busy": record["status"] == "running" or capacity["full"],
                     "owned": True, "run_id": run_id, "progress": [], **capacity, **told, **link}
            state["kind"] = self._kind(record)
            if estimates.get(state["kind"]):
                state["estimate"] = estimates[state["kind"]]
            if record["status"] == "running":
                state["waited_seconds"] = round(self._waited(run_id, events))
            state["progress"] = self._progress(record, events)
            if pending and record["status"] == "running":
                state["status"] = "waiting"
                state["question"] = {"id": pending["question_id"], "text": pending.get("question", ""),
                                     **{k: pending.get(k) for k in ("stage", "why_it_matters", "hard_blocker", "options", "other")}}
            if record["status"] in ("completed", "failed"):
                state["report"] = self._report(record, events)
                state["purge_at"] = record["ended_at"] + RETENTION_SECONDS
            if record["status"] == "failed":
                state["error"] = self._error(record)
            return state

    def _progress(self, record, events):
        """The run's progress as its page shows it."""
        progress = []
        for event in events:
            kind = event.get("kind")
            if kind in ("stage_started", "stage_completed", "stage_progress", "progress", "run_started", "run_completed"):
                message = next((event[key].strip()
                                for key in ("message", "text", "decision", "note", "tool")
                                if isinstance(event.get(key), str) and event[key].strip()), None)
                message = _plain(message) if message else None
                if not message:
                    stage = event.get("stage")
                    stage = stage.strip() if isinstance(stage, str) and stage.strip() else "Demo"
                    message = stage + " " + kind.replace("stage_", "").replace("run_", "")
                progress.append({"text": message, "stage": event.get("stage"), "kind": kind,
                                 "ts": event.get("ts"), "outcome": event.get("outcome")})
        progress = progress[-100:]
        if record.get("checking"):
            progress.append({"text": "Checking your file before the agent starts.", "stage": None, "kind": "progress",
                             "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(record["started_at"])),
                             "outcome": None})
        return progress

    @staticmethod
    def _error(record):
        if record.get("upload_error"):
            return record["upload_error"] + " Clear this run to upload another file."
        if record.get("time_limit"):
            return "This analysis reached its time limit and was stopped. Clear this run to start a new one."
        if str(record.get("why_failed") or "").startswith(NOT_LAUNCHED):
            return ("The analysis could not start because of a problem on the demo's side, not with your file. "
                    "Clear this run and try again later.")
        return "The analysis stopped before it could finish. Clear this run and try again."

    def answer(self, owner, question_id, text):
        with self.lock:
            state = self.status(owner)
            if not isinstance(text, str) or not text.strip() or len(text) > 8000:
                raise WorkerError("invalid", "Answer must contain 1–8000 characters.")
            if state.get("status") != "waiting" or state["question"]["id"] != question_id:
                raise WorkerError("conflict", "That question is no longer waiting for an answer.")
            choices = state["question"].get("options")
            if choices and not state["question"].get("other") and text.strip() not in choices:
                raise WorkerError("invalid", "Choose one of the answers offered.")
            self._append_answer(self._owned(owner), {"kind": "answer", "question_id": question_id,
                                                   "text": text.strip(), "skipped": False})
            return self.status(owner)

    def end(self, owner):
        with self.lock:
            self.sweep()
            record = self._owned(owner)
            if record:
                # A run still going is told its person ended it; an ended one is only deleted.
                if record["status"] == "running":
                    self._append_answer(record, {"kind": "session_end", "outcome": "session-ended-by-person",
                                                 "at_gate": "demo", "note": "The person ended this demo."})
                self._remove(record["run_id"])
            return self.status(owner)

    def close(self):
        # Terminal evidence retains its timer; unfinished evidence is abandoned and removed.
        with self.lock:
            for run_id in list(self.processes) + list(self.checks):
                self._remove(run_id)
            self.lock_file.close()


class BoundedThreads:
    """For a threading server: a connection beyond the server's `slots` (a BoundedSemaphore it
    sets) is closed at once instead of getting a thread, and each thread hands its slot back."""

    def process_request(self, request, client_address):
        if not self.slots.acquire(blocking=False):
            request.close()
            return
        try:
            super().process_request(request, client_address)
        except BaseException:
            self.slots.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self.slots.release()


class WorkerServer(BoundedThreads, socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True
    block_on_close = False

    def __init__(self, socket_path, runs, web_uid, password_service):
        self.runs = runs
        self.web_uid = web_uid
        self.password_service = password_service
        self.slots = threading.BoundedSemaphore(8)
        path = Path(socket_path)
        # Traversable by the web's account where that is another one; private where it is this one.
        mode = 0o700 if web_uid == os.getuid() else 0o755
        path.parent.mkdir(mode=mode, parents=True, exist_ok=True)
        os.chmod(path.parent, mode)
        if path.exists():
            path.unlink()
        super().__init__(str(path), WorkerHandler)
        os.chown(path, web_uid, -1)
        os.chmod(path, 0o600)

    def dispatch(self, payload, upload=None):
        """``upload`` is the open file a "start" request carried; dispatch closes it or hands it
        on to the run."""
        op = payload.get("op")
        allowed = {"start": {"owner", "intake"}, "status": {"owner"},
                   "answer": {"owner", "question_id", "text"}, "end": {"owner"},
                   "login": {"password", "client"}, "link": {"token"}}
        if op not in allowed or set(payload) != allowed[op] | {"op"} or (op == "start") != (upload is not None):
            if upload is not None:
                upload.close()
            raise WorkerError("invalid", "Unsupported worker request.")
        if op == "login":
            if (not isinstance(payload["password"], str) or len(payload["password"]) > 256
                    or not isinstance(payload["client"], str) or len(payload["client"]) > 256):
                raise WorkerError("invalid", "Invalid login request.")
            return self.password_service.login(payload["password"], payload["client"])
        if op == "link":
            if not isinstance(payload["token"], str) or len(payload["token"]) > 64:
                raise WorkerError("invalid", "Invalid link.")
            return self.runs.link(payload["token"])
        if not isinstance(payload["owner"], str) or not OWNER_RE.fullmatch(payload["owner"]):
            if upload is not None:
                upload.close()
            raise WorkerError("invalid", "Invalid session owner.")
        if op == "start":
            try:
                answers = form.check(payload["intake"])
            except form.IntakeError as exc:
                upload.close()
                raise WorkerError("invalid", str(exc)) from exc
            return self.runs.start(payload["owner"], upload, intake=answers)
        return getattr(self.runs, op)(**{key: value for key, value in payload.items() if key != "op"})


class WorkerHandler(socketserver.BaseRequestHandler):
    def handle(self):
        self.request.settimeout(30)
        try:
            _, uid, _ = struct.unpack("3i", self.request.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))
            if uid != self.server.web_uid:
                raise WorkerError("forbidden", "Worker peer is not authorized.")
            payload = _receive(self.request)
            # A "start" carries its upload as an open file right after the request.
            upload = _receive_file(self.request) if payload.get("op") == "start" else None
            data = self.server.dispatch(payload, upload)
            response = {"ok": True, "data": data}
        except WorkerError as exc:
            response = {"ok": False, "code": exc.code, "error": str(exc)}
        except Exception:
            response = {"ok": False, "code": "unavailable", "error": "The demo worker could not complete the request."}
        try:
            _send(self.request, response)
        except (OSError, WorkerError):
            pass


def _run_child(argv):
    run_root, run_id, upload, group = argv
    if group != "-":
        (Path(group) / "cgroup.procs").write_text(str(os.getpid()))
    env = dict(os.environ, PRS_DEMO_ROOT=str(Path(run_root) / "harness"))
    command = [sys.executable, str(REPO / "harness" / "run_harness.py"), "run", "--demo",
               "--backend", "human", "--patient", upload, "--run-id", run_id,
               "--runs-root", str(Path(run_root) / "live"), "--live-run-id", run_id]
    # The person's answers to the routine questions: the harness gets where they are, never
    # what they say.
    answers = Path(run_root) / "intake.json"
    if answers.is_file():
        command += ["--intake", str(answers)]
    os.execve(sys.executable, command, env)


def _tend(runs, auth):
    """One round of the worker's 15-second upkeep: purge what is due, look whether agents can be
    sandboxed, and look for a fixed password (demo_site/auth.py). Each failing leaves the others
    done, and the next round tries again."""
    try:
        runs.sweep()
    except Exception:
        # A transient cleanup error must not disable every future purge.
        print("Demo cleanup failed; retained files will be retried.", file=sys.stderr)
    try:
        runs.check_sandbox()
    except Exception:
        print("Whether agents can be sandboxed could not be checked; the next round tries again.",
              file=sys.stderr)
    try:
        auth.notice()
    except Exception:
        print("The demo's password file could not be read; the next round tries again.", file=sys.stderr)


def _beside(args, name):
    """The failure log and the login flag default to the state directory's parent, which on the
    VM is root's /var/lib/prs-demo and without root the demo's own state directory."""
    given = getattr(args, name, None)
    default = {"failure_log": "failures.log", "login_flag": "claude-login-expired"}[name]
    return given or str(Path(args.state_dir).resolve().parent / default)


def _groups(args):
    """A delegated cgroup where root runs the worker; ProcessGroups for an account without root."""
    if args.cgroup_root:
        return Cgroups(args.cgroup_root)
    if os.geteuid() != 0:
        return ProcessGroups(Path(args.state_dir) / "runs")
    entry = next(line for line in Path("/proc/self/cgroup").read_text().splitlines() if line.startswith("0::"))
    return Cgroups("/sys/fs/cgroup" + entry[3:])


def end_interrupted(argv):
    """Stop and end, as environment failures, the runs a dead worker left unfinished.

    What the next worker does at start anyway; this is for when no worker is going to start
    (the watchdog has stopped restarting it). It takes the worker's state lock, so it refuses
    while a worker is running, and it never deletes a run."""
    parser = argparse.ArgumentParser(prog="python3 -m demo_site.worker end-interrupted")
    parser.add_argument("--state-dir", default="/var/lib/prs-demo/private")
    parser.add_argument("--cgroup-root")
    parser.add_argument("--failure-log", help="default: failures.log beside the state directory")
    args = parser.parse_args(argv)
    os.umask(0o077)
    runs = Runs(args.state_dir, cgroups=_groups(args), failure_log=_beside(args, "failure_log"))
    try:
        for record in runs.interrupted:
            print("ended interrupted demo run %s" % record["run_id"])
    finally:
        runs.close()


def main():
    if len(sys.argv) > 1 and sys.argv[1] == "_launch":
        _run_child(sys.argv[2:])
        return
    if len(sys.argv) > 1 and sys.argv[1] == "end-interrupted":
        end_interrupted(sys.argv[2:])
        return
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state-dir", default="/var/lib/prs-demo/private")
    parser.add_argument("--socket", default="/run/prs-demo/worker.sock")
    parser.add_argument("--web-uid", type=int,
                        help="the web's uid: required as root; without root, this account's own")
    parser.add_argument("--secret-file", default="/var/lib/prs-demo/auth/master.key")
    parser.add_argument("--cgroup-root")
    parser.add_argument("--failure-log", help="default: failures.log beside the state directory")
    parser.add_argument("--login-flag",
                        help="written when a run could not authenticate; default: "
                             "claude-login-expired beside the state directory")
    parser.add_argument("--max-runs", type=int, default=1, help="demos that may run at once")
    parser.add_argument("--run-limit-minutes", type=float,
                        help="stop a demo that has run this long (default: no limit)")
    parser.add_argument("--typical-run-minutes", type=float,
                        help="a demo's usual length, to tell people waiting for a slot when to "
                             "come back")
    args = parser.parse_args()
    if os.geteuid() == 0:
        if args.web_uid in (None, 0):
            parser.error("as root, configure an unprivileged --web-uid")
    elif args.web_uid not in (None, os.getuid()):
        parser.error("without root the web must run as this same account")
    else:
        args.web_uid = os.getuid()
    os.umask(0o077)
    from .auth import PasswordService
    minutes = lambda value: None if value is None else value * 60
    runs = Runs(args.state_dir, cgroups=_groups(args), failure_log=_beside(args, "failure_log"),
                login_flag=_beside(args, "login_flag"), max_runs=args.max_runs,
                run_limit=minutes(args.run_limit_minutes), typical_run=minutes(args.typical_run_minutes),
                measured=measured_run_times(), sandbox_check=sandbox_problem)
    auth = PasswordService(args.secret_file, Path(args.state_dir) / "auth.sqlite3")
    server = WorkerServer(args.socket, runs, args.web_uid, auth)
    stopped = threading.Event()

    def sweep_loop():
        while not stopped.wait(15):
            _tend(runs, auth)

    threading.Thread(target=sweep_loop, daemon=True).start()
    try:
        server.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        pass
    finally:
        stopped.set()
        server.server_close()
        runs.close()
        Path(args.socket).unlink(missing_ok=True)


if __name__ == "__main__":
    main()
