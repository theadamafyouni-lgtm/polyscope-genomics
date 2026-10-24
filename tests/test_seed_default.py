"""One fixed seed by default for the hold-out draw and imputation, recorded where it was used:
never the date, and never one chosen run by run."""
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

REPO = Path(__file__).resolve().parents[1]
TOOLS = REPO / "agent" / "tools"
sys.path.insert(0, str(TOOLS))
import impute_joint  # noqa: E402
import lib_run  # noqa: E402


class SeedTest(unittest.TestCase):
    def cohort(self, *seed):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        (root / "panel").write_text("sample\tpop\tsuper_pop\tgender\n" + "".join(
            "S%03d\tPOP\t%s\tfemale\n" % (i, "EUR" if i < 300 else "AFR") for i in range(400)))
        done = subprocess.run([sys.executable, str(TOOLS / "build_dr2_cohort.py"), "--panel", str(root / "panel"),
                               "--super-pop", "EUR", "--size", "150", *seed, "--out-dir", str(root)],
                              capture_output=True, text=True)
        self.assertEqual(done.returncode, 0, done.stderr)
        return json.loads((root / "dr2_cohort.json").read_text()), (root / "dr2_holdout.txt").read_text()

    def test_the_hold_out_draw_uses_the_fixed_seed_unless_told_otherwise_and_says_which(self):
        record, first = self.cohort()
        self.assertEqual((record["seed"], record["seed_is_the_default"]), (lib_run.DEFAULT_SEED, True))
        self.assertEqual(self.cohort()[1], first)
        record, second = self.cohort("--seed", "2")
        self.assertEqual((record["seed"], record["seed_is_the_default"]), (2, False))
        self.assertNotEqual(second, first)

    def test_imputation_uses_the_fixed_seed_unless_told_otherwise(self):
        required = ["--run", "r", "--target", "t", "--patient-id", "p", "--holdout-samples", "h",
                    "--reference-samples", "r", "--phase3-dir", "d", "--map-dir", "m",
                    "--typed-positions", "t", "--chromosomes", "22", "--out-dir", "o"]
        self.assertEqual(impute_joint.parse_args(required).seed, str(lib_run.DEFAULT_SEED))
        self.assertEqual(impute_joint.parse_args(required + ["--seed", "2"]).seed, "2")


if __name__ == "__main__":
    unittest.main()
