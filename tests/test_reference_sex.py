"""A same-sex comparison group, end to end at the boundaries: the reference builder takes only
the samples of the sex the request asks for, and the adapter refuses a reference whose group is
not the one asked for. Synthetic samples and artifacts only."""
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "reference-provider"))
import build_reference  # noqa: E402

ADAPTER = REPO / "agent" / "tools" / "reference_adapter.py"
FIELDS = ["usable", "reason", "source", "version_string", "ancestry_grouping_definition",
          "cohort_construction_rule", "n_participants", "too_few_threshold_used", "assay_layer",
          "scoring_parity", "distribution"]


class SameSexPool(unittest.TestCase):
    def test_the_pool_is_the_samples_of_the_requested_sex_and_says_what_it_left_out(self):
        with tempfile.TemporaryDirectory() as tmp:
            panel = Path(tmp) / "sample_sex.panel"
            # The 1000 Genomes panel's own layout: a gender column, then two empty ones.
            panel.write_text("sample\tpop\tsuper_pop\tgender\t\t\nA\tGBR\tEUR\tfemale\t\t\n"
                             "B\tGBR\tEUR\tmale\t\t\nC\tGBR\tEUR\tFemale\t\t\nD\tGBR\tEUR\t\t\t\n")
            sexes = build_reference.load_sex_labels(str(panel))
        self.assertEqual(sexes, {"A": "female", "B": "male", "C": "female"})
        keep, record = build_reference.same_sex_pool(["A", "B", "C", "D", "E"], [0, 1, 2, 3], "female", sexes)
        self.assertEqual(keep, [0, 2])
        self.assertEqual(record, {"comparison_group_sex": "female", "n_before": 4, "n_of_that_sex": 2,
                                  "n_excluded_other_sex": 1, "n_excluded_without_a_recorded_sex": 1})


class AdapterChecksTheGroup(unittest.TestCase):
    def adapt(self, asked, built):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            request = {"model": {"pgs_id": "PGS000001", "sha256": "a" * 64}, "patient": {"raw_score": 0.1}}
            if asked:
                request["comparison_group"] = {"sex": asked}
            artifact = {field: None for field in FIELDS}
            artifact.update(usable=False, reason="too few similar participants",
                            model={"pgs_id": "PGS000001", "sha256": "a" * 64})
            if built:
                artifact["comparison_group_sex"] = built
            (root / "request.json").write_text(json.dumps(request))
            (root / "artifact.json").write_text(json.dumps(artifact))
            done = subprocess.run([sys.executable, str(ADAPTER), "--request", str(root / "request.json"),
                                   "--reference", str(root / "artifact.json"), "--out", str(root / "out.json")],
                                  capture_output=True, text=True)
            return done.returncode, json.loads((root / "out.json").read_text())["detail"]

    def test_a_reference_is_for_this_request_only_if_its_group_is_the_sex_asked_for(self):
        self.assertEqual(self.adapt("female", "female")[0], 5)   # the provider's own refusal, read as one
        self.assertEqual(self.adapt(None, None)[0], 5)
        for asked, built in (("female", None), ("female", "male"), (None, "male")):
            rc, detail = self.adapt(asked, built)
            self.assertEqual(rc, 4, (asked, built))
            self.assertIn("comparison group", detail)


if __name__ == "__main__":
    unittest.main()
