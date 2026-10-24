# Polyscope Genomics

An LLM agent (called prs-agent in the code) that reads one person's genotype file and chooses a polygenic scoring model
**for that person**, or refuses and says why. The agent is Claude Code, run inside a
sandbox by a harness that stages each run, checks the boundary before the agent starts,
and records what happened.

Existing tools take the scoring model as an input: which model suits *this* person, with
*this* file, for *this* trait, and what using it costs, is decided before they start, by
somebody else, once for everybody. Most catalogued models were developed in
European-ancestry cohorts and lose accuracy on genomes from other ancestries, so the
person for whom a genetic answer would matter most is often the person for whom the
available answer is least valid. This agent picks per person, and declines when nothing
is defensible. **A refusal is a correct answer.**

## How it works

A run goes through six steps in order. Each is a skill in `agent/.claude/skills/`, written in
prose, not code. The tools only read and extract; the agent makes every call.

1. **The door** (`read-and-reason`): works out what the file is from its contents (a chip, a
   whole genome or an exome, which vendor, which genome build) and what a missing position
   means. It never reads the build off a filename.
2. **Ancestry** (`ancestry`): places the genome among reference populations. If the person's
   self-reported ancestry disagrees, they are told, but the math uses the genome.
3. **Select** (`select`): finds candidate models for the trait in the PGS Catalog and ranks
   them on ancestry match and validation quality. Coverage can rule a model out; it never
   moves one up.
4. **Score** (`score`): matches the model to the file, imputes missing positions when needed,
   and sums the weights. Coverage is measured by weight, not by counting variants: it refuses
   if under 90% of the model's weight is resolved, or if one unresolved position carries 5%
   or more of it. If the measured coverage is materially worse than select estimated, it goes
   back for the next model, and refuses when none is left. The first acceptable model wins;
   it never scores them all and compares.
5. **Interpret** (`interpret`): places the score against a reference group genetically close
   to the person, built for that request. If no close group exists, it refuses rather than
   make one up.
6. **Report** (`report`): what the person is told: the score and where they stand, the limits
   that apply to them, or why there is no score.

`agent/AGENT.md` has the flow and `agent/JUDGMENT-CORE.md` the rules every step follows.

## Look at the interface

No install. With Node 20 or later, from the repository root:

```sh
npm start
```

Open http://127.0.0.1:8000. It is a preview: example reports, an explained refusal and
the question screens, with no DNA analysis behind them. `FRONTEND.md` walks through the
screens.

## Run the demo

The demo is the whole system behind a sign-in page: a person uploads a genotype file,
answers the agent's questions, and gets a scored report or an explained refusal. It needs
Linux with bubblewrap (Ubuntu 24.04 or 22.04), 8 cores, 16 GB of RAM, about 95 GB of free
disk and a Claude account.

```sh
sudo bash setup.sh            # packages, sandbox shim, Claude CLI, test tools; ends with preflight.py
python3 fetch_refdata.py; python3 fetch_refdata.py --verify   # reference data: ~36 GB of downloads
python3 build_refdata.py; python3 build_refdata.py --check    # the rest of its ~65 GB, derived
python3 preflight.py          # says what is still missing
bash demo_site/deploy/install-user.sh "$HOME/prs-demo" "$HOME"
```

The installer needs three files in `~/.config/prs-harness/`: `claude.env` (the Claude
token: a headless machine has no browser sign-in), `host.json` (`{}` unless your tools live
outside the system directories) and `tokens.json`, the launch gate's list of strings no
staged file may contain (the stand-in under *Run the tests* serves for your own files).
`SETUP.md` sections 5 and 7 describe them.

It starts nothing. Add the crontab line it prints; its watchdog keeps the web and worker
processes up. Then open http://localhost:8765 on that machine, or through an SSH tunnel,
and sign in with the day's password:

```sh
(cd "$HOME/prs-demo/app" && python3 -m demo_site.auth password --secret-file ../state/auth/master.key)
```

To use one fixed password instead of a daily one, write it to
`$HOME/prs-demo/state/auth/fixed-password`; `DEMO-README.md` explains it.

`SETUP.md` covers the machine in depth, including the order of the Claude sign-in and the
CLI pin and the reference files that cannot come out bit-identical. `DEMO-README.md`
covers the demo: the root installer, HTTPS fronts, passwords and cleanup.

## Run the tests

`setup.sh` installs what they need: pytest and numpy from Ubuntu, and the frontend's
packages (`npm ci --prefix frontend --ignore-scripts`). Then:

```sh
T=$(mktemp) && echo '{"hard": ["STAND_IN"], "provider_hard": ["STAND_IN_PROVIDER"], "deny_eval_run": "stand-in-run", "prior_run": "stand-in-prior-run"}' > "$T"
PRS_HARNESS_TOKENS=$T python3 -m pytest -q tests demo_site/test_worker_progress.py
node --test frontend/*.test.js
```

The harness does not import without a launch-gate token file, so the tests use a stand-in.
Four tests skip unless the staged plink2 and Beagle are present. CI
(`.github/workflows/tests.yml`) runs the same steps on Ubuntu 24.04.

## Limits

- Research software, not a medical device and not medical advice. A score places a person
  relative to a reference group; it is not a diagnosis.
- The choice is only as good as the catalogue. For many traits and ancestries no model is
  well validated, and the agent refuses.
- It is slow. A demo run on a genotyping-chip file takes about 45 to 60 minutes; a full
  case with imputation took 5 to 7 hours on an 8-core machine.
- Linux only. The sandbox is bubblewrap, and on Ubuntu an account without root needs the
  kernel to allow it user namespaces (`DEMO-README.md` shows the AppArmor profile).
- A few reference files cannot be rebuilt bit-identical; `SETUP.md` section 8 lists them.
- Benchmark data is kept private, so the benchmark cannot be rerun from this repository.
  Without it a machine also lacks one reference file the select stage reads,
  `panel/sites_index.bin` (`SETUP.md` section 8): `preflight.py` reports it and the
  personas as missing, and a run stops at select.

## Layout

```
agent/               what the agent is told (AGENT.md, JUDGMENT-CORE.md), its skills and tools
harness/             stages a run, writes the sandbox profile, probes it, runs the agent
reference-provider/  builds the reference distribution a score is placed in
demo_site/           the demo: web app, worker, watchdog and installers
frontend/            the interface, served by the demo and by `npm start`
tests/               the Python tests (the frontend's are frontend/*.test.js)
setup.sh, preflight.py
                     machine setup, and the check that the machine is ready
fetch_refdata.py, build_refdata.py, refdata_recipe.json
                     the reference data: what is fetched, with a sha256 for every file
```
