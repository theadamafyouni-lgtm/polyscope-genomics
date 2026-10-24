#!/usr/bin/env python3
"""Run the PRS pipeline end to end, unattended, inside enforced isolation.

  ./run_harness.py run --persona p01
  ./run_harness.py run --backend human            # waits for the window
  ./run_harness.py run --stop-after select        # partial run: nothing after select
  ./run_harness.py run --bare                     # no pipeline at all: the file and the transport
  ./run_harness.py run --bare --brief-file b.md   # the same run, a different brief
  ./run_harness.py run --transport minimal        # a different comms/harness_client.py
  ./run_harness.py gate --persona p01             # stage and probe, launch nothing
  ./run_harness.py probe                          # boundary check against a throwaway sandbox
  ./run_harness.py teardown <run-id> [--remove]

`gate` is the one to use while changing anything. It builds the sandboxes, runs the launch
gate, writes the manifest and stops before spending a single token -- so a staging mistake
costs seconds instead of a full pipeline run.
"""
import argparse
import json
import os
import shutil
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from harness import auth, config, liverun, orchestrator, probe  # noqa: E402

# The subcommands that spawn a Claude session, and so the ones that cannot work without a
# credential. `probe` and `teardown` spawn none -- teardown in particular has to keep
# working on a box whose token has gone, because cleaning up after a failed batch is
# exactly when it is needed.
NEEDS_TOKEN = ("run", "gate", "selftest")


def _print_gate(m):
    b = m.get("boundary", {})
    print("\n  launch gate")
    for name, g in (b.get("token_grep") or {}).items():
        state = "pass" if g["passed"] else "FAIL"
        enf = "" if g.get("gate_enforced", True) else "  (not enforced: %s)" % g.get("policy_why", "")
        print("    token grep %-9s %s   %d files scanned, %d skipped, "
              "%d hard, %d soft%s"
              % (name, state, g["files_scanned"], g["files_skipped"],
                 len(g["hard_hits"]), len(g["soft_hits"]), enf))
        for h in g["soft_hits"][:6]:
            print("        soft: %s  %s" % (h["token"], h["path"]))
    for name, per in (b.get("per_agent") or {}).items():
        d = per["denial_probe"]
        print("    denial probe %-7s %s   %d/%d reads denied, %d absent, "
              "%d/%d writes denied, spotlight hits %s"
              % (name, "pass" if d["passed"] else "FAIL",
                 d["reads_denied"], d["reads_attempted"], d["reads_absent"],
                 d["writes_denied"], d["writes_attempted"],
                 d["spotlight_hits_from_inside"]))
        print("    memory store %-7s %s   jail clean=%s"
              % (name, "pass" if per["memory_store"]["passed"] else "FAIL",
                 per["memory_store"]["jail_home_is_clean"]))
    s = m.get("staging", {})
    c = s.get("refdata_clone") or {}
    if c:
        print("    refdata clone       %s in %.3fs, %d files%s"
              % (c["method"], c["elapsed_seconds"], c["n_files"],
                 "" if c["cow_confirmed"] else "   <-- NOT a clone, check the volume"))
    for d in s.get("refdata_deleted", []):
        print("      deleted %-32s %s" % (d["path"], d["status"]))
    for sc in s.get("scrubbed", []):
        n = sum(x["occurrences"] for x in sc["substitutions"])
        print("      scrubbed %-31s %d substitution(s)" % (sc["path"], n))


def model_mix_problem(arm, pass_name, model):
    """A results/<arm>/<pass>/ block is one corpus pass, and a pass is one model. Batches resume
    by skipping cases already filed, so resuming a pass begun on another model would file the
    rest of it on this one without a word. Refuse instead. Older manifests have no `model` key
    and their model is unknown here, which is also a refusal."""
    if not (arm and pass_name):
        return None
    block = os.path.join(orchestrator.RESULTS_ROOT, arm, pass_name)
    seen = {}
    for case in sorted(os.listdir(block)) if os.path.isdir(block) else []:
        path = os.path.join(block, case, "manifest.json")
        try:
            with open(path) as fh:
                got = (json.load(fh).get("model") or {}).get("requested")
        except (OSError, ValueError):
            continue
        if got != model:
            seen.setdefault(got, case)
    if not seen:
        return None
    return ("results/%s/%s/ already holds cases run on %s (e.g. %s); this run is on %s. A pass "
            "is one model: start a new --pass for this one."
            % (arm, pass_name,
               ", ".join("an unrecorded model" if m is None else m for m in seen),
               next(iter(seen.values())), model))


def cmd_run(args):
    problem = model_mix_problem(getattr(args, "arm", None), getattr(args, "pass_name", None),
                                args.model)
    if problem:
        sys.exit("run_harness.py: " + problem)
    # The agent is the host's `claude`, run inside the sandbox. If that binary cannot run, every
    # case dies in well under a second with "execvp claude: Permission denied" -- the same
    # signature SETUP.md attributes to a CLI version problem. On 2026-10-08 it was a
    # half-finished npm reinstall that left a 500-byte stub at bin/claude.exe. Say which.
    try:
        import subprocess
        v = subprocess.run([config.CLAUDE_CLI, "--version"], capture_output=True, text=True,
                           timeout=60)
        cli_ok = v.returncode == 0 and "Claude Code" in v.stdout
        cli_msg = (v.stdout + v.stderr).strip()[:300]
    except (OSError, subprocess.TimeoutExpired) as exc:
        cli_ok, cli_msg = False, str(exc)
    if not cli_ok:
        sys.exit("run_harness.py: the claude CLI on this host does not run (%s). Every agent "
                 "would die at launch. Reinstall it (SETUP.md section 4) and check "
                 "`claude --version` before running a case." % cli_msg)
    if args.stop_after not in ("door", "ancestry", "select") and not args.bare:
        os.makedirs(config.SANDBOXES, exist_ok=True)
        free_gb = shutil.disk_usage(config.SANDBOXES).free / 1e9
        if free_gb < config.FULL_RUN_MIN_FREE_GB:
            sys.exit("run_harness.py: %.1f GB free under %s; a run past select needs about %d GB "
                     "(imputed genome, provider panel). Free space or use --stop-after select."
                     % (free_gb, config.SANDBOXES, config.FULL_RUN_MIN_FREE_GB))
    o = orchestrator.Orchestrator(
        run_id=args.run_id, patient_file=args.patient, persona_name=args.persona,
        backend=args.backend, model=args.model, runs_root=args.runs_root,
        live_run_id=args.live_run_id, keep_sandbox=not args.no_keep,
        stop_after=args.stop_after, bare=args.bare, brief_file=args.brief_file,
        transport=args.transport, arm=args.arm, pass_name=args.pass_name,
        impute_backend=args.impute_backend, demo=args.demo, intake=args.intake)
    print("run id      : %s" % o.run_id)
    print("sandboxes   : %s" % o.sandbox_root)
    print("vault       : %s" % o.vault_dir)
    print("live-run dir: %s" % os.path.join(o.runs_root, o.live_run_id))
    if o.transport != orchestrator.TRANSPORT_DEFAULT:
        # Only when it is not the default, so a run that did not ask for a transport prints
        # the summary it has always printed. The manifest states it either way -- see
        # Orchestrator.transport_record for why that one is unconditional.
        print("transport   : %s   %s" % (o.transport, o.transport_source))
    if o.stop_after:
        print("stop after  : %s   (not run: %s)"
              % (o.stop_after,
                 ", ".join(orchestrator.stages_after(o.stop_after)) or "nothing"))
    if o.bare:
        print("bare        : YES -- the PRS sandbox gets the person's file, the transport")
        print("              and the jail HOME. No skills, no tools, no AGENT.md, no")
        print("              JUDGMENT-CORE.md, no interfaces, no refdata.")
        if o.brief_source:
            print("brief file  : %s" % o.brief_source["path"])
            print("              sha256 %s  (%d bytes)"
                  % (o.brief_source["sha256"], o.brief_source["bytes"]))
            print("              NOT BARE_BRIEF -- this is the second control arm.")
    path = o.run()
    with open(path) as fh:
        m = json.load(fh)
    _print_gate(m)
    out = m.get("outcome", {})
    print("\n  outcome")
    print("    prs exit                %s" % out.get("prs_returncode"))
    print("    messages routed         %s" % (m.get("transcript") or {}).get("n_messages"))
    print("    INT-1 handoffs          %s" % (m.get("int1") or {}).get("n_handoffs"))
    print("    INT-1 arm exercised     %s" % out.get("int1_arm_exercised"))
    print("    person ended session    %s" % out.get("person_ended_session"))
    if m.get("bare"):
        print("    BARE RUN                no pipeline was staged; see caveats")
        bs = (m.get("bare_run") or {}).get("brief_source")
        if bs:
            # Only when there was one, so a BARE_BRIEF run's summary is the summary it has
            # always printed. Its absence here is the same signal as its absence in the
            # manifest: the brief was BARE_BRIEF.
            print("    brief from              %s" % bs["path"])
            print("    brief sha256            %s" % bs["sha256"])
    pr = m.get("partial_run")
    if pr:
        print("    stopped after           %s   honoured=%s%s"
              % (pr.get("stop_after"), pr.get("honoured"),
                 "" if pr.get("honoured") else "   <-- see caveats"))
    net = m.get("network", {})
    print("    network allowed/denied  %s / %s" % (net.get("n_allowed"), net.get("n_denied")))
    print("\n  manifest    : %s" % path)
    print("  transcript  : %s" % (m.get("transcript") or {}).get("path"))
    pub = m.get("published") or {}
    if pub.get("published"):
        print("  results     : %s" % pub.get("dest"))
    elif pub:
        # Including the skip. A case that was already there is the normal way this reads on
        # a resumed batch, and it has to be visible without opening the manifest.
        print("  results     : NOT WRITTEN (%s) -- %s" % (pub.get("state"), pub.get("why")))
    return 0


def cmd_gate(args):
    o = orchestrator.Orchestrator(
        run_id=args.run_id, patient_file=args.patient, persona_name=args.persona,
        backend=args.backend, model=args.model)
    print("run id    : %s" % o.run_id)
    o.stage()
    from harness.proxy import AllowListProxy
    px = AllowListProxy(config.PROXY_ALLOW_HOSTS,
                        os.path.join(o.vault_dir, "network.jsonl"),
                        port_range=config.PROXY_PORT_RANGE)
    port = px.start()
    try:
        o.gate()
        o.gate_profiles(port)
    finally:
        px.stop()
    path = o.manifest.write()
    with open(path) as fh:
        m = json.load(fh)
    _print_gate(m)
    print("\n  manifest  : %s" % path)
    print("  sandbox   : %s" % o.sandbox_root)
    if args.remove:
        print("  teardown  : %s" % o.teardown(remove=True))
    return 0


def cmd_probe(args):
    """Boundary check on its own, against a throwaway sandbox."""
    import tempfile
    from harness import agents, profile as prof
    # A fresh clone has no sandboxes directory yet: it is gitignored, and made by the first run.
    os.makedirs(config.SANDBOXES, exist_ok=True)
    root = tempfile.mkdtemp(prefix="harness-probe-", dir=config.SANDBOXES)
    jail = os.path.join(root, ".home")
    os.makedirs(jail)
    os.makedirs(os.path.join(root, "tmp"))
    os.makedirs(os.path.join(root, "refdata"))
    try:
        text = prof.build_profile(root, jail, 0, label="probe",
                                  extra_read=config.host_reads("prs"))
        p = os.path.join(root, "probe.sb")
        prof.write_profile(p, text)
        env = agents.build_env(jail, os.path.join(root, "tmp"), 0)
        raw = probe.run_denial_probe(p, root, jail, env)
        v = probe.evaluate_probe(raw)
        print("reads:  %d attempted, %d denied, %d absent, %d READABLE"
              % (v["reads_attempted"], v["reads_denied"], v["reads_absent"],
                 v["reads_READABLE"]))
        print("writes: %d attempted, %d denied, %d WRITABLE"
              % (v["writes_attempted"], v["writes_denied"], v["writes_WRITABLE"]))
        print("spotlight hits from inside: %s" % v["spotlight_hits_from_inside"])
        for r in raw["reads"]:
            mark = {"denied": "  denied", "absent": "  absent", "READABLE": "  READABLE <--"}
            print("%s  %s" % (mark.get(r["result"], r["result"]), r["path"]))
        for r in raw["writes"]:
            print("  %-9s %s" % (r["result"], r["path"]))
        print("\nVERDICT: %s" % ("pass" if v["passed"] else "FAIL -- " + v.get("failure", "")))
        return 0 if v["passed"] else 1
    finally:
        shutil.rmtree(root, ignore_errors=True)


def cmd_selftest(args):
    """Round-trip the transport without running the pipeline.

    Stages for real, gates for real, starts the router and the persona backend for real, then
    calls harness_client.py from inside the sandbox exactly as the agent would. What it skips
    is the six-to-ten minutes of pipeline compute -- everything the message has to cross is
    exercised.
    """
    import subprocess
    import threading
    from harness import agents, persona as personamod
    from harness.proxy import AllowListProxy

    o = orchestrator.Orchestrator(run_id=args.run_id, persona_name=args.persona,
                                  backend="persona", model=args.model)
    print("run id  : %s" % o.run_id)
    o.stage()
    px = AllowListProxy(config.PROXY_ALLOW_HOSTS + ["api.anthropic.com",
                                                    "statsig.anthropic.com"],
                        os.path.join(o.vault_dir, "network.jsonl"),
                        port_range=config.PROXY_PORT_RANGE)
    port = px.start()
    try:
        o.gate()
        profiles = o.gate_profiles(port)
        print("  gate    : pass")

        from harness import liverun
        o.live = liverun.LiveRun(o.runs_root, o.live_run_id)
        os.makedirs(o.live.dir, exist_ok=True)
        o.live.emit("run_started", run_id=o.live_run_id, stages=liverun.STAGES)

        pc = profiles["persona"]
        pagent = agents.SandboxedClaude("persona", o.persona_root, pc["profile_path"],
                                        pc["jail_home"], pc["env"], model=args.model,
                                        allowed_tools=orchestrator.PERSONA_TOOLS)
        o.backend = personamod.PersonaBackend(
            os.path.join(o.persona_root, "persona.json"), pagent, o.transcript)
        print("  persona : %s" % o.backend.persona_id)

        t = threading.Thread(target=o._route_forever, args=(profiles,), daemon=True)
        t.start()

        pr = profiles["prs"]
        questions = [
            ("read-and-reason", "Before we start -- what brings you here today?",
             "It tells me which trait to look at, and I have nothing else to go on.", False),
            ("ancestry", "How would you describe your background or ancestry?",
             "It is optional and it will not change the calculation. It only affects what I "
             "tell you, and whether I flag a mismatch.", False),
            ("select", "Have you ever been diagnosed with this condition?",
             "Some models were built on people already diagnosed and others on a general "
             "population. Which one fits you changes which model I should use.", True),
            ("select", "Are you taking any medication for your heart at the moment?",
             "Checking whether this is covered by what you have told me.", False),
        ]
        ok = 0
        for stage, q, why, hard in questions:
            argv = [config.SANDBOX_EXEC, "-f", pr["profile_path"], "/usr/bin/python3",
                    "comms/harness_client.py", "ask-person", "--stage", stage,
                    "--question", q, "--why", why, "--timeout", "300"]
            if hard:
                argv.append("--hard-blocker")
            r = subprocess.run(argv, cwd=o.prs_root, env=pr["env"],
                               capture_output=True, text=True, timeout=420)
            ans = (r.stdout or "").strip()
            print("\n  Q(%s, hard=%s): %s" % (stage, hard, q))
            print("  A(rc=%d): %s" % (r.returncode, ans[:300] or "(nothing)"))
            if r.returncode == 0 and ans:
                ok += 1
            elif r.returncode == 2:
                print("  -> the person ended the session")
                break
        px.stop()
        o._stop.set(); t.join(timeout=5)

        print("\n  %d/%d questions answered" % (ok, len(questions)))
        print("  transcript: %s" % o.transcript.path)
        for rec in o.transcript.read():
            print("    %-18s %-12s %s" % (rec["direction"], rec["kind"],
                                          (rec["text"] or "")[:88]))
        o.manifest.set("selftest", {"questions": len(questions), "answered": ok})
        print("\n  manifest  : %s" % o.manifest.write())
        return 0 if ok else 1
    finally:
        try:
            px.stop()
        except Exception:
            pass


def cmd_teardown(args):
    o = orchestrator.Orchestrator(run_id=args.run_id)
    print(json.dumps(o.teardown(remove=args.remove), indent=2))
    return 0


def main():
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    def common(p):
        p.add_argument("--run-id")
        p.add_argument("--patient", help="the person's genotype file")
        p.add_argument("--persona", default="p01", help="persona id, for unattended runs")
        p.add_argument("--backend", choices=["persona", "human"], default="persona")
        p.add_argument("--model", default=config.DEFAULT_MODEL,
                       help="model for all three spawned agents (default: config.DEFAULT_MODEL, "
                            "%s)" % config.DEFAULT_MODEL)

    r = sub.add_parser("run", help="stage, gate, and run the pipeline end to end")
    common(r)
    r.add_argument("--runs-root", help="live-run root; defaults to $PRS_RUNS_ROOT")
    r.add_argument("--live-run-id")
    r.add_argument("--no-keep", action="store_true", help="do not freeze the sandbox after")
    r.add_argument("--demo", action="store_true",
                   help="mark private demo evidence and never publish it under results/")
    r.add_argument("--intake", metavar="PATH",
                   help="with --demo: the person's answers from the upload form (intake.json), "
                        "handed to the agent with the demo brief")
    # Both say "this is not a full pipeline run", and they say incompatible things:
    # --stop-after names a stage OF the pipeline, and --bare removes the pipeline. argparse
    # refuses the combination here; Orchestrator refuses it again for programmatic callers.
    notfull = r.add_mutually_exclusive_group()
    notfull.add_argument("--stop-after", choices=liverun.STAGES, metavar="STAGE",
                         help="stop the agent cleanly after this stage and run nothing after "
                              "it. For benchmarks that grade one stage. Asked for in the "
                              "brief, not enforced; the manifest records whether the agent "
                              "honoured it. One of: " + ", ".join(liverun.STAGES))
    notfull.add_argument("--bare", action="store_true",
                         help="stage the PRS sandbox with the person's file, the comms "
                              "transport and the jail HOME, and nothing else -- no skills, "
                              "no tools, no AGENT.md, no JUDGMENT-CORE.md, no interfaces, no "
                              "refdata -- and give the agent a fixed one-line brief. The "
                              "control arm: the pipeline against its absence. Everything "
                              "else (persona backend, launch gate, profile, proxy, "
                              "transport) is identical to a normal run. Recorded as "
                              "`bare: true` in the manifest.")
    # Not part of that group: it does not conflict with --bare, it requires it. Enforced
    # below, after parsing, because argparse cannot express "requires".
    r.add_argument("--brief-file", metavar="PATH",
                   help="only with --bare. Hand the PRS agent this file's text as its entire "
                        "brief instead of the fixed BARE_BRIEF, and change nothing else: same "
                        "staging, same launch gate, same profile, same proxy, same persona "
                        "backend. That is what makes it a second control arm -- the brief is "
                        "the only variable between the two. The file is used as written "
                        "except for `{patient}`, which is replaced with the person's file "
                        "name the same way BARE_BRIEF's is, so one brief file works across a "
                        "batch that passes a different --patient every case. A missing or "
                        "empty file stops the run before it stages anything rather than "
                        "falling back to BARE_BRIEF. Recorded in the manifest as "
                        "`bare_run.brief` in full, with the path, sha256 and the "
                        "pre-substitution text under `bare_run.brief_source`.")
    # Not in that group either, and it does not require --bare: it applies to every kind of
    # run, because every kind of run stages a transport through the same call.
    r.add_argument("--transport", metavar="NAME", default=orchestrator.TRANSPORT_DEFAULT,
                   help="which transport to stage as comms/harness_client.py. `%(default)s` "
                        "is sandbox_tools/harness_client.py -- what every run got before this "
                        "flag existed, and what a run gets when the flag is absent. Any other "
                        "NAME is sandbox_tools/harness_client_<NAME>.py; `minimal` is "
                        "harness_client_minimal.py. The destination path, the mode, the "
                        "staging record and everything else about the run are the same either "
                        "way, so nothing downstream can tell which one it got except by "
                        "reading the file -- which is the point. The full transport's "
                        "docstring carries specification content: a worked --why example "
                        "naming the diagnosed-vs-general-cohort criterion, the stage name "
                        "`select`, the INT-1 provider and its request-reference subcommand, "
                        "the hard-blocker concept. That is the environment describing itself "
                        "to a pipeline run, and it is the pipeline arriving through the back "
                        "door in a control arm that is supposed to be specification-free. "
                        "Works with or without --bare. Recorded at the top level of the "
                        "manifest as `transport`, with the source path and the sha256 of the "
                        "file as staged, so a run can be told apart from another without the "
                        "file being to hand. An unknown NAME is refused before anything is "
                        "staged.")
    # Neither changes what the run does. They change only where its four small artifacts are
    # filed, which is the whole of what results/ is: a run findable by case rather than by a
    # run id nobody has to hand.
    r.add_argument("--arm", metavar="NAME",
                   help="which arm of a corpus this run belongs to -- `pipeline`, `arm0`. "
                        "The run's result, manifest and two transcripts are copied to "
                        "results/<arm>/<pass>/<case>/, where <case> is the persona's own id. "
                        "Goes with --pass; neither is any use without the other. A run that "
                        "gives neither is not part of a corpus and is filed under "
                        "results/adhoc/<case>/. Nothing else about the run changes, and an "
                        "existing case directory is reported and skipped rather than "
                        "overwritten.")
    r.add_argument("--pass", dest="pass_name", metavar="NAME",
                   help="which pass of that arm -- `pass1`, `pass2`, `pass3`. Goes with "
                        "--arm; see it for what the pair changes.")
    r.set_defaults(fn=cmd_run)
    r.add_argument("--impute-backend", choices=("local", "gb10"), default="local",
                   help="Beagle execution backend; gb10 uses a host-side broker at 6 threads. "
                        "The gb10's host and ssh key are read from the private config, "
                        "~/.config/prs-harness/gb10.json (or $PRS_HARNESS_GB10)")

    g = sub.add_parser("gate", help="stage and probe; launch nothing")
    common(g)
    g.add_argument("--remove", action="store_true")
    g.set_defaults(fn=cmd_gate)

    p = sub.add_parser("probe", help="boundary check against a throwaway sandbox")
    p.set_defaults(fn=cmd_probe)

    st = sub.add_parser("selftest", help="round-trip the transport without the pipeline")
    common(st)
    st.set_defaults(fn=cmd_selftest)

    t = sub.add_parser("teardown", help="freeze or remove a run's sandbox")
    t.add_argument("run_id")
    t.add_argument("--remove", action="store_true")
    t.set_defaults(fn=cmd_teardown)

    args = ap.parse_args()
    if getattr(args, "demo", False) and (args.arm or args.pass_name):
        r.error("--demo cannot be combined with --arm or --pass")
    if getattr(args, "intake", None) and not args.demo:
        r.error("--intake is a demo run's: it requires --demo")
    # --brief-file replaces the bare brief, so --bare is what it needs and --stop-after is
    # what it cannot have, for the same reason --bare cannot: a stage of a pipeline that was
    # never staged. `--bare --stop-after` is already refused by the group above, so the
    # stop-after arm here is the one that arrives without --bare. Orchestrator refuses both
    # pairs again for programmatic callers.
    if getattr(args, "brief_file", None) is not None:
        if getattr(args, "stop_after", None):
            r.error("--brief-file cannot be combined with --stop-after, for the same reason "
                    "--bare cannot: --stop-after names a stage OF the pipeline, and the "
                    "brief --brief-file replaces belongs to a run that has no pipeline.")
        if not args.bare:
            r.error("--brief-file requires --bare. It replaces BARE_BRIEF, which is the whole "
                    "of what a bare run is told; a pipeline run's brief is PRS_BRIEF plus the "
                    "stages it names, and there is nothing there for one file to stand in "
                    "for.")
    # results/ is <arm>/<pass>/<case>/, so one of the pair without the other has nowhere to
    # go. Refused here rather than filed somewhere plausible, because a corpus block filed
    # under the wrong shape is a block nobody finds. Orchestrator refuses it again for
    # programmatic callers.
    if bool(getattr(args, "arm", None)) != bool(getattr(args, "pass_name", None)):
        r.error("--arm and --pass go together: results/ is <arm>/<pass>/<case>/, and one "
                "without the other names no directory in it. Give both, or give neither "
                "and the run is filed under results/adhoc/<case>/.")
    # Before the Orchestrator is built and long before anything is staged, so a name with no
    # file behind it is one line on stderr rather than a traceback -- and so it is refused at
    # the same point --brief-file's bad path is. The list comes from sandbox_tools/ itself:
    # adding a transport is dropping a file in there, so the answer to "what can I pass?" has
    # to be read off the directory rather than recited from a list that can fall behind it.
    # Orchestrator.resolve_transport refuses the same name again for programmatic callers.
    if getattr(args, "transport", None) is not None:
        if args.transport not in orchestrator.available_transports():
            r.error("unknown --transport %r. sandbox_tools/ has: %s. `%s` is "
                    "harness_client.py and is the default; any other NAME is "
                    "harness_client_<NAME>.py in that directory."
                    % (args.transport, orchestrator.transport_listing(),
                       orchestrator.TRANSPORT_DEFAULT))
    # Before a sandbox exists, before the gate, before a single token is spent. A run with
    # no credential produces nothing at all -- it dies in seconds with `Not logged in` --
    # so the only useful thing to do with one is refuse it. Exits non-zero with the remedy
    # rather than raising, so an overnight log reads as a box that was never set up rather
    # than as a harness that crashed.
    if args.cmd in NEEDS_TOKEN:
        source, line = auth.require_token()
        print("auth        : %s (%s)" % (source, line))
    sys.exit(args.fn(args))


if __name__ == "__main__":
    main()
