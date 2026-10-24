"""sex_evidence.py: the file's own X and Y evidence, counted, never read as a sex."""
import gzip
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

REPO = Path(__file__).resolve().parents[1]
TOOL = REPO / "agent" / "tools" / "sex_evidence.py"
PAR = ["--par", "X:60001-2699520", "--par", "X:154931044-155260560", "--par", "Y:10001-2649520"]


class SexEvidenceTest(unittest.TestCase):
    def run_tool(self, name, content, *args):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / name
            (gzip.open(path, "wt") if name.endswith(".gz") else open(path, "w")).write(content)
            done = subprocess.run([sys.executable, str(TOOL), "--input", str(path), *args,
                                   "--out", str(Path(tmp) / "out.json")], capture_output=True, text=True)
            if done.returncode:
                return done.returncode, done.stderr
            return 0, json.loads((Path(tmp) / "out.json").read_text())

    def test_a_genotype_table_counted_by_contig_inside_and_outside_the_pseudoautosomal_regions(self):
        rows = ["# rsid\tchromosome\tposition\tgenotype",
                "rs1\t1\t100\tAG", "rs2\t1\t200\tAA", "rs3\t2\t300\tCT",
                "rs4\tX\t3000000\tAA", "rs5\tX\t3000100\tA", "rs6\tX\t3000200\tAG", "rs7\tX\t3000300\t--",
                "rs8\tX\t100000\tCT",                       # in PAR1: kept apart
                "rs9\tY\t5000000\tG", "rs10\tY\t5000100\t--", "rs11\tY\t5000200\tTT",
                "rs12\tXY\t2000000\tAG", "rs13\tMT\t500\tA", "rs14\t24\t5000300\tC"]
        rc, out = self.run_tool("genome.txt", "\n".join(rows) + "\n", "--chrom-col", "1", "--pos-col", "2",
                                "--genotype-col", "3", "--no-call-token=--", *PAR)
        self.assertEqual(rc, 0, out)
        x = out["contigs"]["X"]
        self.assertEqual({k: x["outside_par"][k] for k in ("records", "called", "no_call", "haploid", "homozygous", "heterozygous")},
                         {"records": 4, "called": 3, "no_call": 1, "haploid": 1, "homozygous": 1, "heterozygous": 1})
        self.assertEqual(x["outside_par"]["heterozygous_share_of_diploid_calls"], 0.5)
        self.assertEqual((x["in_par"]["records"], x["in_par"]["heterozygous"]), (1, 1))
        y = out["contigs"]["Y"]["outside_par"]
        self.assertEqual((y["records"], y["called"], y["no_call"], y["haploid"], y["homozygous"]), (4, 3, 1, 2, 1))
        self.assertEqual(out["contigs"]["XY"]["outside_par"]["heterozygous"], 1)
        self.assertEqual(out["autosomes_for_comparison"]["heterozygous_share_of_diploid_calls"], 2 / 3)
        self.assertNotIn("sex", json.dumps({k: v for k, v in out.items() if k != "what_this_does_not_do"}).replace("sex_evidence", ""))

    def test_a_vcf_read_by_its_gt_field_and_two_allele_columns_read_together(self):
        vcf = ("##fileformat=VCFv4.2\n#CHROM\tPOS\tID\tREF\tALT\tQUAL\tFILTER\tINFO\tFORMAT\tP\n"
               "chr1\t100\t.\tA\tG\t50\tPASS\t.\tGT:DP\t0/1:30\n"
               "chrX\t3000000\t.\tA\tG\t50\tPASS\t.\tGT:DP\t1:12\n"
               "chrX\t3000100\t.\tA\tG\t50\tPASS\t.\tGT:DP\t0|1:30\n"
               "chrX\t3000200\t.\tA\tG\t50\tPASS\t.\tGT:DP\t./.:0\n"
               "chrY\t5000000\t.\tC\tT\t50\tPASS\t.\tGT:DP\t1:20\n")
        rc, out = self.run_tool("genome.vcf.gz", vcf, *PAR)
        self.assertEqual(rc, 0, out)
        x = out["contigs"]["X"]["outside_par"]
        self.assertEqual((x["records"], x["haploid"], x["heterozygous"], x["no_call"]), (3, 1, 1, 1))
        self.assertEqual(out["contigs"]["Y"]["outside_par"]["haploid"], 1)
        rows = "rsid\tchromosome\tposition\tallele1\tallele2\nrs1\t23\t3000000\tA\tG\nrs2\t23\t3000100\t0\t0\n"
        rc, out = self.run_tool("genome.txt", rows, "--chrom-col", "1", "--pos-col", "2", "--allele-cols", "3,4",
                                "--no-call-token", "0", "--no-par")
        self.assertEqual(rc, 0, out)
        self.assertEqual((out["contigs"]["X"]["outside_par"]["heterozygous"], out["contigs"]["X"]["outside_par"]["no_call"]), (1, 1))

    def test_the_pseudoautosomal_regions_are_the_agents_to_name(self):
        rc, err = self.run_tool("genome.txt", "rs1\tX\t100\tAA\n", "--chrom-col", "1", "--pos-col", "2", "--genotype-col", "3")
        self.assertNotEqual(rc, 0)
        self.assertIn("--par", err)


if __name__ == "__main__":
    unittest.main()
