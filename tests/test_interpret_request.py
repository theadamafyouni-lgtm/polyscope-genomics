"""interpret_request.py writes the trait as a bare ontology id, as the INT-1 contract's example does.

The model's catalog record carries trait_efo as a list of {id, label, description, url}; the
request used to copy that list into trait.id. Synthetic inputs only; the end-to-end case runs a
copy of the tool in a temporary directory, so its ledger lands there and not in agent/runs.
"""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "agent" / "tools"))
import interpret_request  # noqa: E402

EFO = {"id": "MONDO_0000001", "label": "example trait", "description": "an example",
       "url": "http://purl.obolibrary.org/obo/MONDO_0000001"}


class BareTraitId(unittest.TestCase):
    def test_single_catalog_trait_gives_its_id(self):
        self.assertEqual(interpret_request.bare_trait_id(None, [EFO]), "MONDO_0000001")

    def test_given_id_wins(self):
        self.assertEqual(interpret_request.bare_trait_id("EFO_0000001", [EFO]), "EFO_0000001")

    def test_given_id_must_be_bare(self):
        with self.assertRaises(SystemExit):
            interpret_request.bare_trait_id("MONDO_0000001 (example trait)", [EFO])

    def test_several_catalog_traits_need_trait_id(self):
        other = dict(EFO, id="EFO_0000002")
        with self.assertRaises(SystemExit) as cm:
            interpret_request.bare_trait_id(None, [EFO, other])
        self.assertIn("--trait-id", str(cm.exception))
        self.assertEqual(interpret_request.bare_trait_id("EFO_0000002", [EFO, other]),
                         "EFO_0000002")

    def test_no_catalog_trait_gives_none(self):
        self.assertIsNone(interpret_request.bare_trait_id(None, None))
        self.assertIsNone(interpret_request.bare_trait_id(None, []))

    def test_a_bare_string_is_kept(self):
        self.assertEqual(interpret_request.bare_trait_id(None, "MONDO_0000001"), "MONDO_0000001")


class EndToEnd(unittest.TestCase):
    def request(self, *extra):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "tools").mkdir()
            for name in ("interpret_request.py", "lib_run.py"):
                shutil.copy(REPO / "agent" / "tools" / name, root / "tools" / name)
            run = root / "runs" / "run-test"
            inputs = {
                "score/raw_score.json": {"raw_score": 0.5},
                "score/coverage_PGS000001.json": {"resolved_weight_fraction": 0.9},
                "ancestry/ancestry.json": {"inferred_group": "EUR", "evidence": [{"source": "x"}]},
                "door/provenance.json": {"source_assay_class": "array"},
                "select/dl_PGS000001.json": {
                    "pgs_id": "PGS000001",
                    "rest_record": {"trait_efo": [EFO], "trait_reported": "Example trait"}},
            }
            for rel, data in inputs.items():
                (run / rel).parent.mkdir(parents=True, exist_ok=True)
                (run / rel).write_text(json.dumps(data))
            out = run / "interpret" / "request.json"
            proc = subprocess.run(
                [sys.executable, str(root / "tools" / "interpret_request.py"), "--run", "run-test",
                 "--score", str(run / "score/raw_score.json"),
                 "--coverage", str(run / "score/coverage_PGS000001.json"),
                 "--ancestry", str(run / "ancestry/ancestry.json"),
                 "--provenance", str(run / "door/provenance.json"),
                 "--model-download", str(run / "select/dl_PGS000001.json"),
                 "--out", str(out), *extra],
                capture_output=True, text=True, cwd=root, env=dict(os.environ))
            self.assertEqual(proc.returncode, 0, proc.stderr)
            return json.loads(out.read_text())

    def test_request_carries_the_bare_id(self):
        self.assertEqual(self.request()["trait"], {"id": "MONDO_0000001", "label": "Example trait"})

    def test_a_same_sex_comparison_group_is_asked_for_only_when_the_agent_asks(self):
        self.assertIsNone(self.request()["comparison_group"])
        self.assertEqual(self.request("--comparison-sex", "female")["comparison_group"], {"sex": "female"})
        with self.assertRaises(AssertionError):
            self.request("--comparison-sex", "woman")


if __name__ == "__main__":
    unittest.main()
