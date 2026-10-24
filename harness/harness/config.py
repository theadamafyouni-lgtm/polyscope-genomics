"""Paths, constants and the lists the boundary is built out of. LINUX BUILD.

Diverges from the macOS config in five places and nowhere else: SANDBOX_PATH,
KEYCHAIN_DIR, and three entries each in deny_probe_paths and write_probe_paths
that named macOS-only locations (~/Library/Keychains, /private/tmp, ~/.zsh_history).
Every list that defines what the agent must not see is unchanged, so the two
platforms are graded against the same answer-leakage definition. Added since, and
Linux only because the gb10 route is: the gb10 section, GB10_REFDATA_DELETE, and
two deny_probe_paths entries for its key (~/.ssh and the configured key), which
are not answer keys. And for a box without root: SANDBOX_EXEC, the host section
(toolchain and CLI outside the system trees) and the account-home probes.

Everything here is data. The rules that act on it live in staging.py, sandbox.py and
probe.py. Keeping the lists in one file means an audit is a diff of one file, and means the
manifest can record the exact list a run was built against rather than a description of it.

Nothing in this module may import from the rest of the package.
"""
import json
import os
import pwd
import re

# ---------------------------------------------------------------------------
# Roots
# ---------------------------------------------------------------------------

HARNESS_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PRODUCT_ROOT = os.path.dirname(HARNESS_ROOT)

AGENT = os.path.join(PRODUCT_ROOT, "agent")
REFDATA_SRC = os.path.join(AGENT, "refdata")
PROVIDER_SRC = os.path.join(PRODUCT_ROOT, "reference-provider")

# Harness-private. 0700. No sandbox profile may ever name a path under here.
# The restricted demo worker supplies a private, per-run runtime root. Source code,
# reference data and the normal imputation option stay unchanged. No demo artifact
# needs to be written beside the checked-out web assets or benchmark evidence.
DEMO_ROOT = os.environ.get("PRS_DEMO_ROOT")
RUNTIME_ROOT = os.path.abspath(DEMO_ROOT) if DEMO_ROOT else HARNESS_ROOT
VAULT = os.path.join(RUNTIME_ROOT, "vault")
VAULT_RUNS = os.path.join(VAULT, "runs")

SANDBOXES = os.path.join(RUNTIME_ROOT, "sandboxes")
SANDBOX_TOOLS = os.path.join(HARNESS_ROOT, "sandbox_tools")

# The bwrap shim every agent is launched through. Root installs it at /usr/bin/sandbox-exec
# (SETUP.md); a box without root runs the repository's own copy, which is the same file.
# $PRS_HARNESS_SANDBOX_EXEC names another one. Resolved once, here, so the agents, the probe
# and the manifest all name the same shim.
SANDBOX_EXEC_ENV = "PRS_HARNESS_SANDBOX_EXEC"
SANDBOX_EXEC_INSTALLED = "/usr/bin/sandbox-exec"
SANDBOX_EXEC_REPO = os.path.join(HARNESS_ROOT, "sandbox-exec")


def _sandbox_exec():
    named = os.environ.get(SANDBOX_EXEC_ENV)
    if named:
        return os.path.abspath(named)
    if os.access(SANDBOX_EXEC_INSTALLED, os.X_OK):
        return SANDBOX_EXEC_INSTALLED
    return SANDBOX_EXEC_REPO


SANDBOX_EXEC = _sandbox_exec()

# ---------------------------------------------------------------------------
# The pipeline tools
#
# The other eleven files in agent/tools/ are analysis and build-time tools that no skill
# invokes. They are excluded because they are not needed AND because they are the worst
# leak in the directory: rerun_coverage_leaveout.sh lists the whole candidate shortlist,
# leaveout_dr2.sh states the patient's ancestry outright, cg_truth_extract.py hardcodes
# --sample <primary participant>.
# ---------------------------------------------------------------------------

PIPELINE_TOOLS = [
    "lib_run.py",
    "intake_hygiene.py",
    "file_facts.py",
    "sex_evidence.py",
    "cg_to_vcf.py",
    "call_quality.py",
    "normalize.py",
    "record.py",
    "ancestry.py",
    "pgs_catalog.py",
    "chromosome_counts.py",
    "weight_coverage.py",
    "build_dr2_cohort.py",
    "impute_joint.py",
    "impute_remote.py",
    "own_calls.py",
    "match_variants.py",
    "score_plink2.py",
    "interpret_request.py",
    "reference_adapter.py",
    "rank_range.py",
]

EXCLUDED_TOOLS_REASON = {
    "apply_leaveout_dr2.py": "analysis; names the hold-out samples",
    "cg_truth_compare.py": "truth arm; carries the array-vs-CG adjudication",
    "cg_truth_extract.py": "truth arm; hardcodes --sample <primary participant>",
    "cg_truth_split.py": "truth arm",
    "coverage_breakdown.py": "analysis; names a prior run and an allele-frequency column",
    "leaveout_dr2.sh": "analysis; states the patient's ancestry",
    "loo_validate_ancestry.py": "validation",
    "palindromic_fraction.py": "analysis",
    "panel_index.py": "build-time; produces refdata/panel/sites_index",
    "rerun_coverage_leaveout.sh": "analysis; lists the full candidate shortlist",
    "stage_refdata.sh": "build-time fetcher",
}

SKILLS = ["read-and-reason", "ancestry", "select", "score", "interpret", "report"]

# Docs staged alongside the tools. interfaces/reference-distribution.md is cited by two
# skills and echoed into reference_adapter.py's own output JSON, so its absence would be a
# filesystem error at exactly the moment the pipeline is supposed to correctly refuse.
STAGED_DOCS = [
    ("AGENT.md", "AGENT.md"),
    ("JUDGMENT-CORE.md", "JUDGMENT-CORE.md"),
    ("interfaces/reference-distribution.md", "interfaces/reference-distribution.md"),
]

# ---------------------------------------------------------------------------
# refdata: what gets deleted from the per-run clone
#
# These trees were produced by subsetting 1000 Genomes through the patient's own array
# positions -- the provenance is preserved verbatim in the .pvar headers as
# `bcftools view -R /tmp/patient_positions.tsv`. Their pruned marker counts are the two
# numbers ancestry.json cites as the evidence for its two-reference stability check, so
# staging them hands the agent both the completed projection basis and the check it is
# supposed to earn.
#
# phase3/ stays: it is the clean upstream 1000 Genomes and is a legitimate tool input.
# ---------------------------------------------------------------------------

REFDATA_DELETE = [
    ("ancestry-ref/build", "patient-position-derived PCA basis; its pruned marker count is "
                           "ancestry.json's cited evidence"),
    ("ancestry-ref/build_p3chr1", "patient-position-derived chr1 PCA basis; its pruned "
                                  "marker count is the second half of the stability check"),
    ("ancestry-ref/phase3_sub", "1000 Genomes subset through /tmp/patient_positions.tsv; "
                                "the site list IS the patient's array"),
    ("ancestry_build.log", "records the build recipe and the marker count"),
]

# Withheld from the clone as well on the gb10 route, where Beagle runs only on the gb10. With
# no jar and no JVM in the sandbox (the system JVM is masked by the profile, see
# profile.jvm_dirs), nothing in it can run Beagle, so nothing can fall back to this machine.
GB10_REFDATA_DELETE = [
    ("bin/beagle.jar", "on the gb10 route Beagle runs only on the gb10; nothing in the sandbox "
                       "can run it here, so nothing can fall back to this machine"),
    ("jre", "the bundled JVM, withheld on the gb10 route for the same reason"),
]

# Kept, but recorded in the manifest as a known soft key: this is the reference that was
# tried and rejected. Its presence plus the absence of any other single-file genome-wide
# reference is a nudge toward the rejected path. See docs/DECISIONS.md.
REFDATA_FLAGGED = [
    ("ancestry-ref/1kg.omni_broad_sanger_combined.b37.vcf.gz",
     "the reference that was tried and rejected; still the only single-file option after "
     "the deletions above, which is a real gap in the skill wording"),
]

# ---------------------------------------------------------------------------
# Private tokens
#
# The launch gate must match the real participant identifiers and one surname, and the
# benchmark's own strings: its models, its trait and the trait's ontology id, result values and
# run names. None of them can be published, so they load from a file outside the repository,
# together with the soft tokens and scrub rules that name them. The load FAILS CLOSED: a
# missing, unreadable, malformed or empty file raises at import rather than returning an empty
# list. An empty list would not fail -- it would make the gate report `passed` while checking
# nothing, which is worse than having no gate.
#
#   hard, provider_hard   what no file staged for the agent, or for the provider, may contain
#   deny_eval_run         a second scenario's run directory, kept from every sandbox
#   prior_run             the prior run's directory: its files are kept from every sandbox,
#                         and its id is scrubbed from staged text
#   soft                  optional: [token, why] pairs, reported and never failed on
#   scrub_rules           optional: [path in sandbox, literal, replacement, why]
# ---------------------------------------------------------------------------

PRIVATE_TOKENS_ENV = "PRS_HARNESS_TOKENS"
PRIVATE_TOKENS_DEFAULT = os.path.expanduser("~/.config/prs-harness/tokens.json")


def _load_private_tokens():
    path = os.environ.get(PRIVATE_TOKENS_ENV) or PRIVATE_TOKENS_DEFAULT
    where = "%s (override with the %s environment variable)" % (path, PRIVATE_TOKENS_ENV)
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except FileNotFoundError:
        raise RuntimeError("launch-gate token file not found: " + where)
    except (OSError, ValueError) as exc:
        raise RuntimeError("launch-gate token file unreadable or not JSON: %s: %s" % (where, exc))
    if not isinstance(data, dict):
        raise RuntimeError("launch-gate token file must be a JSON object: " + where)
    for key in ("hard", "provider_hard"):
        vals = data.get(key)
        if (not isinstance(vals, list) or not vals
                or not all(isinstance(v, str) and v.strip() for v in vals)):
            raise RuntimeError("launch-gate token file key %r must be a non-empty list of "
                               "non-empty strings: %s" % (key, where))
    for key in ("deny_eval_run", "prior_run"):
        run = data.get(key)
        if not isinstance(run, str) or not run.strip() or "/" in run or ".." in run:
            raise RuntimeError("launch-gate token file key %r must be a bare directory "
                               "name: %s" % (key, where))
    for key, width in (("soft", 2), ("scrub_rules", 4)):
        rows = data.get(key, [])
        if (not isinstance(rows, list)
                or not all(isinstance(r, list) and len(r) == width
                           and all(isinstance(v, str) for v in r)
                           and r[0].strip() and r[1].strip() for r in rows)):
            raise RuntimeError("launch-gate token file key %r, where given, must be a list "
                               "of lists of %d strings: %s" % (key, width, where))
    return {"hard": list(data["hard"]), "provider_hard": list(data["provider_hard"]),
            "deny_eval_run": data["deny_eval_run"], "prior_run": data["prior_run"],
            "soft": [tuple(r) for r in data.get("soft", [])],
            "scrub_rules": [tuple(r) for r in data.get("scrub_rules", [])]}


_PRIVATE = _load_private_tokens()


# ---------------------------------------------------------------------------
# The gb10
#
# Beagle can run on the gb10 instead of this machine (`run --impute-backend gb10`). Where the
# gb10 is and which key reaches it are kept in a private file outside the repository, like the
# launch-gate tokens: its address and account name are not published, in code, docs or tests.
#
# The same file holds the dev allow-list, `dev_cases`: the only cases whose genome may go to the
# gb10. Never a held-out case -- the gb10's own Claude agent can read anything under that
# account -- and the list is outside the repo so a held-out case id is not written into it here.
#
# Read only by a run that asks for the gb10, and then FAIL CLOSED: no file, or a file missing a
# field, refuses the run before anything is staged. Nothing here has a default to fall back to.
# ---------------------------------------------------------------------------

GB10_CONFIG_ENV = "PRS_HARNESS_GB10"
GB10_CONFIG_DEFAULT = os.path.expanduser("~/.config/prs-harness/gb10.json")
# user@address, nothing ssh could read as an option or a second argument
_GB10_HOST_RE = re.compile(r"[A-Za-z0-9_][A-Za-z0-9._-]*@[A-Za-z0-9][A-Za-z0-9.-]*")


def load_gb10_config():
    """The gb10's host, key path and dev allow-list. Errors name the file, never the host."""
    path = os.environ.get(GB10_CONFIG_ENV) or GB10_CONFIG_DEFAULT
    where = "%s (override with the %s environment variable)" % (path, GB10_CONFIG_ENV)
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except FileNotFoundError:
        raise RuntimeError("gb10 configuration not found: " + where)
    except (OSError, ValueError) as exc:
        raise RuntimeError("gb10 configuration unreadable or not JSON: %s: %s" % (where, exc))
    if not isinstance(data, dict):
        raise RuntimeError("gb10 configuration must be a JSON object: " + where)
    host, identity, cases = data.get("host"), data.get("identity"), data.get("dev_cases")
    if not isinstance(host, str) or not _GB10_HOST_RE.fullmatch(host):
        raise RuntimeError("gb10 configuration key 'host' must be user@address: " + where)
    if not isinstance(identity, str) or not os.path.isabs(identity):
        raise RuntimeError("gb10 configuration key 'identity' must be the absolute path of "
                           "the ssh key: " + where)
    if (not isinstance(cases, list) or not cases
            or not all(isinstance(c, str) and c.strip() == c and c for c in cases)):
        raise RuntimeError("gb10 configuration key 'dev_cases' must be a non-empty list of "
                           "case ids: " + where)
    return {"path": path, "host": host, "identity": identity, "dev_cases": list(cases)}


# ---------------------------------------------------------------------------
# The launch gate
#
# HARD tokens fail the run. SOFT tokens are recorded and reported but do not fail, because
# each has a legitimate non-leaking occurrence (a five-way superpopulation enumeration, a
# generic numeric string) and failing on them would train us to disable the gate. The values
# are the private token file's (Private tokens, above), apart from two soft tokens that are
# names in this code.
# ---------------------------------------------------------------------------

HARD_TOKENS = list(_PRIVATE["hard"])

SOFT_TOKENS = [
    ("OPEN_QUESTIONS", "a citation pointer, not answer content -- the skills and three "
                       "tools cite Q1/Q3/Q6 and each states the substance before citing, "
                       "so the agent never needs the file. The file itself is never staged "
                       "and is on the denial probe list. The agent is told in its brief "
                       "that it is out of scope, so the pointers do not send it hunting."),
    ("patient_positions", "matches `def patient_positions(vcf)` in weight_coverage.py, "
                          "which is a function name. The real case -- reference data subset "
                          "through the patient's own array positions -- is caught precisely "
                          "by the .pvar header scan in staging._assert_no_patient_positions, "
                          "which greps provenance headers rather than source code."),
    # and the soft tokens that are a run's own values, from the private token file
    *_PRIVATE["soft"],
]

# Binary and archive extensions the token grep skips. Recorded in the manifest so the gate's
# blind spot is stated rather than implied.
# The three sandboxes are different boundaries and need different lists. One list applied
# everywhere would be a list nobody can satisfy, and the way that fails is that somebody
# turns the gate off.
#
#   prs       the full list. It must learn the trait by asking and the model by selecting.
#   provider  a reduced list. The model id and the trait arrive IN the request it is
#             answering, so seeing them in its own tooling tells it nothing it was not
#             handed. What it must not see is the patient, the outcome, or another run.
#   persona   not enforced. The persona file is the person's own account of themselves --
#             it is the input, not a key -- and it never enters the PRS sandbox.
PROVIDER_HARD_TOKENS = list(_PRIVATE["provider_hard"])

TOKEN_POLICY = {
    "prs": {"enforced": True, "hard": None,
            "why": "the agent under test; it must learn the trait by asking and the model "
                   "by selecting"},
    "provider": {"enforced": True, "hard": PROVIDER_HARD_TOKENS,
                 "why": "the model id and trait are in the request it is answering, so they "
                        "are inputs rather than answers. The patient, the score and any "
                        "other run remain out of bounds."},
    "persona": {"enforced": False, "hard": None,
                "why": "the persona file is the person's own account of themselves. It is "
                       "the input to this sandbox and never enters the PRS one."},
}

GREP_SKIP_EXT = {
    ".gz", ".bgz", ".zip", ".bz2", ".tbi", ".csi", ".bref3", ".pgen", ".pvar", ".psam",
    ".bed", ".bim", ".fam", ".jar", ".fa", ".fai", ".gzi", ".bin", ".sscore", ".eigenvec",
    ".eigenval", ".afreq", ".so", ".dylib", ".class",
}
GREP_MAX_BYTES = 8 * 1024 * 1024

# ---------------------------------------------------------------------------
# Scrub rules: applied to the staged COPY, never to agent/.
#
# These files are inside the sandbox, so no boundary helps. A rule names the literal it
# rewrites, so the rules load from the private token file with the tokens they keep out, and
# the files in this repository already carry neutral examples. A rule for an
# alignment-sensitive file must keep its width, which staging.assert_scrub_widths checks:
# AGENT.md's flow diagram is the first thing the agent reads, and a box whose borders no
# longer line up reads as a corrupted file and invites the agent to go looking for an intact
# copy.
# ---------------------------------------------------------------------------

# (relative path in sandbox, literal to find, replacement, why)
SCRUB_RULES = list(_PRIVATE["scrub_rules"])

# Applied to every staged text file after the literal rules above: the prior run's id.
PRIOR_RUN = _PRIVATE["prior_run"]
SCRUB_RUN_ID_PATTERN = r"\b%s\b" % re.escape(PRIOR_RUN)
SCRUB_RUN_ID_REPLACEMENT = "<RUN_ID>"

# ---------------------------------------------------------------------------
# The denial probe. Every path here is attempted, under the identical profile, by a probe
# process at launch. An unexpected success fails the run.
#
# This is what turns "we thought about it" into "we checked this run".
# ---------------------------------------------------------------------------

def deny_probe_paths(home):
    p = os.path.join
    return [
        (p(AGENT, "runs", PRIOR_RUN, "ledger.jsonl"), "the prior run's ordered decision history"),
        (p(AGENT, "runs", PRIOR_RUN, "ancestry/ancestry.json"), "the ancestry call"),
        (p(AGENT, "runs", PRIOR_RUN, "score/RESOLVED_FRACTIONS.md"), "the coverage table"),
        (p(AGENT, "runs", PRIOR_RUN, "report/report_output_placed.md"), "the finished report"),
        (p(AGENT, "runs", PRIOR_RUN, "truth-cg"), "the Complete Genomics truth arm"),
        (p(AGENT, "runs/" + _PRIVATE["deny_eval_run"]), "a second scenario's full key"),
        (p(AGENT, "BUILD_LOG.md"), "states the ancestry call and the distance"),
        (p(AGENT, "OPEN_QUESTIONS.md"), "Q10 carries the final corrected answer"),
        (p(AGENT, "oracle"), "independent pgsc_calc verification outputs"),
        (p(AGENT, ".git"), "tracked docs; log subjects alone leak"),
        (p(PRODUCT_ROOT, ".git"), "the repository's history; commit subjects alone leak"),
        (p(AGENT, "sim/personas"), "the persona files"),
        (p(AGENT, "refdata/ancestry-ref/build"), "deleted from the clone; assert the "
                                                 "original is unreachable too"),
        (p(PRODUCT_ROOT, "reference-provider/work/cohort_score.sscore"),
         "the 300 per-sample scores that ARE the distribution"),
        (p(PRODUCT_ROOT, "reference-provider/reference_distribution.json"),
         "the finished INT-1 artifact"),
        (p(PRODUCT_ROOT, "frontend/prs_ui/answers.py"),
         "answer key hiding in application code"),
        (p(PRODUCT_ROOT, "pgp-candidates"), "thirteen Complete Genomics truth files"),
        (p(PRODUCT_ROOT, "pgp-fixtures"), "four more truth files"),
        (p(PRODUCT_ROOT, "old-git-history"), "bare repo; log subjects leak the ancestry call"),
        (p(PRODUCT_ROOT, "build-inputs-backup-docs/prd.md"),
         "a second copy of the patient-naming PRD outside agent/"),
        (os.path.expanduser("~/Documents/ProjectVault"), "the Obsidian vault. Absent on "
         "the VM, so this probe returns `absent` rather than `denied` there, which is "
         "reported as its own state and is not a pass"),
        (os.path.expanduser("~/.claude/projects"), "prior session transcripts"),
        (os.path.expanduser("~/.claude/file-history"), "verbatim content snapshots"),
        (os.path.expanduser("~/.claude/paste-cache"), "pasted answer-key excerpts"),
        (os.path.expanduser("~/.claude/history.jsonl"), "prompt history"),
        (os.path.expanduser("~/.claude/.credentials.json"),
         "the real token file. The profile binds a COPY of this to the jail HOME, so the "
         "original path must still be unreachable by its own name"),
        (os.path.expanduser("~/.bash_history"), "shell history"),
        (os.path.expanduser("~/.ssh"), "the host's ssh keys, the gb10's among them by default"),
        (os.path.dirname(PRIVATE_TOKENS_DEFAULT),
         "the harness's private configuration: the launch-gate tokens, the gb10 settings and, "
         "by default, the Claude token file"),
        *_gb10_key_probe(),
        *_account_home_probes(),
        # The macOS list probes /private/tmp/claude-<uid>, because there the CLI's
        # scratch root is shared across projects and listing it would expose other
        # sessions. There is no such path here: TMPDIR points inside the sandbox and
        # /tmp is a private tmpfs that dies with the run, so the host's /tmp is not
        # reachable under any name. Probing "/tmp" would read the private tmpfs, come
        # back READABLE, and fail the run for the one thing that is working correctly.
        # Nothing replaces it, and that absence is the record.
        (VAULT, "the harness's own private state"),
    ]


def _gb10_key_probe():
    """The gb10's key, wherever the private config puts it, on every run's list whatever the
    run's route: network is not unshared, so a sandbox that could read the key could reach the
    gb10 with it. A config that exists but cannot be read stops the run here, rather than
    leaving the key unprobed; with no config there is no key to name."""
    if not os.path.exists(os.environ.get(GB10_CONFIG_ENV) or GB10_CONFIG_DEFAULT):
        return []
    return [(load_gb10_config()["identity"],
             "the gb10's ssh key. It never enters a sandbox; this proves it every run")]


def _account_home_probes():
    """The account's own home, when the harness runs with HOME pointed elsewhere.

    On an account other projects share, the harness gets a HOME of its own, so it neither reads
    the account's ~/.claude.json nor binds its Claude credentials into a sandbox, and every ~
    above names that HOME. The account's real Claude state, keys and settings are then named
    here, so every run still proves them unreachable."""
    real = pwd.getpwuid(os.getuid()).pw_dir
    if os.path.realpath(real) == os.path.realpath(os.path.expanduser("~")):
        return []
    return [(os.path.join(real, name), "the account's own %s, outside the harness's HOME" % name)
            for name in (".claude", ".claude.json", ".ssh", ".config", ".bash_history")]


# Write probes. Seatbelt enforces file-write* even though chmod and chflags do not.
def write_probe_paths(sandbox_root):
    return [
        (os.path.join(REFDATA_SRC, ".harness_write_probe"),
         "the original refdata must be immutable to the sandboxed process"),
        (os.path.join(sandbox_root, "refdata", ".harness_write_probe"),
         "the per-run clone must be read-only to the agent"),
        (os.path.expanduser("~/.claude/.harness_write_probe"), "the real CLI state dir"),
        (os.path.expanduser("~/Documents/ProjectVault/.harness_write_probe"),
         "vault-writing skills must not reach it. Absent on the VM"),
        (os.path.join(VAULT, ".harness_write_probe"), "the harness's own state"),
        (os.path.expanduser("~/.claude/.harness_write_probe_real"),
         "the real CLI state dir, by its own path"),
    ]


# ---------------------------------------------------------------------------
# Network
# ---------------------------------------------------------------------------

# The proxy permits exactly these hosts. pgs_catalog.py cannot run without either of them.
PROXY_ALLOW_HOSTS = [
    "www.pgscatalog.org",
    "ftp.ebi.ac.uk",
]

# The CLI's own hosts, kept separate from the data hosts above because they are the transport
# the agent runs on rather than anything it reads. Until 2026-08-06 these were written at the
# call site in orchestrator.py, which meant the list this file records was not the list a run
# was built against -- the thing this module's docstring says it exists to prevent.
#
# platform.claude.com was added 2026-08-06 after two runs died on `401 OAuth access token has
# expired`. In run-20260806-010140-be6ab8 the CLI hit it four times in six seconds
# (13:02:53-58), the proxy denied all four because it was not listed, and the run stopped six
# seconds later. Without it an access token that expires mid-run cannot be refreshed from
# inside the sandbox, so every run has a hard ceiling at whatever was left on the token it
# started with. The evidence is the retry loop and the timing, not a confirmed identification
# of the refresh endpoint; a run surviving past an expiry is what would confirm it.
CLI_ALLOW_HOSTS = [
    "api.anthropic.com",
    "statsig.anthropic.com",
    "platform.claude.com",
]

PROXY_PORT_RANGE = (18800, 18899)

# Linux. /usr/local/bin first because plink2 and the Linux liftOver live there;
SANDBOX_PATH = "/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"

# ---------------------------------------------------------------------------
# The host's own tools, where they are not in the system trees
#
# On a box where the harness is not root, nothing can be installed under /usr: the toolchain is
# in user-level conda-style prefixes (python with numpy, bcftools, htslib) and the Claude CLI
# is a single binary in the account. A private file names them, ~/.config/prs-harness/host.json
# ($PRS_HARNESS_HOST names another):
#
#   {"tool_prefixes": ["/abs/env", "/abs/bio"], "claude_cli": "/abs/claude"}
#
# Each prefix is bound read-only into the PRS and provider sandboxes at its own path (a conda
# prefix is not relocatable) and its bin/ goes on PATH ahead of the system directories, in the
# order listed; the CLI is bound into all three sandboxes and run by that absolute path. Each is
# bound through an anchor at its parent directory (profile.build_profile), so a parent that
# holds the sandboxes would cover them: that is refused here. Both keys are optional, and with
# no file nothing changes: the CLI is `claude` on PATH and the system directories are all
# there is. A file that exists but does not parse, or names a path that is not there, refuses
# the import, like the token file: a run that cannot see its tools fails in ways that look
# like the agent's judgment.
# ---------------------------------------------------------------------------

HOST_CONFIG_ENV = "PRS_HARNESS_HOST"
HOST_CONFIG_DEFAULT = os.path.expanduser("~/.config/prs-harness/host.json")


def load_host_config(path=None):
    path = path or os.environ.get(HOST_CONFIG_ENV) or HOST_CONFIG_DEFAULT
    out = {"path": None, "tool_prefixes": [], "claude_cli": None}
    if not os.path.exists(path):
        return out
    where = "%s (override with the %s environment variable)" % (path, HOST_CONFIG_ENV)
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError) as exc:
        raise RuntimeError("host configuration unreadable or not JSON: %s: %s" % (where, exc))
    if not isinstance(data, dict) or set(data) - {"tool_prefixes", "claude_cli", "_comment"}:
        raise RuntimeError("host configuration must be a JSON object with only tool_prefixes "
                           "and claude_cli: " + where)
    prefixes = data.get("tool_prefixes") or []
    if (not isinstance(prefixes, list)
            or not all(isinstance(p, str) and os.path.isabs(p) and os.path.isdir(p)
                       for p in prefixes)):
        raise RuntimeError("host configuration key 'tool_prefixes' must list absolute paths of "
                           "existing directories: " + where)
    cli = data.get("claude_cli")
    if cli is not None and (not isinstance(cli, str) or not os.path.isabs(cli)
                            or not os.path.isfile(cli) or not os.access(cli, os.X_OK)):
        raise RuntimeError("host configuration key 'claude_cli' must be the absolute path of "
                           "an executable file: " + where)
    sandboxes = os.path.realpath(SANDBOXES)
    for p in prefixes + ([cli] if cli else []):
        anchor = os.path.dirname(os.path.realpath(p))
        if sandboxes == anchor or sandboxes.startswith(anchor.rstrip(os.sep) + os.sep):
            raise RuntimeError("host configuration path %s would be bound through %s, which "
                               "holds the sandboxes and would cover them: %s" % (p, anchor, where))
    out.update(path=path, tool_prefixes=[os.path.realpath(p) for p in prefixes],
               claude_cli=os.path.realpath(cli) if cli else None)
    return out


HOST = load_host_config()

# The CLI every agent runs, and the PATH every sandbox gets.
CLAUDE_CLI = HOST["claude_cli"] or "claude"
SANDBOX_PATH_HOST = ":".join([os.path.join(p, "bin") for p in HOST["tool_prefixes"]]
                             + [SANDBOX_PATH])


def host_reads(sandbox):
    """What of the host's tools a sandbox gets, read-only: the prefixes for the two that run
    tools, the CLI for all three."""
    reads = list(HOST["tool_prefixes"]) if sandbox in ("prs", "provider") else []
    if HOST["claude_cli"]:
        reads.append(HOST["claude_cli"])
    return reads


# Keys copied from the real ~/.claude.json into the jail HOME. An allow-list, not a
# strip-list, so a new key added by a CLI upgrade is excluded by default rather than
# included by default.
CLAUDE_JSON_SEED_KEYS = [
    "oauthAccount",
    "userID",
    "machineID",
    "installMethod",
    "hasCompletedOnboarding",
    "lastOnboardingVersion",
    "firstStartTime",
    "numStartups",
    "migrationVersion",
]

# No keychain on Linux. The CLI reads ~/.claude/.credentials.json, which the
# bubblewrap profile binds in as a single read-only file. Kept as a name so
# profile.build_profile's allow_keychain argument still resolves.
KEYCHAIN_DIR = os.path.expanduser("~/.claude")

# ---------------------------------------------------------------------------
# Timings, measured. These size the transport rather than being guessed at.
# ---------------------------------------------------------------------------

# The model every spawned agent runs on when run_harness.py is not given --model. Pinned by
# full id, never an alias: an alias moves when the line-up moves, and a model change mid-corpus
# is the same problem as a spec change mid-corpus. The September 2026 corpus (tag
# eval-corpus-2026-09) ran on claude-opus-4-8; a run on this model is a new experiment, not a
# reproduction of it. --model applies to all three agents (agent under test, simulated person,
# provider).
DEFAULT_MODEL = "claude-opus-5-5"

# Free disk a run past select needs under SANDBOXES. Measured 2026-10-08: the agent's
# own PCA basis (~1 GB once its intermediates are dropped), the genome's imputed output
# (~7.5 GB: the 302-sample cohort VCFs and the patient's), and the provider's reference panel
# and work (~2-3 GB). A run that filled the disk mid-imputation was lost outright, ENOSPC in the
# proxy log and all; refusing at launch costs nothing.
FULL_RUN_MIN_FREE_GB = 15

TIMEOUT_ASK_PERSON = 900          # a human may be typing
# The provider may have to BUILD its reference panel before it can build a distribution,
# and the panel is proportional to the model. Measured 2026-08-05: about 1.1 million variants
# took ~34 min via bcftools view -T over phase3, one chromosome at a time. A model of about
# 7.4 million variants is 6.6x that, so ~4h. At the old 1800s the provider was killed six times
# over, five hours into a run, after imputation had already succeeded.
#
# The panel is rebuilt per run rather than cached, deliberately: a cache means nothing
# exercises the build path after the first run, and two runs are then not comparable.
# Faster is worth having -- the per-chromosome bcftools calls are independent and could run
# in parallel -- but faster, not cached.
TIMEOUT_PROVIDER = 21600          # 6h: reference panel build + distribution
# Joint imputation (tools/impute_joint.py) is the binding term, and it is slow on purpose:
# accuracy over speed. Measured 2026-08-04 on chr20 at n=301 hold-outs -- ~24 min/chromosome,
# of which Beagle is about half and the per-chromosome reference rebuild the other half. The
# whole genome is therefore ~5.7h, plus ~40 min for the rest of the pipeline.
#
# Three measured negatives behind that number, so nobody re-derives them. All on chr20,
# 2026-08-04, Beagle time only:
#   full ref 2203 samples, 302 targets, 1,739,315 markers ...... 180s
#   same, reference cut to 31,904 markers (typed + model) ...... 174s   (54x fewer markers, -3%)
#   full ref 2454 samples, 51 targets .......................... 200s   (6x fewer targets, +11%)
# So neither marker count nor target count is the cost. Reference SAMPLE count is, and it looks
# linear: +11% samples gave +11% time. That rules out both restricting the reference to the
# positions the score needs and shrinking the hold-out batch. Beagle is also CPU-only Java with
# a sequential forward-backward pass, so there is no GPU path.
#
# The one lever left is the per-chromosome reference rebuild, which is about half the wall time
# (chr20: 374s total, 180s of it Beagle). Prebuilding a bref3 panel per superpopulation would
# take the genome from ~5.7h to ~2.9h. Not done: it fixes the hold-out set at panel-build time,
# so --size and --seed stop being agent judgments.
# ~5h joint imputation + up to ~4h inside the provider handoff + everything else. 8h was
# not enough for a large model; 14h leaves headroom without being unbounded. If a run needs
# more than this, the answer is a faster panel build, not a larger number.
TIMEOUT_AGENT_TOTAL = 50400       # 14h
POLL_INTERVAL = 0.2
