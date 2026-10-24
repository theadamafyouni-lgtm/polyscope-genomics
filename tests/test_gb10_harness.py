"""The harness side of the gb10 route: private config and launch. No SSH, no Beagle."""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from test_impute_broker import REPO, config, gb10, impute_remote, orchestrator
from harness import manifest, probe, profile

HOST = 'gb10-test@192.0.2.1'  # TEST-NET-1, never contacted
FAKE_GATE = str(REPO / 'tests/fixtures/fake_gate.py')


class PrivateConfig:
    """A synthetic gb10.json in a temporary directory, named by PRS_HARNESS_GB10, and the
    harness's sandbox and vault roots moved under the same directory. The gate client the
    harness builds runs the local fake gate, never ssh."""

    def setUp(self):
        super().setUp()
        tmp = tempfile.TemporaryDirectory(prefix='gb10-harness-')
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        self.config_path = self.dir / 'gb10.json'
        self.key = self.dir / 'key'
        self.key.write_text('not a key: only its mode is checked\n')
        self.key.chmod(0o600)
        self.gate = self.dir / 'fake-gate'
        self.gate.mkdir()
        self.configure_gate()
        for patcher in (patch.dict(os.environ, {config.GB10_CONFIG_ENV: str(self.config_path)}),
                        patch.object(config, 'SANDBOXES', str(self.dir / 'sandboxes')),
                        patch.object(config, 'VAULT', str(self.dir / 'vault')),
                        patch.object(config, 'VAULT_RUNS', str(self.dir / 'vault/runs')),
                        patch.object(orchestrator.Orchestrator, '_gb10_client',
                                     lambda o: self.client(o))):
            patcher.start()
            self.addCleanup(patcher.stop)

    def client(self, o):
        return gb10.GateClient(o.gb10_config['host'], o.gb10_config['identity'],
                               command=[sys.executable, FAKE_GATE, str(self.gate)],
                               poll=.01, ssh_retries=(.01, .01, .01), attempt_cap=2)

    def configure_gate(self, **settings):
        (self.gate / 'config.json').write_text(json.dumps(settings))

    def gate_calls(self):
        events = self.gate / 'events.jsonl'
        return [json.loads(line)['verb'] for line in events.read_text().splitlines()
                if json.loads(line)['event'] == 'start'] if events.exists() else []

    def write_config(self, **fields):
        data = {'host': HOST, 'identity': str(self.key), 'dev_cases': ['p00-synthetic-dev']}
        data.update(fields)
        self.config_path.write_text(json.dumps({k: v for k, v in data.items() if v is not None}))

    def persona(self, case='p00-synthetic-dev'):
        """A synthetic persona file, by absolute path; no real persona is read."""
        path = self.dir / ('persona-%s.json' % (case or 'no-id'))
        path.write_text(json.dumps({'persona_id': case} if case else {'name': 'synthetic'}))
        return str(path)

    def orchestrator(self, **kwargs):
        kwargs.setdefault('persona_name', self.persona())
        return orchestrator.Orchestrator(run_id='run-20261013-000000-abcdef',
                                         impute_backend='gb10', **kwargs)

    def launch(self, **kwargs):
        """run() as far as it gets; returns (exception, manifest, orchestrator)."""
        o = self.orchestrator(**kwargs)
        with self.assertRaises(Exception) as caught:
            o.run()
        manifest = json.loads((Path(o.vault_dir) / 'manifest.json').read_text())
        return caught.exception, manifest, o

    def preflight(self, **kwargs):
        """Only the launch checks, with a manifest to record them in."""
        o = self.orchestrator(**kwargs)
        o.manifest = manifest.Manifest(o.run_id, o.vault_dir)
        o._gb10_preflight()
        return o


class ConfigTest(PrivateConfig, unittest.TestCase):
    def test_host_and_key_come_only_from_the_private_file(self):
        with self.assertRaisesRegex(RuntimeError, 'not found'):
            config.load_gb10_config()
        self.write_config()
        cfg = config.load_gb10_config()
        self.assertEqual((cfg['host'], cfg['identity']), (HOST, str(self.key)))

    def test_malformed_fields_refuse_without_echoing_the_host(self):
        for fields in ({'host': None}, {'host': '-oProxyCommand=sh@x'}, {'host': 'a@b c'},
                       {'identity': 'relative/key'}, {'identity': None}, {'dev_cases': None},
                       {'dev_cases': []}, {'dev_cases': ['']}, {'dev_cases': [' p00']},
                       {'dev_cases': 'p00-synthetic-dev'}):
            with self.subTest(fields=fields):
                self.write_config(**fields)
                with self.assertRaises(RuntimeError) as caught:
                    config.load_gb10_config()
                self.assertNotIn('192.0.2.1', str(caught.exception))
                self.assertNotIn('ProxyCommand', str(caught.exception))

    def test_gb10_run_without_the_file_does_not_launch_and_stages_nothing(self):
        error, manifest, o = self.launch()
        self.assertIn('gb10 configuration not found', str(error))
        self.assertFalse(manifest['outcome']['launched'])
        self.assertFalse(os.path.exists(o.sandbox_root))


class AllowListTest(PrivateConfig, unittest.TestCase):
    def test_case_not_on_the_dev_allow_list_does_not_launch(self):
        self.write_config()
        error, manifest, o = self.launch(persona_name=self.persona('p00-synthetic-held-out'))
        self.assertIsInstance(error, orchestrator.Gb10Refused)
        self.assertIn('not on the dev allow-list', str(error))
        self.assertEqual(manifest['outcome']['failed_before_launch']['type'], 'Gb10Refused')
        self.assertFalse(os.path.exists(o.sandbox_root))
        self.assertNotIn('192.0.2.1', json.dumps(manifest))

    def test_no_case_id_is_refused(self):
        self.write_config()
        for kwargs in ({'backend': 'human', 'persona_name': None},
                       {'persona_name': self.persona(None)}):
            with self.subTest(kwargs=kwargs):
                with self.assertRaises(orchestrator.Gb10Refused):
                    self.preflight(**kwargs)

    def test_dev_case_passes_and_is_recorded(self):
        self.write_config()
        o = self.preflight()
        self.assertEqual(o.manifest.data['imputation']['case_id'], 'p00-synthetic-dev')
        self.assertTrue(o.manifest.data['imputation']['dev_allow_list'])
        self.assertEqual(o.manifest.data['imputation']['launch_check']['ping']['attempts'], 1)
        self.assertEqual(self.gate_calls(), ['ping'])


class LaunchCheckTest(PrivateConfig, unittest.TestCase):
    def test_missing_or_open_key_does_not_launch_and_is_not_tried(self):
        self.write_config()
        self.key.chmod(0o644)
        with self.assertRaisesRegex(orchestrator.Gb10Refused, 'open to other accounts'):
            self.preflight()
        self.key.unlink()
        with self.assertRaisesRegex(orchestrator.Gb10Refused, 'cannot be found'):
            self.preflight()
        self.assertEqual(self.gate_calls(), [])

    def test_gb10_that_does_not_answer_does_not_launch(self):
        self.write_config()
        self.configure_gate(fail='ping', stderr='ssh: connect to host 192.0.2.1: timed out\n')
        error, manifest, o = self.launch()
        self.assertIsInstance(error, orchestrator.Gb10Refused)
        self.assertIn('ping', str(error))
        self.assertFalse(manifest['outcome']['launched'])
        self.assertFalse(os.path.exists(o.sandbox_root))
        self.assertNotIn('192.0.2.1', json.dumps(manifest))
        self.assertEqual(self.gate_calls(), ['ping'])



class KeyProbeTest(PrivateConfig, unittest.TestCase):
    def probe_key(self, sandbox):
        """The real denial probe, under a real bubblewrap profile for a scratch sandbox."""
        jail = sandbox / '.home'
        jail.mkdir(parents=True)
        (sandbox / 'tmp').mkdir()
        text = profile.build_profile(str(sandbox), str(jail), None, allow_keychain=False,
                                     label='key-probe-test')
        # One profile per sandbox: write_profile leaves each 0444, which only root could rewrite.
        path = profile.write_profile(str(self.dir / ('profile-%s.sb' % sandbox.name)), text)
        env = {'PATH': config.SANDBOX_PATH, 'HOME': str(jail), 'TMPDIR': str(sandbox / 'tmp')}
        verdict = probe.evaluate_probe(probe.run_denial_probe(path, str(sandbox), str(jail), env))
        reads = {r['path']: r['result'] for r in verdict['detail']['reads']}
        return verdict, reads

    def test_every_run_probes_the_configured_key(self):
        self.assertNotIn(str(self.key), [p for p, _ in config.deny_probe_paths('/jail')])
        self.write_config()
        paths = [p for p, _ in config.deny_probe_paths('/jail')]
        self.assertIn(str(self.key), paths)
        self.assertIn(os.path.expanduser('~/.ssh'), paths)
        self.config_path.write_text('{not json')
        with self.assertRaisesRegex(RuntimeError, 'gb10 configuration'):
            config.deny_probe_paths('/jail')

    @unittest.skipUnless(os.access(config.SANDBOX_EXEC, os.X_OK), 'needs the bwrap shim')
    def test_probe_finds_the_key_unreachable_and_would_catch_a_reachable_one(self):
        self.write_config()
        verdict, reads = self.probe_key(self.dir / 'sandbox')
        self.assertIn(reads[str(self.key)], ('absent', 'denied'))
        self.assertIn(reads[os.path.expanduser('~/.ssh')], ('absent', 'denied'))
        # The control: the same probe, with the key placed inside a sandbox, reports it.
        inside = self.dir / 'leaky' / 'key'
        inside.parent.mkdir()
        inside.write_text('not a key\n')
        self.write_config(identity=str(inside))
        verdict, reads = self.probe_key(self.dir / 'leaky')
        self.assertEqual(reads[str(inside)], 'READABLE')
        self.assertFalse(verdict['passed'])



class FilingTest(PrivateConfig, unittest.TestCase):
    """A gb10 failure is an environment failure: nothing is filed under results/."""

    def requests(self, *causes, status='error'):
        return {'backend': 'gb10', 'requests': [
            {'message_id': 'i-%012x' % i, 'chromosome': str(7 + i), 'status': status,
             'cause': cause, 'reason': 'run: gate exit 7'} for i, cause in enumerate(causes)]}

    def test_only_the_gb10s_own_failures_block(self):
        blocked = orchestrator.gb10_blocked_reason
        self.assertEqual(blocked(self.requests('request', 'gb10'), False),
                         'gb10 blocked: chr8 run: gate exit 7')
        self.assertIsNone(blocked(self.requests('gb10'), True))
        self.assertIsNone(blocked(self.requests('request', 'closing'), False))
        self.assertIsNone(blocked(self.requests('gb10', status='delivered'), False))
        self.assertIsNone(blocked({'backend': 'local'}, False))
        self.assertIsNone(blocked(None, False))

    def test_a_run_the_gb10_failed_is_not_filed(self):
        self.write_config()
        o = self.orchestrator(arm='arm-test', pass_name='pass-test')
        o.manifest = manifest.Manifest(o.run_id, o.vault_dir)
        o.manifest.set('outcome', {'prs_returncode': 0, 'prs_stdout_tail': 'ended'})
        o.manifest.set('transcript', {'n_messages': 12})
        o.manifest.set('result', {'state': 'absent', 'valid': False})
        o.manifest.set('imputation', self.requests('gb10'))
        results = self.dir / 'results'
        with patch.object(orchestrator, 'RESULTS_ROOT', str(results)):
            rec = o.publish()
        self.assertEqual(rec['state'], 'environment-failure')
        self.assertEqual(rec['environment_failure'], 'gb10 blocked: chr7 run: gate exit 7')
        self.assertFalse(results.exists())


INSIDE = r'''
import json, os, shutil
out = {"java": shutil.which("java"), "jvm_entries": sorted(os.listdir("/usr/lib/jvm"))
       if os.path.isdir("/usr/lib/jvm") else None}
for label, act in (("write", lambda: open("refdata/imputation.json", "w")),
                   ("remove", lambda: os.remove("refdata/imputation.json")),
                   ("create", lambda: open("refdata/bin/beagle.jar", "w"))):
    try:
        act()
        out[label] = "allowed"
    except OSError as exc:
        out[label] = exc.strerror
out["switch"] = json.load(open("refdata/imputation.json"))
print(json.dumps(out))
'''


class RouteTest(PrivateConfig, unittest.TestCase):
    """The harness alone picks where Beagle runs; on the gb10 route nothing can run it here."""

    def stage(self, route, demo=False):
        """Stage a real PRS sandbox from a synthetic refdata tree and a synthetic genome."""
        src = self.dir / 'refdata-src'
        if not src.exists():
            for rel in ('bin/beagle.jar', 'bin/plink2', 'jre/bin/java', 'panel-snv/x.vcf.gz'):
                (src / rel).parent.mkdir(parents=True, exist_ok=True)
                (src / rel).write_text('synthetic\n')
        genome = self.dir / 'synthetic-genome.txt'
        genome.write_text('# synthetic\n')
        o = orchestrator.Orchestrator(run_id='run-20261013-000000-%s' % ('c' * 6 if demo else 'a' * 6
                                                                         if route == 'local' else 'b' * 6),
                                      patient_file=str(genome), impute_backend=route, demo=demo)
        o.manifest = manifest.Manifest(o.run_id, o.vault_dir)
        os.makedirs(o.prs_root)
        with patch.object(config, 'REFDATA_SRC', str(src)):
            o._stage_prs()
        return o

    def test_only_a_demo_run_gets_the_demo_settings(self):
        """The demo's speedups -- a scoring file in byte ranges -- are switched on by a file the
        harness writes into the read-only refdata/, for a demo run and no other."""
        self.assertFalse(os.path.exists(os.path.join(self.stage('local').prs_root, 'refdata', 'demo.json')))
        demo = self.stage('local', demo=True)
        with open(os.path.join(demo.prs_root, 'refdata', 'demo.json')) as fh:
            self.assertEqual(json.load(fh), {'parallel_downloads': 8})

    def inside(self, o, hide):
        """Run INSIDE under the PRS profile the route gets; nothing here starts a JVM."""
        jail = os.path.join(o.prs_root, '.home')
        text = profile.build_profile(o.prs_root, jail, None, allow_keychain=False,
                                     label='prs', hide=hide)
        path = profile.write_profile(str(self.dir / ('profile-%s.sb' % o.run_id)), text)
        env = {'PATH': config.SANDBOX_PATH, 'HOME': jail, 'TMPDIR': os.path.join(o.prs_root, 'tmp')}
        proc = subprocess.run([config.SANDBOX_EXEC, '-f', path, '/usr/bin/python3', '-I',
                               '-c', INSIDE], cwd=o.prs_root, env=env, capture_output=True,
                              text=True, timeout=60)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        return json.loads(proc.stdout), path, jail, env

    def test_the_switch_is_staged_by_the_harness_and_nothing_overrides_it(self):
        local, remote = self.stage('local'), self.stage('gb10')
        for o, route in ((local, 'local'), (remote, 'gb10')):
            switch = Path(o.prs_root, 'refdata/imputation.json')
            self.assertEqual(impute_remote.configured_backend(switch), route)
            self.assertFalse(Path(o.prs_root, 'comms/imputation.json').exists())
        self.assertEqual(impute_remote.configured_backend(self.dir / 'absent.json'), 'local')
        # Beagle and the bundled JVM are in the local clone and withheld from the gb10 one.
        for rel in ('bin/beagle.jar', 'jre/bin/java'):
            self.assertTrue(Path(local.prs_root, 'refdata', rel).exists())
            self.assertFalse(Path(remote.prs_root, 'refdata', rel).exists())
        self.assertTrue(Path(remote.prs_root, 'refdata/bin/plink2').exists())
        self.assertEqual({d['path'] for d in remote.record.deleted if d['status'] == 'deleted'},
                         {'bin/beagle.jar', 'jre'})
        # No option on the tool, and no name for the switch in the skill.
        required = [f'--{flag}=x' for flag in (
            'run', 'target', 'patient-id', 'holdout-samples', 'reference-samples', 'phase3-dir',
            'map-dir', 'typed-positions', 'chromosomes', 'seed', 'out-dir')]
        proc = subprocess.run([sys.executable, str(REPO / 'agent/tools/impute_joint.py'),
                               *required, '--backend', 'local'], capture_output=True, text=True,
                              timeout=60)
        self.assertEqual(proc.returncode, 2)
        self.assertIn('unrecognized arguments: --backend local', proc.stderr)
        skill = (REPO / 'agent/.claude/skills/score/SKILL.md').read_text()
        self.assertNotIn('imputation.json', skill)

    def test_the_manifest_records_the_threads_impute_joint_used(self):
        o = self.stage('local')
        self.assertEqual(o.manifest.data['imputation'], {'backend': 'local'})
        out = Path(o.prs_root, 'runs', o.run_id, 'score/imputed_joint')
        out.mkdir(parents=True)
        (out / 'impute_joint.json').write_text(json.dumps({
            'backend': 'local', 'seed': '20261013', 'n_ok': 22, 'n_failed': 0,
            'parameters': {'threads': '4', 'parallel_chromosomes': 2}}))
        elsewhere = self.dir / 'elsewhere.json'
        elsewhere.write_text(json.dumps({'parameters': {'threads': '99'}}))
        linked = Path(o.prs_root, 'runs', o.run_id, 'linked')
        linked.mkdir()
        (linked / 'impute_joint.json').symlink_to(elsewhere)
        fifo = Path(o.prs_root, 'runs', o.run_id, 'piped')
        fifo.mkdir()
        os.mkfifo(fifo / 'impute_joint.json')
        runs = o._impute_joint_runs()
        self.assertEqual(runs[1]['path'], 'runs/%s/piped/impute_joint.json' % o.run_id)
        self.assertIn('not a regular file', runs[1]['unreadable'])
        self.assertEqual(runs[2], {
            'path': 'runs/%s/score/imputed_joint/impute_joint.json' % o.run_id,
            'backend': 'local', 'threads': '4', 'seed': '20261013',
            'parallel_chromosomes': 2, 'n_ok': 22, 'n_failed': 0})
        self.assertEqual(runs[0]['path'], 'runs/%s/linked/impute_joint.json' % o.run_id)
        self.assertNotIn('threads', runs[0])
        self.assertIn('unreadable', runs[0])

    @unittest.skipUnless(os.access(config.SANDBOX_EXEC, os.X_OK) and shutil.which('java'),
                         'needs the bwrap shim and a java to hide')
    def test_gb10_sandbox_cannot_run_or_reach_beagle_and_cannot_change_the_switch(self):
        o = self.stage('gb10')
        seen, path, jail, env = self.inside(o, profile.jvm_dirs())
        self.assertIsNone(seen['java'])
        self.assertEqual(seen['jvm_entries'], [])
        self.assertEqual(seen['switch'], {'backend': 'gb10'})
        for act in ('write', 'remove', 'create'):
            self.assertEqual(seen[act], 'Read-only file system')
        withheld = o._gb10_withheld()
        self.assertIn(os.path.realpath(shutil.which('java')), [p for p, _ in withheld])
        verdict = probe.evaluate_probe(probe.run_denial_probe(path, o.prs_root, jail, env,
                                                              extra_reads=withheld))
        results = {r['path']: r['result'] for r in verdict['detail']['reads']}
        self.assertTrue(verdict['passed'], verdict.get('failure'))
        self.assertTrue(all(results[p] in ('absent', 'denied') for p, _ in withheld), results)
        # The control: the local route's profile hides nothing, and java is there.
        seen, *_ = self.inside(self.stage('local'), ())
        self.assertEqual(seen['java'], shutil.which('java'))


if __name__ == '__main__':
    unittest.main()
