"""An allow-listing HTTP CONNECT proxy on loopback. Stdlib only.

Why this exists: SBPL has no dependable remote-host predicate, so a network permission broad
enough for the PGS Catalog is also broad enough to fetch an answer from a gist or to post the
patient's genotype out. `select` cannot run without https://www.pgscatalog.org/rest, and
pre-fetching the catalog during staging would hand the agent its candidate list, which is
select's whole job.

So: the profile denies all egress except this port, and host filtering happens here, where it
can be enforced by name and where every connection can be written down. The transcript is not
a side effect -- it is the evidence that the only hosts reached were the permitted ones.

The Claude Code process itself also goes through here (via HTTPS_PROXY), which is why the
Anthropic endpoints are on the allow-list. That is deliberate: it means there is no egress
path from the sandbox that is not in the log.

CONNECT only. The proxy never sees plaintext -- it opens a TCP tunnel after checking the
host, and counts bytes. It cannot inspect or modify what flows through, and it does not try.
"""
import errno
import json
import os
import select
import socket
import threading
import time


class AllowListProxy:
    def __init__(self, allow_hosts, log_path, port_range=(18800, 18899)):
        self.allow = set(h.lower() for h in allow_hosts)
        self.log_path = log_path
        self.port_range = port_range
        self.port = None
        self._sock = None
        self._thread = None
        self._stop = threading.Event()
        self._loglock = threading.Lock()
        self.n_allowed = 0
        self.n_denied = 0

    # -- logging ---------------------------------------------------------
    def _log(self, **rec):
        rec["ts"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        with self._loglock:
            with open(self.log_path, "a") as fh:
                fh.write(json.dumps(rec, sort_keys=False) + "\n")

    # -- lifecycle -------------------------------------------------------
    def start(self):
        lo, hi = self.port_range
        for port in range(lo, hi + 1):
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                s.bind(("127.0.0.1", port))
            except OSError as exc:
                s.close()
                if exc.errno in (errno.EADDRINUSE, errno.EACCES):
                    continue
                raise
            s.listen(64)
            self._sock = s
            self.port = port
            break
        if self._sock is None:
            raise RuntimeError("no free loopback port in %r for the proxy" % (self.port_range,))

        os.makedirs(os.path.dirname(os.path.abspath(self.log_path)), exist_ok=True)
        self._log(event="proxy_start", port=self.port, allow_hosts=sorted(self.allow))
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()
        return self.port

    def stop(self):
        self._stop.set()
        try:
            if self._sock:
                self._sock.close()
        except OSError:
            pass
        if self._thread:
            self._thread.join(timeout=5)
        self._log(event="proxy_stop", n_allowed=self.n_allowed, n_denied=self.n_denied)

    # -- serving ---------------------------------------------------------
    def _serve(self):
        while not self._stop.is_set():
            try:
                self._sock.settimeout(0.5)
                conn, _addr = self._sock.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            threading.Thread(target=self._handle, args=(conn,), daemon=True).start()

    def _handle(self, conn):
        upstream = None
        try:
            conn.settimeout(30)
            head = b""
            while b"\r\n\r\n" not in head:
                chunk = conn.recv(4096)
                if not chunk:
                    return
                head += chunk
                if len(head) > 65536:
                    return

            line = head.split(b"\r\n", 1)[0].decode("latin-1")
            parts = line.split()
            if len(parts) < 2 or parts[0].upper() != "CONNECT":
                # Plain HTTP is refused outright. Everything the pipeline needs is HTTPS,
                # and permitting plaintext GET would mean proxying arbitrary URLs.
                self.n_denied += 1
                self._log(event="deny", reason="non-CONNECT method", request=line[:200])
                conn.sendall(b"HTTP/1.1 405 Method Not Allowed\r\n\r\n")
                return

            hostport = parts[1]
            host, _, port_s = hostport.rpartition(":")
            host = (host or hostport).lower().strip("[]")
            try:
                port = int(port_s)
            except ValueError:
                port = 443

            if host not in self.allow:
                self.n_denied += 1
                self._log(event="deny", reason="host not on allow-list", host=host, port=port)
                conn.sendall(b"HTTP/1.1 403 Forbidden\r\n\r\n")
                return
            if port != 443:
                self.n_denied += 1
                self._log(event="deny", reason="non-443 port", host=host, port=port)
                conn.sendall(b"HTTP/1.1 403 Forbidden\r\n\r\n")
                return

            try:
                upstream = socket.create_connection((host, port), timeout=30)
            except OSError as exc:
                self._log(event="upstream_error", host=host, port=port, error=str(exc))
                conn.sendall(b"HTTP/1.1 502 Bad Gateway\r\n\r\n")
                return

            self.n_allowed += 1
            self._log(event="allow", host=host, port=port)
            conn.sendall(b"HTTP/1.1 200 Connection Established\r\n\r\n")
            up, down = self._pump(conn, upstream)
            self._log(event="close", host=host, port=port,
                      bytes_to_host=up, bytes_from_host=down)
        except Exception as exc:  # noqa: BLE001 - a proxy thread must never kill the run
            self._log(event="proxy_error", error="%s: %s" % (type(exc).__name__, exc))
        finally:
            for s in (conn, upstream):
                try:
                    if s:
                        s.close()
                except OSError:
                    pass

    @staticmethod
    def _pump(a, b):
        a.settimeout(None)
        b.settimeout(None)
        up = down = 0
        socks = [a, b]
        while True:
            try:
                r, _, x = select.select(socks, [], socks, 60)
            except (OSError, ValueError):
                break
            if x or not r:
                break
            for s in r:
                try:
                    data = s.recv(65536)
                except OSError:
                    return up, down
                if not data:
                    return up, down
                if s is a:
                    up += len(data)
                    dst = b
                else:
                    down += len(data)
                    dst = a
                try:
                    dst.sendall(data)
                except OSError:
                    return up, down
        return up, down
