"""Check deployment against the harness mount boundary without reading private inputs."""
import ast
from pathlib import Path
import re
import unittest

REPO = Path(__file__).resolve().parents[1]


class DeploymentBoundaryTests(unittest.TestCase):
    def test_installed_sources_are_outside_broad_system_mounts(self):
        # Parse the constant rather than importing config: this test needs no secret
        # launch-gate configuration and must never inspect any persona data.
        tree = ast.parse((REPO / "harness/harness/profile.py").read_text())
        system_ro = next(ast.literal_eval(node.value) for node in tree.body
                         if isinstance(node, ast.Assign)
                         and any(isinstance(target, ast.Name) and target.id == "SYSTEM_RO"
                                 for target in node.targets))
        service = (REPO / "demo_site/deploy/prs-demo-worker.service").read_text()
        installed = Path(re.search(r"^WorkingDirectory=(.+)$", service, re.MULTILINE)[1])
        for path in system_ro:
            self.assertFalse(installed == Path(path) or Path(path) in installed.parents,
                             f"{installed} would expose unsanitized demo sources through {path}")
        installer = (REPO / "demo_site/deploy/install.sh").read_text()
        self.assertIn("tar -x -C " + str(installed), installer)
        self.assertIn("chmod 0700 " + str(installed / "harness"), installer)
        self.assertNotIn("chmod -R", installer)
        self.assertNotIn("ln -s", installer)

    def test_the_front_door_pins_the_demo_host_and_sends_only_the_visitor_address(self):
        conf = (REPO / "demo_site/deploy/front-door-nginx.conf").read_text()
        directives = {line.strip() for line in conf.splitlines() if not line.lstrip().startswith("#")}
        for required in ("proxy_pass https://@UPSTREAM@;", "proxy_ssl_verify on;", "proxy_ssl_verify_depth 1;",
                         "proxy_ssl_trusted_certificate @PIN@;", "proxy_ssl_name @UPSTREAM_NAME@;",
                         "proxy_set_header Host $host;", "proxy_set_header X-Forwarded-For $remote_addr;",
                         "client_max_body_size 64k;", "client_body_buffer_size 64k;",
                         "proxy_max_temp_file_size 0;", "ssl_reject_handshake on;", "return 444;"):
            self.assertIn(required, directives)
        # Set once for the server, so no location can reach the demo host another way.
        for each in ("proxy_ssl_verify ", "proxy_ssl_trusted_certificate ", "proxy_set_header "):
            self.assertTrue(all(line.startswith("    " + each) for line in conf.splitlines() if line.strip().startswith(each)))
        # An upload alone goes on as it arrives, never whole here, at the web's limit and no more.
        upload = re.search(r"\n    location = /api/run \{\n(.*?)\n    \}\n", conf, re.DOTALL)[1]
        self.assertEqual(sorted(line.strip() for line in upload.splitlines() if not line.lstrip().startswith("#")),
                         ["client_body_buffer_size 1m;", "client_max_body_size 1025m;",
                          "proxy_pass https://@UPSTREAM@;", "proxy_request_buffering off;"])
        self.assertEqual(conf.count("proxy_request_buffering"), 1)
        # Never a forwarded list the browser could start, and no machine's address or name.
        self.assertNotIn("proxy_add_x_forwarded_for", conf)
        self.assertNotRegex(conf, r"\b\d{1,3}(\.\d{1,3}){3}\b|ts\.net|duckdns")

    def test_the_front_door_has_its_own_offline_page_and_tries_the_demo_host_every_time(self):
        conf = (REPO / "demo_site/deploy/front-door-nginx.conf").read_text()
        directives = [line.strip() for line in conf.splitlines() if not line.lstrip().startswith("#")]
        # nginx's own 502 or 504 (the host can't be reached) and the host front's 503 (its web is
        # down); the host's other answers, errors included, pass as they are.
        self.assertEqual([d for d in directives if d.startswith(("error_page", "proxy_intercept_errors"))],
                         ["proxy_intercept_errors on;", "error_page 502 503 504 = @offline;"])
        # One server and no upstream group, which nginx never marks down: the first request after
        # the host is back reaches the demo.
        self.assertEqual({d for d in directives if d.startswith("proxy_pass ")}, {"proxy_pass https://@UPSTREAM@;"})
        self.assertNotRegex(conf, r"\bupstream\s+\S+\s*\{|max_fails|fail_timeout|proxy_next_upstream")
        offline = re.search(r"\n    location @offline \{\n(.*?)\n    \}\n", conf, re.DOTALL)[1]
        for required in ("types { }", 'default_type "text/html; charset=utf-8";',
                         'add_header Cache-Control "no-store" always;'):
            self.assertIn(required, offline)
        page = re.fullmatch(r"(?s).*\n        return 503 '(<!doctype html>\n.*</html>\n)';", offline)[1]
        self.assertIn("<h1>The demo is offline right now.</h1>", page)
        self.assertIn("<p>Please try again later.</p>", page)
        self.assertIn('<meta http-equiv="refresh" content="60">', page)
        # Self-contained, since the demo host isn't there to serve anything, and plain text to
        # nginx: no variable, escape or quote that would end the string.
        self.assertNotRegex(page, r"\b(src|href)\s*=|url\(|@import|[$\\']")

    def test_worker_keeps_cgroup_delegation_and_web_is_unprivileged(self):
        worker = (REPO / "demo_site/deploy/prs-demo-worker.service").read_text()
        web = (REPO / "demo_site/deploy/prs-demo-web.service").read_text()
        for required in ("User=root", "Delegate=yes", "KillMode=control-group"):
            self.assertIn(required, worker.splitlines())
        for required in ("User=prs-demo", "NoNewPrivileges=yes", "ProtectHome=yes"):
            self.assertIn(required, web.splitlines())
        self.assertIn("IPAddressAllow=localhost", web.splitlines())


if __name__ == "__main__":
    unittest.main()
