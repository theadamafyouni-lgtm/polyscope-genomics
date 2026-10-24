"""No model or pipeline: exercise the actual human router with a fake agent process."""
import os
from pathlib import Path
import shutil
import subprocess
import sys
import threading

repo = Path(__file__).resolve().parents[2]
run_root, run_id = Path(sys.argv[1]), sys.argv[2]
os.environ["PRS_DEMO_ROOT"] = str(run_root / "harness")
sys.path.insert(0, str(repo / "harness"))
from harness import liverun, manifest, orchestrator, persona

o = orchestrator.Orchestrator(run_id=run_id, backend="human", demo=True,
                              runs_root=str(run_root / "live"))
comms = Path(o.prs_root) / "comms"
comms.mkdir(parents=True)
shutil.copyfile(repo / "harness" / "sandbox_tools" / "harness_client.py", comms / "harness_client.py")
o.live = liverun.LiveRun(o.runs_root, run_id)
o.backend = persona.HumanBackend()
Path(o.vault_dir).mkdir(parents=True)
o.transcript = manifest.Transcript(str(Path(o.vault_dir) / "transcript.jsonl"))
o.manifest = manifest.Manifest(run_id, o.vault_dir)
o.manifest.set("demo", True)
o.live.emit("run_started", run_id=run_id)
router = threading.Thread(target=o._route_forever, args=({},), daemon=True)
router.start()
try:
    answer = subprocess.check_output(
        [sys.executable, str(comms / "harness_client.py"), "ask-person", "--stage", "ancestry",
         "--question", "How would you describe your ancestry?", "--why", "This informs the explanation.",
         "--timeout", "20"], cwd=o.prs_root, text=True)
    (run_root / "agent-received.txt").write_text(answer)
    o.live.emit("stage_completed", stage="ancestry", outcome="proceed")
    o.manifest.set("stage_judgments", [{"stage": "report", "outcome": "stop",
        "decision": "The synthetic transport check finished.", "reasons": ["The answer arrived."]}])
    o.manifest.write()
    o.publish()
    o.live.emit("run_completed", outcome="stop")
finally:
    o._stop.set()
    router.join(timeout=3)
