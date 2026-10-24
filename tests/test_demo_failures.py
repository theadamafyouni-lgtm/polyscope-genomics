"""The demo's failure log, and what the worker writes into it. Synthetic runs only."""
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from demo_site import failures, worker

REPO = Path(__file__).resolve().parents[1]
OWNER = "session-owner-abcdefghijklmnop"
GENOME = b"# synthetic genome\nrs1\t1\t101\tAA\n"


def upload():
    fh = tempfile.TemporaryFile()
    fh.write(GENOME)
    fh.seek(0)
    return fh


class Exited:
    def __init__(self, rc):
        self.returncode = rc

    def poll(self):
        return self.returncode

    def kill(self):
        pass

    def wait(self, timeout=None):
        return self.returncode


class FailureLogTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix=".demo-failures-test-", dir=REPO)
        self.addCleanup(self.tmp.cleanup)
        self.log = Path(self.tmp.name) / "failures.log"

    def test_five_fields_whatever_the_values(self):
        failures.append(self.log, "demo-1", "demo", "demo run", "line one\nline\ttwo", "2026-10-13T00:00:00Z")
        failures.append(self.log, None, "", "worker", "restart limit 5/24h reached")
        lines = self.log.read_text().splitlines()
        self.assertEqual(lines[0].split("\t"), ["demo-1", "demo", "demo run", "line one line two",
                                                "2026-10-13T00:00:00Z"])
        fields = lines[1].split("\t")
        self.assertEqual(fields[:4], ["-", "-", "worker", "restart limit 5/24h reached"])
        self.assertRegex(fields[4], r"\A\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ\Z")
        self.assertEqual(self.log.stat().st_mode & 0o777, 0o600)

    def test_command_line_and_concurrent_writers(self):
        procs = [subprocess.Popen([sys.executable, "-m", "demo_site.failures", "--log", str(self.log),
                                   "-", "-", "writer %d" % i, "x" * 2000], cwd=REPO)
                 for i in range(8)]
        for p in procs:
            self.assertEqual(p.wait(timeout=60), 0)
        lines = self.log.read_text().splitlines()
        self.assertEqual(sorted(l.split("\t")[2] for l in lines), sorted("writer %d" % i for i in range(8)))
        self.assertTrue(all(len(l.split("\t")) == 5 for l in lines))


class WorkerWritesTest(unittest.TestCase):
    def setUp(self):
        # Free disk is the host's, not what these tests are about.
        self.enterContext(patch.object(worker, "MIN_FREE_BYTES", 0))
        self.tmp = tempfile.TemporaryDirectory(prefix=".demo-failures-test-", dir=REPO)
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.log, self.flag = self.root / "failures.log", self.root / "claude-login-expired"
        self.now = 1000.0
        self.process = Exited(None)
        self.runs = self.open_runs(launcher=lambda *a: self.process)

    def open_runs(self, **kw):
        runs = worker.Runs(self.root / "private", clock=lambda: self.now, failure_log=str(self.log),
                           login_flag=str(self.flag), **kw)
        self.addCleanup(runs.close)
        return runs

    def lines(self):
        return [l.split("\t") for l in self.log.read_text().splitlines()] if self.log.exists() else []

    def test_an_interrupted_run_gets_one_line(self):
        run_id = self.runs.start(OWNER, upload())["run_id"]
        self.runs.processes.clear()
        self.runs.close()
        self.open_runs()
        (line,) = self.lines()
        self.assertEqual(line[:4], [run_id, "demo", "demo run", worker.INTERRUPTED])
        self.assertFalse(self.flag.exists())

    def test_a_failed_run_says_why_and_a_completed_one_writes_nothing(self):
        self.runs.start(OWNER, upload())
        self.process.returncode = 0
        self.runs.sweep()
        self.assertEqual(self.lines(), [])
        self.process = Exited(None)
        run_id = self.runs.start(OWNER, upload())["run_id"]
        self.process.returncode = 3
        self.runs.sweep()
        (line,) = self.lines()
        self.assertEqual(line[:4], [run_id, "demo", "demo run", "the harness exited 3"])
        self.assertEqual(json.loads((self.runs._path(run_id) / "record.json").read_text())["why_failed"],
                         "the harness exited 3")
        self.assertFalse(self.flag.exists())

    def test_the_harness_verdict_is_the_why_and_a_dead_login_raises_the_flag(self):
        run_id = self.runs.start(OWNER, upload())["run_id"]
        vault = self.runs._path(run_id) / "harness/vault/runs" / run_id
        vault.mkdir(parents=True)
        why = "the CLI had no session at all -- the agent's output carries 'Not logged in'"
        (vault / "manifest.json").write_text(json.dumps({"published": {"environment_failure": why}}))
        self.process.returncode = 1
        self.runs.sweep()
        (line,) = self.lines()
        self.assertEqual(line[3], why)
        flag = json.loads(self.flag.read_text())
        self.assertEqual((flag["run"], flag["marker"]), (run_id, "Not logged in"))

    def test_a_dead_login_in_the_harness_log_alone_also_raises_it(self):
        run_id = self.runs.start(OWNER, upload())["run_id"]
        (self.runs._path(run_id) / "harness.log").write_text("...\nAPI Error: 401 OAuth access token has expired\n")
        self.process.returncode = 1
        self.runs.sweep()
        self.assertEqual(json.loads(self.flag.read_text())["marker"], "OAuth access token has expired")


class HarnessVerdictTest(unittest.TestCase):
    def test_a_demo_run_records_the_environment_verdict_without_filing(self):
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        from test_impute_broker import config, orchestrator  # noqa: F401  (synthetic token file)
        from harness import manifest
        with tempfile.TemporaryDirectory(prefix=".demo-verdict-", dir=REPO) as tmp:
            with patch.object(config, "VAULT", tmp + "/vault"), \
                    patch.object(config, "VAULT_RUNS", tmp + "/vault/runs"), \
                    patch.object(config, "SANDBOXES", tmp + "/sandboxes"):
                o = orchestrator.Orchestrator(run_id="demo-" + "c" * 32, backend="human", demo=True)
                o.manifest = manifest.Manifest(o.run_id, tmp)
                o.manifest.set("outcome", {"prs_returncode": 1, "prs_stderr_tail": "Not logged in"})
                o.manifest.set("transcript", {"n_messages": 0})
                o.manifest.set("result", {"state": "absent", "valid": False})
                with patch.object(orchestrator, "RESULTS_ROOT", tmp + "/results"):
                    rec = o.publish()
        self.assertEqual(rec["state"], "demo")
        self.assertIn("Not logged in", rec["environment_failure"])

    def test_a_demo_run_cut_off_by_its_connection_to_claude_is_an_environment_failure(self):
        """And only a demo run: a benchmark run that talked and was then cut off is filed as
        before, on the harness's own rules."""
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        from test_impute_broker import config, orchestrator  # noqa: F401  (synthetic token file)
        from harness import manifest
        cut = {"reason": "cut off before it recorded an ending: the connection to Claude failed", "environment": True}
        for demo in (True, False):
            with tempfile.TemporaryDirectory(prefix=".demo-verdict-", dir=REPO) as tmp:
                with patch.object(config, "VAULT", tmp + "/vault"), \
                        patch.object(config, "VAULT_RUNS", tmp + "/vault/runs"), \
                        patch.object(config, "SANDBOXES", tmp + "/sandboxes"):
                    o = orchestrator.Orchestrator(run_id=("demo-" + "c" * 32) if demo else None, backend="human", demo=demo)
                    o.manifest = manifest.Manifest(o.run_id, tmp)
                    o.manifest.set("outcome", {"prs_returncode": 1, "prs_stdout_tail": "API Error: Connection error."})
                    o.manifest.set("transcript", {"n_messages": 12})
                    o.manifest.set("result", {"state": "absent", "valid": False})
                    o.manifest.set("cut_off", cut)
                    self.assertEqual(o.environment_failure(), cut["reason"] if demo else None)


if __name__ == "__main__":
    unittest.main()
