"""Speedup 3: a demo run's reference panel built while it imputes, handed to the provider only
when it was built from the very scoring file the provider is given. A stand-in builder writes a
synthetic panel; no bcftools, no phase3."""
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "harness"))
from harness import config, early_panel, provider  # noqa: E402

RUN = "demo-" + "e" * 32
BUILDER = '''import json, sys, time
args = dict(zip(sys.argv[1::2], sys.argv[2::2]))
time.sleep(float(open(args["--scoring"] + ".delay").read()) if __import__("os").path.exists(args["--scoring"] + ".delay") else 0)
out = args["--out"]
open(out, "w").write("panel for " + open(args["--scoring"], "rb").read().hex()[:16] + "\\n")
open(out + ".tbi", "w").write("index\\n")
json.dump({"tool": "build_reference_panel.py", "argv": sys.argv[1:]}, open(out + ".provenance.json", "w"))
'''


class Record:
    def add(self, path, *args, **kwargs):
        return {"sha256": None, "bytes": os.path.getsize(path)}


class EarlyPanel(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.run = self.root / "prs" / "runs" / RUN
        (self.run / "select" / "models").mkdir(parents=True)
        self.builder = self.root / "builder.py"
        self.builder.write_text(BUILDER)
        self.panels = early_panel.EarlyPanels(str(self.run), str(self.root / "early"), str(self.root / "phase3"),
                                              builder=str(self.builder), poll_seconds=0.05)
        self.addCleanup(self.panels.stop)

    def choose(self, pgs, outcome="proceed"):
        model = self.run / "select" / "models" / ("%s_hmPOS_GRCh37.txt.gz" % pgs)
        model.write_bytes(os.urandom(64))
        judgment = self.run / "select" / ("model_choice_%s.json" % pgs)
        judgment.write_text(json.dumps({"outcome": outcome, "chosen_model": pgs}))
        with open(self.run / "ledger.jsonl", "a") as fh:
            fh.write(json.dumps({"kind": "judgment", "stage": "select", "outcome": outcome,
                                 "artifact": "%s/select/%s" % (RUN, judgment.name)}) + "\n")
        return model

    def wait(self, pgs):
        deadline = time.time() + 10
        while time.time() < deadline and not (self.panels.builds.get(pgs) or {}).get("finished"):
            time.sleep(0.05)
        return self.panels.builds[pgs]

    def test_the_panel_starts_on_the_model_choice_and_is_given_only_for_the_same_scoring_file(self):
        self.panels.start()
        self.choose("PGS000002", outcome="refuse")          # not a choice: nothing built
        model = self.choose("PGS000001")
        build = self.wait("PGS000001")
        self.assertTrue(build["ok"])
        self.assertNotIn("PGS000002", self.panels.builds)
        argv = json.loads(Path(build["vcf"] + ".provenance.json").read_text())["argv"]
        self.assertEqual(argv[argv.index("--chr-col") + 1:argv.index("--chr-col") + 4], ["hm_chr", "--pos-col", "hm_pos"])
        sha = hashlib.sha256(model.read_bytes()).hexdigest()
        self.assertEqual(self.panels.ready("PGS000001", sha), build["vcf"])
        self.assertIsNone(self.panels.ready("PGS000001", "0" * 64))   # another scoring file
        self.assertIsNone(self.panels.ready("PGS000003", sha))        # another model

    def test_a_build_still_going_when_the_provider_asks_is_waited_for_never_started_again(self):
        here = self.root / "early" / "PGS000001"
        here.mkdir(parents=True)
        (here / "PGS000001_hmPOS_GRCh37.txt.gz.delay").write_text("1.5")
        self.panels.start()
        model = self.choose("PGS000001")
        deadline = time.time() + 10
        while "PGS000001" not in self.panels.builds and time.time() < deadline:
            time.sleep(0.05)
        sha = hashlib.sha256(model.read_bytes()).hexdigest()
        self.assertIsNone(self.panels.ready("PGS000001", sha))  # going, and not to be waited for
        self.assertEqual(self.panels.ready("PGS000001", sha, wait_seconds=10), self.panels.builds["PGS000001"]["vcf"])
        self.assertGreater(self.panels.builds["PGS000001"]["provider_waited_seconds"], 0)
        self.assertNotIn("done", json.dumps(self.panels.record()))

    def test_the_builder_runs_with_the_tools_every_sandbox_gets(self):
        """On the demo host bcftools is in one of the host's tool prefixes, which the sandboxes
        get on their PATH (config.SANDBOX_PATH_HOST) and the harness's own process does not: on
        10-15 the first live early build died at once with no bcftools."""
        tools = self.root / "bio" / "bin"
        tools.mkdir(parents=True)
        (tools / "bcftools").write_text("#!/bin/sh\nexit 0\n")
        (tools / "bcftools").chmod(0o755)
        self.builder.write_text(BUILDER.replace("out = args", "assert __import__('shutil').which('bcftools'), 'no bcftools'\nout = args"))
        bare = self.root / "bare"  # the harness's own PATH, without the host's tools
        bare.mkdir()
        with patch.dict(os.environ, {"PATH": str(bare)}), patch.object(config, "SANDBOX_PATH_HOST", str(tools)):
            self.panels.start()
            self.choose("PGS000001")
            self.assertTrue(self.wait("PGS000001")["ok"])

    def test_a_whole_genome_builds_at_the_providers_pace(self):
        """Beside a chip's imputation the build takes 2 chromosomes and 1 thread, to leave
        imputation room. A whole genome may not impute at all (own_calls.py), and then its panel
        is all that is left to wait for: on 10-15 the provider waited 22 minutes for a build at
        2 x 1, where its own at 4 x 2 takes about 8. So a whole genome builds at the provider's 4 x 2."""
        for assay, pace in (("genotyping-array", ["2", "1"]), ("WGS", ["4", "2"])):
            with self.subTest(assay=assay):
                door = self.run / "door"
                door.mkdir(exist_ok=True)
                (door / "classification.json").write_text(json.dumps({"outcome": "proceed", "assay_class": assay}))
                panels = early_panel.EarlyPanels(str(self.run), str(self.root / ("early-" + pace[0])), str(self.root / "phase3"),
                                                 builder=str(self.builder), poll_seconds=0.05)
                self.addCleanup(panels.stop)
                panels.start()
                if "PGS000001" not in [p.name.split("_")[0] for p in (self.run / "select" / "models").glob("*")]:
                    self.choose("PGS000001")
                deadline = time.time() + 10
                while time.time() < deadline and not (panels.builds.get("PGS000001") or {}).get("finished"):
                    time.sleep(0.05)
                argv = panels.builds["PGS000001"]["argv"]
                self.assertEqual([argv[argv.index("--workers") + 1], argv[argv.index("--threads") + 1]], pace)

    def test_the_provider_is_handed_the_early_panel_or_builds_its_own(self):
        self.panels.start()
        model = self.choose("PGS000001")
        self.wait("PGS000001")
        (self.run / "request.json").write_text(json.dumps({"model": {"pgs_id": "PGS000001"}}))
        for given, expect in ((self.panels, True), (None, False)):
            with self.subTest(early=bool(given)):
                handoff = provider.ProviderHandoff(str(self.root / "provider"), Record(), None)
                handoff.early = given
                (self.root / "provider").mkdir(exist_ok=True)
                handoff.stage(str(self.run / "request.json"), str(self.root / "prs"), str(self.run))
                staged = self.root / "provider" / "inputs" / "ref_1kg_PGS000001.vcf.gz"
                self.assertEqual(staged.exists(), expect)
                if expect:
                    self.assertTrue(Path(str(staged) + ".tbi").exists() and Path(str(staged) + ".provenance.json").exists())
                    self.assertIn("while", handoff.staged["ref_1kg_PGS000001.vcf.gz"]["why"])
        # A scoring file changed after the build is not the one it was built from.
        model.write_bytes(os.urandom(64))
        handoff = provider.ProviderHandoff(str(self.root / "provider"), Record(), None)
        handoff.early = self.panels
        handoff.stage(str(self.run / "request.json"), str(self.root / "prs"), str(self.run))
        self.assertFalse((self.root / "provider" / "inputs" / "ref_1kg_PGS000001.vcf.gz").exists())


if __name__ == "__main__":
    unittest.main()
