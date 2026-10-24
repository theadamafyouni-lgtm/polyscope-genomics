"""pgs_catalog.py's download in byte ranges, in a demo run: the same bytes as one request, from a
server that answers ranges, and one request where it does not. Local server, synthetic bytes."""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import hashlib
import os
from pathlib import Path
import re
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "agent" / "tools"))
import pgs_catalog  # noqa: E402

BODY = os.urandom(3 * 1024 * 1024 + 17)


class Server(BaseHTTPRequestHandler):
    ranges = True
    seen = []

    def log_message(self, *_):
        pass

    def do_GET(self):
        wanted = re.fullmatch(r"bytes=(\d+)-(\d+)", self.headers.get("Range", ""))
        Server.seen.append(self.headers.get("Range"))
        if wanted and Server.ranges:
            a, b = int(wanted[1]), min(int(wanted[2]), len(BODY) - 1)
            self.send_response(206)
            self.send_header("Content-Range", "bytes %d-%d/%d" % (a, b, len(BODY)))
            self.send_header("Content-Length", str(b - a + 1))
            self.end_headers()
            self.wfile.write(BODY[a:b + 1])
        else:
            self.send_response(200)
            self.send_header("Content-Length", str(len(BODY)))
            self.end_headers()
            self.wfile.write(BODY)


class RangedDownload(unittest.TestCase):
    def setUp(self):
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Server)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.url = "http://127.0.0.1:%d/PGS000001_hmPOS_GRCh37.txt.gz" % self.server.server_port
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dest = Path(tmp.name) / "model.txt.gz"
        Server.seen = []

    def test_ranges_give_the_same_bytes_and_a_server_without_them_gets_one_request(self):
        how = pgs_catalog.fetch(self.url, str(self.dest), connections=8)
        self.assertEqual(hashlib.sha256(self.dest.read_bytes()).hexdigest(), hashlib.sha256(BODY).hexdigest())
        self.assertEqual(how["connections"], 8)
        self.assertGreater(len([r for r in Server.seen if r]), 8)
        Server.ranges = False
        try:
            self.dest.unlink()
            how = pgs_catalog.fetch(self.url, str(self.dest), connections=8)
        finally:
            Server.ranges = True
        self.assertEqual(self.dest.read_bytes(), BODY)
        self.assertEqual(how["connections"], 1)

    def test_only_a_demo_run_downloads_in_ranges(self):
        with tempfile.TemporaryDirectory() as tmp:
            settings = Path(tmp) / "demo.json"
            with patch.object(pgs_catalog, "DEMO_SETTINGS", settings):
                self.assertEqual(pgs_catalog.download_connections(), 1)
                settings.write_text('{"parallel_downloads": 8}')
                self.assertEqual(pgs_catalog.download_connections(), 8)


if __name__ == "__main__":
    unittest.main()
