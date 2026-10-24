"""rank_range.py: where a score sits in its comparison group as a range, from the counts, and
its z-score in the same group."""
import json
import statistics
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

REPO = Path(__file__).resolve().parents[1]
TOOL = REPO / "agent" / "tools" / "rank_range.py"


class RankRangeTest(unittest.TestCase):
    def run_tool(self, placement, *choices, scores=None):
        """The adapter's output for the counts: the person at 0, the group below at -1, level at 0
        and above at +1, unless `scores` says otherwise."""
        if scores is None:
            scores = [-1.0] * placement["n_below"] + [0.0] * placement["n_equal"] + [1.0] * placement["n_above"]
        placement = dict({"patient_raw_score": 0.0}, **placement)
        with tempfile.TemporaryDirectory() as tmp:
            response = Path(tmp) / "reference_response.json"
            response.write_text(json.dumps({"status": "reference_valid", "placement": placement,
                                            "reference": {"n_participants": placement["n"],
                                                          "distribution": {"raw_scores": scores, "n": len(scores)}}}))
            done = subprocess.run([sys.executable, str(TOOL), "--reference-response", str(response), *choices,
                                   "--out", str(Path(tmp) / "range.json")], capture_output=True, text=True)
            if done.returncode:
                return done.returncode, done.stderr
            return 0, json.loads((Path(tmp) / "range.json").read_text())

    def test_the_review_example_108_of_150_is_64_to_79(self):
        rc, out = self.run_tool({"n": 150, "n_below": 108, "n_equal": 0, "n_above": 42},
                                "--ties", "half", "--level", "0.95")
        self.assertEqual(rc, 0, out)
        self.assertEqual([round(x, 2) for x in out["percentile_range"]], [64.33, 78.57])
        self.assertEqual(out["percentile_point"], 72.0)
        self.assertEqual((out["method"], out["level"], out["ties"]), ("Wilson score interval", 0.95, "half"))
        self.assertIn("comparison group", out["covers"])
        self.assertTrue(any("imputation" in what for what in out["does_not_cover"]))

    def test_ties_count_as_the_agent_chose_and_the_range_stays_within_0_and_100(self):
        placement = {"n": 20, "n_below": 18, "n_equal": 2, "n_above": 0}
        points = {ties: self.run_tool(placement, "--ties", ties, "--level", "0.9")[1]["percentile_point"]
                  for ties in ("below", "half", "above")}
        self.assertEqual(points, {"below": 100.0, "half": 95.0, "above": 90.0})
        low, high = self.run_tool(placement, "--ties", "below", "--level", "0.9")[1]["percentile_range"]
        self.assertTrue(0 <= low < high <= 100)

    def test_the_z_score_is_from_the_same_group_the_range_is_counted_in(self):
        placement = {"n": 150, "n_below": 108, "n_equal": 0, "n_above": 42}
        rc, out = self.run_tool(placement, "--ties", "half", "--level", "0.95")
        self.assertEqual(rc, 0, out)
        scores = [-1.0] * 108 + [1.0] * 42
        mean, sd = statistics.fmean(scores), statistics.stdev(scores)
        z = out["z_score"]
        self.assertAlmostEqual(z["value"], (0.0 - mean) / sd, places=12)
        self.assertEqual((z["n"], z["sd_ddof"], z["patient_raw_score"]), (150, 1, 0.0))
        self.assertAlmostEqual(z["mean"], mean, places=12)
        self.assertAlmostEqual(z["sd"], sd, places=12)
        self.assertIn("comparison group", z["computed_from"])
        self.assertIn("not a risk", z["note"])
        # No scores to measure against, or none that differ: no z-score, and no range either.
        self.assertNotEqual(self.run_tool(placement, "--ties", "half", "--level", "0.95", scores=[])[0], 0)
        self.assertNotEqual(self.run_tool({"n": 3, "n_below": 0, "n_equal": 3, "n_above": 0}, "--ties", "half",
                                          "--level", "0.95")[0], 0)

    def test_it_gives_the_summary_its_words_for_the_range(self):
        """The report's summary says where the person lands as "higher than about X to Y out of
        every 100 people", and its height comparison by how far from the middle that is."""
        rc, out = self.run_tool({"n": 150, "n_below": 108, "n_equal": 0, "n_above": 42}, "--ties", "half", "--level", "0.95")
        self.assertEqual(out["summary_words"]["where"], "higher than about 64 to 79 out of every 100 people")
        self.assertEqual(out["summary_words"]["comparison"], ["a bit taller than average"])
        rc, out = self.run_tool({"n": 200, "n_below": 196, "n_equal": 0, "n_above": 4}, "--ties", "half", "--level", "0.95")
        low, high = (round(x) for x in out["percentile_range"])
        self.assertEqual(out["summary_words"]["where"], "higher than about %d to %d out of every 100 people" % (low, high))
        self.assertEqual(out["summary_words"]["comparison"], ["among the tallest"] if low >= 95 else ["taller than most", "among the tallest"])

    def test_choices_have_no_defaults_and_counts_must_add_up(self):
        placement = {"n": 150, "n_below": 108, "n_equal": 0, "n_above": 42}
        self.assertNotEqual(self.run_tool(placement, "--level", "0.95")[0], 0)
        self.assertNotEqual(self.run_tool(placement, "--ties", "half")[0], 0)
        self.assertNotEqual(self.run_tool(dict(placement, n_above=40), "--ties", "half", "--level", "0.95")[0], 0)
        self.assertNotEqual(self.run_tool(placement, "--ties", "half", "--level", "1.5")[0], 0)


if __name__ == "__main__":
    unittest.main()
