"""Whole private-demo flow through the HTTP parser, worker RPC and real live router.

No network listener, model, real harness run, real genome or provider credentials.
"""
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import time
from types import SimpleNamespace

from demo_site.sessions import Sessions
from demo_site.worker import Runs, WorkerServer
from test_demo_web import INTAKE, login, request
from test_demo_worker import GroupProcess


REPO = Path(__file__).resolve().parents[1]
GENOME = b"# synthetic genome\nrs1\t1\t101\tAA\n"


def test_http_worker_live_agent_report_and_end(tmp_path, monkeypatch):
    import demo_site.worker
    monkeypatch.setattr(demo_site.worker, "MIN_FREE_BYTES", 0)  # free disk is the host's
    gate = tmp_path / "synthetic-gate.json"
    gate.write_text(json.dumps({"hard": ["SYNTHETIC_PRIVATE"],
                               "provider_hard": ["SYNTHETIC_PROVIDER"],
                               "deny_eval_run": "synthetic-private-run",
                               "prior_run": "synthetic-prior-run"}))

    def launch(record, upload):
        return GroupProcess(subprocess.Popen(
            [sys.executable, str(REPO / "tests/fixtures/demo_fake_harness.py"),
             str(upload.parent), record["run_id"]],
            env=dict(os.environ, PRS_HARNESS_TOKENS=str(gate)),
            start_new_session=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL))

    runs = Runs(tmp_path / "private", launcher=launch)
    # The run's working files are read below, after it ends: their deletion then is held here,
    # and asked for (the worker tests cover the deletion itself).
    cleaned = []
    runs._clean_working = cleaned.append
    try:
        # Call the production RPC allowlist directly; only its Unix transport is omitted.
        rpc = WorkerServer.__new__(WorkerServer)
        rpc.runs = runs
        rpc.password_service = SimpleNamespace(login=lambda password, _client: {
            "ok": password == "test", "retry_after": 1, "expires_at": time.time() + 3600})

        class DirectWorker:
            def request(self, op, upload=None, **payload):
                # As the socket does: the worker gets a descriptor of its own for the open file.
                given = os.fdopen(os.dup(upload.fileno()), "rb") if upload else None
                return rpc.dispatch({"op": op, **payload}, given)

        bridge = DirectWorker()
        server = SimpleNamespace(origin="http://localhost:8765", host="localhost:8765",
                                 origins={"localhost:8765": "http://localhost:8765"},
                                 sessions=Sessions(tmp_path / "sessions.sqlite"), worker=bridge,
                                 trust_funnel=False, upload_slots=threading.BoundedSemaphore(2), samples_dir=None,
                                 spool_dir=str(tmp_path))
        app = server, bridge
        cookie, csrf = login(app)
        other_cookie, other_csrf = login(app)
        code, session, _ = request(app, cookie=cookie)
        assert code == 200 and session["authenticated"]
        code, running, headers = request(app, "POST", "/api/run", GENOME, cookie, csrf,
                                         {"Content-Type": "application/octet-stream", **INTAKE})
        assert code == 201 and running["owned"] and running["status"] == "running"
        assert "no-store" in headers["Cache-Control"]
        _, busy, _ = request(app, path="/api/run", cookie=other_cookie)
        assert busy == {"status": "busy", "busy": True, "owned": False, "full": True, "slots": 1, "retry_at": None}

        # Restart only the web layer: its in-memory locks disappear, while the
        # server-side session database and restricted worker survive unchanged.
        owner = server.sessions.get(cookie.split("=", 1)[1])["owner"]
        server = SimpleNamespace(origin="http://localhost:8765", host="localhost:8765",
                                 origins={"localhost:8765": "http://localhost:8765"},
                                 sessions=Sessions(tmp_path / "sessions.sqlite"), worker=bridge,
                                 trust_funnel=False, upload_slots=threading.BoundedSemaphore(2), samples_dir=None,
                                 spool_dir=str(tmp_path))
        app = server, bridge
        code, restored, _ = request(app, cookie=cookie)
        assert code == 200 and restored["authenticated"]
        assert restored["csrf"] == csrf and restored["expires_at"] == session["expires_at"]
        assert server.sessions.get(cookie.split("=", 1)[1])["owner"] == owner
        code, resumed, _ = request(app, path="/api/run", cookie=cookie)
        assert code == 200 and resumed["owned"]
        assert resumed["run_id"] == running["run_id"]
        assert resumed["status"] in {"running", "waiting"}
        code, busy, _ = request(app, path="/api/run", cookie=other_cookie)
        assert code == 200 and busy == {"status": "busy", "busy": True, "owned": False, "full": True,
                                        "slots": 1, "retry_at": None}
        code, _, _ = request(app, "POST", "/api/run", GENOME, other_cookie, other_csrf,
                              {"Content-Type": "application/octet-stream"})
        assert code == 409
        assert len(runs.records) == 1

        deadline = time.monotonic() + 15

        def wait_for(statuses):
            while time.monotonic() < deadline:
                code, status, _ = request(app, path="/api/run", cookie=cookie)
                assert code == 200
                if status["status"] in statuses:
                    return status
                time.sleep(0.025)
            raise AssertionError("synthetic demo did not reach expected state")

        waiting = wait_for({"waiting", "failed"})
        assert waiting["status"] == "waiting"
        question = waiting["question"]
        assert question["text"] == "How would you describe your ancestry?"
        answer = "My family is from the Levant."
        code, acknowledged, _ = request(app, "POST", "/api/answer",
                                        {"question_id": question["id"], "text": answer}, cookie, csrf)
        assert code == 200 and acknowledged["status"] in {"running", "completed"}
        final = wait_for({"completed", "failed"})
        assert final["status"] == "completed"
        report = final["report"]["bundle"]["report"]
        assert report["stage"] == "report"
        assert report["decision"] == "The synthetic transport check finished."
        assert final["progress"][-1]["kind"] == "run_completed"

        # Send the actual API contract to the production browser renderer, as data.
        script = """
import { renderDemo } from './frontend/demo.js';
let raw = '';
for await (const chunk of process.stdin) raw += chunk;
const { session, waiting, final, busy } = JSON.parse(raw);
const view = run => renderDemo({ ready: true, session, run });
process.stdout.write(JSON.stringify([view(waiting), view(final), view(busy)]));
"""
        rendered = subprocess.run(["node", "--input-type=module", "-e", script],
            input=json.dumps({"session": session, "waiting": waiting, "final": final, "busy": busy}),
            text=True, capture_output=True, check=True, timeout=5, cwd=REPO)
        question_html, report_html, busy_html = json.loads(rendered.stdout)
        assert question["text"] in question_html and 'id="answer-form"' in question_html
        # The report stage's decision is its internal note: the page never shows it.
        assert report["decision"] not in report_html and 'data-action="end"' in report_html
        assert "One analysis at a time" in busy_html and 'id="upload-form"' not in busy_html

        root = runs._path(running["run_id"])
        assert (root / "agent-received.txt").read_text().strip() == answer
        manifest = json.loads((root / "harness/vault/runs" / running["run_id"] / "manifest.json").read_text())
        assert manifest["published"]["state"] == "demo" and manifest["published"]["dest"] is None
        assert not (REPO / "results/adhoc" / running["run_id"]).exists()
        assert cleaned == [running["run_id"]]
        code, ended, _ = request(app, "POST", "/api/end", {}, cookie, csrf)
        assert code == 200 and ended["status"] == "idle"
        assert not root.exists()
    finally:
        runs.close()
