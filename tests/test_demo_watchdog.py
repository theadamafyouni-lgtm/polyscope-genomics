"""The demo watchdog, against fake worker and web processes started for real. No harness, no model."""
import contextlib
import fcntl
import io
import json
import os
from pathlib import Path
import signal
import subprocess
import tempfile
import time
import unittest
from unittest.mock import patch

from demo_site import watchdog

REPO = Path(__file__).resolve().parents[1]

FAKE_WORKER = r'''
import json, os, sys, time
home = os.environ["HOME"]
if sys.argv[1:2] == ["end-interrupted"]:
    marker = os.path.join(home, "interrupted.txt")
    if os.path.exists(marker):
        print(open(marker).read().strip())
        os.remove(marker)
    sys.exit(0)
if os.path.exists(os.path.join(home, "worker-dies")):
    sys.exit(1)
time.sleep(600)
'''

FAKE_WEB = r'''
import json, os, sys, time
with open(os.path.join(os.environ["HOME"], "web-seen.json"), "w") as fh:
    json.dump({"argv": sys.argv, "env": dict(os.environ)}, fh)
time.sleep(600)
'''

FAKE_PROXY = r'''
import json, os, sys, time
with open(os.path.join(os.environ["HOME"], "proxy-seen.json"), "w") as fh:
    json.dump({"argv": sys.argv}, fh)
time.sleep(600)
'''


class WatchdogTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix=".demo-watchdog-test-", dir=REPO)
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.base, self.home = root / "demo", root / "home"
        app = self.base / "app" / "demo_site"
        app.mkdir(parents=True)
        self.home.mkdir()
        (app / "__init__.py").write_text("")
        (app / "worker.py").write_text(FAKE_WORKER)
        (app / "web.py").write_text(FAKE_WEB)
        (app / "tls_proxy.py").write_text(FAKE_PROXY)
        self.settings = {"harness_home": str(self.home), "max_restarts": 3, "port": 18765}
        self.addCleanup(self.kill_all)

    def dog(self):
        return watchdog.Watchdog(self.base, settings=self.settings, settle=0.4)

    def run_pass(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            result = self.dog().run_once()
        return result, out.getvalue()

    def pid(self, component):
        return int((self.base / "state" / ("%s.pid" % component)).read_text())

    def failure_lines(self):
        log = self.base / "state" / "failures.log"
        return [l.split("\t") for l in log.read_text().splitlines()] if log.exists() else []

    def kill_all(self):
        # Only what this test started and is still running: a pid file can name anything,
        # the test runner itself included (test_a_pid_running_something_else_is_dead).
        for component in watchdog.COMPONENTS + (watchdog.PROXY,):
            try:
                pid = self.dog().alive(component)
                if pid:
                    os.killpg(pid, signal.SIGKILL)
            except (OSError, ValueError):
                pass

    def test_starts_worker_then_web_then_is_quiet(self):
        started, out = self.run_pass()
        self.assertEqual(started, ["worker", "web"])
        lines = out.splitlines()
        self.assertIn("started worker", lines[0])
        self.assertIn("started web", lines[1])
        self.assertEqual(self.run_pass(), ([], ""))
        self.assertEqual(self.failure_lines(), [])

    def test_a_dead_worker_has_its_runs_ended_first_and_only_it_restarts(self):
        self.run_pass()
        web = self.pid("web")
        os.killpg(self.pid("worker"), signal.SIGKILL)
        time.sleep(.2)
        (self.home / "interrupted.txt").write_text("ended interrupted demo run demo-" + "d" * 32)
        started, out = self.run_pass()
        self.assertEqual(started, ["worker"])
        lines = out.splitlines()
        self.assertIn("ended interrupted demo run", lines[0])
        self.assertIn("started worker", lines[1])
        self.assertEqual(self.pid("web"), web)

    def test_restart_cap_writes_one_line_and_stops(self):
        (self.home / "worker-dies").write_text("")
        for _ in range(3):
            expected = [] if "web" in self.alive_now() else ["web"]
            started, out = self.run_pass()
            self.assertEqual(started, expected)
            self.assertIn("worker did not come up", out)
        _, out = self.run_pass()
        self.assertIn("worker: restart limit 3/24h reached", out)
        self.assertEqual(self.run_pass(), ([], ""))
        whats = [(l[0], l[1], l[2], l[3]) for l in self.failure_lines()]
        self.assertEqual(whats.count(("-", "-", "worker", "restart limit 3/24h reached")), 1)
        self.assertEqual(sum(1 for w in whats if w[3].startswith("did not come up")), 3)
        # Removing restarts.json is the reset.
        (self.base / "state" / "restarts.json").unlink()
        (self.home / "worker-dies").unlink()
        self.assertEqual(self.run_pass()[0], ["worker"])

    def alive_now(self):
        dog = self.dog()
        return [c for c in watchdog.COMPONENTS if dog.alive(c)]

    def test_an_expired_login_stops_every_start_but_still_ends_runs(self):
        (self.base / "state").mkdir(parents=True)
        (self.base / "state" / "claude-login-expired").write_text("{}")
        (self.home / "interrupted.txt").write_text("ended interrupted demo run demo-" + "e" * 32)
        result, out = self.run_pass()
        self.assertEqual(result, "login-expired")
        self.assertIn("ended interrupted demo run", out)
        self.assertEqual(self.alive_now(), [])
        self.assertEqual(self.run_pass(), ("login-expired", ""))
        (line,) = self.failure_lines()
        self.assertEqual(line[2], "demo")
        self.assertIn("Claude login expired", line[3])
        (self.base / "state" / "claude-login-expired").unlink()
        self.assertEqual(self.run_pass()[0], ["worker", "web"])

    def test_no_secret_reaches_a_command_line_or_environment(self):
        with patch.dict(os.environ, {"CLAUDE_CODE_OAUTH_TOKEN": "sk-synthetic-secret"}):
            self.run_pass()
        deadline = time.monotonic() + 5
        while not (self.home / "web-seen.json").exists() and time.monotonic() < deadline:
            time.sleep(.05)
        seen = json.loads((self.home / "web-seen.json").read_text())
        self.assertEqual(sorted(seen["env"]), ["HOME", "LANG", "PATH", "PYTHONDONTWRITEBYTECODE"])
        self.assertEqual(seen["env"]["HOME"], str(self.home))
        for component in watchdog.COMPONENTS:
            with open("/proc/%d/cmdline" % self.pid(component), "rb") as fh:
                self.assertNotIn(b"sk-synthetic-secret", fh.read())

    def test_every_origin_reaches_the_web(self):
        self.settings["origin"] = ["https://demo.example.org", "https://demo.example.ts.net:8443"]
        self.run_pass()
        deadline = time.monotonic() + 5
        while not (self.home / "web-seen.json").exists() and time.monotonic() < deadline:
            time.sleep(.05)
        argv = json.loads((self.home / "web-seen.json").read_text())["argv"]
        self.assertEqual([argv[i + 1] for i, a in enumerate(argv) if a == "--origin"], self.settings["origin"])
        for bad in ([], 8443, {"https://demo.example.org": True}, ["https://demo.example.org", 8443]):
            self.settings["origin"] = bad
            with self.assertRaises(ValueError):
                self.dog()

    def test_a_named_tls_proxy_starts_after_the_web_and_stops_once_unnamed(self):
        self.settings["tls_proxy"] = {"listen": ["127.0.0.1:18443", "[::1]:18443"]}
        started, out = self.run_pass()
        self.assertEqual(started, ["worker", "web", "tls_proxy"])
        self.assertIn("started tls_proxy", out.splitlines()[2])
        deadline = time.monotonic() + 5
        while not (self.home / "proxy-seen.json").exists() and time.monotonic() < deadline:
            time.sleep(.05)
        state = self.base.resolve() / "state"
        self.assertEqual(json.loads((self.home / "proxy-seen.json").read_text())["argv"][1:],
                         ["serve", "--upstream-port", "18765", "--cert", str(state / "tls" / "cert.pem"),
                          "--key", str(state / "tls" / "key.pem"), "--listen", "127.0.0.1:18443",
                          "--listen", "[::1]:18443"])
        self.assertEqual(self.run_pass(), ([], ""))
        proxy, web, worker = self.pid("tls_proxy"), self.pid("web"), self.pid("worker")
        del self.settings["tls_proxy"]
        started, out = self.run_pass()
        self.assertEqual(started, [])
        self.assertIn("stopped tls_proxy (pid %d): watchdog.json no longer names it" % proxy, out)
        self.assertFalse((state / "tls_proxy.pid").exists())
        deadline = time.monotonic() + 5
        while os.path.exists("/proc/%d/cmdline" % proxy) and open("/proc/%d/cmdline" % proxy, "rb").read() \
                and time.monotonic() < deadline:
            time.sleep(.05)
        self.assertIsNone(self.dog().alive("tls_proxy"))
        self.assertEqual((self.pid("web"), self.pid("worker")), (web, worker))
        self.assertEqual(self.run_pass(), ([], ""))

    def test_a_samples_directory_reaches_the_web(self):
        self.assertNotIn("--samples-dir", self.dog().command("web"))
        (self.base / "samples").mkdir()
        self.assertEqual(self.dog().command("web")[-2:], ["--samples-dir", str(self.base.resolve() / "samples")])

    def test_run_settings_reach_the_worker(self):
        self.settings.update(max_runs=2, run_limit_minutes=240, typical_run_minutes=80)
        dog = self.dog()
        self.assertEqual(dog.command("worker")[-6:], ["--max-runs", "2", "--run-limit-minutes", "240",
                                                      "--typical-run-minutes", "80"])
        for bad in ({"max_runs": 0}, {"max_runs": 1.5}, {"max_runs": "2"}, {"run_limit_minutes": 0},
                    {"typical_run_minutes": "80"}, {"max_runs": True}):
            self.settings.update(max_runs=2, run_limit_minutes=240, typical_run_minutes=80)
            self.settings.update(bad)
            with self.assertRaises(ValueError):
                self.dog()

    def test_trusted_front_doors_reach_the_tls_proxy(self):
        self.settings["tls_proxy"] = {"listen": ["127.0.0.1:18443"],
                                      "trusted_proxies": ["100.64.0.9", "fd7a:115c:a1e0::9"]}
        self.run_pass()
        deadline = time.monotonic() + 5
        while not (self.home / "proxy-seen.json").exists() and time.monotonic() < deadline:
            time.sleep(.05)
        argv = json.loads((self.home / "proxy-seen.json").read_text())["argv"]
        self.assertEqual(argv[-6:], ["--listen", "127.0.0.1:18443", "--trusted-proxy", "100.64.0.9",
                                     "--trusted-proxy", "fd7a:115c:a1e0::9"])

    def test_a_malformed_tls_proxy_setting_is_refused(self):
        for bad in ({}, {"listen": []}, {"listen": "127.0.0.1:18443"}, {"listen": [18443]}, ["127.0.0.1:18443"],
                    {"listen": ["127.0.0.1:18443"], "trusted_proxies": "100.64.0.9"},
                    {"listen": ["127.0.0.1:18443"], "trusted_proxies": [9]},
                    {"listen": ["127.0.0.1:18443"], "trust": ["100.64.0.9"]}):
            self.settings["tls_proxy"] = bad
            with self.assertRaises(ValueError):
                self.dog()

    def test_one_pass_at_a_time(self):
        (self.base / "state").mkdir(parents=True)
        with open(self.base / "state" / "watchdog.lock", "a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            self.assertEqual(self.run_pass(), ("busy", ""))

    def test_a_pid_running_something_else_is_dead(self):
        (self.base / "state").mkdir(parents=True)
        (self.base / "state" / "worker.pid").write_text(str(os.getpid()))
        self.assertIsNone(self.dog().alive("worker"))

    def test_the_cron_wrapper_runs_from_base(self):
        wrapper = (REPO / "demo_site/deploy/demo-watchdog").read_text()
        self.assertIn('cd "$BASE/app"', wrapper)
        self.assertIn("env -i", wrapper)
        self.assertNotRegex(wrapper, r"TOKEN|KEY=|PASSWORD")
        installer = (REPO / "demo_site/deploy/install-user.sh").read_text()
        self.assertIn("git archive HEAD", installer)
        self.assertIn("cp -al", installer)
        commands = [l for l in installer.splitlines() if not l.lstrip().startswith(("#", "echo"))]
        self.assertFalse([l for l in commands if "crontab" in l])
        self.assertEqual(subprocess.run(["bash", "-n", str(REPO / "demo_site/deploy/install-user.sh")]).returncode, 0)


if __name__ == "__main__":
    unittest.main()
