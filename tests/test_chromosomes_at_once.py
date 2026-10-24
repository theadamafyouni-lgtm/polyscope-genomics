"""Speedup 2: a demo run imputes 3 chromosomes at once while it is the only demo going, else 2.
The demo worker says how many are going (refdata/demo_load.json); impute_joint.py reads it each
time a chromosome would start. Each chromosome is its own Beagle at the same threads either way."""
import json
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "agent" / "tools"))
import impute_joint  # noqa: E402


class ChromosomesAtOnce(unittest.TestCase):
    def run_all(self, load):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "demo_load.json"
            if load is not None:
                path.write_text(json.dumps({"demo_runs_going": load}))
            lock, now, most, order = threading.Lock(), [0], [0], []

            def one(c):
                with lock:
                    now[0] += 1
                    most[0] = max(most[0], now[0])
                    order.append(c)
                time.sleep(0.05)
                if c == "20" and load == 1:
                    path.write_text(json.dumps({"demo_runs_going": 2}))  # a second demo starts
                with lock:
                    now[0] -= 1
                return {"chrom": c, "status": "ok"}
            with patch.object(impute_joint, "DEMO_LOAD", path), patch.object(impute_joint, "LOAD_RECHECK_SECONDS", 0.01):
                runs, at_once = impute_joint.run_chromosomes([str(c) for c in range(1, 23)], one, 2)
            return runs, at_once, most[0], order

    def test_three_at_once_only_while_this_is_the_only_demo_going(self):
        runs, at_once, most, _ = self.run_all(None)          # not a demo run: as asked
        self.assertEqual((most, at_once), (2, 2))
        self.assertEqual([r["chrom"] for r in runs], [str(c) for c in range(1, 23)])
        _, at_once, most, _ = self.run_all(2)               # another demo going
        self.assertEqual((most, at_once), (2, 2))
        for _ in range(10):                                  # alone, until a second starts
            runs, at_once, most, order = self.run_all(1)
            self.assertEqual((most, at_once), (3, 3))
            self.assertEqual([r["chrom"] for r in runs], [str(c) for c in range(1, 23)])
            # Started largest first, as given, every time: two waiting for one slot never swap.
            self.assertEqual(order, [str(c) for c in range(1, 23)])


if __name__ == "__main__":
    unittest.main()
