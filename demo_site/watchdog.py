"""Keep the demo's worker and web running from cron, on an account without root or systemd.

One pass, every few minutes (demo_site/deploy/demo-watchdog is the cron entry point):

  1. One pass at a time: a lock in the state directory, or exit.
  2. A component is alive only if its pidfile names a process of this account whose command
     line is `python3 -m demo_site.<component>`, which also rules out a reused pid.
  3. If the worker is dead, the runs it left unfinished are ended first, as environment
     failures with a line each in the failure log (`demo_site.worker end-interrupted`), whether
     or not anything is restarted afterwards. Runs are never deleted here.
  4. If `claude-login-expired` is there (the worker writes it when a run could not
     authenticate), nothing is started or restarted until a person logs in again and removes
     it. Said once, in the failure log.
  5. Each component's starts are counted over a rolling 24 h in restarts.json. At the cap
     (default 5) one failure-log line is written and that component is left alone until a
     person removes restarts.json.
  6. The worker starts before the web, each in a session of its own, logging to
     state/<component>.log. Its environment is built here, not inherited: HOME (the harness's
     own, where its private configuration and token file live), PATH and LANG, and nothing
     secret, in the environment or on a command line. A component not running 5 s after its
     start gets a failure-log line.
  7. One dated line per action on stdout, which cron appends to watchdog.log; nothing on a
     quiet pass.

Layout under --base: app/ (the code, run from there), state/ (worker state in private/, web
sessions in web/, auth/master.key, failures.log, the login flag, pidfiles, logs), run/ (the
worker's socket) and, if there, samples/ (genomes signed-in people may download; the web sees
files added or removed there at once, the directory itself when it starts). Settings, all
optional, in base/watchdog.json: harness_home (HOME for both, default ~), port (8765), origin
(http://localhost:<port>; a list where people reach the demo under more than one host name),
path, max_restarts (5), trust_funnel (false), app (default base/app), tls_proxy, and for the
worker max_runs (1, the demos that may run at once), run_limit_minutes (none: when to stop a
run) and typical_run_minutes (a run's usual length, from which people waiting for a slot are
told when to come back while no run time has been measured: see the worker's RUN_TIMES). With tls_proxy, {"listen": ["ADDRESS:PORT", ...]}, a pass also keeps
demo_site.tls_proxy running after the web, with state/tls/cert.pem and key.pem: the self-signed
HTTPS front on the tailnet without root. Its "trusted_proxies": ["ADDRESS", ...] names front
doors on the tailnet (the public link's reverse proxy) whose X-Forwarded-For it keeps. When the
setting goes, the next pass stops the front, which frees its port for Tailscale Serve.
"""
import argparse
import fcntl
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time

from . import failures

DAY = 24 * 3600
COMPONENTS = ("worker", "web")
PROXY = "tls_proxy"
RUN_SETTINGS = ("max_runs", "run_limit_minutes", "typical_run_minutes")


def now_iso(when=None):
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(when))


def say(message):
    print("%s %s" % (now_iso(), message), flush=True)


class Watchdog:
    def __init__(self, base, settings=None, clock=time.time, settle=5.0):
        self.base = Path(base).resolve()
        self.settings = settings if settings is not None else self._load_settings()
        self.state = self.base / "state"
        self.app = Path(self.settings.get("app") or self.base / "app")
        self.failure_log = self.state / "failures.log"
        self.login_flag = self.state / "claude-login-expired"
        self.restarts_file = self.state / "restarts.json"
        self.clock = clock
        self.settle = settle
        self.max_restarts = int(self.settings.get("max_restarts", 5))
        runs = {key: self.settings[key] for key in RUN_SETTINGS if key in self.settings}
        if not all(type(value) in (int, float) and value > 0 for value in runs.values()) or \
                not isinstance(runs.get("max_runs", 1), int):
            raise ValueError("max_runs is a whole number, run_limit_minutes and typical_run_minutes "
                             "numbers of minutes, all above 0")
        origin = self.settings.get("origin")
        if origin is not None and not isinstance(origin, str) and not (
                isinstance(origin, list) and origin and all(isinstance(o, str) for o in origin)):
            raise ValueError('origin must be "https://HOST" or a list of them')
        self.proxy = self.settings.get(PROXY)
        strings = lambda value: isinstance(value, list) and all(isinstance(a, str) for a in value)
        if self.proxy is not None and not (
                isinstance(self.proxy, dict) and set(self.proxy) <= {"listen", "trusted_proxies"}
                and strings(self.proxy.get("listen")) and self.proxy["listen"]
                and strings(self.proxy.get("trusted_proxies", []))):
            raise ValueError('tls_proxy must be {"listen": ["ADDRESS:PORT", ...]} and optionally '
                             '"trusted_proxies": ["ADDRESS", ...]')

    def _load_settings(self):
        path = self.base / "watchdog.json"
        if not path.exists():
            return {}
        with open(path) as fh:
            data = json.load(fh)
        if not isinstance(data, dict):
            raise ValueError("%s must be a JSON object" % path)
        return data

    # ------------------------------------------------------------------ processes

    def components(self):
        return COMPONENTS + ((PROXY,) if self.proxy else ())

    def command(self, component):
        python = sys.executable
        if component == "worker":
            argv = [python, "-m", "demo_site.worker", "--state-dir", str(self.state / "private"),
                    "--socket", str(self.base / "run" / "worker.sock"),
                    "--secret-file", str(self.state / "auth" / "master.key")]
            for key in RUN_SETTINGS:
                if key in self.settings:
                    argv += ["--" + key.replace("_", "-"), str(self.settings[key])]
            return argv
        port = int(self.settings.get("port", 8765))
        if component == PROXY:
            argv = [python, "-m", "demo_site.tls_proxy", "serve", "--upstream-port", str(port),
                    "--cert", str(self.state / "tls" / "cert.pem"),
                    "--key", str(self.state / "tls" / "key.pem")]
            for address in self.proxy["listen"]:
                argv += ["--listen", address]
            for address in self.proxy.get("trusted_proxies", []):
                argv += ["--trusted-proxy", address]
            return argv
        origins = self.settings.get("origin") or "http://localhost:%d" % port
        argv = [python, "-m", "demo_site.web", "--port", str(port)]
        for origin in [origins] if isinstance(origins, str) else origins:
            argv += ["--origin", origin]
        argv += ["--state-dir", str(self.state / "web"),
                 "--socket", str(self.base / "run" / "worker.sock")]
        if (self.base / "samples").is_dir():
            argv += ["--samples-dir", str(self.base / "samples")]
        if self.settings.get("trust_funnel"):
            argv.append("--trust-funnel")
        return argv

    def environment(self):
        return {"HOME": str(self.settings.get("harness_home") or os.path.expanduser("~")),
                "PATH": self.settings.get("path") or "/usr/local/bin:/usr/bin:/bin",
                "LANG": "C.UTF-8", "PYTHONDONTWRITEBYTECODE": "1"}

    def alive(self, component):
        try:
            pid = int((self.state / ("%s.pid" % component)).read_text().strip())
            if os.stat("/proc/%d" % pid).st_uid != os.getuid():
                return None
            with open("/proc/%d/cmdline" % pid, "rb") as fh:
                argv = fh.read().split(b"\0")
        except (OSError, ValueError):
            return None
        return pid if argv[1:3] == [b"-m", ("demo_site.%s" % component).encode()] else None

    def start(self, component):
        log = open(self.state / ("%s.log" % component), "ab")
        with log:
            proc = subprocess.Popen(self.command(component), cwd=self.app, env=self.environment(),
                                    stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
                                    start_new_session=True, close_fds=True)
        pidfile = self.state / ("%s.pid" % component)
        pidfile.write_text("%d\n" % proc.pid)
        time.sleep(self.settle)
        if proc.poll() is None and self.alive(component) == proc.pid:
            say("started %s (pid %d)" % (component, proc.pid))
            return True
        say("%s did not come up (exit %s); see state/%s.log" % (component, proc.poll(), component))
        self.fail("-", component, "did not come up after a start; see state/%s.log" % component)
        return False

    def end_interrupted(self):
        proc = subprocess.run([sys.executable, "-m", "demo_site.worker", "end-interrupted",
                               "--state-dir", str(self.state / "private")],
                              cwd=self.app, env=self.environment(), capture_output=True,
                              text=True, timeout=120, stdin=subprocess.DEVNULL)
        for line in proc.stdout.splitlines():
            say(line)
        if proc.returncode:
            say("end-interrupted exited %d: %s" % (proc.returncode, proc.stderr.strip()[-300:]))
            self.fail("-", "worker", "end-interrupted exited %d; unfinished runs were not ended"
                      % proc.returncode)

    # ------------------------------------------------------------------ bookkeeping

    def fail(self, run, what, why):
        failures.append(self.failure_log, run, "-" if run == "-" else "demo", what, why,
                        now_iso(self.clock()))

    def restarts(self):
        try:
            with open(self.restarts_file) as fh:
                data = json.load(fh)
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    def save_restarts(self, data):
        temporary = self.restarts_file.with_suffix(".tmp")
        temporary.write_text(json.dumps(data, indent=1))
        os.replace(temporary, self.restarts_file)

    # ------------------------------------------------------------------ one pass

    def run_once(self):
        self.state.mkdir(mode=0o700, parents=True, exist_ok=True)
        (self.base / "run").mkdir(mode=0o700, parents=True, exist_ok=True)
        with open(self.state / "watchdog.lock", "a") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return "busy"
            return self._pass()

    def _pass(self):
        if not self.proxy:
            pid = self.alive(PROXY)
            if pid:
                os.kill(pid, signal.SIGTERM)
                (self.state / ("%s.pid" % PROXY)).unlink(missing_ok=True)
                say("stopped %s (pid %d): watchdog.json no longer names it" % (PROXY, pid))
        alive = {c: self.alive(c) for c in self.components()}
        if not alive["worker"]:
            self.end_interrupted()
        data = self.restarts()
        if self.login_flag.exists():
            if not data.get("login_noted"):
                say("Claude login expired (%s): nothing is started until a person logs in "
                    "again and removes it" % self.login_flag.name)
                self.fail("-", "demo", "Claude login expired; nothing restarted until a person "
                          "logs in again and removes %s" % self.login_flag.name)
                data["login_noted"] = now_iso(self.clock())
                self.save_restarts(data)
            return "login-expired"
        data.pop("login_noted", None)
        started = []
        for component in self.components():
            if alive[component]:
                continue
            entry = data.setdefault(component, {})
            starts = [t for t in entry.get("starts", []) if t > self.clock() - DAY]
            if entry.get("capped_at") or len(starts) >= self.max_restarts:
                if not entry.get("capped_at"):
                    entry["capped_at"] = now_iso(self.clock())
                    say("%s: restart limit %d/24h reached; left stopped until restarts.json is "
                        "removed" % (component, self.max_restarts))
                    self.fail("-", component, "restart limit %d/24h reached" % self.max_restarts)
                entry["starts"] = starts
                continue
            starts.append(self.clock())
            entry["starts"] = starts
            self.save_restarts(data)
            if self.start(component):
                started.append(component)
        self.save_restarts(data)
        return started


def main(argv=None):
    parser = argparse.ArgumentParser(description="One watchdog pass for the demo (run from cron)")
    parser.add_argument("--base", required=True, help="the demo's directory: app/, state/, run/")
    args = parser.parse_args(argv)
    os.umask(0o077)
    Watchdog(args.base).run_once()


if __name__ == "__main__":
    main()
