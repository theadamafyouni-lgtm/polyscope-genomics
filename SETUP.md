# Building the eval VM from a bare box

Ubuntu 24.04 (noble), 8 vCPU / 16 GB, 160 GB disk. Everything below was read off the
working machine on 2026-09-05 rather than recalled, **except where it says UNVERIFIED**.

> **Read §8 before you plan anything.** On 2026-09-23 this document was audited against
> the working box, section by section, and §8 was found wrong. Since 2026-09-24,
> `agent/refdata` can be rebuilt from published sources with `fetch_refdata.py` and
> `build_refdata.py`, every file checked by sha256 against the working box's copy. It has
> not yet been run on an empty machine. §8 lists the few files that cannot be reproduced,
> and what to copy off the working box before a wipe. Everything else in
> §1–§9 is corrected and evidenced. §10 has still never been run on a rebuilt box.
> Sections carrying an UNVERIFIED marker are the remaining gaps.

The four known failure points from the last attempt: numpy missing, plink2 not
executable, macOS binaries staged instead of Linux ones, and `setup.sh` / `preflight.py`
/ `fetch_refdata.py` having only ever run where everything was already present.

## 1. System packages

```bash
apt update
apt install -y build-essential bcftools tabix zstd bubblewrap openjdk-17-jre-headless \
               python3-numpy python3-scipy python3-pandas \
               git curl unzip zip tmux
```

The Python packages come from apt, not pip — see §2 for why. `zip` is used when repacking
staged artifacts; `tmux` because both the refdata fetch and a 40-minute run outlive an SSH
session.

`setup.sh` in this repo installs the same set idempotently and is the supported path.
**It differs from the list above in one respect**: it installs `default-jre`, which on
noble resolves to `openjdk-21-jre` and is not headless. The working box runs
`openjdk-17-jre-headless`, which is what the table below records, so this document keeps
17. The two should be reconciled; until they are, prefer the list above on a headless box.

Confirmed present on the working box at these versions:

| package | version |
|---|---|
| bcftools | 1.19-1build2 |
| bubblewrap | 0.9.0-1ubuntu0.1 |
| build-essential | 12.10ubuntu1 |
| openjdk-17-jre-headless | 17.0.20.1+1~24.04 |

`tabix` and `bgzip` come from the `tabix` package, not with bcftools, which does not depend
on it. `zstd` unpacks the HGDP+1kGP tarball. `build_refdata.py` (§8) needs all three.

**bubblewrap is not optional.** It is the sandbox boundary on Linux, the counterpart of
the macOS seatbelt, and `harness/harness/profile.py` generates its profile.

## 2. Python

System python 3.12.3, packages installed system-wide rather than in a venv on the
working box.

Installed by apt in §1, **not by pip**. The harness runs behind an allow-list proxy that
returns 403 for PyPI, so `pip install` cannot reach anything from inside a run. `setup.sh`
and `preflight.py` both say so at their install sites.

VERIFIED 2026-09-23 by import-grep over the code. This replaces the earlier
UNVERIFIED note, which asked for exactly this list:

| package | needed by | status |
|---|---|---|
| `numpy` | `agent/tools/ancestry.py`, `reference-provider/build_reference.py` | **required.** Its absence is the failure that produced four worthless FAIL grades |
| `scipy` | nothing imports it | **install it anyway.** `preflight.py` treats its absence as FAIL and will refuse the machine. Either install it or fix preflight; do not skip it silently |
| `pandas` | nothing imports it | **not required.** Harmless if present |
| `requests` | nothing, anywhere in either repository | **not required.** It was in the old list by mistake |

The previous wording here told you to `pip3 install numpy pandas openpyxl requests`. That
installed two packages nothing imports, omitted the one package preflight fails on, and
used the one mechanism the proxy blocks.

## 3. Binaries that are not packaged

Four live outside apt, and two of them are the ones that went wrong last time.

| binary | path on the working box | note |
|---|---|---|
| `plink2` | `/usr/local/bin/plink2` | **must be the Linux build and must be `chmod +x`.** Staging a macOS binary here is one of the four known failures. The copy that matters is `agent/refdata/bin/plink2`: it must be exactly `PLINK v2.0.0-a.6.6LM 64-bit Intel (16 Jan 2025)`, sha256 `469bdf52…0d49`, the build every published run used. `build_refdata.py` (§8) installs it from `plink2-assets/alpha6/plink2_linux_x86_64_20250116.zip`, and `setup.sh` copies a system plink2 into refdata only if it is that same file |
| `sandbox-exec` | `/usr/bin/sandbox-exec` | **not optional.** The harness launches every agent as `sandbox-exec -f <profile> <cmd>`, a macOS command; this shim translates the profile into bubblewrap arguments, so the harness never learns which platform it is on. It ships in this repo at `harness/sandbox-exec` and nothing installs it but the line below |
| `nextflow` | `/usr/local/bin/nextflow` | needs java, which is why openjdk is above. **UNVERIFIED whether anything still uses it** — see below |
| `claude` | `/usr/bin/claude` | via npm, see below |

```bash
install -m 755 harness/sandbox-exec /usr/bin/sandbox-exec
```

Until 2026-09-23 this document never named `sandbox-exec` as a thing to install. `setup.sh` installs it; following this
document by hand did not, and the result is a box where every agent launch fails.
`preflight.py` checks for it.

Without root the install line is not needed: when `/usr/bin/sandbox-exec` is absent the harness
runs the repository's own copy, `harness/sandbox-exec`, and `$PRS_HARNESS_SANDBOX_EXEC` names
any other. The manifest records the one a run used (`host.sandbox_exec_path`).

**nextflow may be vestigial.** `git grep -l nextflow` over the tracked tree matches four
files — `.gitignore`, this document, `agent/.gitignore` and
`agent/docs/research-R9-missing-weight-fraction.md` — and none of them is code.
`grep -rn nextflow harness/ agent/tools/` returns nothing. The `agent/.nextflow.log*`
files on the working box are dated 2026-07-28, before the current harness. It is installed
here and kept in the list because **a box without it has not been tested**, not because
anything is known to need it.

`liftOver` and `beagle.jar` live under `agent/refdata/bin/` and come with the refdata,
not with the system. **The `liftOver` in refdata must be the Linux build**, same trap as
plink2. `beagle.jar` is in the manifest. `liftOver` is not: `setup.sh` downloads UCSC's
current build, which is no longer the working box's (§8, "What is not reproduced").

## 4. Node and the Claude CLI

```bash
apt install -y nodejs npm
npm install -g @anthropic-ai/claude-code@2.1.293
claude --version    # must print 2.1.293
which -a claude     # must be ONE entry, and it must be the npm one
```

**Pin the version and pin it after any sign-in.** The September 2026 corpus (tag
`eval-corpus-2026-09`) ran on 2.1.224 with `claude-opus-4-8`; some versions after that could
not open their scratch directory inside the sandbox, so every case died in under a second
with what read like a permissions error. The agent now runs on `claude-opus-5-5`, which needs
2.1.280 or newer; 2.1.293 was checked inside the sandbox on 2026-10-08 (a door-only run and
full runs). A run on it is a new experiment, not a reproduction of the corpus. This is still
the single most expensive thing to get wrong.

**The same symptom has a second cause.** A half-finished `npm install -g` of the CLI can
leave `bin/claude.exe` as a 500-byte stub ("claude native binary not installed"); every case
then dies with `execvp claude: Permission denied`. It happened on 2026-10-08. `claude
--version` failing on the host is the tell, and `run_harness.py` now checks it before launch.

**The pin is only as good as your `PATH`.** npm installs to `/usr/bin/claude`, and
`/usr/local/bin` comes before `/usr/bin` on Ubuntu — so a second `claude` there silently
wins and the pin means nothing. The VM carried a leftover
`/usr/local/bin/claude -> ~/.local/bin/claude` from an older install until
2026-09-23; it never bit, only because its target had been deleted, which made it
unexecutable and skipped by `PATH` lookup. `which -a claude` is the check.
`preflight.py` catches a *working* shadow through the version warning, but a broken one
is invisible to it.

## 5. Authentication

```bash
# on a machine with a browser:
claude setup-token          # prints a one-year token, once

# on the VM:
echo "export CLAUDE_CODE_OAUTH_TOKEN='<token>'" >> ~/.bashrc
chmod 600 ~/.bashrc
```

**There is no browser login path on a headless box**, and the four-hourly OAuth refresh
does not work there, which is what made every previous batch die every few hours.

Two things learned on 2026-09-05:

- **`build_env` in `harness/harness/agents.py` builds the sandbox environment from
  scratch and drops anything not listed.** The token has to be passed through
  explicitly; a plain export in the shell does not reach the agent.
- **A shell that predates the `.bashrc` edit does not have it**, and then every case
  fails in seconds with `Not logged in`. Reading the token from a file rather than the
  environment is on the backlog for exactly this.

Move any stored credentials aside once the token works, so it cannot lose to them:
`mv ~/.claude/.credentials.json ~/.claude/.credentials.json.bak`.

## 6. The repository, and what is kept private

This repository holds the agent, the harness, the demo and the frontend. Benchmark data
is kept private, so the benchmark's cases cannot be run from here.

The harness still needs a launch-gate token file, `~/.config/prs-harness/tokens.json`
(or the path in `PRS_HARNESS_TOKENS`). It holds every value the boundary checks against,
so none of them is written into the code: `hard` and `provider_hard`, the strings no file
staged for the agent or for the provider may contain; `deny_eval_run` and `prior_run`, run
directories every sandbox is kept from; and, if you want them, `soft` tokens to report and
`scrub_rules` for staged copies (`harness/harness/config.py` describes each). `config.py`
loads it at import and fails closed: without it every harness command, `--help` included,
raises and names the path. That is deliberate. An empty token list would let the launch
gate report `passed` while checking nothing. With no benchmark data, a stand-in with
strings of your own serves (README.md, "Run the tests").

```bash
install -d -m 700 ~/.config/prs-harness
install -m 600 <your tokens.json> ~/.config/prs-harness/tokens.json
```

`preflight.py` checks the token file.

## 7. The Linux port

The committed harness is already the Linux build: `config.py` says so in its
docstring, and `profile.py` and `probe.py` were confirmed byte-identical to the VM's
working copies on 2026-09-15.

### Without root, on an account other projects share

Needs unprivileged user namespaces (`unshare -Ur true` works) and `/usr/bin/bwrap`.

- **Give the harness a HOME of its own**, e.g. `HOME=~/prs-agent/home`, for every command
  that runs it. The account's `~/.claude.json` and `~/.claude/.credentials.json` belong to
  its other projects: with its own HOME the harness neither seeds a sandbox from them nor
  binds them into one, and the denial probe names the account's real `~/.claude`, `~/.ssh`
  and `~/.config` instead (`config._account_home_probes`).
- In that HOME, mode 600 under `.config/prs-harness/`: `tokens.json` (section 9),
  `claude.env` with the `CLAUDE_CODE_OAUTH_TOKEN=` line (`auth.token_file`), and
  `host.json` naming the toolchain prefixes and the CLI, which nothing can put under
  `/usr`:

  ```json
  {"tool_prefixes": ["/home/<user>/prs-agent/tools/env", "/home/<user>/prs-agent/tools/bio"],
   "claude_cli": "/home/<user>/prs-agent/tools/claude-2.1.293"}
  ```

  Each prefix is bound read-only into the PRS and provider sandboxes and its `bin/` goes
  first on their PATH; the CLI is bound into all three and run by that path. Keep them out
  of any directory that holds the repository: each is bound through its parent, and a
  parent holding the sandboxes is refused.
- The shim needs no install: without `/usr/bin/sandbox-exec` the repository's copy runs.

## 8. Reference data, about 65 GB

> **As of 2026-09-24 `agent/refdata` can be rebuilt from published sources, with the
> exceptions listed under "What is not reproduced".** Two scripts do it:
> `fetch_refdata.py` downloads and `build_refdata.py` derives. Every file they produce is
> checked by sha256 against the working box's copy. **UNVERIFIED on an empty machine.**
> On the working box, `build_refdata.py` ran over an empty scratch directory, in two
> passes because of disk space, from archives downloaded from the recipe's URLs.
> `fetch_refdata.py`'s own download mode was run for 9 records, the JRE among them. The
> 16 GB tarball, the FASTA and phase3 were downloaded by other means and hash-checked.
> Nobody has yet run the whole procedure on a bare box. The exceptions exist only on the
> working box, so read the last subsection before wiping it.

Three files are involved, and they are easy to confuse:

| file | tracked | what it is |
|---|---|---|
| `refdata_recipe.json` (repo root) | yes | The recipe. 81 records: URL, size and sha256 of every download. For archives, it also has the sha256 of each file they unpack to. Both scripts read it. |
| `agent/refdata/MANIFEST.json` | yes | The 32-record manifest the Mac staging wrote, tracked **byte for byte as the published runs had it** (sha256 `268928a6…9cbe`). Every run clone contains it, and agents read it. Do not edit it; `build_refdata.py --check` fails if it changes. |
| `agent/refdata-downloads/` | no, ignored | Where the archives land. It is outside `agent/refdata`, so no run clone sees it. |

### The procedure

```bash
# 1. The 16 GB HGDP+1kGP tarball first. One connection from the working box got
#    0.6-0.7 MB/s, i.e. 6-8 hours with no resume, so fetch it any way you like
#    (16 ranged connections took 37 min) and let it FINISH before step 2: a partial
#    file counts as wrong-size and fetch_refdata.py would download it again over it.
mkdir -p agent/refdata-downloads
#    -> agent/refdata-downloads/pgsc_HGDP+1kGP_v1.tar.zst from
#       http://ftp.ebi.ac.uk/pub/databases/spot/pgs/resources/pgsc_HGDP+1kGP_v1.tar.zst
#       (md5 2bd3586e9a09700f9bcbe62ba4dcee49, published beside it in md5s.txt)
#    Parallel downloaders often preallocate the full size, and fetch_refdata.py checks
#    size only, so an interrupted one looks complete. Check before going on:
md5sum agent/refdata-downloads/pgsc_HGDP+1kGP_v1.tar.zst   # must print 2bd3586e...

# 2. Everything else: about 20 GB more. --profile full adds the 6 GB imputation panel,
#    which no case in the selection benchmark opens.
python3 fetch_refdata.py --plan
python3 fetch_refdata.py
python3 fetch_refdata.py --verify        # reads every byte; expect 0 problems

# 3. Build what is derived: bin/plink2, hgdp_1kgp/, jre/ and maps/ out of their
#    archives, fasta/ and its indexes, hgdp_1kgp.panel, the phase3 .done markers, the
#    PCA basis hgdp_basis/, panel/sites_index (which needs a builder kept private
#    with the benchmark data), and panel-snv/, the phase3 panel filtered to
#    biallelic SNVs over all 2,504 samples that the score stage imputes against (16 GB,
#    about 6 h of CPU, built one chromosome at a time; reproduced bit for bit only by
#    bcftools/htslib 1.19, i.e. Ubuntu 24.04, and that was checked on chr22 only). About
#    42 GB free beyond the downloads, and several hours. It never overwrites anything.
python3 build_refdata.py --plan
python3 build_refdata.py

# 4. Check everything build_refdata.py is answerable for.
python3 build_refdata.py --check
```

The whole rebuild peaks at about 78 GB (36 GB of downloads, 42 GB derived). After step 4
you can delete `agent/refdata-downloads/` (17 GB). `fetch_refdata.py` counts an archive
whose files are all in place as present, so it will not fetch the archives again.

Run steps 2-4 as the user who owns the checkout. `sudo bash setup.sh` hands
`agent/refdata/bin` to that user (`$SUDO_USER`) for this reason.

What a correct rebuild reports on Linux, as observed on the working box's scratch rebuild:

- **Step 2 `--verify`:** every record `ok`, 0 problems.
- **Step 4:** exit status **2**, not 0, with every row `ok` except:
  - `LINUX` for `hgdp_basis/ref_pca.eigenvec` and `ref_pca.eigenvec.allele`;
  - `TEXT` for the six `hgdp_basis` logs, `reference_build.json` and
    `panel/sites_index.json`;
  - `NOT-REPRODUCED` for `bin/liftOver`.

  Status 2 means "identical except what cannot be identical here". Status 1 means
  something is missing or wrong. Without its builder, kept private with the
  benchmark data, `panel/sites_index.bin` is missing: step 4 exits 1, and the select stage cannot run.

Do not run `agent/tools/stage_refdata.sh`. It wrote the original manifest on the Mac, and
it ends every run by rewriting `agent/refdata/MANIFEST.json` from its own download
records. That would replace the file run clones must see. `git checkout
agent/refdata/MANIFEST.json` undoes it, and `build_refdata.py --check` notices it.

### Where each piece comes from, and how it was checked

Unless noted, each source was downloaded or streamed in full on 2026-09-23 and its sha256
compared with the working box's file.

| under `agent/refdata/` | source | identity |
|---|---|---|
| `ancestry-ref/hgdp_1kgp/` (4 files, 15.0 GB) | The PGS Catalog `pgsc_calc` reference bundle `pgsc_HGDP+1kGP_v1.tar.zst` (ftp.ebi.ac.uk, published 2023-10-05). We use its GRCh37 half: gnomAD v3.1.2 HGDP+1kGP lifted down to GRCh37 by the PGS Catalog team. | The tarball's md5 equals the distributor's `md5s.txt`, which is the only anchor from outside, and it is an md5. Each member's sha256 equals the working box's file. gnomAD itself ships only GRCh38, so no gnomAD file can match. Downloaded over http; https content was not hashed. |
| `ancestry-ref/phase3/` (44 files, 15.5 GB) | 1000 Genomes phase 3 **v5a** (2015), from the 1000 Genomes S3 bucket; NCBI's mirror serves the same bytes. It is **not** the Beagle panel, which is a filtered derivative, and **not** EBI's current `release/20130502/`, which is v5b (v5a was withdrawn there on 2021-03-17). | sha256 of all 44, from both hosts |
| `ancestry-ref/phase3/*.done` | written by `build_refdata.py` once the chromosome's `.vcf.gz` and `.tbi` match | empty files |
| `ancestry-ref/hgdp_1kgp.panel` | derived from the `.psam` by `build_refdata.py` | sha256 |
| `ancestry-ref/hgdp_basis/` (9.2 GB) | `agent/tools/ancestry.py build-ref` with `--maf 0.05 --geno 0.02 --prune-window 200 --prune-step 50 --prune-r2 0.2 --pcs 20 --exclude-palindromic yes`, run by `build_refdata.py`. The "five plink2 commands" this section used to list are that script's output, and it writes `nonpalindromic.ids` itself. | sha256 for 15 of its 24 files, `prune.prune.in` (`64a05450…039e`) included. The rest are below. |
| `bin/plink2` | `PLINK v2.0.0-a.6.6LM 64-bit Intel (16 Jan 2025)`, the member of `plink2-assets/alpha6/plink2_linux_x86_64_20250116.zip`. This is the binary `ancestry.py` calls and every published run used. The old manifest records the macOS build here, and fetching that on Linux replaced a working binary with a zip. | sha256 |
| `jre/` (259 files) | The macOS aarch64 Temurin 21.0.11+10 JRE the Mac staged, from its versioned Adoptium URL. The old manifest's "latest" URL now serves 21.0.12.1. Every run clone had it, and no selection run starts Java. It does not run on Linux. | sha256 per file |
| `fasta/GRCh37.fa.gz` + `.fai` + `.gzi` | Ensembl GRCh37 primary assembly, re-compressed with `bgzip` and indexed by htslib | sha256, plus a separate sha256 of the uncompressed sequence. htslib 1.19 (Ubuntu 24.04) reproduces the working box's bytes; which bgzip the Mac used is not recorded. |
| `maps/` | the Beagle `plink.GRCh37.map.zip`, unpacked | sha256 per member |
| `ancestry-ref/1kg.omni…vcf.gz` | the 1000 Genomes S3 mirror. The old manifest's EBI URL serves the same size but was not hashed (0.31 MB/s). | sha256 |
| `chains/`, `bin/bref3.jar`, `bin/beagle.jar`, `bin/unbref3.jar`, `panel/` bref3 + sites VCF, `ancestry-ref/integrated_call_samples…panel` | recipe URLs (UCSC, Browning lab, Beagle, 1000 Genomes) | sha256 |
| `MANIFEST.json` | this repository | sha256 |

The basis is not slow. The Mac built it in 1 min 47 s. On this box, `ancestry.py` with
the Linux binary takes about 10 min.

### What is not reproduced, and why

1. **`hgdp_basis/ref_pca.eigenvec` and `ref_pca.eigenvec.allele`.** The working box's
   copies were built on macOS with `plink2 a.7.1 M1`. On Linux, 1 of 78,840 `eigenvec`
   values and about 36 of 9.2 million `eigenvec.allele` values come out one unit
   different in the 6th significant digit. There are no sign or order changes, and the
   effect on a projection is below 1e-10 of a PC's SD. No Linux plink2 build, version,
   thread count or MKL setting tested reproduces the Mac's bytes, and why the Mac
   differs is not established. On Linux, `eigenvec.allele` also varies with `--threads`
   and with the binary's SIMD level, so a machine with another CPU count gives a sha256
   nobody has seen. `build_refdata.py` reports these files as `LINUX` in two cases: the
   sha256 is one a Linux rebuild has produced before, or the file matches a fingerprint
   of the Mac one (identical row IDs and order, 60 per-PC checksums within 1e-5). Anything
   else is `BAD`.

   **This does not touch the published results. No published run read `hgdp_basis/`.**
   Each run built its own basis in `runs/<run>/ancestry/ref/` with `ancestry.py`, the
   inputs above and the Linux plink2, and the harness recorded a sha256 of every per-run
   file of 64 MB or less. For the 129 published runs that used these parameters, every
   recorded hash equals the Linux rebuild:
   - `prune.prune.in`, `nonpalindromic.ids`, `ref_pca.afreq`, `ref_pca.eigenval`,
     `ref_pruned.pvar`, `ref_pruned.psam`, `sample_labels.json`;
   - `ref_pca.eigenvec`, which is the Linux `c013b043…`, not the Mac file.

   `eigenvec.allele` (97 MB) was not hashed. Its recorded size equals the Linux 8-thread
   build's in 128 of the 129 runs. The other 106 published runs chose other parameters,
   and 56 have no per-run basis files. Rerunning a case rebuilds its basis. On a machine
   with a different CPU count, its `eigenvec.allele` differs from this box's in a few
   printed values, because `ancestry.py` does not pin `--threads`.
2. **`hgdp_basis/*.log`, `hgdp_basis/reference_build.json` and `panel/sites_index.json`.**
   They carry timestamps, a host, a working directory and tool versions. The `hgdp_basis`
   copies were also path-edited by the `build/ → agent/` rename on 2026-09-23. The six
   logs are kept with the benchmark data. `reference_build.json` and `sites_index.json` exist only on the working box.
3. **`bin/liftOver`.** `setup.sh` downloads UCSC's current `linux.x86_64/liftOver`. UCSC
   replaced that on 2026-09-09, and the working box's copy (sha256 `6529f911…`) matches
   no published build found. The agent runs it itself at the door stage, for hg18 → hg19
   lifts with `chains/hg18ToHg19.over.chain.gz`. Keep the working box's copy.
4. **`panel/sites_index.bin`: reproducible, but only with a builder kept private with the
   benchmark data.** The select stage reads it (`weight_coverage.py`), so without it a run
   stops at select. With the builder, `build_refdata.py` rebuilds the `.bin` byte for byte.
5. **`ancestry-ref/build/` and `build_p3chr1/` (about 265 MB).** An older `ancestry.py`
   derived these from phase3 chr1–3, restricted to one patient's array positions. That
   input is gone, so they cannot be rebuilt at all. `REFDATA_DELETE` removes both from
   every run clone, so no run sees them.
6. **The staging records at the top of `agent/refdata`:** `staging.log`,
   `ancestry_build.log`, `phase3_fetch.log`, `phase3_rest.log` and `panel_index.log`.
   These are logs of the original staging on the Mac, not inputs, and they cannot be
   regenerated. `staging.log` and `ancestry_build.log` were also path-edited on
   2026-09-23. The empty `ancestry-ref/phase3_sub/` and a `.DS_Store` are not
   reproduced either.

A rebuilt Linux box cannot score. Scoring runs Beagle, which needs Java, and the only JRE
recorded is the macOS one above. That is what the published runs had, and none of them
scored.

### Before wiping the working box

Items 1, 3, 5 and 6, plus `reference_build.json` and `sites_index.json` from item 2, exist
only on the working box. That is about 400 MB:
- `bin/liftOver`;
- the two `hgdp_basis` PCA files and its `reference_build.json`;
- `panel/sites_index.json`;
- `ancestry-ref/build/` and `build_p3chr1/`;
- the top-level logs.

Copy them somewhere safe first. Nothing a published run read would be lost by skipping
item 5, but it is the only provenance for the earlier results built from it.

## 9. Fixtures and personas

Benchmark data is kept private, so the personas and fixtures the benchmark runs on are
not in this repository. `agent/sim/SIMULATOR.md` describes what a persona is.

## 10. Prove it before trusting it

```bash
cd harness && ./run_harness.py gate --persona <case>      # stages everything, launches nothing
cd harness && ./run_harness.py run  --persona <case> --model claude-opus-5-5
```

The gate is free and catches a staging or scrub failure. The run costs tokens and is the
only thing that proves the whole path. A good run writes `result.json` and the manifest
carries `present: true, valid: true, errors: []`.

## What this document is not

**It has still not been executed end to end on an empty machine.** It was audited against
the working box on 2026-09-23 — every claim checked against the files, nothing wiped —
and eight corrections came out of that. An audit is not an execution. Nobody has yet
cloned this repository onto a bare box and reached a passing §10.

What the audit settled:

- **§2, the real Python requirements.** Answered. `numpy` is required; `scipy` only
  because preflight demands it; `pandas` and `requests` are needed by nothing.
- **§8, the refdata fetch.** Answered. As of 2026-09-24, `fetch_refdata.py` and
  `build_refdata.py` rebuild it, checked by sha256, except for the items §8 lists.

What it did not settle, and what is still UNVERIFIED:

- **§10.** Neither the gate nor a paid run has been executed against a rebuilt machine.
- **§8 on an empty machine.** On the working box, `build_refdata.py` ran over an empty
  scratch directory from archives downloaded from the recipe's URLs, and `--check` passed
  over the live tree. `fetch_refdata.py`'s download mode was run for 9 records. Every
  recipe URL was checked by hash. Nobody has yet run `fetch_refdata.py` and then
  `build_refdata.py` on a bare box.

Settled since: the basis rebuild on Linux reproduces `prune.prune.in` (`64a05450…039e`)
and every other data file of `hgdp_basis/` except the two PCA files §8 names. It also
reproduces, by the hashes the harness recorded, the per-run bases of the 129 published
runs that used the standard parameters.

**Do not wipe the working box before copying off the ~400 MB §8 lists** under "Before
wiping the working box". Everything else under `agent/refdata` can now be rebuilt.
