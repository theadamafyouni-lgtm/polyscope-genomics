"""Host-side client for the six-command gb10 gate. Never staged into a sandbox.

One gate run id belongs to one chromosome. The compute lock covers only `run`:
`get` for the previous chromosome can hold its own run lock at the same time.
Archives are copied as bytes; tar paths, permissions and owners are never restored.
"""
import gzip
import hashlib
import os
import re
import secrets
import shutil
import stat
import subprocess
import tarfile
import tempfile
import threading
import time
import zlib
from pathlib import Path

THREADS = 6
MAX_UPLOAD = 1 << 30
MAX_RESULT = 16 << 30
RUN_RE = re.compile(r"run-[0-9]{8}-[0-9]{6}-[0-9a-f]{6}")
# The gate's own limit on one Beagle job (gate.sh): JAVA_TIMEOUT 3 h, then 60 s to kill it.
GATE_TIMEOUT = 10800 + 60
CONNECT_TIMEOUT = 30
NOT_FINISHED, BUSY, SSH_FAILED = 5, 6, 255
# ssh's own words when it never reached the gate, so the request did not run there.
NEVER_CONNECTED = ("ssh: connect to host", "ssh: Could not resolve hostname",
                   "kex_exchange_identification", "Connection timed out during banner exchange",
                   "Permission denied (publickey", "Host key verification failed")


class GateError(RuntimeError):
    def __init__(self, step, detail, returncode=None):
        super().__init__(f"{step}: {detail}")
        self.step = step
        self.returncode = returncode


class GateCancelled(GateError):
    """Stopped because the harness is closing: the agent's run ended first."""


def validate_request(chrom, seed, threads=THREADS):
    if str(chrom) not in {str(c) for c in range(1, 23)}:
        raise ValueError("chromosome must be 1..22")
    if not re.fullmatch(r"[0-9]{1,10}", str(seed)):
        raise ValueError("seed must be 1..10 decimal digits")
    if str(threads) != str(THREADS):
        raise ValueError("gb10 requires threads=6; thread count changes Beagle results")


def check_target(path, chrom, cancel=None):
    """Refuse a genome-wide target even if a caller bypasses the indexed merge."""
    rows = expanded = 0
    try:
        with gzip.open(path, "rb") as source:
            for line in iter(lambda: source.readline(16 << 20), b""):
                expanded += len(line)
                if len(line) == 16 << 20 or expanded > MAX_RESULT:
                    raise GateError("target", "expanded target exceeds validation limits")
                if cancel is not None and cancel.is_set():
                    raise GateCancelled("target", "cancelled")
                if line.startswith(b"#"):
                    continue
                if line.split(b"\t", 1)[0] != str(chrom).encode():
                    raise GateError("target", "upload contains another chromosome")
                rows += 1
    except (OSError, EOFError) as exc:
        raise GateError("target", "unreadable compressed VCF") from exc
    if not rows:
        raise GateError("target", "upload contains no variants")


def check_identity(path):
    """The ssh key is a regular file only this account can read. Checked by stat, never opened."""
    try:
        info = os.stat(path)
    except OSError as exc:
        raise GateError("key", "the gb10 ssh key cannot be found or read (%s)"
                        % type(exc).__name__) from exc
    if not stat.S_ISREG(info.st_mode):
        raise GateError("key", "the gb10 ssh key is not a regular file")
    if info.st_mode & 0o077:
        raise GateError("key", "the gb10 ssh key is open to other accounts (mode %03o), and ssh "
                        "refuses such a key" % (info.st_mode & 0o777))


def file_digest(path):
    h = hashlib.sha256()
    with open(path, "rb") as source:
        for chunk in iter(lambda: source.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def check_gzip(path):
    """The whole file decompresses to its end, every member's CRC and length checked, so a
    truncated or corrupted download is caught here and never delivered."""
    try:
        with gzip.open(path, "rb") as source:
            while source.read(1 << 24):
                pass
    except (OSError, EOFError, zlib.error) as exc:
        raise GateError("get", "the downloaded VCF is not intact gzip (%s: %s)"
                        % (type(exc).__name__, exc)) from exc


def copy_archive(archive, chrom, destination):
    """Read exactly two regular members, with bounded sizes and fixed local names.

    No extract/extractall call: even an archive made by root with uid/gid 1000,
    mode 000 or a hostile member name cannot apply metadata to the local files.
    Stage both members privately and publish only after validating the whole tar.
    """
    expected = {f"chr{chrom}.vcf.gz": MAX_RESULT, f"chr{chrom}.log": 16 << 20}
    destination = Path(destination)
    destination.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".download-", dir=destination) as scratch:
        seen = set()
        try:
            with tarfile.open(fileobj=archive, mode="r:") as bundle:
                for member in bundle:
                    if (member.name not in expected or member.name in seen
                            or not member.isreg() or member.issparse()
                            or not 0 < member.size <= expected[member.name]):
                        raise GateError("get", "unexpected, duplicate or unsafe tar member")
                    seen.add(member.name)
                    with bundle.extractfile(member) as source, open(
                            Path(scratch) / member.name, "xb") as dest:
                        shutil.copyfileobj(source, dest, 1 << 20)
                    if (Path(scratch) / member.name).stat().st_size != member.size:
                        raise GateError("get", "truncated tar member")
            if seen != expected.keys():
                raise GateError("get", "tar is missing an expected member")
            log = (Path(scratch) / f"chr{chrom}.log").read_text()
            if not log.rstrip().endswith(" finished"):
                raise GateError("get", "Beagle log does not end with finished")
            check_gzip(Path(scratch) / f"chr{chrom}.vcf.gz")
            for name in expected:
                os.replace(Path(scratch) / name, destination / name)
        except (tarfile.TarError, UnicodeError, EOFError) as exc:
            raise GateError("get", "invalid or truncated tar") from exc
    return {name: {"bytes": (destination / name).stat().st_size,
                   "sha256": file_digest(destination / name)} for name in expected}


class GateClient:
    """The gate's six requests, with the waiting and retrying the design set out.

    ssh failing (exit 255) is retried three times, after 30 s, 2 min and 5 min; the gate busy
    (exit 6: its one Beagle slot, or this id's lock) is retried every `poll`, 60 s. An upload
    gives up after 30 min, a `run` waits up to 3 h 15 min for the slot -- another job holds it
    for at most the gate's 3 h -- and a running `run` gets 3 h 10 min. A `run` whose connection
    drops is never sent again: Beagle may still be running there, so `get` is polled every 60 s
    until it answers -- 0 collected, 5 Beagle did not finish, 6 still running -- up to the
    gate's own timeout. `clean` is retried until that timeout has passed since the id's last
    `run`, since a Beagle still running there holds the id's lock until the gate stops it.
    Nothing else is retried: 2, 3, 4, 7, a 5 after a successful `run`, or Beagle's own exit.
    """

    def __init__(self, host, identity, *, command=None, poll=60, ssh_retries=(30, 120, 300),
                 upload_window=1800, download_window=1800, run_busy_window=11700,
                 run_timeout=11400, gate_timeout=GATE_TIMEOUT, margin=600, attempt_cap=300,
                 cancel=None):
        # Configuration belongs to the harness operator, never to a sandbox message. The host
        # and key come from the private config (config.load_gb10_config); there is no default.
        self.command = list(command) if command is not None else [
            "ssh", "-T", "-i", os.path.abspath(identity),
            "-o", "IdentitiesOnly=yes", "-o", "BatchMode=yes",
            "-o", "StrictHostKeyChecking=yes", "-o", "ConnectTimeout=%d" % CONNECT_TIMEOUT,
            "-o", "ServerAliveInterval=60", "-o", "ServerAliveCountMax=3", host]
        # ssh's and Beagle's stderr can name the address and the account (a connect error, a
        # path under its home). It reaches the manifest and the agent's reply, so neither
        # carries them.
        self._private = sorted({host, *host.split("@", 1)} - {""}, key=len, reverse=True)
        self.poll = poll
        self.ssh_retries = tuple(ssh_retries)
        self.upload_window = upload_window
        self.download_window = download_window
        self.run_busy_window = run_busy_window
        self.run_timeout = run_timeout
        self.gate_timeout = gate_timeout
        self.margin = margin
        self.attempt_cap = attempt_cap
        self.cancel = cancel or threading.Event()
        self._compute = threading.Lock()
        self._upload = threading.Lock()
        self._download = threading.Lock()
        self._lock = threading.Lock()
        # run id -> when its last `run` was sent, while Beagle may still be running there
        self._maybe_running = {}
        self.jobs = {}

    def _attempt(self, words, timeout, *, source=None, output=None, cancellable=True):
        """One ssh invocation. Returns (exit code, stderr tail); None for a call this side
        stopped at its timeout. Raises GateCancelled if cancelled while it runs."""
        step = words[0]
        if source:
            source.seek(0)
        if output:
            output.seek(0)
            output.truncate()
        # Spool diagnostics too: subprocess pipes must never block a large transfer.
        with tempfile.TemporaryFile() as errors:
            proc = subprocess.Popen(self.command + [" ".join(words)],
                                    stdin=source or subprocess.DEVNULL,
                                    stdout=output or subprocess.DEVNULL, stderr=errors)
            deadline = time.monotonic() + timeout
            timed_out = False
            try:
                while proc.poll() is None:
                    if output and os.fstat(output.fileno()).st_size > MAX_RESULT + (32 << 20):
                        raise GateError(step, "archive exceeds result size limit")
                    if cancellable and self.cancel.is_set():
                        raise GateCancelled(step, "cancelled")
                    if time.monotonic() >= deadline:
                        timed_out = True
                        break
                    try:
                        proc.wait(timeout=0.1)
                    except subprocess.TimeoutExpired:
                        pass
            finally:
                if proc.poll() is None:
                    proc.kill()
                    proc.wait()
            errors.seek(0, os.SEEK_END)
            errors.seek(max(0, errors.tell() - 2000))
            detail = self.redact(errors.read().decode(errors="replace")).strip()
        return (None if timed_out else proc.returncode), detail

    def _wait(self, step, seconds, cleaning=False):
        """Sleep between attempts. Only cleanup waits out a cancellation."""
        if cleaning:
            time.sleep(max(0, seconds))
        elif self.cancel.wait(max(0, seconds)):
            raise GateCancelled(step, "cancelled")

    def _beagle_ended(self, rid):
        with self._lock:
            self._maybe_running.pop(rid, None)

    def _request(self, words, window, *, source=None, output=None, cleaning=False,
                 attempt_cap=None):
        """Any request but `run`, until it succeeds or `window` seconds have passed."""
        step = words[0]
        started = time.monotonic()
        deadline = started + window
        measure = {"attempts": 0, "busy": 0, "ssh_failures": 0}
        while True:
            if self.cancel.is_set() and not cleaning:
                raise GateCancelled(step, "cancelled")
            measure["attempts"] += 1
            left = max(1e-3, deadline - time.monotonic())
            capped = attempt_cap is not None and attempt_cap < left
            rc, detail = self._attempt(words, attempt_cap if capped else left, source=source,
                                       output=output, cancellable=not cleaning)
            if rc == 0:
                measure["seconds"] = round(time.monotonic() - started, 6)
                return measure
            last = detail or ("no answer" if rc is None else "gate exit %s" % rc)
            if rc == BUSY:
                measure["busy"] += 1
                pause = self.poll
            elif rc == SSH_FAILED or (rc is None and capped):
                # A capped attempt that hung is a connection that failed, like 255.
                measure["ssh_failures"] += 1
                n = measure["ssh_failures"]
                if n <= len(self.ssh_retries):
                    pause = self.ssh_retries[n - 1]
                elif cleaning:
                    pause = self.poll
                else:
                    raise GateError(step, "ssh failed %d times: %s" % (n, last), rc)
            elif rc is None:
                pause = None  # the window ended during this attempt
            else:
                raise GateError(step, last, rc)
            if pause is None or time.monotonic() + pause >= deadline:
                raise GateError(step, "timed out: gave up after %g s (%d busy, %d ssh "
                                "failures); last: %s" % (window, measure["busy"],
                                                         measure["ssh_failures"], last), rc)
            self._wait(step, pause, cleaning)

    def _run(self, job, chrom, seed, archive):
        """`run`, waiting for the box's one Beagle slot, and recovering a dropped connection."""
        rid = job["run_id"]
        words = ["run", rid, str(chrom), str(THREADS), str(seed)]
        started = time.monotonic()
        measure = {"attempts": 0, "busy": 0, "ssh_failures": 0}
        while True:
            # Never start Beagle once the harness is closing; a run already going is waited out.
            if self.cancel.is_set():
                raise GateCancelled("run", "cancelled")
            measure["attempts"] += 1
            sent = time.monotonic()
            with self._lock:
                self._maybe_running[rid] = sent
            rc, detail = self._attempt(words, self.run_timeout, cancellable=False)
            never_connected = rc == SSH_FAILED and any(m in detail for m in NEVER_CONNECTED)
            if rc not in (SSH_FAILED, None) or never_connected:
                self._beagle_ended(rid)  # the gate's `run` answered, or never started
            if rc == 0:
                measure["seconds"] = round(time.monotonic() - started, 6)
                return measure
            if rc == BUSY:
                measure["busy"] += 1
                if time.monotonic() + self.poll >= started + self.run_busy_window:
                    raise GateError("run", "the gb10's Beagle slot stayed busy for %g s: %s"
                                    % (self.run_busy_window, detail), rc)
                self._wait("run", self.poll)
                continue
            if never_connected:
                # ssh never reached the gate, so nothing ran there: sending it again is safe.
                measure["ssh_failures"] += 1
                if measure["ssh_failures"] > len(self.ssh_retries):
                    raise GateError("run", "ssh failed %d times: %s"
                                    % (measure["ssh_failures"], detail), rc)
                self._wait("run", self.ssh_retries[measure["ssh_failures"] - 1])
                continue
            if rc in (SSH_FAILED, None):
                measure["dropped"] = detail or "no answer within %g s" % self.run_timeout
                measure["recovery_polls"] = self._recover(rid, chrom, sent, archive)
                measure["recovered"] = True
                measure["seconds"] = round(time.monotonic() - started, 6)
                return measure
            raise GateError("run", detail or "Beagle exited %s" % rc, rc)

    def _recover(self, rid, chrom, sent, archive):
        """The `run` connection dropped and Beagle may still be running: never run it again.
        Poll `get` until the gate's own timeout has passed since the `run` was sent."""
        deadline = sent + self.gate_timeout + self.margin
        polls = 0
        while True:
            if self.cancel.is_set():
                raise GateCancelled("run", "cancelled while recovering a dropped run")
            polls += 1
            left = max(1e-3, deadline - time.monotonic())
            rc, detail = self._attempt(["get", rid, str(chrom)],
                                       min(left, self.download_window), output=archive)
            if rc in (0, NOT_FINISHED):
                self._beagle_ended(rid)  # get held the id's lock: Beagle is not running
            if rc == 0:
                return polls
            if rc == NOT_FINISHED:
                raise GateError("run", "the connection dropped and Beagle did not finish: %s"
                                % detail, rc)
            if rc not in (BUSY, SSH_FAILED, None):
                raise GateError("run", "the connection dropped; get then answered %s: %s"
                                % (rc, detail), rc)
            if time.monotonic() + self.poll >= deadline:
                raise GateError("run", "the connection dropped and the run had not finished "
                                "when the gate's own timeout passed", rc)
            self._wait("run", self.poll)

    def worst_case_seconds(self):
        """The longest one chromosome's request can take from submit to reply, queued behind
        the other one in flight at every lock: two uploads, the run -- the wait for the box's
        slot, ssh retries, then running, or recovering, until the gate's own timeout -- the
        download, and the clean. impute_remote waits longer than this for its reply."""
        uploads = 2 * self.upload_window
        run = (self.run_busy_window + sum(self.ssh_retries)
               + (len(self.ssh_retries) + 1) * CONNECT_TIMEOUT
               + max(self.run_timeout, self.gate_timeout + self.margin))
        clean = max(self.margin, self.gate_timeout + self.margin) + self.attempt_cap
        return 2 * (uploads + run + self.download_window) + clean

    def ping(self):
        """The gate answers `ok <its hostname>`. Only that it did is kept, not the name."""
        with tempfile.TemporaryFile() as answer:
            window = sum(self.ssh_retries) + (len(self.ssh_retries) + 1) * self.attempt_cap
            measure = self._request(["ping"], window, output=answer,
                                    attempt_cap=self.attempt_cap)
            answer.seek(0)
            if not answer.read(64).startswith(b"ok "):
                raise GateError("ping", "the gate did not answer ok")
        return measure

    def redact(self, text):
        for private in self._private:
            text = text.replace(private, "<gb10>")
        return text

    def _new_job(self, chrom, seed):
        with self._lock:
            while True:
                rid = time.strftime("run-%Y%m%d-%H%M%S-", time.gmtime()) + secrets.token_hex(3)
                if rid not in self.jobs:
                    break
            job = {"run_id": rid, "chromosome": str(chrom), "threads": THREADS,
                   "seed": str(seed), "steps": {}, "cleaned": False}
            # Record before any upload, including failed/partial uploads.
            self.jobs[rid] = job
            return job

    def clean(self, job):
        """Until it succeeds, for at least `margin`, and while Beagle may still be running there
        -- a `run` whose end was never seen -- until the gate's own timeout has passed since it
        was sent: that Beagle holds the id's lock, and clean answers busy until it is stopped."""
        rid = job["run_id"]
        with self._lock:
            sent = self._maybe_running.get(rid)
        now = time.monotonic()
        window = max(self.margin, (sent + self.gate_timeout + self.margin - now) if sent else 0)
        try:
            job["steps"]["clean"] = self._request(["clean", rid], window, cleaning=True,
                                                  attempt_cap=self.attempt_cap)
            job["cleaned"] = True
            job.pop("cleanup_error", None)
        except (GateError, OSError) as exc:
            job["cleaned"] = False
            job["cleanup_error"] = str(exc)
            raise

    def cleanup_all(self):
        """Retry every outstanding id even if an earlier cleanup fails."""
        failures = {}
        with self._lock:
            jobs = list(self.jobs.values())
        for job in jobs:
            if not job["cleaned"]:
                try:
                    self.clean(job)
                except (GateError, OSError) as exc:
                    failures[job["run_id"]] = str(exc)
        return failures

    def impute(self, chrom, target, holdout, seed, destination, threads=THREADS):
        validate_request(chrom, seed, threads)
        for path in (target, holdout):
            if not 0 < os.path.getsize(path) < MAX_UPLOAD:
                raise GateError("upload", "input must be nonempty and below the gate's 1 GiB cap")
        check_target(target, chrom, self.cancel)
        job = self._new_job(chrom, seed)
        rid = job["run_id"]
        error = None
        try:
            with self._upload:
                for verb, path, suffix in (("put-holdout", holdout, []),
                                           ("put-gt", target, [str(chrom)])):
                    with open(path, "rb") as source:
                        job["steps"][verb] = self._request([verb, rid, *suffix],
                                                           self.upload_window, source=source)
                    job["steps"][verb].update(bytes=os.path.getsize(path), sha256=file_digest(path))
            with tempfile.TemporaryFile() as archive:
                with self._compute:
                    job["steps"]["run"] = self._run(job, chrom, seed, archive)
                # Separate ids mean this get can run while the next chromosome owns Beagle. A
                # recovered run has its archive already: its last `get` collected it.
                if job["steps"]["run"].get("recovered"):
                    job["steps"]["get"] = {"seconds": 0.0, "attempts": 0,
                                           "collected_by_run_recovery": True}
                else:
                    with self._download:
                        job["steps"]["get"] = self._request(["get", rid, str(chrom)],
                                                            self.download_window, output=archive)
                size = os.fstat(archive.fileno()).st_size
                job["steps"]["get"]["bytes"] = size
                if size > MAX_RESULT + (32 << 20):
                    raise GateError("get", "archive exceeds result size limit")
                archive.seek(0)
                job["files"] = copy_archive(archive, chrom, destination)
        except BaseException as exc:
            error = exc
            job["error"] = str(exc)
            raise
        finally:
            try:
                self.clean(job)
            except (GateError, OSError):
                # Preserve the original failure while keeping cleanup errors in the receipt.
                if error is None:
                    raise
        return job
