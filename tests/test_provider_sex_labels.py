"""The provider is handed each reference sample's recorded sex, for a same-sex comparison group."""
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "harness"))
from harness import config, provider  # noqa: E402


class Record:
    def add(self, path, *args, **kwargs):
        return {"sha256": None, "bytes": os.path.getsize(path)}


class SexLabelsStaged(unittest.TestCase):
    def test_the_panel_with_each_samples_sex_is_staged_under_the_name_the_builder_reads(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            refdata = root / "refdata" / "ancestry-ref"
            refdata.mkdir(parents=True)
            (refdata / "integrated_call_samples_v3.20130502.ALL.panel").write_text(
                "sample\tpop\tsuper_pop\tgender\t\t\nHG00096\tGBR\tEUR\tmale\t\t\n")
            run = root / "prs" / "runs" / "run-x"
            run.mkdir(parents=True)
            (run / "request.json").write_text(json.dumps({"model": {"pgs_id": "PGS000001"},
                                                          "comparison_group": {"sex": "female"}}))
            (root / "provider").mkdir()
            with patch.object(config, "REFDATA_SRC", str(root / "refdata")):
                handoff = provider.ProviderHandoff(str(root / "provider"), Record(), None)
                handoff.stage(str(run / "request.json"), str(root / "prs"), str(run))
            staged = root / "provider" / "inputs" / provider.PROVIDER_INPUTS["sex_labels"]
            self.assertEqual(staged.read_text().splitlines()[1], "HG00096\tGBR\tEUR\tmale\t\t")
            self.assertIn("sample_sex.panel", (root / "provider" / "inputs" / "INPUTS.md").read_text())
            sys.path.insert(0, str(REPO / "reference-provider"))
            import build_reference
            self.assertEqual(provider.PROVIDER_INPUTS["sex_labels"], build_reference.SEX_LABELS)


if __name__ == "__main__":
    unittest.main()
