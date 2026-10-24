"""Chromosome transport over the existing harness queue. Contains no SSH credential.

The harness selects the backend, in a file it writes into the read-only refdata/, and nothing
here can override it. Local when there is no such file, which is outside the harness; a failed
remote job never falls back locally, and on the gb10 route there is no Beagle here to fall to.
"""
import json
import os
import shutil
import time
import uuid
from pathlib import Path

COMMS = Path(__file__).resolve().parent.parent / 'comms'
SWITCH = Path(__file__).resolve().parent.parent / 'refdata' / 'imputation.json'
# How long to wait for the harness's reply: longer than the gate client's worst case for one
# chromosome (harness/gb10.py GateClient.worst_case_seconds(), about 19.5 h queued behind
# another), so this side never gives up on a request the harness is still working on and leaves
# its delivery orphaned. The run's own time cap ends a run long before this.
REPLY_TIMEOUT = 24 * 3600


class RemoteError(RuntimeError):
    """`cause` is the harness's word for whose failure it was: 'gb10' when the gb10 or the
    route to it failed, 'request' when the request itself was refused, 'closing' when the
    harness was ending the run. No reply at all counts as 'gb10'."""

    def __init__(self, reason, cause='gb10'):
        super().__init__(reason)
        self.cause = cause


def configured_backend(path=SWITCH):
    path = Path(path)
    if not path.exists():
        return 'local'
    with open(path) as source:
        backend = json.load(source)['backend']
    if backend not in ('local', 'gb10'):
        raise RemoteError('unrecognized imputation backend in harness configuration')
    return backend


def impute_chromosome(run_id, chrom, target, holdout, seed, cohort_path, log_path,
                      *, threads=6, timeout=REPLY_TIMEOUT, comms=COMMS, poll=.3, stop=None):
    """Beagle's output VCF lands at `cohort_path` and its log at `log_path`, where Beagle
    itself writes them on this machine. `stop`, an Event: stop waiting once it is set
    (another chromosome's gb10 job failed)."""
    if str(threads) != '6':
        raise RemoteError('gb10 requires threads=6', 'request')
    comms = Path(comms)
    mid = 'i-' + uuid.uuid4().hex[:12]
    request = {'message_id': mid, 'kind': 'impute-chromosome', 'run_id': run_id,
               'chromosome': str(chrom), 'target': os.path.abspath(target),
               'holdout': os.path.abspath(holdout), 'seed': str(seed), 'threads': 6}
    outbox = comms / 'outbox'
    temporary = outbox / ('.' + mid + '.tmp')
    with open(temporary, 'x') as dest:
        json.dump(request, dest)
        dest.flush()
        os.fsync(dest.fileno())
    os.replace(temporary, outbox / (mid + '.json'))
    reply_path = comms / 'inbox' / (mid + '.json')
    deadline = time.monotonic() + timeout
    while not reply_path.exists():
        if time.monotonic() >= deadline:
            raise RemoteError('no imputation reply before deadline; request ' + mid)
        if stop is not None and stop.is_set():
            raise RemoteError('not waited for: the gb10 failed another chromosome first; '
                              'request ' + mid)
        time.sleep(poll)
    with open(reply_path) as source:
        reply = json.load(source)
    if reply.get('status') == 'busy':
        raise RemoteError(reply.get('reason', 'two chromosomes already in flight'), 'request')
    if reply.get('status') != 'delivered':
        raise RemoteError(reply.get('reason', 'imputation request failed'),
                          reply.get('cause', 'gb10'))
    directory = Path(reply['directory'])
    # The broker has verified and atomically delivered both files before this reply.
    shutil.move(str(directory / f'chr{chrom}.vcf.gz'), cohort_path)
    shutil.move(str(directory / f'chr{chrom}.log'), log_path)
    receipt = reply['receipt']
    (directory / 'receipt.json').unlink()
    directory.rmdir()
    return receipt
