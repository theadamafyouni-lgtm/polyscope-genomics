"""call_quality.py: a sequencing file's own quality fields, reported; the agent's policy, applied."""
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

REPO = Path(__file__).resolve().parents[1]
TOOL = REPO / "agent" / "tools" / "call_quality.py"
HEADER = ("##fileformat=VCFv4.2\n##contig=<ID=1>\n##FILTER=<ID=LowQual,Description=\"Low quality\">\n"
          "##INFO=<ID=DP,Number=1,Type=Integer,Description=\"Total depth\">\n"
          "##FORMAT=<ID=GT,Number=1,Type=String,Description=\"Genotype\">\n"
          "##FORMAT=<ID=DP,Number=1,Type=Integer,Description=\"Read depth\">\n"
          "##FORMAT=<ID=GQ,Number=1,Type=Integer,Description=\"Genotype quality\">\n"
          "#CHROM\tPOS\tID\tREF\tALT\tQUAL\tFILTER\tINFO\tFORMAT\tP\n")
RECORDS = ["1\t100\t.\tA\tG\t50\tPASS\tDP=31\tGT:DP:GQ\t0/1:30:99",
           "1\t200\t.\tA\tG\t12.5\tLowQual\tDP=29\tGT:DP:GQ\t1/1:28:99",
           "1\t300\t.\tA\tG\t40\tPASS\tDP=3\tGT:DP:GQ\t0/1:3:10",
           "1\t400\t.\tA\tG\t.\tPASS\t.\tGT:DP\t0/1:40",
           "1\t500\t.\tA\tG\t60\tPASS\tDP=20\tGT:DP:GQ\t./.:0:0"]


def vcf(path, records=RECORDS):
    """Bgzipped and indexed, as the door writes the normalized VCF."""
    plain = Path(str(path)[:-len(".gz")])
    plain.write_text(HEADER + "\n".join(records) + "\n")
    subprocess.run(["bgzip", "-f", str(plain)], check=True)
    subprocess.run(["tabix", "-f", "-p", "vcf", str(path)], check=True)


class CallQualityTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)

    def tool(self, *args):
        done = subprocess.run([sys.executable, str(TOOL), *args], capture_output=True, text=True)
        self.assertEqual(done.returncode, 0, done.stderr)

    def test_facts_report_what_the_file_has_and_how_it_is_spread_without_a_verdict(self):
        vcf(self.root / "in.vcf.gz")
        self.tool("facts", "--input", str(self.root / "in.vcf.gz"), "--out", str(self.root / "facts.json"))
        facts = json.loads((self.root / "facts.json").read_text())
        self.assertEqual(facts["n_records"], 5)
        self.assertEqual(facts["n_called"], 4)
        self.assertEqual(facts["filter_values"], {"PASS": 4, "LowQual": 1})
        self.assertEqual(facts["declared"]["FILTER"]["LowQual"], "Low quality")
        self.assertEqual(facts["qual"]["n_missing"], 1)
        gq = facts["format"]["GQ"]
        # Over called genotypes: three carry GQ and one does not, which a policy on GQ would fail.
        self.assertEqual((gq["n_called_with_it"], gq["n_called_without_it"]), (3, 1))
        self.assertEqual((gq["min"], gq["max"]), (10.0, 99.0))
        self.assertEqual(facts["format"]["DP"]["n_called_with_it"], 4)
        self.assertEqual(facts["info"]["DP"]["n_records_with_it"], 4)
        self.assertNotIn("pass_rate", json.dumps(facts))

    def test_the_agents_policy_makes_failing_calls_missing_and_keeps_every_record(self):
        normalized = self.root / "normalized.vcf.gz"
        vcf(normalized)
        self.tool("apply", "--vcf", str(normalized), "--include", 'FILTER="PASS" && FMT/GQ>=20 && FMT/DP>=10',
                  "--out", str(self.root / "call_quality.json"))
        result = json.loads((self.root / "call_quality.json").read_text())
        self.assertEqual(result["n_records"], 5)
        self.assertEqual((result["n_called_before"], result["n_called_after"], result["n_calls_made_missing"]), (4, 1, 3))
        self.assertEqual(result["include"], 'FILTER="PASS" && FMT/GQ>=20 && FMT/DP>=10')
        # The policy's file is the normalized VCF every stage reads; the one before it is kept beside it.
        before = self.root / "normalized.before_call_quality.vcf.gz"
        self.assertTrue(before.exists() and Path(str(normalized) + ".tbi").exists())
        gts = subprocess.run(["bcftools", "query", "-f", "[%GT]\n", str(normalized)], capture_output=True, text=True).stdout.split()
        self.assertEqual(gts, ["0/1", "./.", "./.", "./.", "./."])
        self.assertEqual(subprocess.run(["bcftools", "query", "-f", "[%GT]\n", str(before)], capture_output=True,
                                        text=True).stdout.split()[:2], ["0/1", "1/1"])
        # Applied once: a second policy on top of the first is refused, not stacked.
        done = subprocess.run([sys.executable, str(TOOL), "apply", "--vcf", str(normalized), "--include", "QUAL>0",
                               "--out", str(self.root / "again.json")], capture_output=True, text=True)
        self.assertNotEqual(done.returncode, 0)


if __name__ == "__main__":
    unittest.main()
