import base64
import http.client
import io
import os
from types import SimpleNamespace
import json
import threading
import time

import pytest

from demo_site import web
from demo_site.sessions import Sessions
from demo_site.web import DemoServer, Handler, FRONTEND
from demo_site.uploads import MAX_UPLOAD_BYTES
from demo_site.worker import WorkerError

# A run's private link token, as the worker makes them, and its owner.
GOOD_LINK, LINKED_OWNER = "A" * 43, "linked-owner-abcdefghijklmnop"
# The routine questions, answered on the upload form, and the header they come in.
ANSWERS = {"condition": "Condition X", "reason": "A rash keeps coming back.", "consent": True}
INTAKE = {"X-PRS-Intake": base64.b64encode(json.dumps(ANSWERS).encode()).decode()}
OCTET = {"Content-Type": "application/octet-stream", **INTAKE}


class FakeWorker:
    def __init__(self):
        self.calls = []
        self.state = {"status": "idle", "busy": False, "owned": False}

    def request(self, op, upload=None, **payload):
        if upload is not None:
            # What the worker would read from the open file it is handed.
            payload["upload"] = (upload.read(), os.fstat(upload.fileno()).st_nlink)
        self.calls.append((op, payload))
        if op == "login":
            if payload["password"] == "test":
                return {"ok": True, "retry_after": 0, "expires_at": time.time() + 3600}
            return {"ok": False, "retry_after": 2, "reason": getattr(self, "login_reason", "incorrect")}
        if op == "start":
            self.state = {"status": "running", "busy": True, "owned": True}
        if op == "end":
            self.state = {"status": "idle", "busy": False, "owned": False}
        if op == "link":
            if payload["token"] != GOOD_LINK:
                raise WorkerError("not_found", "This link has expired or is not valid.")
            return {"owner": LINKED_OWNER, "run_id": "demo-" + "a" * 32}
        return self.state


class MemorySocket:
    """Runs the real HTTP parser/handler without opening a forbidden socket."""
    def __init__(self, raw):
        self.input = io.BytesIO(raw)
        self.output = io.BytesIO()

    def makefile(self, mode, *_args):
        return self.input

    def settimeout(self, _timeout):
        pass

    def sendall(self, data):
        self.output.write(data)


@pytest.fixture
def app(tmp_path):
    worker = FakeWorker()
    server = SimpleNamespace(origin="http://localhost:8765", host="localhost:8765",
                             origins={"localhost:8765": "http://localhost:8765"},
                             sessions=Sessions(tmp_path / "sessions.sqlite"), worker=worker,
                             trust_funnel=False, upload_slots=threading.BoundedSemaphore(2), samples_dir=None,
                             spool_dir=str(tmp_path))
    return server, worker


def raw_request(app, raw):
    conn = MemorySocket(raw)
    Handler(conn, ("127.0.0.1", 12345), app[0])
    response = http.client.HTTPResponse(MemorySocket(conn.output.getvalue()))
    response.begin()
    raw = response.read()
    try:
        body = json.loads(raw)
    except ValueError:
        body = raw
    return response.status, body, dict(response.getheaders())


def request(app, method="GET", path="/api/session", data=None, cookie=None, csrf=None, headers=None):
    server, _ = app
    h = {"Host": server.host, "Origin": server.origin}
    if cookie:
        h["Cookie"] = cookie
    if csrf:
        h["X-CSRF-Token"] = csrf
    if data is not None and not isinstance(data, bytes):
        data = json.dumps(data, ensure_ascii=False).encode()
        h["Content-Type"] = "application/json"
    data = data or b""
    h["Content-Length"] = str(len(data))
    h.update(headers or {})
    raw = f"{method} {path} HTTP/1.0\r\n" + "".join(f"{k}: {v}\r\n" for k, v in h.items()) + "\r\n"
    return raw_request(app, raw.encode() + data)


def login(app):
    status, body, headers = request(app)
    anon = headers["Set-Cookie"].split(";")[0]
    status, body, headers = request(app, "POST", "/api/login", {"password": "test"}, anon, body["csrf"])
    assert status == 200
    return headers["Set-Cookie"].split(";")[0], body["csrf"]


def test_login_cookie_gate_csrf_logout_and_no_cache(app):
    assert request(app, path="/")[0] == 200
    assert request(app, path="/demo.js")[0] == 401
    assert request(app, path="/api/run")[0] == 401
    assert request(app, path="/../harness/run_harness.py")[0] == 404
    status, body, headers = request(app)
    cookie = headers["Set-Cookie"].split(";")[0]
    assert all(flag in headers["Set-Cookie"] for flag in ["Secure", "HttpOnly", "SameSite=Strict", "Path=/"])
    assert "no-store" in headers["Cache-Control"]
    assert request(app, "POST", "/api/login", {"password": "test"}, cookie)[0] == 403
    assert request(app, "POST", "/api/login", {"password": "test"}, cookie, body["csrf"], {"Origin": "https://evil.test"})[0] == 403
    status, bad, _ = request(app, "POST", "/api/login", {"password": "wrong"}, cookie, body["csrf"])
    assert status == 401 and bad["retry_after"] == 2
    assert bad["error"] == "That password is not correct. Please try again."
    # Refused before the password is checked (too many tries): says to wait, not that it is wrong.
    app[1].login_reason = "throttled"
    status, wait, _ = request(app, "POST", "/api/login", {"password": "wrong"}, cookie, body["csrf"])
    assert status == 429 and wait["retry_after"] == 2
    assert "wait" in wait["error"].lower() and "not correct" not in wait["error"]
    app[1].login_reason = "incorrect"
    fresh, csrf = login(app)
    assert request(app, path="/demo.js", cookie=fresh)[0] == 200
    for route, data in [("/api/end", {}), ("/api/logout", {}), ("/api/answer", {"question_id": "q", "text": "yes"})]:
        assert request(app, "POST", route, data, fresh)[0] == 403
    assert request(app, "POST", "/api/logout", {}, fresh, csrf)[0] == 200
    assert request(app, path="/api/run", cookie=fresh)[0] == 401


def test_use_this_sample_starts_a_full_run_on_a_named_sample_no_upload(tmp_path, app):
    server, worker = app
    samples = tmp_path / "samples"
    samples.mkdir()
    (samples / "sample-genome-1.txt").write_bytes(b"# synthetic genome\nrs1\t1\t101\tAA\n")
    server.samples_dir = str(samples)
    cookie, csrf = login(app)

    def use(name, intake=ANSWERS, headers=None):
        h = dict(headers or {})
        if intake is not None:
            h["X-PRS-Intake"] = base64.b64encode(json.dumps(intake).encode()).decode()
        return request(app, "POST", "/api/sample", {"name": name}, cookie, csrf, h)

    idle = lambda: setattr(worker, "state", {"status": "idle", "busy": False, "owned": False})
    # It starts the run on the sample's own bytes — the worker gets the file, not the page.
    status, body, _ = use("sample-genome-1.txt")
    assert status == 201
    call = next(payload for op, payload in worker.calls if op == "start")
    assert set(call) == {"owner", "upload", "intake"}
    # The worker gets the sample file itself to read (a real file, so one link), never its bytes.
    assert call["upload"] == (b"# synthetic genome\nrs1\t1\t101\tAA\n", 1)
    assert call["intake"]["condition"] == "Condition X"
    # The routine answers are still required, and still checked the same way.
    idle(); assert use("sample-genome-1.txt", intake={**ANSWERS, "consent": False})[0] == 400
    idle(); assert use("sample-genome-1.txt", intake=None)[0] == 400
    # Only a real sample, by its plain name; never a path or a missing file.
    idle(); assert use("../demo_site/web.py")[0] == 404
    idle(); assert use("no-such-sample.txt")[0] == 404

    # Busy, full and paused are refused here too, before the sample is opened.
    worker.calls.clear()
    worker.state = {"status": "running", "busy": True, "owned": True}
    assert use("sample-genome-1.txt")[0] == 409
    worker.state = {"status": "idle", "owned": False, "full": True, "slots": 1, "retry_at": 1791990000}
    assert use("sample-genome-1.txt")[:2] == (409, {"error": "All demo slots are in use.", "full": True, "retry_at": 1791990000})
    worker.state = {"status": "idle", "owned": False, "paused": "No analysis can start right now."}
    assert use("sample-genome-1.txt")[:2] == (503, {"error": "No analysis can start right now.", "paused": True})
    assert not any(op == "start" for op, _ in worker.calls)


def test_a_private_link_cannot_start_a_sample_run(app):
    server, worker = app
    status, body, headers = request(app, path="/r/" + GOOD_LINK)
    cookie = headers["Set-Cookie"].split(";")[0]
    status, session, _ = request(app, path="/api/session", cookie=cookie)
    status, body, _ = request(app, "POST", "/api/sample", {"name": "x.txt"}, cookie, session["csrf"],
                              {"X-PRS-Intake": base64.b64encode(json.dumps(ANSWERS).encode()).decode()})
    assert status == 403 and not any(op == "start" for op, _ in worker.calls)


def test_upload_limits_busy_and_filename_not_forwarded(app):
    cookie, csrf = login(app)
    headers = {**OCTET, "Content-Length": str(MAX_UPLOAD_BYTES + 1)}
    assert request(app, "POST", "/api/run", b"", cookie, csrf, headers)[0] == 413
    assert not any(op == "start" for op, _ in app[1].calls)
    assert request(app, "POST", "/api/run", b"genome", cookie, csrf, {**OCTET, "X-Filename": "../../evil"})[0] == 201
    call = next(payload for op, payload in app[1].calls if op == "start")
    # The body as an open file that has no name, never its bytes in the request.
    assert set(call) == {"owner", "upload", "intake"} and call["upload"] == (b"genome", 0)
    assert request(app, "POST", "/api/run", b"genome", cookie, csrf, OCTET)[0] == 409


def test_an_upload_brings_the_answers_to_the_routine_questions_and_the_agreement(app):
    cookie, csrf = login(app)

    def upload(answers=None, raw=None):
        headers = {"Content-Type": "application/octet-stream"}
        if answers is not None:
            headers["X-PRS-Intake"] = base64.b64encode(json.dumps(answers).encode()).decode()
        if raw is not None:
            headers["X-PRS-Intake"] = raw
        return request(app, "POST", "/api/run", b"genome", cookie, csrf, headers)
    status, body, _ = upload()
    assert status == 400 and "form" in body["error"]
    assert upload({**ANSWERS, "consent": False})[:2] == (400, {"error": "Tick the box to agree before starting."})
    for bad in ({"consent": True}, {**ANSWERS, "condition": " "}, {**ANSWERS, "condition": "x" * 201},
                {**ANSWERS, "sex": "other"}, {**ANSWERS, "extra": "x"}):
        assert upload(bad)[0] == 400, bad
    assert upload(raw="not base64!")[0] == 400 and upload(raw="A" * 9000)[0] == 400
    assert not any(op == "start" for op, _ in app[1].calls)
    status, _, _ = upload({**ANSWERS, "reason": " ", "ancestry": "  Mixed heritage ", "sex": "female",
                           "diagnosed": "no", "doctor_note": True})
    assert status == 201
    call = next(payload for op, payload in app[1].calls if op == "start")
    assert call["intake"] == {"condition": "Condition X", "ancestry": "Mixed heritage", "sex": "female",
                              "diagnosed": "no", "doctor_note": True, "consent": True}


def test_a_full_demo_refuses_an_upload_without_keeping_it_and_says_when_to_come_back(app):
    cookie, csrf = login(app)
    app[1].state = {"status": "busy", "busy": True, "owned": False, "full": True, "slots": 2,
                    "retry_at": 1791990000}
    # Answered once what was sent of it is read and dropped: here the declared length never comes.
    headers = {"Content-Type": "application/octet-stream", "Content-Length": str(MAX_UPLOAD_BYTES)}
    status, body, _ = request(app, "POST", "/api/run", b"", cookie, csrf, headers)
    assert (status, body) == (409, {"error": "All demo slots are in use.", "full": True, "retry_at": 1791990000})
    assert not any(op == "start" for op, _ in app[1].calls)


def test_while_no_analysis_can_start_an_upload_is_refused_without_keeping_it_and_says_why(app):
    cookie, csrf = login(app)
    paused = "The demo cannot start a new analysis right now because of a problem on its side. Please try again later."
    app[1].state = {"status": "idle", "busy": False, "owned": False, "paused": paused}
    status, body, _ = request(app, "POST", "/api/run", b"x" * 64, cookie, csrf, OCTET)
    assert (status, body) == (503, {"error": paused, "paused": True})
    assert not any(op == "start" for op, _ in app[1].calls)

    # Paused between the status and the start: the worker's refusal, said as it says it.
    class Refusing(FakeWorker):
        def request(self, op, upload=None, **payload):
            if op == "start":
                raise WorkerError("paused", paused)
            return super().request(op, upload, **payload)
    server = app[0]
    server.worker = Refusing()
    status, body, _ = request((server, server.worker), "POST", "/api/run", b"x" * 64, cookie, csrf, OCTET)
    assert (status, body) == (503, {"error": paused, "paused": True})


def test_an_upload_refused_before_it_is_read_is_read_and_dropped_then_answered(app):
    """Answered while the body is still coming, the connection is reset under the answer and
    the visitor never sees it: so a signed-in visitor's refused upload is read first, and dropped."""
    cookie, csrf = login(app)
    server = app[0]
    genome = os.urandom(2 * 1024 * 1024)
    head = (f"POST /api/run HTTP/1.0\r\nHost: {server.host}\r\nOrigin: {server.origin}\r\nCookie: {cookie}\r\n"
            f"X-CSRF-Token: {csrf}\r\nContent-Type: application/octet-stream\r\n"
            f"Content-Length: {len(genome)}\r\n\r\n").encode()
    class Body(io.BytesIO):
        def close(self):
            self.unread = len(self.getvalue()) - self.tell()
            super().close()

    def unread_after(raw):
        conn = MemorySocket(raw)
        conn.input = Body(raw)
        Handler(conn, ("127.0.0.1", 12345), server)
        response = http.client.HTTPResponse(MemorySocket(conn.output.getvalue()))
        response.begin()
        return conn.input.unread, response.status, json.loads(response.read())["error"]
    for state, error in (({"status": "busy", "busy": True, "owned": False, "full": True, "retry_at": None},
                          "All demo slots are in use."),
                         ({"status": "running", "busy": True, "owned": True}, "End the current demo first.")):
        app[1].state = state
        assert unread_after(head + genome) == (0, 409, error)  # All of it read before the answer.
    # Not signed in: no answer is worth reading a stranger's upload for.
    assert unread_after(head.replace(cookie.encode(), b"x=y") + genome)[:2] == (len(genome), 401)


def test_signed_in_people_can_download_the_sample_genomes_and_nothing_else(app, tmp_path):
    samples = tmp_path / "samples"
    (samples / "sub").mkdir(parents=True)
    genome = os.urandom(200_000)
    (samples / "sample-genome-1.txt").write_bytes(genome)
    (samples / "sample-genome-10.zip").write_bytes(b"PK zip")
    (samples / "sample-genome-2.zip").write_bytes(b"PK zip too")
    (samples / ".hidden").write_bytes(b"x")
    (samples / "empty.txt").write_bytes(b"")
    (tmp_path / "outside.txt").write_bytes(b"private")
    (samples / "link.txt").symlink_to(tmp_path / "outside.txt")
    with open(samples / "too-big.txt", "wb") as fh:
        fh.truncate(MAX_UPLOAD_BYTES + 1)
    app[0].samples_dir = str(samples)
    _, anonymous, headers = request(app)
    assert "samples" not in anonymous
    anon = headers["Set-Cookie"].split(";")[0]
    assert request(app, path="/samples/sample-genome-1.txt", cookie=anon)[0] == 401
    cookie, _ = login(app)
    _, session, _ = request(app, cookie=cookie)
    assert session["samples"] == [{"name": "sample-genome-1.txt", "bytes": 200_000},
                                  {"name": "sample-genome-2.zip", "bytes": 10},
                                  {"name": "sample-genome-10.zip", "bytes": 6}]
    status, body, headers = request(app, path="/samples/sample-genome-1.txt", cookie=cookie)
    assert (status, body) == (200, genome)
    assert headers["Content-Type"] == "application/octet-stream"
    assert headers["Content-Disposition"] == 'attachment; filename="sample-genome-1.txt"'
    assert headers["Content-Length"] == "200000" and "no-store" in headers["Cache-Control"]
    assert headers["X-Content-Type-Options"] == "nosniff"
    for name in ("link.txt", ".hidden", "empty.txt", "too-big.txt", "sub", "missing.txt", "..%2Foutside.txt",
                 "../outside.txt", "sub/x"):
        assert request(app, path="/samples/" + name, cookie=cookie)[0] == 404, name
    app[0].samples_dir = None
    _, session, _ = request(app, cookie=cookie)
    assert session["samples"] == []
    assert request(app, path="/samples/sample-genome-1.txt", cookie=cookie)[0] == 404


def test_expired_session_cannot_read_or_mutate(app):
    cookie, csrf = login(app)
    token = cookie.split("=", 1)[1]
    with app[0].sessions.connect() as db:
        db.execute("UPDATE sessions SET expires=0 WHERE token=?", (app[0].sessions.digest(token),))
    assert request(app, path="/api/run", cookie=cookie)[0] == 401
    assert request(app, "POST", "/api/end", {}, cookie, csrf)[0] == 401
    status, session, _ = request(app, cookie=cookie)
    assert status == 200 and not session["authenticated"]


def test_a_visitor_with_a_demo_going_stays_signed_in_past_the_password_change(app):
    """Sessions end when the daily password changes, at midnight in New York. A visitor whose demo
    is still going then, or whose result is still there to read, stays signed in for that demo:
    its status, its questions, End demo, the page. Not for a new upload or a sample download,
    which need the new password."""
    cookie, csrf = login(app)
    token = cookie.split("=", 1)[1]
    with app[0].sessions.connect() as db:
        db.execute("UPDATE sessions SET expires=1 WHERE token=?", (app[0].sessions.digest(token),))
    octet = {"Content-Type": "application/octet-stream"}
    for status in ("running", "waiting", "completed", "failed"):
        app[1].state = {"status": status, "busy": True, "owned": True}
        assert request(app, path="/api/run", cookie=cookie)[0] == 200, status
        assert request(app, path="/demo.js", cookie=cookie)[0] == 200
        _, session, _ = request(app, cookie=cookie)
        assert session["authenticated"] and session["kept_for_run"]
        assert request(app, "POST", "/api/answer", {"question_id": "q", "text": "yes"}, cookie, csrf)[0] == 200
        assert request(app, "POST", "/api/run", b"genome", cookie, csrf, octet)[0] == 401
    app[1].state = {"status": "running", "busy": True, "owned": True}
    assert request(app, "POST", "/api/end", {}, cookie, csrf)[0] == 200
    # Its demo ended (or its result was purged): signed out, as everyone else is.
    app[1].state = {"status": "idle", "busy": False, "owned": False}
    assert request(app, path="/api/run", cookie=cookie)[0] == 401
    _, session, _ = request(app, cookie=cookie)
    assert not session["authenticated"]


def test_host_and_proxy_client_validation(app):
    assert request(app, headers={"Host": "evil.test"})[0] == 400
    app[0].trust_funnel = True
    _, body, headers = request(app)
    cookie = headers["Set-Cookie"].split(";")[0]
    request(app, "POST", "/api/login", {"password": "test"}, cookie, body["csrf"], {"X-Forwarded-For": "203.0.113.7"})
    assert app[1].calls[-1][1]["client"] == "203.0.113.7"


def test_each_host_takes_only_its_own_origin(app, tmp_path):
    public = {"Host": "demo.example.org", "Origin": "https://demo.example.org"}
    app[0].origins["demo.example.org"] = "https://demo.example.org"
    status, body, headers = request(app, headers=public)
    assert status == 200 and headers["Strict-Transport-Security"] == "max-age=31536000"
    cookie = headers["Set-Cookie"].split(";")[0]
    # The other host's origin is not this host's: refused like any cross-site POST.
    crossed = dict(public, Origin="http://localhost:8765")
    assert request(app, "POST", "/api/login", {"password": "test"}, cookie, body["csrf"], crossed)[0] == 403
    status, signed_in, _ = request(app, "POST", "/api/login", {"password": "test"}, cookie, body["csrf"], public)
    assert status == 200 and signed_in["authenticated"]
    assert request(app)[0] == 200
    assert request(app, headers={"Host": "other.example.org"})[0] == 400
    sessions = Sessions(tmp_path / "s.sqlite")
    for bad in ([], ["https://demo.example.org", "https://demo.example.org"], ["http://demo.example.org"],
                ["https://demo.example.org/path"]):
        with pytest.raises(ValueError):
            DemoServer(0, bad, sessions, FakeWorker())


def test_referrer_policy_lets_browsers_send_the_origin(app):
    # Under no-referrer, Firefox (as the Fetch standard says) sends Origin: null on a
    # same-origin POST, which check_csrf refuses: nobody on Firefox could sign in.
    _, _, headers = request(app)
    assert headers["Referrer-Policy"] == "same-origin"
    for page in ("login.html", "demo.html"):
        html = (FRONTEND / page).read_text()
        assert '<meta name="referrer" content="same-origin">' in html
        assert "no-referrer" not in html


def test_an_upload_is_received_into_a_nameless_file_at_most_two_at_a_time(app, tmp_path, monkeypatch):
    cookie, csrf = login(app)
    octet = OCTET
    genome = os.urandom(3 * 1024 * 1024 + 7)
    assert request(app, "POST", "/api/run", genome, cookie, csrf, octet)[0] == 201
    assert next(p for op, p in app[1].calls if op == "start")["upload"] == (genome, 0)
    assert list(tmp_path.iterdir()) == [tmp_path / "sessions.sqlite"]  # Nothing left of it here.
    app[1].state = {"status": "idle", "busy": False, "owned": False}
    # Two uploads may be received at once; a third waits for one of them.
    for _ in range(2):
        app[0].upload_slots.acquire()
    assert request(app, "POST", "/api/run", genome, cookie, csrf, octet)[:2] == (409, {"error": "The demo is busy."})
    for _ in range(2):
        app[0].upload_slots.release()
    # Short of room for it: refused before a byte of it is read.
    usage = web.shutil.disk_usage(tmp_path)._replace(free=len(genome) + web.UPLOAD_HEADROOM - 1)
    monkeypatch.setattr(web.shutil, "disk_usage", lambda path: usage)
    calls = len(app[1].calls)
    status, body, _ = request(app, "POST", "/api/run", genome, cookie, csrf, octet)
    assert (status, body["error"]) == (507, "The demo server is short of disk space. Please try again later.")
    assert [op for op, _ in app[1].calls[calls:]] == ["status"]


def test_an_upload_gets_the_time_its_size_needs_and_no_more(app, monkeypatch):
    cookie, csrf = login(app)
    genome = os.urandom(1024 * 1024)
    clock = iter(range(0, 10**6, 30))  # Each read of the web's clock 30 s on.
    monkeypatch.setattr(web, "time", SimpleNamespace(monotonic=lambda: next(clock), time=time.time))
    monkeypatch.setattr(web, "UPLOAD_RATE", len(genome) / 30)  # 60 s + 30 s for this one.
    reads = []

    class Slow(io.BytesIO):
        def read1(self, size=-1):
            reads.append(size)
            return super().read1(min(size, 256 * 1024))
    server = app[0]
    raw = (f"POST /api/run HTTP/1.0\r\nHost: {server.host}\r\nOrigin: {server.origin}\r\nCookie: {cookie}\r\n"
           f"X-CSRF-Token: {csrf}\r\nContent-Type: application/octet-stream\r\nX-PRS-Intake: {INTAKE['X-PRS-Intake']}\r\n"
           f"Content-Length: {len(genome)}\r\n\r\n").encode() + genome
    conn = MemorySocket(raw)
    conn.input = Slow(raw)
    Handler(conn, ("127.0.0.1", 12345), server)
    response = http.client.HTTPResponse(MemorySocket(conn.output.getvalue()))
    response.begin()
    assert response.status == 408 and len(reads) == 3
    assert not any(op == "start" for op, _ in app[1].calls)


def test_partial_upload_never_starts_worker(app):
    cookie, csrf = login(app)
    server = app[0]
    raw = (f"POST /api/run HTTP/1.0\r\nHost: {server.host}\r\nOrigin: {server.origin}\r\nCookie: {cookie}\r\n"
           f"X-CSRF-Token: {csrf}\r\nContent-Type: application/octet-stream\r\nX-PRS-Intake: {INTAKE['X-PRS-Intake']}\r\n"
           "Content-Length: 100\r\n\r\nshort")
    assert raw_request(app, raw.encode())[0] == 400
    assert not any(op == "start" for op, _ in app[1].calls)


def test_malformed_framing_csrf_and_unicode_answer(app):
    cookie, csrf = login(app)
    assert request(app, "POST", "/api/end", {}, cookie, "é")[0] == 403
    assert request(app, "POST", "/api/end", {}, cookie, csrf, {"Transfer-Encoding": ""})[0] == 400
    answer = "家" * 8000
    assert request(app, "POST", "/api/answer", {"question_id": "q", "text": answer}, cookie, csrf)[0] == 200
    assert app[1].calls[-1][1]["text"] == answer


def test_real_loopback_listener_when_permitted(tmp_path):
    try:
        server = DemoServer(0, "http://localhost:8765", Sessions(tmp_path / "sessions.sqlite"), FakeWorker())
    except PermissionError:
        pytest.skip("sandbox prohibits creating sockets; HTTP parser tests run in memory")
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        assert server.server_address[0] == "127.0.0.1"
        conn = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=3)
        conn.request("GET", "/", headers={"Host": server.host})
        response = conn.getresponse()
        assert response.status == 200
        assert b"prs-agent" in response.read()
        conn.close()
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def test_a_private_link_opens_its_run_without_the_password(app):
    _, worker = app
    status, body, headers = request(app, path="/r/" + GOOD_LINK)
    assert status == 200
    # Opened from another site, a SameSite=Strict cookie would not ride a redirect: the page
    # sets it, then goes on to the workspace from this site.
    assert b'http-equiv="refresh" content="0;url=/"' in body
    assert all(flag in headers["Set-Cookie"] for flag in ["Secure", "HttpOnly", "SameSite=Strict", "Path=/"])
    cookie = headers["Set-Cookie"].split(";")[0]
    status, session, _ = request(app, cookie=cookie)
    assert (status, session["authenticated"], session["via_link"]) == (200, True, True)
    assert b"/demo.js" in request(app, path="/", cookie=cookie)[1]
    worker.calls.clear()
    assert request(app, path="/api/run", cookie=cookie)[0] == 200
    assert worker.calls[-1] == ("status", {"owner": LINKED_OWNER})
    # It opens one run: no new upload, no samples; those need today's password.
    status, body, _ = request(app, "POST", "/api/run", b"genome", cookie, session["csrf"],
                              headers={"Content-Type": "application/octet-stream"})
    # The password, which may be one fixed password now: not "today's".
    assert status == 403 and "password" in body["error"] and "today" not in body["error"]
    assert not any(op == "start" for op, _ in worker.calls)
    status, body, _ = request(app, path="/samples/sample-genome-1.txt", cookie=cookie)
    assert status == 403 and "password" in body["error"] and "today" not in body["error"]
    # A password session is not a link's.
    password, _ = login(app)
    assert request(app, cookie=password)[1]["via_link"] is False
    # A token no run has, or not a token at all, opens nothing and sets nothing.
    for bad in ("/r/" + "B" * 43, "/r/short", "/r/" + "A" * 42 + "!", "/r/"):
        status, body, headers = request(app, path=bad)
        assert status == 404 and "Set-Cookie" not in headers, bad
        assert b"expired or is not valid" in body, bad

