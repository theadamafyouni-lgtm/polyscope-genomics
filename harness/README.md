# The run harness

Runs the PRS pipeline end to end, unattended, with an isolation boundary that makes the
result mean something.

Four participants. The PRS agent runs the pipeline. The persona agent plays the patient. The
provider agent builds the INT-1 reference when one is asked for. The orchestrator routes
messages between them, enforces the boundaries, and records everything.

Nothing under `../agent/` or `../reference-provider/` is modified. Staging copies, and
scrubbing operates on the copy.

## Running it

```bash
./run_harness.py probe                     # boundary check, throwaway sandbox, seconds
./run_harness.py gate --persona p01        # stage + gate, launch nothing, no tokens spent
./run_harness.py selftest --persona p01    # transport round trip, four questions
./run_harness.py run --persona p01         # the whole pipeline, unattended
./run_harness.py run --backend human       # the live demo; waits for the window
./run_harness.py run --stop-after select   # partial run, for grading one stage
./run_harness.py run --bare                # no pipeline at all: the control arm
./run_harness.py run --transport minimal   # a different comms/harness_client.py
./run_harness.py teardown <run-id> --remove
```

Use `gate` while changing anything. It builds the sandboxes, runs every check, writes the
manifest and stops before spending a single token, so a staging mistake costs seconds rather
than a full pipeline run.

### Partial runs (`--stop-after`)

`run --stop-after <stage>` asks the agent to run the stages up to and including `<stage>` and
then end its turn. Everything else about the run is unchanged: same staging, same launch
gate, same routing, same manifest, and the sandbox is frozen the same way. Without the flag
nothing about a run differs from before it existed.

The one thing a partial run does announce differently is its stage list. `run_started`
declares only the stages it intends to reach, which is what the window plans its progress
rows off (`planned = declared or STAGES`) and what `interfaces/live-run.md`'s open question 2
asks the harness for. Declaring all six would leave the ones it was never going to reach
sitting at `pending` for the rest of the run.

The stop is **asked for in the brief, not enforced.** The agent is one blocking subprocess,
so stopping it from outside means killing it mid-stage — which truncates its ledger and
destroys anything it had backgrounded, and is exactly what freezing was taught not to do.
Instead the instruction is given and the result is read back: `partial_run.honoured` in the
manifest says whether the agent stopped where it was asked to, and a run that overran or that
never reached the stage gets a caveat saying so. Check that field before grading a run.

Two things to know before you grade a stop-after-`select` run:

- It stops **before** the `SEL-12` reselect loop can happen. Reselect is driven by what
  `score` measures, so a run that never scores can only ever produce select's *first* model
  choice. That is a coherent thing to grade — it is not the same thing as the model the full
  pipeline would have ended on.
- The stage names are the ledger's and the window's (`door ancestry select score interpret
  report`), not the skill directory names in `agent/.claude/skills/` — the door's skill is
  called `read-and-reason`.

### Bare runs (`--bare`)

`run --bare` is the control arm. The PRS sandbox gets three things — the person's file, the
comms transport, and the jail HOME — and the agent gets a fixed brief that says where the
file is, how to reach the person, and *talk to them, and decide what to do*. Nothing about
stages, refusal, coverage or model selection.

Not staged: `.claude/skills/`, `tools/`, `AGENT.md`, `JUDGMENT-CORE.md`, `interfaces/`,
`refdata/`, and no `runs/<run-id>/` either — a run directory is the ledger's shape and the
ledger is part of what is being withheld. The sandbox root is writable, so an agent that
wants somewhere to put things can make it, and that is its own decision.

Everything else is **identical to a normal run** and deliberately so: same persona backend,
same launch gate, same profile, same proxy, same transport, same manifest, same freeze. The
comparison is the pipeline against its absence, not two different test setups. Three things
are staged that are not the pipeline, because removing them would change what the CLI can
physically do rather than what the agent knows: `.claude/settings.json` (the CLI's own deny
rules), `tmp/` (what `TMPDIR` points at), and the `comms/` directories.

The brief is **fixed**. It is not assembled from the pipeline brief and it does not grow — a
brief that gains a clause every time a bare run does something surprising is a second
variable, and then neither arm means anything. It is recorded verbatim in the manifest under
`bare_run.brief`.

The manifest records `bare: true` at the top level, plus a `bare_run` block listing what was
kept and what was withheld, and a caveat saying the run is not evidence about the pipeline.
Absence of the key means a pipeline run, the same convention `partial_run` uses. `run_started`
and `run_completed` carry `bare: true` as well, so a live-run reader can tell without opening
the manifest.

Three things to expect when you read one:

- `stage_judgments` and `post_run_inventory` are normally empty, and the window's progress
  rows never advance. Nothing asked the agent for a ledger. The evidence is the harness
  transcript and the agent's own session transcript, both in the vault.
- `outcome.what_this_run_can_and_cannot_test` is replaced, not qualified. Every decision the
  usual sentence names is produced by a stage.
- The write probe still attempts `<sandbox>/refdata/.harness_write_probe`, and a bare sandbox
  has no refdata, so that one probe records ENOENT where a pipeline run records EPERM. The
  profile emits the refdata write-deny either way; there is nothing there to deny. The gate's
  pass criteria are untouched.

`--bare` and `--stop-after` cannot be combined: `--stop-after` names a stage of the pipeline,
and a bare run has no pipeline to stop in. `gate` does not take `--bare` — it stages a
pipeline sandbox, which is what you want to check before a real run.

### Which transport (`--transport`)

`run --transport <name>` chooses the file staged as `comms/harness_client.py`. `full` is the
default and is `sandbox_tools/harness_client.py`, the transport every run got before the flag
existed; any other name is `sandbox_tools/harness_client_<name>.py`. Nothing else changes —
same destination, same mode, same staging call, same brief, same gate — so a run that differs
here differs in the transport and in nothing else. It works with or without `--bare`.

It exists because the transport's own text is part of what the agent reads, and the full
one's docstring carries specification content: a worked `--why` example naming the
diagnosed-vs-general-cohort criterion, the stage name `select`, the INT-1 provider and its
`request-reference` subcommand, the hard-blocker concept. For a pipeline run that is the
environment describing itself and it costs nothing — the skills say all of it already. For a
control arm the paper describes as specification-free it is the pipeline arriving through the
one file that arm is defined by keeping.

An unknown name is refused before anything is staged, with a listing of what `sandbox_tools/`
actually holds. The list is read off that directory, so adding a transport is dropping a file
into it.

The manifest records `transport` at the top level — `name`, `source`, the `sha256` of the
file as staged, and where it landed — for **every** run, including the default one. Unlike
`bare` and `partial_run`, this one cannot be read from its own absence: two runs that differ
only in their transport leave otherwise identical artifacts, so it is stated rather than
inferred. A non-default transport also gets a caveat saying the run must not be pooled with
runs that got a different one.

`gate` and `selftest` do not take the flag; they stage `full`, which is what you want when
checking staging.

## What each participant can reach

| | PRS agent | persona agent | provider agent |
|---|---|---|---|
| its own sandbox | read/write | read/write | read/write |
| `refdata/` (per-run clone) | read only | — | — |
| the login keychain | read only | read only | read only |
| network | only via the proxy | only via the proxy | only via the proxy |
| everything else under `/Users` | denied | denied | denied |

The persona sandbox holds one persona JSON and nothing else. No tools, no refdata, no run
directory, no score, no coverage number. A person does not know their own polygenic score.

The provider sandbox holds the request, `build_reference.py`, `plink2`, `CONTRACT.md` and an
`inputs/` directory. No answer keys, no other run, no persona, no vault.

## The boundary

`sandbox-exec` (Seatbelt). It is marked deprecated and works perfectly, prints nothing on
stderr, and is inherited by children and grandchildren.

The profile is not deny-by-default globally — that aborts even `/bin/cat`, because the
dynamic loader reads paths that are impractical to enumerate. It is allow-default, then deny
the whole of `/Users` for both read and write, then re-allow the staged subtrees. Every
answer key on this machine is under `/Users`, so that is a real default-deny over the region
that matters without fighting the loader.

Measured, against a live sandboxed agent instructed to break out with cat, python, find,
mdfind, git and symlinks:

- direct read of an answer key — denied
- symlink from an allowed directory to a denied file — denied; Seatbelt resolves the path
- `refdata/../BUILD_LOG.md` through a staged symlink — denied
- write into refdata — denied, which `chmod -R a-w` cannot achieve since the owner reverses it
- `mdfind` for indexed answer content — returns nothing
- `ls` on a denied parent — denied

Every run repeats all of it as the launch gate and writes the errnos into the manifest.

## The three things that cost the most to get right

**`refdata` is cloned, not shared.** `cp -Rc` clones 27 GB in about 250 ms with free disk
unchanged, because APFS clones are copy-on-write. This removes the symlink and therefore the
traversal question — and, far more importantly, makes it possible to delete things. Three
subtrees under `ancestry-ref/` were built by subsetting 1000 Genomes through the patient's
own array positions; the provenance is preserved verbatim in their `.pvar` headers. They go.
`phase3/` stays, and the agent builds its own basis.

**`~/.claude` is the largest leak on the machine and it is not in the repo.** 165 files
matching the answer tokens, including a verbatim content snapshot of
a run's live record under `file-history/`, and seven prior build sessions that
could be `--resume`d into. The fix is a throwaway HOME inside the sandbox, seeded by
allow-list with the keys needed to authenticate and nothing else, with `Library/Keychains`
symlinked back so Security can still find the login keychain. This also closes the memory
feature, whose store lives under HOME.

**Network is denied except a loopback allow-listing proxy.** `select` cannot run without the
PGS Catalog, SBPL has no dependable host predicate, and pre-fetching the catalog would hand
the agent its candidate list — which is select's whole job. Every connection is in
`network.jsonl`, permitted or refused.

## Transport

File-based, and the messages are the record.

The agent calls one tool. `ask-person` blocks; a reference request can take hours, so it is
sent with `--no-wait` and collected with `await-reference`, which waits up to `--timeout` and
exits 6 while the provider is still running:

```
python3 comms/harness_client.py ask-person --stage <s> --question "..." --why "..."
python3 comms/harness_client.py request-reference --no-wait --request runs/<RUN>/interpret/request.json
python3 comms/harness_client.py await-reference --id <the id it printed> --timeout 540
```

The provider runs in its own thread in the orchestrator, one request at a time, so questions to
the person are still routed while a reference is being built. A second request while one is
being built is answered `busy` with the id of the one in flight. Each request starts from an
emptied provider `inputs/`, `work/` and artifact, and a provider still running when the PRS agent
exits is stopped, not left behind.

Every path in a reference request must resolve inside the run's own `runs/<RUN>/`, which must
itself be a real directory; files are read from a descriptor checked to be a regular file still
inside it, and the artifact and every reply are written into the sandbox one directory at a time
without following a link, by rename. A refusal does not say where a path resolved to.

It computes nothing and decides nothing. The orchestrator picks the message up, appends it to
the canonical transcript, and routes it — to the person via the live-run contract, or to a
provider agent spawned for that one request.

Which file is staged there is `run --transport <name>`; the path the agent calls is
`comms/harness_client.py` for every one of them.

The person is reached through `../frontend/interfaces/live-run.md`: two append-only JSONL
files in a run directory. A persona and a human satisfy that contract identically, by
appending the same `answer` line. That is what makes the unattended run and the live demo the
same test rather than two tests that share a pipeline.

A watcher also notices `interpret/request.json` appearing without the transport being called.
It records that; it does not deliver. Auto-delivering would conceal an agent that wrote the
request and never asked.

## The manifest

`vault/runs/<RUN_ID>/manifest.json`. Written from what happened, not from what staging was
supposed to do.

- every file staged, with source, sha256, size and mode; the clone's timing and file count;
  every deletion; every scrub substitution with its before and after
- the full SBPL profile per agent, plus the denial probe: every forbidden path attempted
  under that exact profile, with the errno
- the token grep, including what it could not scan
- which transport was staged: `transport.name`, its source path and its sha256, on every run
- what the Spotlight index contained before the run
- the transcript, the network log, both agents' own CLI transcripts
- a post-run inventory of everything the run created

Two things it deliberately records that are not flattering: the token grep's blind spot
(compressed and binary reference data is not scanned), and the fact that the Spotlight index
already held the answer text before the run started.

## Layout

```
harness/            the package
sandbox_tools/      harness_client.py -- the only thing injected into the agent's sandbox
                    harness_client_<name>.py is `run --transport <name>`; the manifest says
                    which one a run got
vault/              0700; manifests, transcripts, probe results. Never named by any profile.
sandboxes/<RUN_ID>/ prs/ persona/ provider/ -- frozen read-only after the run, kept as evidence
live-runs/          the live-run contract's run directories, shared with the window
docs/               DECISIONS-AND-FLAGS.md -- what needs a human decision
```
