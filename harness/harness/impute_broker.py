"""Bounded chromosome handoff from the PRS sandbox to the host-owned gb10 client."""
import json
import os
import re
import shutil
import tempfile
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from . import gb10, provider

MESSAGE_RE = re.compile(r'i-[0-9a-f]{12}')


def failure_cause(exc):
    """Whose failure a request's was. The harness files the run on it (gb10_blocked_reason).

    'request': the agent's own request or files -- a path outside its run, a link, an input
    that is not one chromosome or is too big. 'closing': the harness stopped it because the
    agent's run had ended. 'gb10': everything else -- the gb10, the network, the gate, Beagle
    there, a download that did not validate, or delivering it here.
    """
    if isinstance(exc, gb10.GateCancelled):
        return 'closing'
    if isinstance(exc, (provider.OutsideRun, ValueError)):
        return 'request'
    if isinstance(exc, gb10.GateError) and exc.step in ('target', 'upload'):
        return 'request'
    return 'gb10'


def copy_input(path, prs_root, run_root, destination):
    """Copy from a verified descriptor, not an agent-controlled path reopened later."""
    real = provider.resolve_inside(path, run_root, prs_root, 'imputation input')
    fd = provider.open_regular(real, run_root, 'imputation input', gb10.MAX_UPLOAD - 1)
    with os.fdopen(fd, 'rb') as source, open(destination, 'xb') as dest:
        remaining = gb10.MAX_UPLOAD
        while remaining:
            data = source.read(min(1 << 20, remaining))
            if not data:
                break
            dest.write(data)
            remaining -= len(data)
        if not remaining or dest.tell() == 0:
            raise ValueError('input must be nonempty and below the gate upload cap')


def deliver(source, dir_fd, name):
    """Stream a large host-owned file into the sandbox through a fixed directory fd."""
    temporary = '.' + uuid.uuid4().hex + '.part'
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                 0o600, dir_fd=dir_fd)
    try:
        with os.fdopen(fd, 'wb') as dest, open(source, 'rb') as data:
            shutil.copyfileobj(data, dest, 1 << 20)
            dest.flush()
            os.fsync(dest.fileno())
        os.replace(temporary, name, src_dir_fd=dir_fd, dst_dir_fd=dir_fd)
    finally:
        try:
            os.unlink(temporary, dir_fd=dir_fd)
        except FileNotFoundError:
            pass


class ImputeBroker:
    def __init__(self, prs_root, run_id, vault_dir, client, reply):
        self.prs_root = os.path.realpath(prs_root)
        self.run_id = run_id
        self.vault_dir = Path(vault_dir) / 'imputation'
        self.vault_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.client = client
        self.reply = reply
        self.pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix='gb10')
        self._lock = threading.Lock()
        self._in_flight = {}
        self._seen = set()
        self._closed = False
        self.requests = []

    def submit(self, msg):
        mid = msg.get('message_id', '')
        if not isinstance(mid, str) or not MESSAGE_RE.fullmatch(mid):
            raise ValueError('invalid chromosome message id')
        try:
            if msg.get('run_id') != self.run_id:
                raise ValueError('imputation request belongs to another run')
            gb10.validate_request(msg.get('chromosome'), msg.get('seed'), msg.get('threads'))
            for key in ('target', 'holdout'):
                if not isinstance(msg.get(key), str) or not msg[key]:
                    raise ValueError('missing imputation input path')
        except ValueError as exc:
            self.reply(mid, {'status': 'error', 'cause': 'request', 'reason': str(exc)})
            return
        with self._lock:
            if self._closed:
                self.reply(mid, {'status': 'error', 'cause': 'closing',
                                 'reason': 'imputation broker is closing'})
                return
            if mid in self._seen:
                return  # Delivery retries must never launch the same request twice.
            if len(self._in_flight) >= 2:
                self.reply(mid, {'status': 'busy', 'reason': 'two chromosomes already in flight',
                                 'in_flight': list(self._in_flight)})
                return
            self._seen.add(mid)
            self._in_flight[mid] = dict(msg)
            self.pool.submit(self._work, dict(msg))

    def _work(self, msg):
        mid = msg['message_id']
        chrom = str(msg['chromosome'])
        # The threads and seed this request asked for; submit() refuses any but 6 threads.
        record = {'message_id': mid, 'chromosome': chrom, 'threads': msg['threads'],
                  'seed': msg['seed']}
        try:
            run_root = provider.real_run_dir(self.prs_root, self.run_id)
            with tempfile.TemporaryDirectory(prefix=mid + '-', dir=self.vault_dir) as scratch:
                work = Path(scratch)
                copy_input(msg['target'], self.prs_root, run_root, work / 'target.vcf.gz')
                copy_input(msg['holdout'], self.prs_root, run_root, work / 'holdout.txt')
                receipt = self.client.impute(chrom, work / 'target.vcf.gz', work / 'holdout.txt',
                                             msg['seed'], work / 'result', msg['threads'])
                record['gate'] = receipt
                if self.client.cancel.is_set():
                    raise gb10.GateCancelled('deliver', 'harness is closing')
                # Every component is fixed here, never supplied as an output path by the agent.
                parts = ('runs', self.run_id, 'score', 'gb10', mid)
                dfd = provider.open_dir_chain(self.prs_root, parts, create=True)
                try:
                    for name in (f'chr{chrom}.vcf.gz', f'chr{chrom}.log'):
                        deliver(work / 'result' / name, dfd, name)
                    provider.write_in_dir(dfd, 'receipt.json', json.dumps(receipt).encode())
                finally:
                    os.close(dfd)
                output = str(Path(self.prs_root).joinpath(*parts))
                record['status'] = 'delivered'
                payload = {'status': 'delivered', 'directory': output, 'receipt': receipt}
        except Exception as exc:
            cause = failure_cause(exc)
            record.update(status='error', cause=cause, reason=str(exc))
            payload = {'status': 'error', 'cause': cause, 'reason': str(exc)}
        finally:
            # The private input/output bytes die with TemporaryDirectory; retain only receipts.
            with self._lock:
                self.requests.append(record)
                self._in_flight.pop(mid, None)
            (self.vault_dir / (mid + '.json')).write_text(json.dumps(record, indent=2))
            # Free the slot before publishing the reply: its reader can submit again.
            self.reply(mid, payload)

    def close(self):
        with self._lock:
            self._closed = True
        self.client.cancel.set()
        self.pool.shutdown(wait=True)
        failures = self.client.cleanup_all()
        report = {'backend': 'gb10', 'requests': self.requests,
                  'gate_runs': list(self.client.jobs.values()), 'cleanup_failures': failures}
        (self.vault_dir / 'summary.json').write_text(json.dumps(report, indent=2))
        return report
