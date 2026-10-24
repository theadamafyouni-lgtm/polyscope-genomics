#!/usr/bin/env python3
"""Local gate double: real per-run/global locks, event trace, injected failures.

Besides the gate's own answers it plays ssh's: `ssh_fail` makes the first N requests of a verb
fail as a connection that never reached the gate (exit 255), and `drop_run` makes `run` lose
its connection (exit 255) while a detached "Beagle" goes on holding the run's and the box's
locks for `delay` seconds, then finishes -- or not, with `beagle_fails`.
"""
import fcntl
import gzip
import io
import json
import shutil
import subprocess
import sys
import tarfile
import time
from pathlib import Path

state = Path(sys.argv[1])
words = sys.argv[2].split()
verb = words[0]
rid = words[1] if len(words) > 1 else None
chrom = words[2] if len(words) > 2 else None
settings = json.loads((state / 'config.json').read_text())

# The request never reaches the gate: no lock taken, no event, nothing run.
failures = settings.get('ssh_fail', {}).get(verb, 0)
if failures:
    counter = state / ('ssh-failed-' + verb)
    with open(counter, 'a+') as count:
        fcntl.flock(count, fcntl.LOCK_EX)
        count.seek(0)
        n = len(count.read())
        if n < failures:
            count.write('x')
            sys.stderr.write('ssh: connect to host 192.0.2.1 port 22: Connection timed out\n')
            sys.exit(255)



def canned(gt, reference_samples):
    """Beagle-shaped output without Beagle: the uploaded target's own records and samples,
    phased, with DS, GP, DR2 and IMP, and a log with the sample counts Beagle reports. Not an
    imputation -- only the shape impute_joint.py reads back."""
    rows = []
    with gzip.open(gt, 'rt') as source:
        for line in source:
            fields = line.rstrip('\n').split('\t')
            if line.startswith('#CHROM'):
                samples = fields[9:]
            elif not line.startswith('#'):
                calls = []
                for call in fields[9:]:
                    alleles = [int(a == '1') for a in call.split(':')[0].replace('|', '/').split('/')]
                    probabilities = ['0', '0', '0']
                    probabilities[sum(alleles)] = '1'
                    calls.append('%d|%d:%d:%s' % (*alleles, sum(alleles), ','.join(probabilities)))
                rows.append('\t'.join(fields[:7] + ['DR2=0.50;AF=0.50;IMP', 'GT:DS:GP'] + calls))
    header = ['##fileformat=VCFv4.2',
              '##INFO=<ID=AF,Number=A,Type=Float,Description="Estimated ALT allele frequency">',
              '##INFO=<ID=DR2,Number=A,Type=Float,Description="Dosage R-squared">',
              '##INFO=<ID=IMP,Number=0,Type=Flag,Description="Imputed marker">',
              '##FORMAT=<ID=GT,Number=1,Type=String,Description="Genotype">',
              '##FORMAT=<ID=DS,Number=A,Type=Float,Description="Estimated ALT dose">',
              '##FORMAT=<ID=GP,Number=G,Type=Float,Description="Genotype probability">',
              '\t'.join(['#CHROM', 'POS', 'ID', 'REF', 'ALT', 'QUAL', 'FILTER', 'INFO', 'FORMAT']
                        + samples)]
    vcf = subprocess.run(['bgzip', '-c'], input='\n'.join(header + rows).encode() + b'\n',
                         capture_output=True, check=True).stdout
    log = ('beagle.27Feb25.75f.jar (version 5.5), canned by the fake gate: no Beagle ran\n'
           'Reference samples: %15s\nStudy samples: %19s\n\nfake gate finished\n'
           % ('{:,}'.format(reference_samples), '{:,}'.format(len(samples))))
    return vcf, log.encode()


DETACHED = """
import pathlib, sys, time
time.sleep(float(sys.argv[2]))
if sys.argv[3] == 'finishes':
    pathlib.Path(sys.argv[1], 'done').touch()
"""


def event(action):
    data = json.dumps({'event': action, 'verb': verb, 'id': rid, 'chrom': chrom,
                       'time': time.monotonic(), 'args': words}) + '\n'
    with open(state / 'events.jsonl', 'a') as log:
        fcntl.flock(log, fcntl.LOCK_EX)
        log.write(data)


def lock(name):
    fd = open(state / (name + '.lock'), 'a')
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        event('busy')
        sys.exit(6)
    return fd


def wait_for(name):
    deadline = time.monotonic() + 8
    while not (state / name).exists():
        if time.monotonic() > deadline:
            sys.exit('test barrier timed out: ' + name)
        time.sleep(.01)


run_lock = lock(rid) if rid else None
event('start')
if settings.get('fail') == verb:
    sys.stderr.write(settings.get('stderr', ''))
    sys.exit(5)
if settings.get('busy_once') == verb and not (state / (verb + '.retried')).exists():
    (state / (verb + '.retried')).touch()
    sys.exit(6)
work = state / str(rid)
if verb == 'ping':
    print('ok fake-gate')
elif verb in ('put-gt', 'put-holdout'):
    work.mkdir(exist_ok=True)
    (work / ('gt.vcf.gz' if verb == 'put-gt' else verb)).write_bytes(sys.stdin.buffer.read())
elif verb == 'run':
    compute_lock = lock('beagle')
    assert words[3] == '6'
    (state / ('run-started-' + chrom)).touch()
    drop = settings.get('drop_run')
    if drop:
        # Beagle inherits both locks, as it does from the real gate, and outlives this session.
        subprocess.Popen([sys.executable, '-c', DETACHED, str(work), str(drop.get('delay', .3)),
                          'fails' if drop.get('beagle_fails') else 'finishes'],
                         pass_fds=(run_lock.fileno(), compute_lock.fileno()),
                         start_new_session=True)
        event('dropped')
        sys.stderr.write('Connection to 192.0.2.1 closed by remote host.\n')
        sys.exit(255)
    time.sleep(settings.get('run_delay', .02))
    if settings.get('beagle_jar'):
        result = subprocess.run(['java', '-Xmx512m', '-jar', settings['beagle_jar'],
            f'gt={work / "gt.vcf.gz"}', f'ref={settings["panel"]}/chr{chrom}.snv.vcf.gz',
            f'map={settings["maps"]}/plink.chr{chrom}.GRCh37.map', f'out={work / "out"}',
            f'chrom={chrom}', 'nthreads=6', f'seed={words[4]}', 'gp=true', 'impute=true',
            f'excludesamples={work / "put-holdout"}'], capture_output=True, timeout=60)
        if result.returncode:
            sys.stderr.buffer.write(result.stdout + result.stderr)
            sys.exit(result.returncode)
    (work / 'done').touch()
elif verb == 'get':
    if not (work / 'done').exists():
        event('not-finished')
        sys.exit(5)
    (state / ('get-started-' + chrom)).touch()
    if settings.get('hold_get') == chrom:
        wait_for('release-get-' + chrom)
    with tarfile.open(fileobj=sys.stdout.buffer, mode='w|') as tar:
        members = [('vcf.gz', gzip.compress(b'output\n')), ('log', b'Beagle test finished\n')]
        if settings.get('canned'):
            members = list(zip(('vcf.gz', 'log'),
                               canned(work / 'gt.vcf.gz', settings['canned'])))
        for suffix, data in members:
            if settings.get('beagle_jar'):
                data = (work / ('out.' + suffix)).read_bytes()
            name = f'chr{chrom}.{suffix}'
            member = tarfile.TarInfo(name)
            member.size = len(data)
            member.uid = 999999
            member.gid = 999999
            member.mode = 0
            tar.addfile(member, io.BytesIO(data))
elif verb == 'clean':
    shutil.rmtree(work, ignore_errors=True)
else:
    sys.exit(2)
event('end')
