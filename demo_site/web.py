"""Small authenticated HTTP boundary. The only listener is IPv4 loopback."""
import argparse
from http import cookies
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import hmac
import ipaddress
import json
import mimetypes
import os
from pathlib import Path
import re
import shutil
import socket
import stat
import tempfile
import threading
import time
from urllib.parse import urlsplit

from . import intake
from .sessions import Sessions
from .uploads import MAX_UPLOAD_BYTES, MAX_EXPANDED_BYTES
from .worker import BoundedThreads, WorkerClient, WorkerError

COOKIE = "__Host-prs_demo"
FRONTEND = Path(__file__).resolve().parent.parent / "frontend"
PUBLIC = {"login.js", "styles.css", "demo.css", "mark.svg"}
PRIVATE = {"demo.js", "contracts.js", "session.js", "exploration.js", "notebook.js"}
# A run's private link, /r/<token>, opens a session for that run's owner without the daily
# password: its progress and result, its questions and End, never a new upload or a sample. It
# lasts as long as a run can (its time limit) and its result is kept (24 hours), with room.
LINK = 2
LINK_SECONDS = 30 * 3600
LINK_RE = re.compile(r"[A-Za-z0-9_-]{20,64}\Z")
LINK_PAGE = (b'<!doctype html><html lang="en"><head><meta charset="utf-8">'
             b'<meta name="viewport" content="width=device-width, initial-scale=1">'
             b'<meta name="referrer" content="no-referrer"><meta http-equiv="refresh" content="0;url=/">'
             b'<title>Opening your result \xc2\xb7 prs-agent</title></head>'
             b'<body><p>Opening your result\xe2\x80\xa6 <a href="/">Continue</a></p></body></html>')
EXPIRED_PAGE = (b'<!doctype html><html lang="en"><head><meta charset="utf-8">'
                b'<meta name="viewport" content="width=device-width, initial-scale=1">'
                b'<meta name="referrer" content="no-referrer"><title>Link expired \xc2\xb7 prs-agent</title></head>'
                b'<body><p>This private link has expired or is not valid. A result is kept for 24 hours '
                b'after its run ends, then deleted.</p><p><a href="/">Go to the demo</a></p></body></html>')
SAMPLE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,99}\Z")
# An upload may pause this long between two reads, and must come at UPLOAD_RATE on average: a
# slow link gets the time its file needs, and a stalled or dripping one is let go.
UPLOAD_STALL_SECONDS = 60
UPLOAD_RATE = 128 * 1024  # bytes a second, 1 Mbit/s
# Room left beside an upload being received, for everything else on the disk.
UPLOAD_HEADROOM = 1024 * 1024 * 1024


def list_samples(directory):
    """The sample genomes signed-in people may download: regular files directly in directory,
    plainly named, no larger than an upload may be, in name order with numbers as numbers."""
    found = []
    try:
        entries = list(os.scandir(directory)) if directory else []
    except OSError:
        return []
    for entry in entries:
        try:
            if SAMPLE.match(entry.name) and entry.is_file(follow_symlinks=False):
                size = entry.stat(follow_symlinks=False).st_size
                if 0 < size <= MAX_UPLOAD_BYTES:
                    found.append({"name": entry.name, "bytes": size})
        except OSError:
            continue
    return sorted(found, key=lambda s: [int(p) if p.isdigit() else p for p in re.split(r"(\d+)", s["name"])])


class HTTPError(Exception):
    def __init__(self, status, message):
        self.status, self.message = status, message


class DemoServer(BoundedThreads, ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, port, origins, sessions, worker, trust_funnel=False, samples_dir=None,
                 spool_dir=None):
        # One origin per host: a request must name one of these hosts, and a POST must come
        # from that same host's origin. The first is the one this server reports.
        self.origins = {}
        for origin in [origins] if isinstance(origins, str) else origins:
            parsed = urlsplit(origin)
            if parsed.path or parsed.query or parsed.fragment or parsed.username:
                raise ValueError("origin must contain only scheme, hostname and optional port")
            if parsed.scheme != "https" and not (parsed.scheme == "http" and parsed.hostname == "localhost"):
                raise ValueError("use HTTPS public origin or http://localhost for local development")
            if parsed.netloc in self.origins:
                raise ValueError("two origins for host %s" % parsed.netloc)
            self.origins[parsed.netloc] = origin
        if not self.origins:
            raise ValueError("at least one origin is required")
        self.host, self.origin = next(iter(self.origins.items()))
        self.sessions, self.worker = sessions, worker
        self.trust_funnel = trust_funnel
        self.samples_dir = samples_dir
        # Where an upload is received, into a file that never has a name (tempfile's O_TMPFILE).
        self.spool_dir = spool_dir or tempfile.gettempdir()
        self.slots = threading.BoundedSemaphore(16)
        self.upload_slots = threading.BoundedSemaphore(2)
        super().__init__(("127.0.0.1", port), Handler)


class Handler(BaseHTTPRequestHandler):
    server_version = "prs-demo"
    sys_version = ""
    protocol_version = "HTTP/1.0"  # One request per connection; reject ambiguous framing.

    def setup(self):
        super().setup()
        self.connection.settimeout(15)
        self.header_deadline = threading.Timer(15, self._expire_headers)
        self.header_deadline.daemon = True
        self.header_deadline.start()

    def _expire_headers(self):
        # A socket idle timeout alone permits an endless drip of header bytes.
        try:
            self.connection.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass

    def finish(self):
        self.header_deadline.cancel()
        super().finish()

    def log_message(self, *_args):
        pass  # Never log upload names, tokens, typed answers, passwords or reports.

    def send_error(self, code, message=None, explain=None):
        self.respond(code, {"error": "Request rejected."})

    def respond(self, status, value, content_type="application/json; charset=utf-8", cookie=None, retry_after=None):
        body = json.dumps(value).encode() if isinstance(value, dict) else value
        self.send_response(status)
        self.common_headers(content_type, len(body))
        if cookie:
            self.send_header("Set-Cookie", f"{COOKIE}={cookie}; Path=/; Secure; HttpOnly; SameSite=Strict")
        if retry_after:
            self.send_header("Retry-After", str(retry_after))
        self.end_headers()
        self.wfile.write(body)
        self.close_connection = True

    def common_headers(self, content_type, length):
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(length))
        self.send_header("Cache-Control", "no-store, private, max-age=0")
        self.send_header("Pragma", "no-cache")
        self.send_header("Expires", "0")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        # Not no-referrer: under it Firefox, as the Fetch standard says, sends Origin: null on a
        # same-origin POST, and check_csrf refuses every sign-in.
        self.send_header("Referrer-Policy", "same-origin")
        self.send_header("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
        self.send_header("Content-Security-Policy", "default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self'; connect-src 'self'; font-src 'self'; base-uri 'none'; form-action 'self'; frame-ancestors 'none'; object-src 'none'")
        if any(origin.startswith("https:") for origin in self.server.origins.values()):
            self.send_header("Strict-Transport-Security", "max-age=31536000")

    def open_sample(self, name):
        """One sample genome opened read-only for reading, by its plain name directly in the
        samples directory: never a path, a symlink or anything but a regular file of a size an
        upload may be. HTTPError(404) if there is no such sample."""
        if not self.server.samples_dir or not isinstance(name, str) or not SAMPLE.match(name):
            raise HTTPError(404, "Not found.")
        try:
            directory = os.open(self.server.samples_dir, os.O_RDONLY | os.O_DIRECTORY)
            try:
                fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
            finally:
                os.close(directory)
        except OSError:
            raise HTTPError(404, "Not found.") from None
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or not 0 < info.st_size <= MAX_UPLOAD_BYTES:
            os.close(fd)
            raise HTTPError(404, "Not found.")
        return os.fdopen(fd, "rb"), info

    def send_sample(self, name):
        """One sample genome as a download, a block at a time: a 15 MB file crossing a slow
        link takes longer than one write may."""
        fh, info = self.open_sample(name)
        with fh:
            self.send_response(200)
            self.common_headers("application/octet-stream", info.st_size)
            self.send_header("Content-Disposition", 'attachment; filename="%s"' % name)
            self.end_headers()
            self.close_connection = True
            self.connection.settimeout(60)
            try:
                remaining = info.st_size
                while remaining:
                    block = fh.read(min(65536, remaining))
                    if not block:
                        break  # Shrunk while sent: the connection closes short, a failed download.
                    self.wfile.write(block)
                    remaining -= len(block)
            except OSError:
                pass  # The connection is gone; nothing more can be said on it.

    def token(self):
        try:
            jar = cookies.SimpleCookie(self.headers.get("Cookie", ""))
            return jar[COOKIE].value if COOKIE in jar else ""
        except cookies.CookieError:
            return ""

    def session(self, required=False, for_demo=True):
        """The visitor's session. A sign-in lasts 24 hours; one that has ended is still its
        visitor's, with for_demo, while their demo is going or its result is still there to read:
        its status, its questions, End demo and the page, never a new upload or a sample, which
        need the password again."""
        token = self.token()
        session = self.server.sessions.get(token)
        if session is None and for_demo:
            prior = self.server.sessions.get(token, expired=True)
            if prior and prior["authenticated"] and self.demo_going(prior["owner"]):
                session = dict(prior, kept_for_run=True)
        if required and (not session or not session["authenticated"]):
            raise HTTPError(401, "Please log in again.")
        return session

    def demo_going(self, owner):
        """Whether this owner has a demo going, or one that ended and is not yet purged: the
        worker's say, asked only for a session past its end."""
        try:
            state = self.server.worker.request("status", owner=owner)
        except WorkerError:
            return False
        return bool(state.get("owned")) and state.get("status") in ("running", "waiting", "completed", "failed")

    def check_host(self):
        hosts = self.headers.get_all("Host") or []
        if len(hosts) != 1 or hosts[0] not in self.server.origins:
            raise HTTPError(400, "Invalid host.")
        self.origin = self.server.origins[hosts[0]]

    def check_csrf(self, session):
        token = self.headers.get("X-CSRF-Token", "")
        if (self.headers.get("Origin") != self.origin or not session
                or len(token) > 128 or not hmac.compare_digest(token.encode(), session["csrf"].encode())):
            raise HTTPError(403, "Please reload the page and try again.")

    def read_body(self, limit):
        lengths = self.headers.get_all("Content-Length") or []
        if self.headers.get_all("Transfer-Encoding") is not None or len(lengths) != 1 or not lengths[0].isdigit():
            raise HTTPError(400, "A single Content-Length is required.")
        size = int(lengths[0])
        if size > limit:
            raise HTTPError(413, "The file or request exceeds the size limit.")
        chunks, remaining, deadline = [], size, time.monotonic() + 60
        while remaining:
            if time.monotonic() > deadline:
                raise HTTPError(408, "Upload timed out.")
            block = self.rfile.read1(min(65536, remaining))
            if not block:
                raise HTTPError(400, "Incomplete upload.")
            chunks.append(block)
            remaining -= len(block)
        return b"".join(chunks)  # Incomplete uploads never reach disk or the worker.

    def drop_body(self):
        """Read and drop the body of an upload refused before it was read, then answer. Answered
        while the body is still coming, the connection is reset under the answer, by this server
        closing it or by a proxy between, and the visitor never sees why. Only for a signed-in
        visitor's upload, within the time its size needs; never past the size limit."""
        lengths = self.headers.get_all("Content-Length") or []
        if len(lengths) != 1 or not lengths[0].isdigit() or int(lengths[0]) > MAX_UPLOAD_BYTES:
            return
        remaining, deadline = int(lengths[0]), time.monotonic() + 60 + int(lengths[0]) / UPLOAD_RATE
        try:
            self.connection.settimeout(UPLOAD_STALL_SECONDS)
            while remaining and time.monotonic() < deadline:
                block = self.rfile.read1(min(1024 * 1024, remaining))
                if not block:
                    break
                remaining -= len(block)
        except OSError:
            pass

    def receive_upload(self, limit):
        """The body, an uploaded file however large, received into a file that has no name, so
        nothing is left of it once it is closed, by this process or the worker it goes to. A
        block at a time, as it arrives; never whole in memory."""
        lengths = self.headers.get_all("Content-Length") or []
        if self.headers.get_all("Transfer-Encoding") is not None or len(lengths) != 1 or not lengths[0].isdigit():
            raise HTTPError(400, "A single Content-Length is required.")
        size = int(lengths[0])
        if size > limit:
            raise HTTPError(413, "The file or request exceeds the size limit.")
        if not size:
            raise HTTPError(400, "Choose a genome file to upload.")
        if shutil.disk_usage(self.server.spool_dir).free < size + UPLOAD_HEADROOM:
            self.drop_body()
            raise HTTPError(507, "The demo server is short of disk space. Please try again later.")
        spool = tempfile.TemporaryFile(dir=self.server.spool_dir)
        try:
            remaining, deadline = size, time.monotonic() + 60 + size / UPLOAD_RATE
            self.connection.settimeout(UPLOAD_STALL_SECONDS)
            while remaining:
                if time.monotonic() > deadline:
                    raise HTTPError(408, "Upload timed out.")
                block = self.rfile.read1(min(1024 * 1024, remaining))
                if not block:
                    raise HTTPError(400, "Incomplete upload.")
                spool.write(block)
                remaining -= len(block)
            spool.flush()
            spool.seek(0)
            return spool
        except BaseException:
            spool.close()  # An incomplete upload never reaches the worker.
            raise

    def client_key(self):
        peer = self.client_address[0]
        if self.server.trust_funnel and ipaddress.ip_address(peer).is_loopback:
            forwarded = self.headers.get("X-Forwarded-For", "")
            try:
                # Tailscale overwrites this with the source IP. Never accept a list.
                return str(ipaddress.ip_address(forwarded))
            except ValueError:
                pass
        return peer

    def do_GET(self):
        self.dispatch(False)

    def do_POST(self):
        self.dispatch(True)

    def dispatch(self, post):
        self.header_deadline.cancel()
        try:
            self.check_host()
            if "?" in self.path or "#" in self.path:
                raise HTTPError(404, "Not found.")
            if post:
                return self.post()
            if self.path == "/api/session":
                session = self.session()
                cookie = None
                if not session:
                    prior = self.server.sessions.get(self.token(), expired=True)
                    cookie, session = self.server.sessions.create(owner=prior["owner"] if prior else None, previous=self.token())
                body = {"authenticated": bool(session["authenticated"]), "csrf": session["csrf"],
                        "expires_at": session["expires"], "kept_for_run": bool(session.get("kept_for_run")),
                        "via_link": session["authenticated"] == LINK,
                        "limits": {"upload_bytes": MAX_UPLOAD_BYTES, "expanded_bytes": MAX_EXPANDED_BYTES}}
                if session["authenticated"]:
                    body["samples"] = list_samples(self.server.samples_dir)
                return self.respond(200, body, cookie=cookie)
            if self.path.startswith("/r/"):
                token = self.path[len("/r/"):]
                try:
                    if not LINK_RE.fullmatch(token):
                        raise WorkerError("not_found", "This link has expired or is not valid.")
                    run = self.server.worker.request("link", token=token)
                except WorkerError:
                    return self.respond(404, EXPIRED_PAGE, "text/html; charset=utf-8")
                cookie, _ = self.server.sessions.create(expires=time.time() + LINK_SECONDS, owner=run["owner"],
                                                        authenticated=LINK)
                return self.respond(200, LINK_PAGE, "text/html; charset=utf-8", cookie=cookie)
            if self.path.startswith("/samples/"):
                if self.session(required=True, for_demo=False)["authenticated"] == LINK:
                    raise HTTPError(403, "Samples need the password.")
                return self.send_sample(self.path[len("/samples/"):])
            if self.path == "/api/run":
                session = self.session(required=True)
                return self.respond(200, self.server.worker.request("status", owner=session["owner"]))
            if self.path == "/":
                session = self.session()
                name = "demo.html" if session and session["authenticated"] else "login.html"
            else:
                name = self.path[1:]
                if name in PRIVATE:
                    self.session(required=True)
                elif name not in PUBLIC:
                    raise HTTPError(404, "Not found.")
            data = (FRONTEND / name).read_bytes()
            mime = mimetypes.guess_type(name)[0] or "application/octet-stream"
            return self.respond(200, data, mime + ("; charset=utf-8" if mime.startswith("text/") or mime == "application/javascript" else ""))
        except HTTPError as exc:
            self.respond(exc.status, {"error": exc.message})
        except WorkerError as exc:
            status = {"busy": 409, "full": 409, "conflict": 409, "upload": 400, "invalid": 400, "not_found": 404, "forbidden": 403, "too_large": 413, "space": 507, "paused": 503}.get(exc.code, 503)
            self.respond(status, {"error": str(exc) if status != 503 or exc.code == "paused" else "The demo worker is unavailable. Please try again.",
                                  **({"full": True} if exc.code == "full" else {}), **({"paused": True} if exc.code == "paused" else {})})
        except (socket.timeout, TimeoutError):
            self.respond(408, {"error": "Request timed out."})
        except (OSError, ValueError):
            self.respond(503, {"error": "The demo service is unavailable. Please try again."})

    def post(self):
        # A new upload or sample run needs a session that has not ended: a sign-in lasts 24 hours.
        starts_a_run = self.path in ("/api/run", "/api/sample")
        session = self.session(required=self.path != "/api/login", for_demo=not starts_a_run)
        self.check_csrf(session)
        if starts_a_run and session["authenticated"] == LINK:
            if self.path == "/api/run":
                self.drop_body()
            raise HTTPError(403, "A private link opens one run. Sign in with the password to start another.")
        if self.path == "/api/sample":
            # A full run on a supplied sample, the same as an upload but with no file to send:
            # the phone case, where downloading a sample and choosing it back is the hard part.
            # The body names the sample; the answers come in the header, as an upload's do.
            if self.headers.get("Content-Type") != "application/json":
                raise HTTPError(415, "JSON required.")
            data = json.loads(self.read_body(65536))
            if not isinstance(data, dict) or set(data) != {"name"}:
                raise HTTPError(400, "Name one sample to run.")
            current = self.server.worker.request("status", owner=session["owner"])
            if current.get("owned") and current.get("status") in ("running", "waiting"):
                raise HTTPError(409, "End the current demo first.")
            if current.get("full"):
                return self.respond(409, {"error": "All demo slots are in use.", "full": True,
                                          "retry_at": current.get("retry_at")})
            if current.get("paused"):
                return self.respond(503, {"error": current["paused"], "paused": True})
            try:
                answers = intake.from_header(self.headers.get(intake.HEADER))
            except intake.IntakeError as exc:
                raise HTTPError(400, str(exc))
            upload, _ = self.open_sample(data["name"])
            with upload:
                self.session(required=True, for_demo=False)
                result = self.server.worker.request("start", upload=upload, owner=session["owner"],
                                                    intake=answers)
            return self.respond(201, result)
        if self.path == "/api/run":
            if self.headers.get("Content-Type") != "application/octet-stream":
                raise HTTPError(415, "Upload one file as application/octet-stream.")
            if not self.server.upload_slots.acquire(blocking=False):
                self.drop_body()
                raise HTTPError(409, "The demo is busy.")
            try:
                current = self.server.worker.request("status", owner=session["owner"])
                if current.get("owned") and current.get("status") in ("running", "waiting"):
                    self.drop_body()
                    raise HTTPError(409, "End the current demo first.")
                if current.get("full"):
                    # Never kept: when a slot should be free, for the page to say.
                    self.drop_body()
                    return self.respond(409, {"error": "All demo slots are in use.", "full": True,
                                              "retry_at": current.get("retry_at")})
                if current.get("paused"):
                    # No analysis can start (the worker's sandbox_problem): the file is not kept.
                    self.drop_body()
                    return self.respond(503, {"error": current["paused"], "paused": True})
                # The person's answers to the routine questions, and their agreement, come with
                # the file: no run starts without them.
                try:
                    answers = intake.from_header(self.headers.get(intake.HEADER))
                except intake.IntakeError as exc:
                    self.drop_body()
                    raise HTTPError(400, str(exc))
                with self.receive_upload(MAX_UPLOAD_BYTES) as upload:
                    # Do not accept state-changing work if a session expired during upload.
                    self.session(required=True, for_demo=False)
                    # The worker gets the open file, never its bytes through the socket.
                    result = self.server.worker.request("start", upload=upload, owner=session["owner"],
                                                        intake=answers)
                return self.respond(201, result)
            finally:
                self.server.upload_slots.release()
        if self.path not in {"/api/login", "/api/logout", "/api/answer", "/api/end"}:
            raise HTTPError(404, "Not found.")
        if self.headers.get("Content-Type") != "application/json":
            raise HTTPError(415, "JSON required.")
        try:
            data = json.loads(self.read_body(65536))
        except (ValueError, UnicodeError):
            raise HTTPError(400, "Invalid request.")
        if not isinstance(data, dict):
            raise HTTPError(400, "Invalid request.")
        if self.path == "/api/login":
            if set(data) != {"password"} or not isinstance(data["password"], str) or len(data["password"]) > 128:
                raise HTTPError(400, "Invalid request.")
            result = self.server.worker.request("login", password=data["password"], client=self.client_key())
            if not result.get("ok"):
                wait = result.get("retry_after", 1)
                if result.get("reason") == "incorrect":
                    # The password was checked and is wrong: say so plainly. 401, not 429.
                    return self.respond(401, {"error": "That password is not correct. Please try again.",
                                              "retry_after": wait}, retry_after=wait)
                return self.respond(429, {"error": "Too many attempts. Please wait %d second%s and try again."
                                          % (wait, "" if wait == 1 else "s"), "retry_after": wait}, retry_after=wait)
            cookie, fresh = self.server.sessions.create(expires=result["expires_at"], owner=session["owner"], authenticated=True, previous=self.token())
            return self.respond(200, {"authenticated": True, "csrf": fresh["csrf"], "expires_at": fresh["expires"]}, cookie=cookie)
        self.session(required=True)
        if self.path == "/api/answer":
            if (set(data) != {"question_id", "text"} or not isinstance(data["question_id"], str)
                    or not isinstance(data["text"], str) or not data["text"].strip()
                    or len(data["question_id"]) > 200 or len(data["text"]) > 8000):
                raise HTTPError(400, "Enter an answer of at most 8000 characters.")
            return self.respond(200, self.server.worker.request("answer", owner=session["owner"], **data))
        if data:
            raise HTTPError(400, "Invalid request.")
        if self.path == "/api/end":
            return self.respond(200, self.server.worker.request("end", owner=session["owner"]))
        # Retain owner in a fresh anonymous session to permit explicit re-login/resume.
        cookie, _ = self.server.sessions.create(owner=session["owner"], previous=self.token())
        return self.respond(200, {"authenticated": False}, cookie=cookie)


def main():
    parser = argparse.ArgumentParser(description="Serve the private demo on 127.0.0.1 only")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--origin", action="append",
                        help="the origin people use (repeat for each host); default http://localhost:8765")
    parser.add_argument("--state-dir", default="/var/lib/prs-demo-web")
    parser.add_argument("--socket", default="/run/prs-demo/worker.sock")
    parser.add_argument("--trust-funnel", action="store_true")
    parser.add_argument("--samples-dir", help="sample genomes to offer signed-in people as downloads")
    args = parser.parse_args()
    if os.geteuid() == 0:
        parser.error("the web service must run as an unprivileged user")
    os.umask(0o077)
    state = Path(args.state_dir)
    state.mkdir(parents=True, exist_ok=True, mode=0o700)
    spool = state / "uploads"
    spool.mkdir(exist_ok=True, mode=0o700)
    server = DemoServer(args.port, args.origin or ["http://localhost:8765"], Sessions(state / "sessions.sqlite3"),
                        WorkerClient(args.socket), args.trust_funnel, args.samples_dir, spool)
    print(f"Private demo listening on http://127.0.0.1:{server.server_port}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
