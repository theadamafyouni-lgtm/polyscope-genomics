"""Gate transport tests. No SSH, private data or remote Beagle calls."""
import fcntl
import gzip
import io
import json
import os
import sys
import tarfile
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'harness'))
from harness.gb10 import GateCancelled, GateClient, GateError, copy_archive

# TEST-NET-1, never contacted: every client here runs a local fake gate instead of ssh.
TEST_HOST = 'gb10-test@192.0.2.1'
# The design's timings, scaled down: poll 60 s, ssh retries after 30 s, 2 min and 5 min,
# uploads and downloads 30 min, run busy 3 h 15 min, run 3 h 10 min, the gate's 3 h + 60 s.
FAST = dict(poll=.01, ssh_retries=(.01, .02, .03), upload_window=2, download_window=2,
            run_busy_window=1, run_timeout=5, gate_timeout=1, margin=2, attempt_cap=2)


class GateTransportTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='gate-test-')
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.configure()
        self.holdout = self.root / 'holdout.txt'
        self.holdout.write_text('SYNTHETIC_HOLDOUT\n')
        self.client = GateClient(TEST_HOST, '/nonexistent/key', command=[sys.executable, str(
            Path(__file__).parent / 'fixtures' / 'fake_gate.py'), str(self.root)], **FAST)

    def configure(self, **kwargs):
        (self.root / 'config.json').write_text(json.dumps(kwargs))
        for counter in self.root.glob('ssh-failed-*'):
            counter.unlink()

    def cancel_once_running(self, chrom='22'):
        """Close the client once `run` has reached the gate, as the harness closing would."""
        def cancel():
            self.wait_for('run-started-' + chrom)
            self.client.cancel.set()
        thread = threading.Thread(target=cancel)
        thread.start()
        self.addCleanup(thread.join)

    def target(self, chrom):
        path = self.root / ('target-' + chrom + '.vcf.gz')
        with gzip.open(path, 'wb') as out:
            out.write(b'##fileformat=VCFv4.2\n')
            out.write(f'{chrom}\t100\t.\tA\tG\t.\tPASS\t.\n'.encode())
        return path

    def impute(self, chrom='22'):
        return self.client.impute(chrom, self.target(chrom), self.holdout, '20261008',
                                  self.root / ('out-' + chrom))

    def wait_for(self, name):
        deadline = time.monotonic() + 5
        while not (self.root / name).exists():
            if time.monotonic() > deadline:
                self.fail('barrier not reached: ' + name)
            time.sleep(.01)

    def events(self):
        return [json.loads(line) for line in (self.root / 'events.jsonl').read_text().splitlines()]

    def count(self, verb, event='start'):
        return sum(1 for e in self.events() if e['verb'] == verb and e['event'] == event)

    def assert_cleaned(self, count):
        self.assertEqual(len(self.client.jobs), count)
        self.assertTrue(all(job['cleaned'] for job in self.client.jobs.values()))
        self.assertFalse(any((self.root / rid).exists() for rid in self.client.jobs))

    def test_download_overlaps_next_run_with_distinct_ids(self):
        self.configure(hold_get='22')
        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(self.impute, '22')
            try:
                self.wait_for('get-started-22')
                second = pool.submit(self.impute, '21')
                self.wait_for('run-started-21')
                self.assertFalse(first.done())
            finally:
                (self.root / 'release-get-22').touch()
            a, b = first.result(timeout=5), second.result(timeout=5)
        self.assertNotEqual(a['run_id'], b['run_id'])
        self.assert_cleaned(2)
        trace = self.events()
        get_start = next(e['time'] for e in trace if e['verb'] == 'get' and
                         e['chrom'] == '22' and e['event'] == 'start')
        get_end = next(e['time'] for e in trace if e['verb'] == 'get' and
                       e['chrom'] == '22' and e['event'] == 'end')
        run_start = next(e['time'] for e in trace if e['verb'] == 'run' and
                         e['chrom'] == '21' and e['event'] == 'start')
        self.assertLess(get_start, run_start)
        self.assertLess(run_start, get_end)
        print('\nOVERLAP: chr22 get active while chr21 run starts; distinct gate IDs; '
              'threads=6; both IDs cleaned', flush=True)

    def test_busy_upload_rewinds_and_retries(self):
        self.configure(busy_once='put-gt')
        result = self.impute()
        self.assertEqual(result['steps']['put-gt']['attempts'], 2)
        self.assertGreater(result['steps']['get']['bytes'], 0)
        self.assert_cleaned(1)

    def test_every_failure_cleans_its_id(self):
        for step in ('put-holdout', 'put-gt', 'run', 'get'):
            with self.subTest(step=step):
                self.configure(fail=step)
                with self.assertRaises(GateError):
                    self.impute()
                self.assert_cleaned(len(self.client.jobs))
        self.assertEqual(len(self.client.jobs), 4)

    def test_cleanup_failure_is_reported_and_retried_at_end(self):
        self.configure(fail='clean')
        with self.assertRaises(GateError):
            self.impute()
        job = next(iter(self.client.jobs.values()))
        self.assertFalse(job['cleaned'])
        self.assertIn('cleanup_error', job)
        self.assertEqual(len(self.client.cleanup_all()), 1)
        self.configure()
        self.assertEqual(self.client.cleanup_all(), {})
        self.assert_cleaned(1)

    def test_cancel_during_download_still_cleans(self):
        self.configure(hold_get='22')
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(self.impute)
            self.wait_for('get-started-22')
            self.client.cancel.set()
            with self.assertRaises(GateError):
                future.result(timeout=5)
        self.assert_cleaned(1)

    def test_request_timeout_still_cleans(self):
        self.configure(hold_get='22')
        self.client.download_window = .3
        with self.assertRaisesRegex(GateError, 'timed out'):
            self.impute()
        self.assert_cleaned(1)

    def test_the_design_timings_are_the_defaults(self):
        client = GateClient(TEST_HOST, '/nonexistent/key')
        self.assertEqual(client.poll, 60)
        self.assertEqual(client.ssh_retries, (30, 120, 300))
        self.assertEqual((client.upload_window, client.download_window), (1800, 1800))
        self.assertEqual((client.run_busy_window, client.run_timeout), (11700, 11400))
        self.assertEqual(client.gate_timeout, 10800 + 60)
        argv = ' '.join(client.command)
        for option in ('ConnectTimeout=30', 'ServerAliveInterval=60', 'BatchMode=yes',
                       'StrictHostKeyChecking=yes', 'IdentitiesOnly=yes'):
            self.assertIn(option, argv)

    def test_ssh_failures_are_retried_on_the_schedule_and_then_fail(self):
        self.configure(ssh_fail={'put-holdout': 1, 'put-gt': 3, 'get': 2, 'clean': 2})
        job = self.impute()
        self.assertEqual(job['steps']['put-gt']['ssh_failures'], 3)
        self.assertEqual(job['steps']['put-gt']['attempts'], 4)
        self.assertEqual(job['steps']['get']['ssh_failures'], 2)
        self.assertEqual(job['steps']['clean']['ssh_failures'], 2)
        self.assert_cleaned(1)
        # A fourth failure is past the three retries.
        self.configure(ssh_fail={'put-holdout': 4})
        with self.assertRaisesRegex(GateError, 'ssh failed 4 times'):
            self.impute()
        self.assert_cleaned(2)

    def test_busy_is_polled_every_poll_until_the_window(self):
        self.client.poll = .2
        self.configure(busy_once='put-gt')
        started = time.monotonic()
        job = self.impute()
        self.assertEqual(job['steps']['put-gt']['busy'], 1)
        self.assertGreaterEqual(time.monotonic() - started, .2)
        # Another job holds the box's one Beagle slot for longer than run may wait for it.
        self.client.poll, self.client.run_busy_window = .05, .4
        with open(self.root / 'beagle.lock', 'a') as slot:
            fcntl.flock(slot, fcntl.LOCK_EX)
            with self.assertRaisesRegex(GateError, 'Beagle slot stayed busy'):
                self.impute('21')
        self.assertGreaterEqual(self.count('run', 'busy'), 2)
        self.assert_cleaned(2)

    def test_run_that_never_reached_the_gate_is_sent_again(self):
        self.configure(ssh_fail={'run': 2})
        job = self.impute()
        self.assertEqual(job['steps']['run']['ssh_failures'], 2)
        self.assertNotIn('recovered', job['steps']['run'])
        self.assertEqual(self.count('run'), 1)
        self.assert_cleaned(1)

    def test_dropped_run_is_recovered_through_get_and_never_run_again(self):
        self.configure(drop_run={'delay': .5})
        job = self.impute()
        run = job['steps']['run']
        self.assertTrue(run['recovered'])
        self.assertIn('closed by remote host', run['dropped'])
        self.assertNotIn('192.0.2.1', run['dropped'])
        self.assertGreater(run['recovery_polls'], 1)
        self.assertTrue(job['steps']['get']['collected_by_run_recovery'])
        self.assertGreater(job['steps']['get']['bytes'], 0)
        self.assertEqual(self.count('run'), 1)
        self.assertGreater(self.count('get', 'busy'), 0)
        self.assertEqual(gzip.decompress((self.root / 'out-22/chr22.vcf.gz').read_bytes()),
                         b'output\n')
        self.assert_cleaned(1)

    def test_dropped_run_whose_beagle_did_not_finish_fails_and_is_cleaned(self):
        self.configure(drop_run={'delay': .3, 'beagle_fails': True})
        with self.assertRaisesRegex(GateError, 'Beagle did not finish'):
            self.impute()
        self.assertEqual(self.count('run'), 1)
        self.assert_cleaned(1)

    def test_clean_keeps_trying_until_the_gates_own_timeout_has_passed(self):
        # The harness closes while a dropped run's Beagle is still going: clean answers busy
        # until it ends, and is tried until the gate's timeout has passed since the run.
        self.configure(drop_run={'delay': 1.0})
        self.cancel_once_running()
        with self.assertRaises(GateCancelled):
            self.impute()
        self.assertGreater(self.count('clean', 'busy'), 0)
        self.assert_cleaned(1)

    def test_clean_gives_up_after_the_gates_timeout_and_cleanup_all_finishes_it(self):
        self.client.gate_timeout, self.client.margin = .3, .2
        self.configure(drop_run={'delay': 1.5})
        self.cancel_once_running()
        with self.assertRaises(GateCancelled):
            self.impute()
        job = next(iter(self.client.jobs.values()))
        self.assertFalse(job['cleaned'])
        self.assertIn('gave up', job['cleanup_error'])
        self.assertGreater(self.count('clean', 'busy'), 0)
        time.sleep(1.5)
        self.assertEqual(self.client.cleanup_all(), {})
        self.assert_cleaned(1)

    def test_cancel_drains_active_compute_before_cleaning(self):
        self.configure(run_delay=.4)
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(self.impute)
            self.wait_for('run-started-22')
            self.client.cancel.set()
            with self.assertRaises(GateError):
                future.result(timeout=5)
        trace = self.events()
        self.assertTrue(any(e['verb'] == 'run' and e['event'] == 'end' for e in trace))
        self.assertFalse(any(e['verb'] == 'get' for e in trace))
        self.assert_cleaned(1)

    def test_errors_never_carry_the_gb10_address_or_account(self):
        self.configure(fail='put-holdout',
                       stderr='ssh: connect to host 192.0.2.1: refused; /home/gb10-test/x; '
                              'gb10-test@192.0.2.1\n')
        with self.assertRaises(GateError) as caught:
            self.impute()
        text = str(caught.exception) + json.dumps(list(self.client.jobs.values()))
        self.assertIn('<gb10>', text)
        self.assertNotIn('192.0.2.1', text)
        self.assertNotIn('gb10-test', text)
        with self.assertRaises(TypeError):
            GateClient('/nonexistent/key')  # no default host

    def test_multichromosome_upload_and_thread_change_refused(self):
        path = self.target('22')
        with gzip.open(path, 'ab') as out:
            out.write(b'1\t200\t.\tA\tG\t.\tPASS\t.\n')
        with self.assertRaisesRegex(GateError, 'another chromosome'):
            self.client.impute('22', path, self.holdout, '1', self.root / 'out')
        with self.assertRaisesRegex(ValueError, 'threads=6'):
            self.client.impute('22', path, self.holdout, '1', self.root / 'out', threads=8)
        self.assertEqual(self.client.jobs, {})


class ArchiveTest(unittest.TestCase):
    VCF = gzip.compress(b'vcf bytes')

    def archive(self, variant=None, vcf=VCF):
        out = io.BytesIO()
        with tarfile.open(fileobj=out, mode='w') as tar:
            for name, data in [('chr22.vcf.gz', vcf),
                               ('chr22.log', b'Beagle test finished\n')]:
                item = tarfile.TarInfo(name)
                item.uid = item.gid = 999999
                item.mode = 0
                item.size = len(data)
                if variant and name == 'chr22.vcf.gz':
                    variant(item)
                tar.addfile(item, io.BytesIO(data))
        out.seek(0)
        return out

    def test_archive_metadata_is_never_restored(self):
        with tempfile.TemporaryDirectory() as dest:
            files = copy_archive(self.archive(), '22', dest)
            path = Path(dest) / 'chr22.vcf.gz'
            self.assertEqual(gzip.decompress(path.read_bytes()), b'vcf bytes')
            self.assertEqual(path.stat().st_uid, os.getuid())
            self.assertTrue(path.stat().st_mode & 0o600)
            self.assertEqual(files['chr22.vcf.gz']['bytes'], len(self.VCF))

    def test_a_download_that_is_not_intact_gzip_is_never_delivered(self):
        corrupt = bytearray(self.VCF)
        corrupt[12] ^= 0xFF
        for label, vcf in (('not gzip', b'vcf bytes'), ('truncated', self.VCF[:-6]),
                           ('corrupted', bytes(corrupt)),
                           ('second member cut', self.VCF + gzip.compress(b'more')[:-4])):
            with self.subTest(label), tempfile.TemporaryDirectory() as dest:
                with self.assertRaisesRegex(GateError, 'not intact gzip'):
                    copy_archive(self.archive(vcf=vcf), '22', dest)
                self.assertEqual(list(Path(dest).iterdir()), [])

    def test_unsafe_or_wrong_members_are_rejected_without_publishing(self):
        cases = [lambda t: setattr(t, 'name', '../escape'),
                 lambda t: setattr(t, 'name', '/absolute'),
                 lambda t: setattr(t, 'name', 'chr21.vcf.gz'),
                 lambda t: setattr(t, 'name', 'chr22.log'),
                 lambda t: setattr(t, 'type', tarfile.SYMTYPE),
                 lambda t: setattr(t, 'type', tarfile.LNKTYPE)]
        for variant in cases:
            with self.subTest(variant=variant), tempfile.TemporaryDirectory() as dest:
                with self.assertRaises(GateError):
                    copy_archive(self.archive(variant), '22', dest)
                self.assertEqual(list(Path(dest).iterdir()), [])

    def test_truncated_or_missing_member_is_rejected(self):
        for data in (self.archive().getvalue()[:520], self.archive().getvalue()[:1024]):
            with tempfile.TemporaryDirectory() as dest:
                with self.assertRaises(GateError):
                    copy_archive(io.BytesIO(data), '22', dest)
                self.assertEqual(list(Path(dest).iterdir()), [])


if __name__ == '__main__':
    unittest.main()
