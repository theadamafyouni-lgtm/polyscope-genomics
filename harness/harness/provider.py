"""The INT-1 handoff.

The harness resolves the request's file references, stages exactly what it has, and hands
the request to a provider agent in its own sandbox. It does not decide whether a reference
can be built.

That last sentence is the whole design. The harness could decide whether the inputs suffice
and write the refusal itself. It does not, because deciding that a usable reference cannot be
built is precisely the call INT-2 exists for, and a harness that makes it bypasses the agent
whose existence is the point. The harness stages honestly, describes what is present and what
is not, and lets the provider agent reach its own conclusion and say why.

WHAT CHANGED 2026-08-04/05. The provider used to be pre-baked for one model in three ways.
Two are gone:

  * `build_reference.py` was de-hardcoded. Input paths, the cohort size, the weight column
    and the sum-of-|BETA| target are now arguments; the target has no default and is an
    OPTIONAL regression guard rather than a mandatory check against one run's constant. The
    artifact's trait and pgs_id are read from the scoring file's own `#` header instead of
    being asserted, so a different model can no longer be mislabelled as the native one.
  * `build_reference_panel.py` now exists, so `ref_1kg_<PGS>.vcf.gz` has a generating
    script. A missing panel is no longer a dead end -- the provider is given the tool and
    `phase3/` to build from, and may build one. Rebuilt per run, never cached: a cache would
    mean nothing exercises the build path after the first run, and two runs would not be
    comparable.

What remains model-specific is only the input BASENAMES, which is a staging convention.

`PROVIDER_NATIVE_PGS` therefore no longer means "the only model that can work". It records
which model the pre-staged inputs happen to be for, so a run that got a panel for free is
distinguishable afterwards from one that had to build its own.

What the harness DOES record is whether the inputs matched, so a run where the INT-1 arm was
never truly exercised is visible as such afterwards rather than being read as a pass.
"""
import hashlib
import json
import os
import re
import shutil
import stat

from . import config, early_panel

# The staged basenames. projection.json, ref_pca.eigenvec, ref_pca.eigenval,
# sample_labels.json and sample_sex.panel are read by exactly these names through main()'s
# `I = lambda name: join(args.inputs, name)` -- there is no per-file flag for them, so the
# names are the interface. The rest are passed to the tool as arguments.
PROVIDER_INPUTS = {
    "dosages": "dosages_leaveout_{pgs}.tsv",
    "score_input": "plink_score_input.tsv",
    "scoring": "{pgs}_hmPOS_GRCh37.txt.gz",
    "projection": "projection.json",
    "eigenvec": "ref_pca.eigenvec",
    "eigenval": "ref_pca.eigenval",
    "labels": "sample_labels.json",
    "sex_labels": "sample_sex.panel",
    "ref_vcf": "ref_1kg_{pgs}.vcf.gz",
}

# The model the provider's own staged inputs were built for: none, since no inputs are
# pre-staged for any model any more (summary() below).
PROVIDER_NATIVE_PGS = None

BRIEF = """\
You are the reference-distribution provider for one request against the INT-1 contract.

You have been given: the request, `build_reference.py`, a `plink2` binary, an `inputs/`
directory, and `CONTRACT.md`. Nothing else. You cannot see the patient, the conversation, the
agent that sent this, or any other run.

Read `CONTRACT.md` first -- it is the interface you must satisfy. Then read
`inputs/INPUTS.md`, which lists exactly which input files are present and which are absent.
Then read the request.

Your job is to decide whether a usable reference distribution can be built for THIS request,
and then either build it or say why not.

Two outcomes are correct:

  * You build it. Write the artifact to the path given below.

  * You cannot build it. Write a well-formed artifact to the same path with
    `"usable": false` and a `reason` that names the ACTUAL cause. Per INT-2 a refusal is a
    correct outcome, not a failure -- but only if the reason is true. "Too few genetically
    similar participants exist" and "the inputs for this model are not present" are
    completely different claims, and putting the wrong one into a clinical artifact is worse
    than refusing loudly.

Whichever you do, every field the contract requires must be present. A refusal artifact still
needs `usable`, `reason`, `model` (with `pgs_id` and `sha256` equal to the request's -- a
refusal for another scoring file is not this request's refusal), `source`, `version_string`,
`ancestry_grouping_definition`, `cohort_construction_rule`, `n_participants`,
`too_few_threshold_used`, `assay_layer`, `scoring_parity` and `distribution` -- with nulls
where a refusal makes a value meaningless, not zeros. A zero quantile is a claim; a null is an
absence.

## The tools, and how they are invoked

`build_reference.py` takes every input as an argument -- there are no defaults to fall back
on, because a default would be one run's value applied to every other run:

    python3 build_reference.py \\
      --dosages inputs/<the dosages file named in INPUTS.md> \\
      --score-input inputs/plink_score_input.tsv \\
      --scoring inputs/<the scoring file> \\
      --ref-vcf inputs/<the reference panel VCF> \\
      --request request.json \\
      --effect-weight-col <the weight column, read off the scoring file's own header> \\
      --reference-assay "<what assay produced the reference genotypes, in your words>" \\
      --k <how many nearest reference samples to select> \\
      --min-cohort <refuse below this cohort size> \\
      --k-rationale "<why you chose these values>" \\
      --workdir work --out {out}

`--score-input` is the file the patient's own score was computed from. The cohort is scored
from that same file by the same plink2 recipe, and the tool reports how many of its variants
plink2 actually used for each side; `scoring_parity` follows from those counts.

`--k` and `--min-cohort` are yours to choose and the contract asks you to state what you
used -- `cohort_construction_rule` and `too_few_threshold_used` are required fields. There is
no derived value for either; say what you chose and why, in `--k-rationale`.

`--max-kth-distance` is also available: refuse when the k-th nearest reference sample is
further than that distance in PC space. It has no default and the tool applies no check
without it. Whether to set one, and to what, is your call; the artifact records the value
used, or that there was none, and `--k-rationale` is where to say why.

If the request names a `comparison_group` sex, the trait is sex-specific and the agent judged
its model developed and validated in that sex: `build_reference.py` then builds the group only
from reference samples recorded as that sex (`inputs/sample_sex.panel`), and the artifact says
so. You pass nothing for it; say in `--k-rationale` that the group is of that sex alone, and
choose `--k` and `--min-cohort` knowing the pool is about half the panel.

`--expected-abs-beta` is OPTIONAL and is a regression guard against a PRIOR run of this same
patient and model. Do not compute it from the very data it would check -- that turns a real
check into a no-op. Omitted, the tool records the computed value and states plainly that the
join was not verified, which is the honest position on a first run.

**If `inputs/INPUTS.md` says the reference panel VCF is absent, you can build one.**
`build_reference_panel.py` extracts the model's positions from the 1000 Genomes panel:

    python3 build_reference_panel.py \\
      --scoring inputs/<the scoring file> \\
      --chr-col hm_chr --pos-col hm_pos \\
      --phase3-dir <the phase3 directory named in INPUTS.md> \\
      --chromosomes 1-22 --workers 4 --threads 2 \\
      --out inputs/<the reference panel file INPUTS.md names as absent, .vcf.gz included>

`--out` is that file name in full, ending in `.vcf.gz`, and the same path is what
`build_reference.py` takes as `--ref-vcf`. bcftools finds the index only under that ending;
the builder refuses any other name before it starts.

`--workers` extracts that many chromosomes concurrently and `--threads` is bcftools
threads within each one; they multiply, so keep their product near the core count.

How long it takes scales with the model's position count, so there is no single figure:
roughly half an hour for a model of about 1.1M positions at the concurrency above, and
proportionally longer for a larger one. Treat that as an order of magnitude and not a
deadline. **Running longer than the estimate is not a reason to refuse** -- if you decide a
usable distribution cannot be built, the `reason` must name the actual cause, and "it was
slower than the brief said" is not one.

**A command that runs longer than one tool call.** Each Bash call you make is cut off at ten
minutes at most, and at two unless you ask for more, and a bare long `sleep` is refused. The
panel build, and `build_reference.py` on a large model, can take longer than that. So start it in
the background, then wait for it with calls that each return inside one tool call -- give each
of those a 600000 ms timeout:

    rm -f work/panel.exit
    ( trap '' HUP; python3 build_reference_panel.py ... > work/panel.log 2>&1; echo $? > work/panel.exit ) > /dev/null 2>&1 &
    timeout 540 bash -c 'until [ -e work/panel.exit ]; do sleep 20; done'; tail -3 work/panel.log; cat work/panel.exit 2>/dev/null

`work/panel.exit` appears when the builder has stopped, whichever way it stopped, so the wait
cannot outlast a dead build. Repeat the wait until it exists. Then: exit status 0 with
`<its --out>.provenance.json` present (written last, after the VCF and its index) means the
panel is complete; anything else means the builder died, and the end of `work/panel.log` says
why. Remove the old exit file before every launch, as above: left over from a failed
attempt, it would end the next wait at once with the old status. The subshell takes your
command exactly as you would write it on its own, quotes and apostrophes included, and
`trap '' HUP` does what `nohup` did. Do not wrap the command in `sh -c '...'`: an apostrophe in
a free-text argument such as `--k-rationale` ends that quote and the build never starts. Do
the same for `build_reference.py`, with its own log and `.exit` file. **Your background job
dies when your turn ends**: do not finish your turn while a build is still running.

Build it at the MODEL's positions, which is what the command
above does: a patient's imputed dosages come out of this same panel, so building at the
model's positions covers every position the patient could have resolved. Building at the
patient's positions instead would leave the cohort scored over a different variant set from
the patient, and that is measured -- a 0.55% mismatch moved a percentile by 8.3 points.

Do not edit either tool. If one will not run for this request, that is a finding to report in
`reason`, not a thing to patch around.

Write your artifact to: {out}
Working directory: {cwd}
"""


class AmbiguousInput(Exception):
    """More than one candidate for an input that must be unique.

    Refusing is the only safe answer. One real run does carry both
    ancestry/projection.json and ancestry-fixed/projection.json, and they differ materially
    -- different nearest neighbours, different corrected PCs. Both record the same
    reference_build, which is what makes picking the wrong one silent: the provider's
    neighbour_selfcheck only verifies the projection against its OWN recorded neighbours, so
    a stale-but-self-consistent projection passes every check and lands a wrong cohort, a
    wrong distribution and a wrong percentile.
    """


class OutsideRun(Exception):
    """A path the agent supplied cannot be taken: it resolves outside the requesting run's
    own directory, or what it names there is not a regular file.

    The harness copies these files with its own authority, into a sandbox the requesting
    agent cannot reach. A request naming /etc/..., another run, or a symlink that points out
    of the run would have the harness fetch it on the agent's behalf. A FIFO or a device
    would hang or exhaust the harness reading it. Refused, not staged.

    The message never says where a path resolved to. That would tell the sandboxed agent
    where a host link points, and whether a host path exists.
    """


# JSON the harness parses out of a sandbox -- the request, a projection, a basis's
# reference_build.json, the provider's artifact -- is read whole into memory, so it is
# bounded. A technical limit, far above any such file: a sparse file can claim any size.
JSON_READ_LIMIT = 64 << 20


def _under(path, root):
    """True if `path` is `root` or below it, compared as given (nothing resolved)."""
    root = root.rstrip(os.sep) or os.sep
    return path == root or path.startswith(root + os.sep)


def contained(path, root):
    """realpath(path) if it lies inside realpath(root), else None.

    Symlinks are resolved before the comparison, so a link inside the run that points out
    of it is outside. realpath does not need the path to exist, so the answer is the same
    whether or not the target does.
    """
    rp = os.path.realpath(path)
    return rp if _under(rp, os.path.realpath(root)) else None


def real_run_dir(prs_root, run_id):
    """The requesting run's directory, by its real path, or OutsideRun.

    `runs/<RUN>` must be a real directory where it says it is. The sandbox is the agent's to
    write, so it can replace `runs/<RUN>` (or `runs`) with a link to anywhere; every
    containment check below would then be against wherever that link points.
    """
    lexical = os.path.join(prs_root, "runs", run_id)
    expected = os.path.join(os.path.realpath(prs_root), "runs", run_id)
    if (os.path.islink(lexical) or os.path.realpath(lexical) != expected
            or not os.path.isdir(expected)):
        raise OutsideRun("refused: runs/%s is not a plain directory inside the sandbox"
                         % run_id)
    return expected


def resolve_inside(path, root, base, what):
    """The real path `path` names, if it lies inside `root`; else OutsideRun.

    `path` is taken relative to `base` when it is not absolute. Decided before anything is
    opened and whether or not the path exists, so the answer is no oracle for host paths.
    """
    if not isinstance(path, str) or not path:
        raise OutsideRun("refused: %s is not a path" % what)
    ab = os.path.normpath(path if os.path.isabs(path) else os.path.join(base, path))
    real = os.path.realpath(ab)
    if not _under(real, root):
        raise OutsideRun("refused: %s %r is not inside the requesting run's directory"
                         % (what, path))
    return real


def _fd_path(fd):
    """The path the kernel holds for an open descriptor, or None where it will not say."""
    try:
        return os.readlink("/proc/self/fd/%d" % fd)
    except OSError:
        pass
    try:
        import fcntl
        if hasattr(fcntl, "F_GETPATH"):
            buf = fcntl.fcntl(fd, fcntl.F_GETPATH, b"\0" * 1024)
            return buf.split(b"\0", 1)[0].decode("utf-8", "replace")
    except (OSError, ImportError):
        pass
    return None


def open_regular(real, root, what, limit=None):
    """Open a file already resolved inside `root`, and check what was actually opened.

    The check-then-open window is closed by checking the descriptor: the kernel's own path
    for it must still lie inside `root`, so a directory swapped for a link after
    resolve_inside() is caught here. O_NOFOLLOW refuses a final-component link and
    O_NONBLOCK keeps a FIFO from hanging the open. FileNotFoundError passes through: an
    absent input is reported as absent, not refused.
    """
    flags = (os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
             | getattr(os, "O_CLOEXEC", 0))
    try:
        fd = os.open(real, flags)
    except OSError as exc:
        if isinstance(exc, FileNotFoundError):
            raise
        raise OutsideRun("refused: %s cannot be opened as a plain file (%s)"
                         % (what, type(exc).__name__))
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            raise OutsideRun("refused: %s is not a regular file" % what)
        held = _fd_path(fd)
        if held is not None and not _under(held, os.path.realpath(root)):
            raise OutsideRun("refused: %s was moved outside the requesting run's "
                             "directory while it was being read" % what)
        if limit is not None and st.st_size > limit:
            raise OutsideRun("refused: %s is %d bytes, over the %d-byte limit for a file "
                             "the harness parses" % (what, st.st_size, limit))
    except BaseException:
        os.close(fd)
        raise
    return fd


def read_regular(real, root, what, limit=JSON_READ_LIMIT):
    fd = open_regular(real, root, what, limit)
    with os.fdopen(fd, "rb") as fh:
        data = fh.read(limit + 1 if limit is not None else -1)
    if limit is not None and len(data) > limit:
        raise OutsideRun("refused: %s grew past the %d-byte limit while it was being read"
                         % (what, limit))
    return data


def sha256_regular(real, root, what):
    fd = open_regular(real, root, what)
    h = hashlib.sha256()
    with os.fdopen(fd, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def open_dir_chain(base_real, parts, create=False):
    """A descriptor for base_real/parts..., walked one component at a time without
    following a link at any step. `create` makes missing components.

    Every write the harness makes into an agent-writable tree goes through this, because a
    link planted at any component would otherwise carry the harness's write out of it.
    """
    dflags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_CLOEXEC", 0)
    fd = os.open(base_real, dflags)
    try:
        for part in parts:
            if not part or part in (".", "..") or os.sep in part:
                raise OutsideRun("refused: %r is not a single path component" % part)
            try:
                nxt = os.open(part, dflags | getattr(os, "O_NOFOLLOW", 0), dir_fd=fd)
            except FileNotFoundError:
                if not create:
                    raise
                os.mkdir(part, 0o755, dir_fd=fd)
                nxt = os.open(part, dflags | getattr(os, "O_NOFOLLOW", 0), dir_fd=fd)
            os.close(fd)
            fd = nxt
    except OSError as exc:
        os.close(fd)
        if isinstance(exc, FileNotFoundError):
            raise
        raise OutsideRun("refused: %s is not a plain directory (%s)"
                         % (os.path.join(*parts) if parts else base_real,
                            type(exc).__name__))
    except BaseException:
        os.close(fd)
        raise
    return fd


def write_in_dir(dir_fd, name, data):
    """Write `data` to dir_fd/name by a fresh temp file and a rename.

    The temp file is created exclusively and never through a link, and the rename replaces
    whatever directory entry `name` is -- a planted link included -- rather than writing
    through it.
    """
    if not name or name in (".", "..") or os.sep in name:
        raise OutsideRun("refused: %r is not a file name" % name)
    tmp = ".%s.%d.tmp" % (name, os.getpid())
    try:
        os.unlink(tmp, dir_fd=dir_fd)
    except FileNotFoundError:
        pass
    flags = (os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
             | getattr(os, "O_CLOEXEC", 0))
    fd = os.open(tmp, flags, 0o644, dir_fd=dir_fd)
    with os.fdopen(fd, "wb") as fh:
        fh.write(data)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, name, src_dir_fd=dir_fd, dst_dir_fd=dir_fd)


class ProviderHandoff:
    def __init__(self, sandbox_root, record, transcript):
        self.root = sandbox_root
        self.root_real = os.path.realpath(sandbox_root)
        self.inputs = os.path.join(self.root_real, "inputs")
        self.workdir = os.path.join(self.root_real, "work")
        self.out = os.path.join(self.root_real, "reference_distribution.json")
        self.record = record
        self.transcript = transcript
        self.staged = {}
        self.absent = {}
        self.request = None
        self.pgs = None
        self.ambiguous = None
        self.refused = None
        self.basis = None
        self.run_dir = None
        self.prs_real = None
        self._artifact_bytes = None
        # A demo run's panel built while it imputed (early_panel.EarlyPanels), or None.
        self.early = None
        # Set at stage() time: whether the provider has what it needs to build a missing
        # reference panel itself. Recorded so a run where it could not is distinguishable
        # from one where it chose not to.
        self.phase3_dir = os.path.join(config.REFDATA_SRC, "ancestry-ref", "phase3")
        self.panel_buildable = os.path.isdir(self.phase3_dir)
        # The 1000 Genomes panel, whose gender column records each sample's sex.
        self.sex_panel = os.path.join(config.REFDATA_SRC, "ancestry-ref",
                                      "integrated_call_samples_v3.20130502.ALL.panel")

    # -- staging ---------------------------------------------------------
    def _reset(self):
        """Empty what an earlier handoff left in the provider sandbox.

        The sandbox is one directory reused for every request in a run. Left as it was, a
        file INPUTS.md calls absent could still sit in inputs/ from the last request -- and
        be used, under this request's name -- and a provider that wrote nothing would have
        the last request's artifact read back as this one's. A link the last provider left
        at one of these names would also carry the harness's next write out of the sandbox.
        """
        for name in ("request.json", "reference_distribution.json", "inputs", "work"):
            p = os.path.join(self.root_real, name)
            try:
                st = os.lstat(p)
            except FileNotFoundError:
                continue
            if stat.S_ISDIR(st.st_mode):
                shutil.rmtree(p)
            else:
                os.unlink(p)
        os.makedirs(self.inputs)
        os.makedirs(self.workdir)

    def stage(self, request_path, prs_sandbox_root, run_dir, request_bytes=None):
        """Copy in what exists, name what does not, and record both.

        Every file taken from the PRS sandbox on the strength of a path the agent wrote must
        resolve inside `run_dir`, the requesting run's own runs/<RUN>/ directory, by its
        real path (see real_run_dir). `request_bytes` is the request as the caller already
        read it, so what is staged is what was hashed.
        """
        self._reset()
        self.run_dir = os.path.realpath(run_dir)
        self.prs_real = os.path.realpath(prs_sandbox_root)
        self.prs_root = prs_sandbox_root

        if request_bytes is None:
            request_path = resolve_inside(request_path, self.run_dir, prs_sandbox_root,
                                          "the request")
            request_bytes = read_regular(request_path, self.run_dir, "the request")
        try:
            self.request = json.loads(request_bytes)
        except ValueError:
            self.request = None
        if not isinstance(self.request, dict):
            raise OutsideRun("refused: the request is not a JSON object")
        model = self.request.get("model")
        self.pgs = (str((model if isinstance(model, dict) else {}).get("pgs_id") or "")
                    .strip())
        # The id goes into staged file names, so it must be a bare id: anything else could
        # carry a path component into the provider sandbox's directory layout.
        if self.pgs and not re.fullmatch(r"PGS[0-9]{6}", self.pgs):
            raise OutsideRun("refused: model.pgs_id %r is not a PGS Catalog id" % self.pgs)

        # The request is copied, never linked. Its own file references point into the PRS
        # sandbox, which the provider cannot reach, so the copy is rewritten to name the
        # local staged files instead -- otherwise the provider would read paths it cannot
        # open and could mistake an unreachable path for a missing input.
        req_local = os.path.join(self.root_real, "request.json")
        local = dict(self.request)
        patient = local.get("patient")
        patient = dict(patient if isinstance(patient, dict) else {})
        patient["resolved_positions_manifest_original"] = patient.get(
            "resolved_positions_manifest")
        patient["resolved_positions_manifest"] = os.path.join(
            "inputs", PROVIDER_INPUTS["dosages"].format(pgs=self.pgs or "UNKNOWN"))
        patient["score_input_original"] = patient.get("score_input")
        patient["score_input"] = os.path.join("inputs", PROVIDER_INPUTS["score_input"])
        local["patient"] = patient
        local["_harness_note"] = (
            "Paths rewritten to the local inputs/ directory. The original values, which "
            "pointed into the requesting agent's sandbox, are preserved alongside.")
        root_fd = open_dir_chain(self.root_real, ())
        try:
            write_in_dir(root_fd, "request.json",
                         json.dumps(local, indent=2).encode("utf-8"))
        finally:
            os.close(root_fd)
        self.record.add(req_local, request_path, "int1-request")

        want = {k: v.format(pgs=self.pgs or "UNKNOWN") for k, v in PROVIDER_INPUTS.items()}

        # From the requesting run.
        self._try(want["dosages"], self._resolve_patient_path("resolved_positions_manifest",
                                                              prs_sandbox_root),
                  "the patient's resolved per-variant dosages, from the requesting run",
                  self.run_dir)
        self._try(want["score_input"], self._resolve_patient_path("score_input",
                                                                  prs_sandbox_root),
                  "the score input the patient's raw score was computed from; the cohort "
                  "is scored from this same file", self.run_dir)
        self._try(want["scoring"], self._resolve_scoring(),
                  "the harmonized scoring file the requesting run actually used",
                  self.run_dir)
        self._try(want["projection"], self._resolve_projection(),
                  "the patient's PCA projection, from the requesting run", self.run_dir)

        # The PCA basis. It MUST be the one the patient's projection was computed against;
        # a mismatched pair puts the patient's coordinates in a different decomposition from
        # the panel's, and every distance downstream is meaningless. build_reference.py has
        # a hard self-check for exactly this and stops rather than reconciling. The basis is
        # identified by the projection's own record of it, and its labels travel with it:
        # they are the labels the projection's recorded neighbours were counted with.
        # The projection is read from the copy just staged, so the basis is matched to the
        # projection the provider will actually use.
        staged_projection = (os.path.join(self.inputs, want["projection"])
                             if want["projection"] in self.staged else None)
        base = self._resolve_pca_basis(self.prs_real, staged_projection)

        def in_base(f):
            return self._basis_file(base, f) if base else None

        self._try(want["eigenvec"], in_base("ref_pca.eigenvec"),
                  "the PCA basis the patient's projection was computed against", self.prs_real)
        self._try(want["eigenval"], in_base("ref_pca.eigenval"),
                  "eigenvalues of that basis; absent, the artifact silently changes shape",
                  self.prs_real)
        self._try(want["labels"], in_base("sample_labels.json"),
                  "that basis's own sample-to-population labels, the ones ancestry.py "
                  "counted the projection's recorded neighbours with", self.prs_real)

        # Each 1000 Genomes sample's recorded sex, for a same-sex comparison group: read by
        # build_reference.py by this name, and only when the request asks for one.
        self._try(want["sex_labels"], self.sex_panel if os.path.isfile(self.sex_panel) else None,
                  "each 1000 Genomes sample's recorded sex, for a same-sex comparison group when "
                  "the request asks for one", None)

        # The model-specific reference VCF is never pre-staged: the provider builds it from
        # phase3 with build_reference_panel.py, for every model. The hand-built panel under
        # reference-provider/inputs/ (2026-07-28) lacks 12,943 of the variants a
        # real patient's score input carried on 2026-10-08, and staging it contradicted the
        # rule above that panels are rebuilt per run, never cached.
        early = None
        if self.early is not None and want["scoring"] in self.staged:
            with open(os.path.join(self.inputs, want["scoring"]), "rb") as fh:
                early = self.early.ready(self.pgs, hashlib.sha256(fh.read()).hexdigest(),
                                         wait_seconds=early_panel.EARLY_WAIT_SECONDS)
        if early:
            why = ("1000 Genomes genotypes at this model's positions, built by the harness with "
                   "build_reference_panel.py from this very scoring file (same sha256), by the "
                   "command above, while the requesting run imputed; nothing here needs building")
            for ext in ("", ".tbi", ".provenance.json"):
                self._try(want["ref_vcf"] + ext, early + ext, why, None)
        else:
            self._try(want["ref_vcf"], None,
                      "1000 Genomes genotypes at this model's positions", None)

        self._write_inputs_readme(want)
        return self.summary()

    def stage_guarded(self, request_path, prs_sandbox_root, run_dir, request_bytes=None):
        """stage(), but an ambiguous input is a reported refusal rather than a coin flip,
        and a path outside the requesting run is refused rather than fetched."""
        try:
            return self.stage(request_path, prs_sandbox_root, run_dir, request_bytes)
        except AmbiguousInput as exc:
            self.ambiguous = str(exc)
            return self.summary()
        except OutsideRun as exc:
            self.refused = str(exc)
            return self.summary()

    def _try(self, basename, src, why, root):
        """Stage `src` as inputs/<basename>, or record it absent.

        `root` is the tree `src` must stay inside while it is read -- the run's directory or
        the sandbox -- and None only for the harness's own files. The copy is made from the
        descriptor that was checked, into a fresh inputs/ (see _reset), and never through a
        link at the destination.
        """
        dest = os.path.join(self.inputs, basename)
        fd = None
        if src:
            try:
                fd = (open_regular(src, root, basename) if root is not None
                      else os.open(src, os.O_RDONLY))
            except FileNotFoundError:
                fd = None
        if fd is None:
            self.absent[basename] = {"why_wanted": why,
                                     "looked_for": src or "(nothing to look for)"}
            return
        flags = (os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
                 | getattr(os, "O_CLOEXEC", 0))
        with os.fdopen(fd, "rb") as fin, os.fdopen(os.open(dest, flags, 0o644), "wb") as fout:
            shutil.copyfileobj(fin, fout, 1 << 20)
        rec = self.record.add(dest, src, "provider-input", note=why)
        self.staged[basename] = {"source": src, "sha256": rec.get("sha256"),
                                 "bytes": rec.get("bytes"), "why": why}

    # -- resolution ------------------------------------------------------
    def _resolve_patient_path(self, field, prs_root):
        """A file the request's patient block names, resolved against the sandbox.

        The request is authoritative for which file. Guessing a filename here would risk
        handing over a different variant set from the one the score was computed on, and
        every parse would succeed -- the failure would be silent and would land in the
        percentile. Where the file is, though, is checked rather than taken: it must resolve
        inside the requesting run's directory, or the request is refused -- whether or not
        it exists, so a refusal says nothing about a path outside the run.
        """
        patient = self.request.get("patient")
        p = (patient if isinstance(patient, dict) else {}).get(field)
        if not p:
            return None
        return resolve_inside(p, self.run_dir, prs_root, "patient.%s" % field)

    @staticmethod
    def _all(root, pred):
        """Every match, in a deterministic order."""
        hits = []
        for cur, dirs, files in os.walk(root):
            dirs.sort()
            for f in sorted(files):
                if pred(cur, f):
                    hits.append(os.path.join(cur, f))
        return hits

    @staticmethod
    def _one(hits, what):
        if not hits:
            return None
        if len(hits) > 1:
            raise AmbiguousInput("%s is ambiguous: %d candidates -- %s" % (what, len(hits), hits))
        return hits[0]

    def _in_run(self, hit):
        real = contained(hit, self.run_dir)
        if real is None:
            raise OutsideRun("refused: %s links outside the requesting run's directory"
                             % os.path.relpath(hit, self.run_dir))
        return real

    def _resolve_scoring(self):
        if not self.pgs:
            return None
        # Exact basename, not startswith: `PGS0_hmPOS_GRCh38.txt.gz` also starts with the
        # model id, and staging it under the GRCh37 name would present a build mismatch to
        # the provider as a match.
        want = PROVIDER_INPUTS["scoring"].format(pgs=self.pgs)
        hit = self._one(self._all(self.run_dir, lambda c, f: f == want), "the scoring file")
        return self._in_run(hit) if hit else None

    def _resolve_projection(self):
        hit = self._one(self._all(self.run_dir, lambda c, f: f == "projection.json"),
                        "projection.json")
        return self._in_run(hit) if hit else None

    def _basis_file(self, base, f):
        """base/f by its real path, which must lie inside the sandbox, or OutsideRun."""
        real = contained(os.path.join(base, f), self.prs_real)
        if real is None:
            raise OutsideRun("refused: %s in a PCA basis directory links outside the sandbox"
                             % f)
        return real

    def _resolve_pca_basis(self, prs_real, projection_path):
        """The basis directory the patient's projection was computed against.

        ancestry.py copies the basis's reference_build.json, generated_utc and all, into
        projection.json as `reference_build`, so the basis is the directory whose own
        reference_build.json equals that record exactly. Anything looser is ambiguous in a
        sandbox that holds both refdata/ancestry-ref/hgdp_basis and a basis the agent built
        under runs/<RUN>/: both carry ref_pca.eigenvec and ref_pca.afreq. No match leaves the
        basis absent, which the provider sees and reports.

        Every candidate file is opened as open_regular() opens a patient file, against the
        sandbox: the agent writes the candidates under runs/<RUN>/, so one of them can be a
        link out of the sandbox, a FIFO or a device, and the harness reads them itself.
        """
        try:
            with open(projection_path, "rb") as fh:
                want = json.loads(fh.read(JSON_READ_LIMIT + 1)).get("reference_build")
        except (TypeError, OSError, ValueError, AttributeError):
            want = None
        if not want:
            self.basis = {"dir": None, "why": "projection.json carries no reference_build"}
            return None
        dirs, n_bases = [], 0
        for cur, d, files in os.walk(prs_real):
            d.sort()
            if "ref_pca.eigenvec" not in files or "ref_pca.afreq" not in files:
                continue
            n_bases += 1
            try:
                data = read_regular(self._basis_file(cur, "reference_build.json"), prs_real,
                                    "a PCA basis's reference_build.json")
                if json.loads(data) == want:
                    dirs.append(cur)
            except (FileNotFoundError, ValueError):
                continue
        self.basis = {"n_basis_directories_found": n_bases, "candidates": dirs,
                      "matched_on": "reference_build.json equal to projection.json's "
                                    "reference_build"}
        staged = ("ref_pca.eigenvec", "ref_pca.eigenval", "sample_labels.json")

        def fingerprint(d):
            out = []
            for f in staged:
                try:
                    out.append(sha256_regular(self._basis_file(d, f), prs_real, f))
                except FileNotFoundError:
                    out.append(None)
            return tuple(out)

        # Copies of one basis are one basis: if every candidate's staged files hash the
        # same, the choice between them changes nothing that is staged.
        if len(dirs) > 1 and len({fingerprint(d) for d in dirs}) == 1:
            dirs = dirs[:1]
            self.basis["note"] = "identical copies; the first in walk order was staged"
        base = self._one(dirs, "the PCA basis directory")
        self.basis["dir"] = base
        return base

    def _write_inputs_readme(self, want):
        lines = [
            "# What is in this directory",
            "",
            "`build_reference.py` reads `projection.json`, `ref_pca.eigenvec`,",
            "`ref_pca.eigenval`, `sample_labels.json` and `sample_sex.panel` from here by those",
            "exact names; there is no flag for them. The others are passed as arguments -- see",
            "the brief. The",
            "request itself is `request.json`, one directory up.",
            "",
            "## Present",
            "",
        ]
        for name in sorted(self.staged):
            s = self.staged[name]
            lines.append("- `%s` (%s bytes) -- %s" % (name, s["bytes"], s["why"]))
        lines += ["", "## Absent", ""]
        if not self.absent:
            lines.append("- nothing; every input the tool asks for is present.")
        for name in sorted(self.absent):
            lines.append("- `%s` -- %s" % (name, self.absent[name]["why_wanted"]))
        lines += [
            "",
            "## The verification target has no default, on purpose",
            "",
            "`--expected-abs-beta` is the sum of |BETA| over the join between the scoring",
            "file and the patient's resolved set. It is a REGRESSION GUARD against a prior",
            "run of this same patient and model, not a property of the scoring file -- the",
            "value changes whenever the resolved set changes, so there is no correct default",
            "and the tool no longer carries one.",
            "",
            "Omit it on a first run. The tool records the computed value and states that the",
            "join was not verified, which is true. **Do not compute a value from the very",
            "data it is meant to check and pass that** -- it would turn a real check into a",
            "no-op while making the artifact claim a verification happened.",
            "",
            "`--expected-imputed-abs-beta` is the same shape, reported and never enforced.",
        ]
        if self.panel_buildable:
            lines += [
                "",
                "## The reference panel VCF, if it is absent above",
                "",
                "`build_reference_panel.py` is in the working directory and `phase3/` is",
                "readable at:",
                "",
                "    %s" % self.phase3_dir,
                "",
                "so a missing panel is not a dead end. How long it takes scales with",
                "the model's position count -- roughly half an hour for a model of about",
                "1.1M positions at the concurrency the brief gives, proportionally longer",
                "for a larger one, and overrunning it is not a reason to refuse.",
                "See the brief for the invocation. Whether it is worth building for",
                "this request, or whether something else about the request makes a usable",
                "distribution impossible anyway, is your call to make and to state.",
            ]
        p = os.path.join(self.inputs, "INPUTS.md")
        with open(p, "w") as fh:
            fh.write("\n".join(lines) + "\n")
        self.record.add(p, "(generated)", "provider-input-manifest")

    # -- reporting -------------------------------------------------------
    def summary(self):
        """What the harness knows about the fit. Recorded; never shown to the provider."""
        # No reference panel is pre-staged for any model any more (see the staging code), so
        # nothing "came for free"; the provider builds every panel from phase3.
        matched = False
        return {
            "requested_pgs_id": self.pgs,
            "provider_native_pgs_id": PROVIDER_NATIVE_PGS,
            "inputs_match_requested_model": matched,
            "staged": self.staged,
            "absent": self.absent,
            "staging_ok": not self.ambiguous and not self.refused,
            "ambiguous_input": self.ambiguous,
            "refused_path": self.refused,
            "pca_basis": self.basis,
            "panel_buildable": self.panel_buildable,
            "note": (
                "No reference panel is pre-staged for any model, the hand-built one included: it "
                "was incomplete for a real patient's score input. build_reference_panel.py "
                "and phase3/ are provided and the provider builds the panel per request; "
                "whether to, or to refuse, is its own call. The harness neither writes the "
                "refusal nor builds the panel on its behalf."),
        }

    def brief(self):
        return BRIEF.format(out=self.out, cwd=self.root)

    # -- result ----------------------------------------------------------
    def read_artifact(self):
        """The provider's artifact, read once, as bytes from a regular file in the sandbox.

        The same bytes are what deliver_to() writes, so what is recorded and what is
        delivered cannot differ. A link at the artifact's name is not followed: the provider
        could otherwise have the harness read any host file and copy it to the agent.
        """
        self._artifact_bytes = None
        try:
            data = read_regular(self.out, self.root_real, "the provider's artifact")
        except FileNotFoundError:
            return None, "the provider produced no artifact at %s" % self.out
        except OutsideRun as exc:
            return None, "the provider's artifact could not be taken: %s" % exc
        try:
            art = json.loads(data)
        except ValueError as exc:
            return None, "the provider's artifact is not readable JSON: %s" % exc
        self._artifact_bytes = data
        return art, None

    @property
    def artifact_bytes(self):
        """The bytes read_artifact() read, which are the bytes deliver_to() writes."""
        return self._artifact_bytes

    def deliver_to(self, prs_real, run_id):
        """Write the artifact into runs/<RUN>/interpret/provider_reference.json, and hash
        both ends.

        The PRS sandbox is the agent's to write, so the path is walked one directory at a
        time without following a link, and the file is replaced by a rename rather than
        written through: a link planted at any step would otherwise have this write land
        wherever it points, with the harness's authority.
        """
        name = "provider_reference.json"
        dfd = open_dir_chain(prs_real, ("runs", run_id, "interpret"), create=True)
        try:
            write_in_dir(dfd, name, self._artifact_bytes)
            fd = os.open(name, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0), dir_fd=dfd)
            with os.fdopen(fd, "rb") as fh:
                delivered = fh.read()
        finally:
            os.close(dfd)
        return {
            "from": self.out,
            "to": os.path.join(prs_real, "runs", run_id, "interpret", name),
            "sha256_source": hashlib.sha256(self._artifact_bytes).hexdigest(),
            "sha256_delivered": hashlib.sha256(delivered).hexdigest(),
            "bytes": len(delivered),
        }
