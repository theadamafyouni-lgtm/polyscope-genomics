"""Exercise the real harness router and host broker with a local locked gate double."""
import gzip
import inspect
import json
import os
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / 'harness'))
sys.path.insert(0, str(REPO / 'agent/tools'))
# Import the harness with a synthetic launch-gate configuration, never a private token file.
with tempfile.TemporaryDirectory() as config_dir:
    config_path = Path(config_dir) / 'launch-gate.json'
    config_path.write_text(json.dumps({'hard': ['PRIVATE_TEST_TOKEN'],
        'provider_hard': ['PRIVATE_PROVIDER_TEST_TOKEN'], 'deny_eval_run': 'private-test-run',
        'prior_run': 'private-prior-run'}))
    with patch.dict(os.environ, PRS_HARNESS_TOKENS=str(config_path)):
        from harness import config, gb10, impute_broker, orchestrator
import impute_remote


class BrokerFixture:
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='broker-test-')
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.prs = self.root / 'prs'
        self.run_id = 'synthetic-run'
        self.run = self.prs / 'runs' / self.run_id
        self.run.mkdir(parents=True)
        for name in ('outbox', 'inbox'):
            (self.prs / 'comms' / name).mkdir(parents=True)
        state = self.root / 'fake-gate'
        state.mkdir()
        (state / 'config.json').write_text('{}')
        # TEST-NET-1, never contacted: the command is the local fake gate, not ssh.
        self.client = gb10.GateClient('gb10-test@192.0.2.1', '/nonexistent/key',
            command=[sys.executable, str(REPO / 'tests/fixtures/fake_gate.py'), str(state)],
            poll=.01, ssh_retries=(.01, .01, .01), upload_window=2, download_window=2,
            run_busy_window=1, gate_timeout=1, margin=2, attempt_cap=2)
        self.o = orchestrator.Orchestrator(run_id=self.run_id, impute_backend='gb10')
        self.o.prs_root = str(self.prs)
        self.o.vault_dir = str(self.root / 'vault')
        self.broker = impute_broker.ImputeBroker(str(self.prs), self.run_id,
            self.o.vault_dir, self.client, lambda mid, reply: self.o._reply(None, mid, reply))
        self.o._impute_broker = self.broker
        self.hold = self.run / 'hold.txt'
        self.hold.write_text('SYNTHETIC_HOLDOUT\n')
        self.target = self.run / 'target.vcf.gz'
        with gzip.open(self.target, 'wb') as dest:
            dest.write(b'##fileformat=VCFv4.2\n22\t100\t.\tA\tG\t.\tPASS\t.\n')
        self.addCleanup(self.broker.close)

    def request(self, suffix=0, **kwargs):
        msg = {'message_id': 'i-' + f'{suffix:012x}', 'kind': 'impute-chromosome',
               'run_id': self.run_id, 'chromosome': '22', 'threads': 6, 'seed': '20261008',
               'target': str(self.target), 'holdout': str(self.hold)}
        msg.update(kwargs)
        return msg

    def response(self, msg):
        path = self.prs / 'comms/inbox' / (msg['message_id'] + '.json')
        deadline = time.monotonic() + 5
        while not path.exists():
            if time.monotonic() >= deadline:
                self.fail('broker did not reply')
            time.sleep(.01)
        return json.loads(path.read_text())

class BrokerTest(BrokerFixture, unittest.TestCase):
    def test_real_router_round_trip_and_cleanup_receipt(self):
        self.o._router = threading.Thread(target=self.o._route_forever, args=({},))
        self.o._router.start()
        try:
            receipt = impute_remote.impute_chromosome(self.run_id, '22', self.target,
                self.hold, '20261008', self.run / 'result.vcf.gz', self.run / 'result.log',
                comms=self.prs / 'comms', poll=.01, timeout=5)
        finally:
            self.o._stop.set()
            self.o._router.join(timeout=5)
        self.assertTrue(receipt['cleaned'])
        self.assertEqual(receipt['threads'], 6)
        self.assertEqual(gzip.decompress((self.run / 'result.vcf.gz').read_bytes()), b'output\n')
        self.assertEqual(self.o._errors.qsize(), 0)
        # Input and output bytes are removed from the host vault; only JSON receipts remain.
        self.assertTrue(all(p.suffix == '.json' for p in self.broker.vault_dir.iterdir()))
        report = self.broker.close()
        self.assertEqual(report['cleanup_failures'], {})
        self.assertEqual(len(report['gate_runs']), 1)
        # The threads each request asked for, not a constant for the route.
        self.assertNotIn('threads', report)
        self.assertEqual((report['requests'][0]['threads'], report['requests'][0]['seed']),
                         (6, '20261008'))
        print('\nBROKER: real router -> host gate client -> sandbox output; '
              'private staging removed; cleanup recorded', flush=True)

    def test_outside_paths_links_and_special_files_refused_before_upload(self):
        outside = self.root / 'outside'
        outside.write_text('must never upload')
        link = self.run / 'link'
        link.symlink_to(outside)
        fifo = self.run / 'fifo'
        os.mkfifo(fifo)
        for i, path in enumerate((outside, link, fifo)):
            with self.subTest(path=path.name):
                msg = self.request(i, target=str(path))
                self.o._handle(msg, None, {})
                self.assertEqual(self.response(msg)['status'], 'error')
                self.assertEqual(self.response(msg)['cause'], 'request')
        self.assertEqual(self.client.jobs, {})

    def test_output_directory_link_cannot_escape_sandbox(self):
        outside = self.root / 'outside-output'
        outside.mkdir()
        (self.run / 'score').symlink_to(outside)
        msg = self.request()
        self.o._handle(msg, None, {})
        self.assertEqual(self.response(msg)['status'], 'error')
        self.assertEqual(list(outside.iterdir()), [])
        self.assertTrue(all(job['cleaned'] for job in self.client.jobs.values()))

    def test_wrong_run_parameters_and_disabled_backend_refused(self):
        for i, kwargs in enumerate(({'run_id': 'another-run'}, {'threads': 8},
                                   {'chromosome': '22; bad'}, {'seed': '1 bad'})):
            msg = self.request(i, **kwargs)
            self.o._handle(msg, None, {})
            self.assertEqual(self.response(msg)['status'], 'error')
            self.assertEqual(self.response(msg)['cause'], 'request')
        self.o._impute_broker = None
        msg = self.request(10)
        self.o._handle(msg, None, {})
        self.assertIn('not enabled', self.response(msg)['reason'])
        self.assertEqual(self.client.jobs, {})

    def test_shutdown_cleans_and_closes_broker(self):
        msg = self.request()
        self.o._handle(msg, None, {})
        self.assertEqual(self.response(msg)['status'], 'delivered')
        self.o._close_imputation()
        self.assertIsNone(self.o._impute_broker)
        summary = json.loads((self.broker.vault_dir / 'summary.json').read_text())
        self.assertEqual(summary['cleanup_failures'], {})
        msg = self.request(1)
        self.broker.submit(msg)
        self.assertIn('closing', self.response(msg)['reason'])
        self.assertEqual(self.response(msg)['cause'], 'closing')

    def test_each_failure_says_whose_it_was(self):
        (self.root / 'fake-gate/config.json').write_text('{"fail": "run"}')
        msg = self.request()
        self.o._handle(msg, None, {})
        reply = self.response(msg)
        self.assertEqual((reply['status'], reply['cause']), ('error', 'gb10'))
        self.assertEqual(self.broker.requests[0]['cause'], 'gb10')
        for exc, cause in ((gb10.GateCancelled('run', 'cancelled'), 'closing'),
                           (gb10.GateError('target', 'upload contains another chromosome'),
                            'request'),
                           (impute_broker.provider.OutsideRun('refused'), 'request'),
                           (ValueError('input must be nonempty'), 'request'),
                           (gb10.GateError('get', 'invalid or truncated tar'), 'gb10'),
                           (OSError(28, 'No space left on device'), 'gb10')):
            self.assertEqual(impute_broker.failure_cause(exc), cause, exc)

    def test_sandbox_waits_longer_than_the_clients_worst_case(self):
        worst = gb10.GateClient('gb10-test@192.0.2.1', '/nonexistent/key').worst_case_seconds()
        self.assertGreater(worst, 4 * 3600)  # what it used to wait, which was too short
        self.assertLess(worst, impute_remote.REPLY_TIMEOUT)
        default = inspect.signature(impute_remote.impute_chromosome).parameters['timeout']
        self.assertEqual(default.default, impute_remote.REPLY_TIMEOUT)

    def test_duplicate_message_is_not_run_twice(self):
        msg = self.request()
        self.o._handle(msg, None, {})
        self.assertEqual(self.response(msg)['status'], 'delivered')
        self.o._handle(msg, None, {})
        self.assertEqual(len(self.client.jobs), 1)

    def test_backend_configuration_and_staging_allowlist(self):
        switch = self.prs / 'refdata/imputation.json'
        self.assertEqual(impute_remote.configured_backend(switch), 'local')
        switch.parent.mkdir()
        switch.write_text('{"backend":"gb10"}')
        self.assertEqual(impute_remote.configured_backend(switch), 'gb10')
        self.assertIn('impute_remote.py', config.PIPELINE_TOOLS)
        self.assertNotIn('gb10.py', config.PIPELINE_TOOLS)
        with self.assertRaisesRegex(ValueError, 'bare'):
            orchestrator.Orchestrator(bare=True, impute_backend='gb10')


if __name__ == '__main__':
    unittest.main()
