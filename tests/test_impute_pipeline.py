"""Synthetic whole-tool runs of impute_joint.py: local Beagle vs the host broker/gate
transport, and the gb10 route through a fake gate that runs no Beagle."""
import gzip
import json
import os
import random
import re
import shutil
import subprocess
import sys
import threading
import unittest
from pathlib import Path

from test_impute_broker import BrokerFixture, REPO, orchestrator
from test_impute_target import BEAGLE, body, row, write_vcf
from harness import manifest


def output_files(directory):
    """Every file a run of impute_joint.py leaves in its --out-dir."""
    return sorted(str(p.relative_to(directory)) for p in Path(directory).rglob('*')
                  if p.is_file())


class SyntheticInputs(BrokerFixture):
    """Two synthetic chromosomes laid out as a staged sandbox has them, the real router
    running, and impute_joint.py run the way the agent runs it."""

    def setUp(self):
        super().setUp()
        tools = self.prs / 'tools'
        tools.mkdir()
        for name in ('impute_joint.py', 'impute_remote.py', 'lib_run.py'):
            shutil.copyfile(REPO / 'agent/tools' / name, tools / name)
        binary = self.prs / 'refdata/bin'
        binary.mkdir(parents=True)
        (binary / 'beagle.jar').symlink_to(BEAGLE)
        self.panel = self.prs / 'refdata/panel-snv'
        self.phase3 = self.prs / 'refdata/phase3'
        self.maps = self.prs / 'refdata/maps'
        for d in (self.panel, self.phase3, self.maps):
            d.mkdir(parents=True)
        names = [f'REFERENCE_{i:02}' for i in range(64)]
        self.hold.write_text('\n'.join(names[:8]) + '\n')
        self.reference = self.run / 'reference.txt'
        self.reference.write_text('\n'.join(names[8:]) + '\n')
        patients, positions = [], []
        rng = random.Random(193)
        donors = [(i % 8, (i * 3 + 1) % 8) for i in range(len(names))]
        for chrom in ('1', '22'):
            rows = []
            for i in range(300):
                alleles = [rng.randrange(2) for _ in range(8)]
                alleles[0], alleles[1] = 0, 1
                genotypes = [f'{alleles[a]}|{alleles[b]}' for a, b in donors]
                pos = 1_000_000 + i * 1000
                rows.append(row(chrom, pos, genotypes))
                if i % 3 == 0:
                    patients.append(row(chrom, pos, [genotypes[17].replace('|', '/')]))
                    positions.append(f'{chrom}\t{pos}')
            panel = write_vcf(self.panel, f'chr{chrom}.snv', names, rows)
            for suffix in ('', '.tbi'):
                shutil.copyfile(str(panel) + suffix, self.phase3 / f'chr{chrom}.vcf.gz{suffix}')
            (self.maps / f'plink.chr{chrom}.GRCh37.map').write_text(
                f'{chrom} . 0 1000000\n{chrom} . 3 1300000\n')
        self.target = write_vcf(self.run, 'patient', ['PATIENT'], patients)
        self.typed = self.run / 'typed.tsv'
        self.typed.write_text('\n'.join(positions) + '\n')
        self.settings = {'panel': str(self.panel), 'maps': str(self.maps)}
        self.write_settings()
        self.o._router = threading.Thread(target=self.o._route_forever, args=({},))
        self.o._router.start()
        self.addCleanup(self.stop_router)

    def stop_router(self):
        self.o._stop.set()
        self.o._router.join(timeout=5)

    def write_settings(self):
        (self.root / 'fake-gate/config.json').write_text(json.dumps(self.settings))

    def pipeline(self, backend, returncode=0):
        # What the harness stages for each route: the switch in refdata/, and on the gb10
        # route no Beagle in the tree.
        (self.prs / 'refdata/imputation.json').write_text(json.dumps({'backend': backend}))
        jar = self.prs / 'refdata/bin/beagle.jar'
        if backend == 'gb10' and jar.is_symlink():
            jar.unlink()
        elif backend == 'local' and not jar.is_symlink():
            jar.symlink_to(BEAGLE)
        output = self.run / backend
        command = [sys.executable, str(self.prs / 'tools/impute_joint.py'), '--run', self.run_id,
                   '--target', str(self.target), '--patient-id', 'PATIENT',
                   '--holdout-samples', str(self.hold), '--reference-samples', str(self.reference),
                   '--phase3-dir', str(self.phase3), '--panel-dir', str(self.panel),
                   '--map-dir', str(self.maps), '--typed-positions', str(self.typed),
                   '--chromosomes', '1,22', '--seed', '20261008', '--threads', '6',
                   '--parallel-chromosomes', '2', '--xmx', '512m', '--out-dir', str(output)]
        proc = subprocess.run(command, cwd=self.prs, capture_output=True, text=True, timeout=90)
        self.assertEqual(proc.returncode, returncode, proc.stdout + '\n' + proc.stderr)
        self.stderr = proc.stderr
        return output, json.loads((output / 'impute_joint.json').read_text())


@unittest.skipUnless(shutil.which('bcftools') and shutil.which('java') and BEAGLE.is_file(),
                     'bcftools, java and BEAGLE_JAR are required')
class PipelineTest(SyntheticInputs, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.settings['beagle_jar'] = str(BEAGLE)
        self.write_settings()

    def test_full_tool_backend_identity(self):
        # When supplied, exercise the actual installed gate's protocol on a disposable
        # local copy. Only its root and disk threshold change; Beagle stays at 6 threads.
        gate_source = os.environ.get('GB10_GATE_SCRIPT')
        if gate_source:
            remote = self.root / 'local-gb10'
            for directory in ('bin', 'panel-snv', 'maps'):
                (remote / 'refdata' / directory).mkdir(parents=True)
            shutil.copyfile(BEAGLE, remote / 'refdata/bin/beagle.jar')
            for chrom in ('1', '22'):
                shutil.copyfile(self.panel / f'chr{chrom}.snv.vcf.gz',
                                remote / f'refdata/panel-snv/chr{chrom}.snv.vcf.gz')
                shutil.copyfile(self.maps / f'plink.chr{chrom}.GRCh37.map',
                                remote / f'refdata/maps/plink.chr{chrom}.GRCh37.map')
            script = self.root / 'local-gate.sh'
            original = Path(gate_source).read_text()
            # The gate's own ROOT names the gb10 account, which stays out of the repository.
            self.assertEqual(len(re.findall(r'(?m)^ROOT=\S+', original)), 1)
            self.assertIn('MIN_FREE_GB=50', original)
            script.write_text(re.sub(r'(?m)^ROOT=\S+', f'ROOT={remote}', original)
                              .replace('MIN_FREE_GB=50', 'MIN_FREE_GB=0'))
            self.client.command = [sys.executable, str(REPO / 'tests/fixtures/local_gate.py'),
                                   str(script)]
        local_dir, local = self.pipeline('local')
        remote_dir, remote = self.pipeline('gb10')
        self.assertEqual(output_files(local_dir), output_files(remote_dir))
        for result in (local, remote):
            self.assertEqual(result['n_ok'], 2, result['per_chromosome'])
            self.assertEqual(result['n_failed'], 0)
            self.assertEqual(result['parameters']['threads'], '6')
        self.assertEqual(remote['backend'], 'gb10')
        self.assertEqual(remote['parameters']['xmx'], '10g')
        for chrom in ('1', '22'):
            for relative in (f'chr{chrom}.vcf.gz', f'cohort/chr{chrom}.cohort.vcf.gz'):
                self.assertEqual(body(local_dir / relative), body(remote_dir / relative))
            with gzip.open(local_dir / f'chr{chrom}.dr2.tsv.gz', 'rb') as a, gzip.open(
                    remote_dir / f'chr{chrom}.dr2.tsv.gz', 'rb') as b:
                self.assertEqual(a.read(), b.read())
        jobs = list(self.client.jobs.values())
        self.assertEqual(len(jobs), 2)
        self.assertEqual(len({j['run_id'] for j in jobs}), 2)
        self.assertTrue(all(j['cleaned'] for j in jobs))
        self.assertTrue(all(r['beagle_reference_samples'] == 56 and
                            r['beagle_study_samples'] == 9 for r in remote['per_chromosome']))
        print('\nPIPELINE IDENTITY: 2 synthetic chromosomes, 600 sites; local and gate-backed '
              'cohort/patient VCF records and DR2 tables identical; threads=6; all IDs cleaned',
              flush=True)
        if gate_source:
            self.assertEqual(list((self.root / 'local-gb10/runs').iterdir()), [])
            print('GATE: installed gate copy + real Beagle; no SSH; remote run tree empty',
                  flush=True)



@unittest.skipUnless(shutil.which('bcftools'), 'bcftools is required')
class Gb10RouteTest(SyntheticInputs, unittest.TestCase):
    """The gb10 route end to end, through the fake gate: no Beagle runs anywhere."""

    def test_the_score_stage_opens_when_imputation_starts(self):
        """The live page shows a stage from the ledger's first line for it (ledger_tail), and
        imputation is most of the score stage: impute_joint.py records its start, not only its
        end, or the page says it is waiting for the next step for half an hour."""
        self.pipeline('local')
        ledger = self.prs / 'runs' / self.run_id / 'ledger.jsonl'
        mine = [json.loads(line) for line in ledger.read_text().splitlines()
                if json.loads(line).get('tool') == 'impute_joint.py']
        self.assertEqual([(line['kind'], line['stage']) for line in mine], [('facts', 'score'), ('artifact', 'score')])
        self.assertEqual(mine[0]['chromosomes'], ['1', '22'])

    def test_gb10_route_leaves_the_files_a_local_run_leaves(self):
        self.settings['canned'] = 56  # the reference samples Beagle would report
        self.write_settings()
        output, result = self.pipeline('gb10')
        self.assertEqual(result['n_ok'], 2, result['per_chromosome'])
        local_shape = ['impute_joint.json'] + [
            name % chrom for chrom in ('1', '22') for name in (
                'chr%s.vcf.gz', 'chr%s.vcf.gz.tbi', 'chr%s.dr2.tsv.gz', 'chr%s.beagle.log',
                'cohort/chr%s.cohort.vcf.gz', 'cohort/chr%s.cohort.vcf.gz.tbi',
                'cohort/chr%s.cohort.log')]
        self.assertEqual(output_files(output), sorted(local_shape))
        for chrom, entry in zip(('1', '22'), result['per_chromosome']):
            log = (output / f'cohort/chr{chrom}.cohort.log').read_text()
            self.assertIn('Study samples', log)
            self.assertTrue((output / f'chr{chrom}.beagle.log').read_text().endswith(log))
            self.assertEqual((entry['beagle_reference_samples'], entry['beagle_study_samples']),
                             (56, 9))

    def test_gb10_failure_stops_the_tool_and_is_an_environment_failure(self):
        self.settings['fail'] = 'run'
        self.write_settings()
        output, result = self.pipeline('gb10', returncode=1)
        self.assertIn('Beagle on the gb10 failed', self.stderr)
        self.assertEqual(result['gb10_failure']['cause'], 'gb10')
        self.assertEqual(result['n_ok'], 0)
        self.assertEqual(result['n_failed'], 2)
        self.assertEqual(list(output.glob('*.vcf.gz')) + list(output.glob('cohort/*')), [])
        self.assertTrue(all(r['failed_step'] == 'gb10' for r in result['per_chromosome']))
        # The harness's side: every gate id cleaned, and the run is an environment failure.
        self.o.manifest = manifest.Manifest(self.run_id, self.o.vault_dir)
        self.o._close_imputation()
        imputation = self.o.manifest.data['imputation']
        self.assertTrue(imputation['gate_runs'])
        self.assertTrue(all(j['cleaned'] for j in imputation['gate_runs']))
        self.assertRegex(orchestrator.gb10_blocked_reason(imputation, False),
                         r'^gb10 blocked: chr(1|22) run: ')
        self.assertIsNone(orchestrator.gb10_blocked_reason(imputation, True))


if __name__ == '__main__':
    unittest.main()
