"""The demo's HTTPS front (demo_site/tls_proxy.py): a certificate from `cert`, the front run as
a process of its own on loopback, and TLS clients that check the certificate by name."""
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import shutil
import socket
import ssl
import subprocess
import sys
import threading
import time

import pytest

from demo_site import tls_proxy
from demo_site.sessions import Sessions
from demo_site.web import DemoServer

REPO = Path(__file__).resolve().parents[1]
NAME = "demo.example.ts.net"


def sockets_permitted():
    try:
        socket.socket().close()
        return True
    except PermissionError:
        return False


needs_sockets = pytest.mark.skipif(not sockets_permitted(), reason="sandbox prohibits sockets")
needs_openssl = pytest.mark.skipif(not shutil.which("openssl"), reason="no openssl")


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def cert(tmp_path):
    paths = tmp_path / "tls" / "cert.pem", tmp_path / "tls" / "key.pem"
    tls_proxy.make_cert(NAME, *paths)
    return paths


def start_front(cert, upstream_port, trusted=(), idle=None):
    port = free_port()
    code = ("import asyncio; from demo_site import tls_proxy; %sasyncio.run(tls_proxy.serve("
            "[('127.0.0.1', %d)], %r, %r, %d, %r))" % ("tls_proxy.IDLE_SECONDS = %r; " % idle if idle else "",
                                                     port, str(cert[0]), str(cert[1]), upstream_port,
                                                     tuple(trusted)))
    # serve() directly: main() refuses root, which the test may run as.
    proc = subprocess.Popen([sys.executable, "-c", code], cwd=REPO, stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT)
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        try:
            socket.create_connection(("127.0.0.1", port), timeout=1).close()
            return proc, port
        except OSError:
            time.sleep(.05)
    proc.kill()
    raise AssertionError("front did not listen: %s" % proc.stdout.read())


def exchange(port, cert, raw):
    """One request over TLS, checked against the certificate by NAME; (status, headers, body)."""
    context = ssl.create_default_context(cafile=str(cert[0]))
    with socket.create_connection(("127.0.0.1", port), timeout=10) as sock:
        with context.wrap_socket(sock, server_hostname=NAME) as tls:
            tls.sendall(raw)
            chunks = []
            while True:
                data = tls.recv(65536)
                if not data:
                    break
                chunks.append(data)
    head, _, body = b"".join(chunks).partition(b"\r\n\r\n")
    lines = head.decode("latin-1").split("\r\n")
    headers = [tuple(part.strip() for part in line.split(":", 1)) for line in lines[1:]]
    return int(lines[0].split()[1]), headers, body


class Echo(BaseHTTPRequestHandler):
    """Stands in for the web: answers with what it was sent, one request per connection."""
    protocol_version = "HTTP/1.0"
    seen = []

    def log_message(self, *_args):
        pass

    def reply(self):
        body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
        Echo.seen.append(self.path)
        data = json.dumps({"xff": self.headers.get_all("X-Forwarded-For"), "host": self.headers["Host"],
                           "sha256": hashlib.sha256(body).hexdigest(), "size": len(body)}).encode()
        self.send_response(200)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    do_GET = do_POST = reply


@pytest.fixture
def echo():
    server = ThreadingHTTPServer(("127.0.0.1", 0), Echo)
    Echo.seen = []
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield server.server_port
    server.shutdown()
    server.server_close()


@pytest.fixture
def front(cert, echo):
    proc, port = start_front(cert, echo)
    yield port
    proc.terminate()
    proc.wait(10)


@needs_openssl
def test_cert_is_one_browsers_let_a_person_accept(cert, tmp_path):
    text = subprocess.run(["openssl", "x509", "-noout", "-text", "-in", str(cert[0])],
                          capture_output=True, text=True, check=True).stdout
    for expected in ("CA:FALSE", "TLS Web Server Authentication", "DNS:%s" % NAME,
                     "prime256v1", "ecdsa-with-SHA256", "Digital Signature"):
        assert expected in text
    assert text.split("Subject Alternative Name")[1].split("\n")[1].strip() == "DNS:" + NAME
    dates = subprocess.run(["openssl", "x509", "-noout", "-startdate", "-enddate", "-in", str(cert[0])],
                           capture_output=True, text=True, check=True).stdout.split("\n")
    start, end = (ssl.cert_time_to_seconds(line.split("=", 1)[1]) for line in dates[:2])
    assert end - start == 397 * 86400
    assert os.stat(cert[1]).st_mode & 0o777 == 0o600
    openssl = subprocess.run(["openssl", "x509", "-noout", "-fingerprint", "-sha256", "-in", str(cert[0])],
                             capture_output=True, text=True, check=True).stdout
    assert openssl.strip().split("=", 1)[1] == tls_proxy.fingerprint(cert[0])
    with pytest.raises(FileExistsError):
        tls_proxy.make_cert(NAME, *cert)
    for bad in ("demo", "a b.ts.net", "x.ts.net\nCN = y", "-x.ts.net", ""):
        with pytest.raises(ValueError):
            tls_proxy.make_cert(bad, tmp_path / "c.pem", tmp_path / "k.pem")


@needs_openssl
def test_cert_command_prints_the_fingerprint(tmp_path, capsys):
    tls_proxy.main(["cert", "--host", NAME, "--cert", str(tmp_path / "c.pem"), "--key", str(tmp_path / "k.pem")])
    assert capsys.readouterr().out.strip() == "SHA-256 fingerprint " + tls_proxy.fingerprint(tmp_path / "c.pem")


def test_listens_on_tailnet_or_loopback_addresses_only():
    assert tls_proxy.listen_address("100.101.102.103:8443") == ("100.101.102.103", 8443)
    assert tls_proxy.listen_address("[fd7a:115c:a1e0::1]:8443") == ("fd7a:115c:a1e0::1", 8443)
    assert tls_proxy.listen_address("127.0.0.1:8443") == ("127.0.0.1", 8443)
    assert tls_proxy.listen_address("[::1]:8443") == ("::1", 8443)
    for bad in ("0.0.0.0:8443", "[::]:8443", "192.168.1.20:8443", "10.0.0.5:8443", "100.63.255.255:8443",
                "100.128.0.1:8443", "8.8.8.8:443", "[fd7a:115c:a1e1::1]:8443", "demo.example.ts.net:8443",
                "100.101.102.103", "100.101.102.103:0", "100.101.102.103:70000", ":8443"):
        with pytest.raises(ValueError):
            tls_proxy.listen_address(bad)


def test_x_forwarded_for_is_replaced_by_the_peer():
    head = (b"POST /api/login HTTP/1.1\r\nHost: h:8443\r\nX-Forwarded-For: 203.0.113.9\r\n"
            b"Origin: https://h:8443\r\nx-forwarded-for:198.51.100.1, 203.0.113.9\r\n\r\n")
    assert tls_proxy.forwarded_head(head, "100.64.0.7") == (
        b"POST /api/login HTTP/1.1\r\nHost: h:8443\r\nOrigin: https://h:8443\r\n"
        b"X-Forwarded-For: 100.64.0.7\r\n\r\n")
    assert tls_proxy.forwarded_head(b"GET / HTTP/1.0\r\n\r\n", "::1") == (
        b"GET / HTTP/1.0\r\nX-Forwarded-For: ::1\r\n\r\n")


def test_a_trusted_front_doors_one_visitor_address_is_kept(capsys):
    def sent(xff_lines, trusted=True):
        head = b"GET / HTTP/1.1\r\nHost: h\r\n" + xff_lines + b"\r\n"
        return tls_proxy.forwarded_head(head, "100.64.0.7", trusted).split(b"\r\n")[2:-2]
    assert sent(b"X-Forwarded-For: 203.0.113.9\r\n") == [b"X-Forwarded-For: 203.0.113.9"]
    assert sent(b"x-forwarded-for:2001:db8::7\r\n") == [b"X-Forwarded-For: 2001:db8::7"]
    assert sent(b"X-Forwarded-For: 203.0.113.9\r\n", trusted=False) == [b"X-Forwarded-For: 100.64.0.7"]
    # Anything but one line naming one address: the front door itself, as for any other peer.
    for lines in (b"", b"X-Forwarded-For: 198.51.100.1, 203.0.113.9\r\n", b"X-Forwarded-For: unknown\r\n",
                  b"X-Forwarded-For: 203.0.113.9\r\nX-Forwarded-For: 198.51.100.1\r\n",
                  b"X-Forwarded-For: 203.0.113.9\r\nX-Forwarded-For: 203.0.113.9\r\n"):
        assert sent(lines) == [b"X-Forwarded-For: 100.64.0.7"]
    assert tls_proxy.tailnet_address("100.101.102.103") == tls_proxy.ipaddress.ip_address("100.101.102.103")
    for bad in ("8.8.8.8", "192.168.1.20", "fd7a:115c:a1e1::1", "100.101.102.103:8443", "demo.example.ts.net"):
        with pytest.raises(ValueError):
            tls_proxy.tailnet_address(bad)
    with pytest.raises(SystemExit):
        tls_proxy.main(["serve", "--listen", "127.0.0.1:8443", "--cert", "c", "--key", "k",
                        "--trusted-proxy", "203.0.113.1"])
    assert "203.0.113.1 is not a tailnet or loopback address" in capsys.readouterr().err


def test_a_head_the_web_might_split_differently_is_refused():
    for head in (b"GET / HTTP/1.0\r\nHost: h\nX-Forwarded-For: 203.0.113.9\r\n\r\n",   # bare LF
                 b"GET / HTTP/1.0\r\nHost: h\rX-Forwarded-For: 203.0.113.9\r\n\r\n",   # bare CR
                 b"GET / HTTP/1.0\r\nHost: h\r\n X-Forwarded-For: 203.0.113.9\r\n\r\n",  # folded
                 b"GET / HTTP/1.0\r\nX-Forwarded-For : 203.0.113.9\r\n\r\n",          # space before colon
                 b"GET / HTTP/1.0\r\nno colon here\r\n\r\n",
                 b"\r\nGET / HTTP/1.0\r\n\r\n",
                 b"GET / HTTP/1.0\r\n"):
        assert tls_proxy.forwarded_head(head, "100.64.0.7") is None


@needs_sockets
@needs_openssl
def test_front_passes_requests_through_with_the_peer_as_x_forwarded_for(front, cert):
    host = "Host: %s:%d\r\n" % (NAME, front)
    status, _, body = exchange(front, cert, ("GET /api/session HTTP/1.1\r\n%sX-Forwarded-For: 203.0.113.9\r\n"
                                             "Connection: close\r\n\r\n" % host).encode())
    assert status == 200
    assert json.loads(body) == {"xff": ["127.0.0.1"], "host": "%s:%d" % (NAME, front),
                                "sha256": hashlib.sha256(b"").hexdigest(), "size": 0}
    upload = os.urandom(3 * 1024 * 1024)
    status, _, body = exchange(front, cert, ("POST /api/run HTTP/1.1\r\n%sContent-Type: application/octet-stream\r\n"
                                             "Content-Length: %d\r\n\r\n" % (host, len(upload))).encode() + upload)
    assert status == 200
    assert json.loads(body)["sha256"] == hashlib.sha256(upload).hexdigest()


@needs_sockets
@needs_openssl
def test_an_upload_outlasting_the_idle_limit_goes_through_while_its_bytes_flow(cert, echo):
    proc, port = start_front(cert, echo, idle=1)
    context = ssl.create_default_context(cafile=str(cert[0]))
    try:
        # Nothing comes back while the web reads the upload: 3 s of it, against an idle limit of 1 s.
        upload = os.urandom(12 * 65536)
        with socket.create_connection(("127.0.0.1", port), timeout=10) as sock:
            with context.wrap_socket(sock, server_hostname=NAME) as tls:
                tls.sendall(b"POST /api/run HTTP/1.0\r\nHost: h\r\nContent-Length: %d\r\n\r\n" % len(upload))
                for at in range(0, len(upload), 65536):
                    time.sleep(0.25)
                    tls.sendall(upload[at:at + 65536])
                answer = b""
                while data := tls.recv(65536):
                    answer += data
        assert answer.startswith(b"HTTP/1.0 200")
        assert json.loads(answer.partition(b"\r\n\r\n")[2])["sha256"] == hashlib.sha256(upload).hexdigest()
        # Nothing moving either way for the idle limit: the connection is closed, unanswered.
        with socket.create_connection(("127.0.0.1", port), timeout=10) as sock:
            with context.wrap_socket(sock, server_hostname=NAME) as tls:
                tls.sendall(b"POST /api/run HTTP/1.0\r\nHost: h\r\nContent-Length: 100\r\n\r\nhalf")
                started = time.monotonic()
                try:
                    assert tls.recv(65536) == b""
                except ConnectionResetError:
                    pass
                assert 0.9 < time.monotonic() - started < 5
    finally:
        proc.terminate()
        proc.wait(10)


@needs_sockets
@needs_openssl
def test_front_keeps_the_visitor_address_only_from_a_trusted_front_door(cert, echo):
    proc, port = start_front(cert, echo, trusted=("127.0.0.1",))
    try:
        for xff, seen in (("X-Forwarded-For: 203.0.113.9\r\n", ["203.0.113.9"]),
                          ("X-Forwarded-For: 203.0.113.9, 198.51.100.1\r\n", ["127.0.0.1"]),
                          ("", ["127.0.0.1"])):
            status, _, body = exchange(port, cert, ("GET / HTTP/1.0\r\nHost: h\r\n%s\r\n" % xff).encode())
            assert (status, json.loads(body)["xff"]) == (200, seen)
    finally:
        proc.terminate()
        proc.wait(10)


@needs_sockets
@needs_openssl
def test_front_refuses_a_folded_header_before_the_web_sees_it(front, cert):
    status, _, body = exchange(front, cert, b"GET / HTTP/1.0\r\nHost: h\r\n X-Forwarded-For: 203.0.113.9\r\n\r\n")
    assert (status, json.loads(body)) == (400, {"error": "Request rejected."})
    assert Echo.seen == []


@needs_sockets
@needs_openssl
def test_front_answers_503_while_the_web_is_down(cert):
    proc, port = start_front(cert, free_port())
    try:
        status, headers, body = exchange(port, cert, b"GET / HTTP/1.0\r\nHost: h\r\n\r\n")
        assert status == 503
        assert json.loads(body) == {"error": "The demo service is unavailable. Please try again."}
        assert ("Cache-Control", "no-store") in headers
    finally:
        proc.terminate()
        proc.wait(10)


class LoginWorker:
    def __init__(self):
        self.clients = []

    def request(self, op, **payload):
        if op == "login":
            self.clients.append(payload["client"])
            return {"ok": payload["password"] == "test", "retry_after": 1, "expires_at": time.time() + 3600}
        return {"status": "idle", "busy": False, "owned": False}


@needs_sockets
@needs_openssl
def test_the_real_web_behind_the_front_signs_in_and_limits_per_person(cert, tmp_path):
    port = free_port()
    origin = "https://%s:%d" % (NAME, port)
    worker = LoginWorker()
    web = DemoServer(0, origin, Sessions(tmp_path / "sessions.sqlite"), worker, trust_funnel=True)
    threading.Thread(target=web.serve_forever, daemon=True).start()
    code = ("import asyncio; from demo_site import tls_proxy; asyncio.run(tls_proxy.serve("
            "[('127.0.0.1', %d)], %r, %r, %d))" % (port, str(cert[0]), str(cert[1]), web.server_port))
    proc = subprocess.Popen([sys.executable, "-c", code], cwd=REPO)
    try:
        deadline = time.monotonic() + 10
        while True:
            try:
                socket.create_connection(("127.0.0.1", port), timeout=1).close()
                break
            except OSError:
                assert time.monotonic() < deadline
                time.sleep(.05)
        common = "Host: %s:%d\r\nOrigin: %s\r\n" % (NAME, port, origin)
        status, headers, body = exchange(port, cert, ("GET /api/session HTTP/1.1\r\n%s\r\n" % common).encode())
        assert status == 200
        cookie = dict(headers)["Set-Cookie"]
        assert "Secure" in cookie and cookie.startswith("__Host-prs_demo=")
        login = json.dumps({"password": "test"}).encode()
        status, _, body = exchange(port, cert, (
            "POST /api/login HTTP/1.1\r\n%sCookie: %s\r\nX-CSRF-Token: %s\r\nX-Forwarded-For: 203.0.113.9\r\n"
            "Content-Type: application/json\r\nContent-Length: %d\r\n\r\n"
            % (common, cookie.split(";")[0], json.loads(body)["csrf"], len(login))).encode() + login)
        assert (status, json.loads(body)["authenticated"]) == (200, True)
        # The limit is keyed on the front's peer, never on what the client claimed.
        assert worker.clients == ["127.0.0.1"]
        status, _, _ = exchange(port, cert, ("GET / HTTP/1.0\r\nHost: 100.64.0.7:%d\r\n\r\n" % port).encode())
        assert status == 400
    finally:
        proc.terminate()
        proc.wait(10)
        web.shutdown()
        web.server_close()
