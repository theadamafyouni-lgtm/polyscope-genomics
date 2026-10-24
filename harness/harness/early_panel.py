"""A demo run's reference panel, built while it imputes (demo speedup 3).

The provider builds the comparison group's genotypes at the model's positions with
build_reference_panel.py (ref_1kg_<PGS>.vcf.gz). That depends only on the model's scoring file
and the 1000 Genomes panel, never on the person, and for a large model it takes half an hour.
In a demo run the harness starts the same build, by the brief's own arguments, as soon as the
agent records its model choice, and runs it while imputation does. The provider is handed the
finished panel only if it was built from the very scoring file the provider is given (the same
sha256) and finished cleanly; otherwise INPUTS.md says the panel is absent and the provider
builds it as before. A model the agent later turns from is built for nothing, and that is all.
A build still going when the provider asks is waited for (up to EARLY_WAIT_SECONDS), never
started again beside itself: it began earlier than any new one could.

Beside a chip's imputation it runs at lower concurrency than the brief's (2 chromosomes, 1
bcftools thread each) so it takes little from imputation. A whole genome may not impute at all
(own_calls.py), and then the panel is all that is left to wait for, so for a file the door
classified as a whole genome it runs at the brief's own 4 x 2. How many chromosomes are extracted
at once changes when it finishes, not what it writes. It runs with the tools the provider's sandbox has: the PATH every sandbox
gets (config.SANDBOX_PATH_HOST, the host's tool prefixes first) and the python3 on it.
"""
import hashlib
import json
import re
import os
import shutil
import subprocess
import sys
import threading
import time

from . import config

BUILDER = os.path.join(config.PRODUCT_ROOT, "reference-provider", "build_reference_panel.py")
PANEL = "ref_1kg_{pgs}.vcf.gz"
# A large model's panel takes the provider about half an hour to build on the demo host, so a
# build already under way is never worth abandoning before this.
EARLY_WAIT_SECONDS = 45 * 60
SCORING = "{pgs}_hmPOS_GRCh37.txt.gz"


def whole_genome(run_dir):
    """Whether the door classified the run's file as a whole genome (an exome is not one)."""
    try:
        with open(os.path.join(run_dir, "door", "classification.json")) as fh:
            assay = str(json.load(fh).get("assay_class") or "").lower()
    except (OSError, ValueError, AttributeError):
        return False
    return bool(re.search(r"\bwgs\b|whole[\s-]*genome|sequenc", assay)) and not re.search(r"exome|\bwes\b", assay)


def sha256_of(path):
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


class EarlyPanels:
    def __init__(self, run_dir, work_dir, phase3_dir, builder=BUILDER, poll_seconds=20):
        self.run_dir, self.work_dir, self.phase3_dir = run_dir, work_dir, phase3_dir
        self.builder, self.poll_seconds = builder, poll_seconds
        self.builds = {}  # pgs -> what was built, from which scoring file, how it ended
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread = None
        self._offset = 0

    # -- following the agent's ledger -----------------------------------------
    def start(self):
        if self._thread is None:
            self._thread = threading.Thread(target=self._follow, daemon=True)
            self._thread.start()
        return self

    def _follow(self):
        while not self._stop.is_set():
            try:
                self._read_ledger()
            except Exception:  # noqa: BLE001 - a speedup must never be why a run fails
                pass
            self._stop.wait(self.poll_seconds)

    def _read_ledger(self):
        path = os.path.join(self.run_dir, "ledger.jsonl")
        try:
            with open(path, "rb") as fh:
                fh.seek(self._offset)
                data = fh.read()
        except OSError:
            return
        end = data.rfind(b"\n") + 1
        self._offset += end
        for line in data[:end].splitlines():
            try:
                entry = json.loads(line)
            except ValueError:
                continue
            if not (isinstance(entry, dict) and entry.get("kind") == "judgment" and entry.get("stage") == "select"
                    and entry.get("outcome") == "proceed" and isinstance(entry.get("artifact"), str)):
                continue
            judgment = os.path.join(os.path.dirname(self.run_dir), entry["artifact"])
            try:
                with open(judgment) as fh:
                    pgs = json.load(fh).get("chosen_model")
            except (OSError, ValueError, AttributeError):
                continue
            if isinstance(pgs, str) and pgs.startswith("PGS") and pgs[3:].isdigit():
                self._build(pgs)

    # -- building ---------------------------------------------------------------
    def _scoring_file(self, pgs):
        """The run's scoring file for the model, by the exact name the provider stages it under;
        None unless there is exactly one."""
        want = SCORING.format(pgs=pgs)
        hits = [os.path.join(cur, f) for cur, _, files in os.walk(self.run_dir) for f in files if f == want]
        return hits[0] if len(hits) == 1 else None

    def _build(self, pgs):
        with self._lock:
            if pgs in self.builds:
                return
            source = self._scoring_file(pgs)
            if source is None:
                return
            here = os.path.join(self.work_dir, pgs)
            os.makedirs(here, exist_ok=True)
            scoring = os.path.join(here, os.path.basename(source))
            shutil.copyfile(source, scoring)  # the bytes built from, fixed and hashed
            vcf = os.path.join(here, PANEL.format(pgs=pgs))
            python = shutil.which("python3", path=config.SANDBOX_PATH_HOST) or sys.executable
            workers, threads = ("4", "2") if whole_genome(self.run_dir) else ("2", "1")
            argv = [python, self.builder, "--scoring", scoring, "--chr-col", "hm_chr",
                    "--pos-col", "hm_pos", "--phase3-dir", self.phase3_dir, "--chromosomes", "1-22",
                    "--workers", workers, "--threads", threads, "--out", vcf]
            log = open(os.path.join(here, "build.log"), "wb")
            build = {"pgs": pgs, "scoring_sha256": sha256_of(scoring), "vcf": vcf, "argv": argv[1:],
                     "started": time.time(), "finished": None, "ok": None, "used_by_provider": False,
                     "done": threading.Event()}
            build["process"] = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=log,
                                                stderr=subprocess.STDOUT, cwd=here,
                                                env=dict(os.environ, PATH=config.SANDBOX_PATH_HOST))
            log.close()
            self.builds[pgs] = build
            threading.Thread(target=self._wait, args=(build,), daemon=True).start()

    def _wait(self, build):
        rc = build["process"].wait()
        build["finished"] = time.time()
        build["ok"] = rc == 0 and all(os.path.isfile(build["vcf"] + ext) for ext in ("", ".tbi", ".provenance.json"))
        build["done"].set()

    # -- handing over ---------------------------------------------------------------
    def ready(self, pgs, scoring_sha256, wait_seconds=0):
        """The finished panel for this model, built from the scoring file with this sha256; a
        build of it still going is waited for up to wait_seconds. None for anything else."""
        build = self.builds.get(pgs)
        if not build or build["scoring_sha256"] != scoring_sha256:
            return None
        if wait_seconds and not build["done"].is_set():
            asked = time.time()
            build["done"].wait(wait_seconds)
            build["provider_waited_seconds"] = round(time.time() - asked, 1)
        if not build["ok"]:
            return None
        build["used_by_provider"] = True
        return build["vcf"]

    def stop(self):
        self._stop.set()
        for build in list(self.builds.values()):
            if build["process"].poll() is None:
                build["process"].kill()
                build["process"].wait()

    def record(self):
        """What was built, for the manifest."""
        return {pgs: {k: v for k, v in b.items() if k not in ("process", "done")} for pgs, b in self.builds.items()}
