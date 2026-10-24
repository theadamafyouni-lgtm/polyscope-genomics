"""cg_to_vcf.py: a Complete Genomics var file as a VCF the door's other tools read, its call
scores and filters carried. Synthetic loci in the layout of a real var file (format 2.5)."""
import bz2
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
import json

REPO = Path(__file__).resolve().parents[1]
TOOL = REPO / "agent" / "tools" / "cg_to_vcf.py"
HEADER = ("#ASSEMBLY_ID\tGS000000000-ASM\n#CALLED_GENDER\tFEMALE\n#FORMAT_VERSION\t2.5\n"
          "#GENOME_REFERENCE\tNCBI build 37\n#TYPE\tVAR-ANNOTATION\n\n"
          ">locus\tploidy\tallele\tchromosome\tbegin\tend\tvarType\treference\talleleSeq\t"
          "varScoreVAF\tvarScoreEAF\tvarFilter\thapLink\txRef\talleleFreq\talternativeCalls\n")
ROWS = [
    "1\t2\tall\tchr1\t0\t10000\tno-ref\t=\t?\t\t\t\t\t\t\t",
    "2\t2\tall\tchr1\t10000\t11481\tno-call\t=\t?\t\t\t\t\t\t\t",
    "3\t2\tall\tchr1\t11481\t38231\tref\t=\t=\t\t\t\t\t\t\t",
    "4\t2\t1\tchr1\t38231\t38232\tsnp\tA\tG\t151\t151\tPASS\t\tdbsnp.86:rs1\t\t",       # hom-alt
    "4\t2\t2\tchr1\t38231\t38232\tsnp\tA\tG\t72\t72\tPASS\t\tdbsnp.86:rs1\t\t",
    "5\t2\t1\tchr1\t46669\t46670\tref\tA\tA\t28\t28\tVQLOW\t\t\t\t",                   # het, one allele weak
    "5\t2\t2\tchr1\t46669\t46670\tsnp\tA\tG\t450\t450\tPASS\t\t\t\t",
    "6\t2\t1\tchr1\t47000\t47001\tsnp\tT\tC\t90\t95\tPASS\t\t\t\t",                    # two different alts
    "6\t2\t2\tchr1\t47000\t47001\tsnp\tT\tG\t80\t85\tAMBIGUOUS\t\t\t\t",
    "7\t2\t1\tchr1\t48000\t48001\tno-call\tC\t?\t\t\t\t\t\t\t",                        # half-call: missing
    "7\t2\t2\tchr1\t48000\t48001\tsnp\tC\tT\t60\t60\tPASS\t\t\t\t",
    "8\t2\t1\tchr1\t49000\t49003\tdel\tGTA\t\t70\t70\tPASS\t\t\t\t",                   # not an SNV: dropped
    "8\t2\t2\tchr1\t49000\t49003\tref\tGTA\tGTA\t70\t70\tPASS\t\t\t\t",
    "10\t2\t1\tchr2\t500\t503\tref\t=\t=\t40\t40\tPASS\t\t\t\t",                         # an SNV inside a longer locus
    "10\t2\t2\tchr2\t500\t501\tref\tG\tG\t40\t40\tPASS\t\t\t\t",
    "10\t2\t2\tchr2\t501\t502\tsnp\tC\tT\t66\t66\tPASS\t\t\t\t",
    "10\t2\t2\tchr2\t502\t503\tref\tA\tA\t40\t40\tPASS\t\t\t\t",
    "9\t1\tall\tchrX\t3000000\t3000001\tsnp\tC\tA\t200\t200\tPASS\t\t\t\t",             # haploid
]


class CgToVcfTest(unittest.TestCase):
    def test_snvs_with_their_genotypes_scores_and_filters(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            var = root / "genome.tsv.bz2"
            var.write_bytes(bz2.compress((HEADER + "\n".join(ROWS) + "\n").encode()))
            done = subprocess.run([sys.executable, str(TOOL), "--input", str(var), "--sample-id", "P",
                                   "--out", str(root / "cg.vcf.gz")], capture_output=True, text=True)
            self.assertEqual(done.returncode, 0, done.stderr)
            records = subprocess.run(["bcftools", "query", "-f", "%CHROM %POS %REF %ALT %FILTER [%GT %VAFMIN %EAFMIN %VAFS]\n",
                                      str(root / "cg.vcf.gz")], capture_output=True, text=True, check=True).stdout.splitlines()
            self.assertEqual(records, [
                "1 38232 A G PASS 1/1 72 72 151,72",
                "1 46670 A G VQLOW 0/1 28 28 28,450",
                "1 47001 T C,G AMBIGUOUS 1/2 80 85 90,80",
                "1 48001 C T PASS ./. . . .,60",
                "2 502 C T PASS 0/1 40 40 40,66",
                "X 3000001 C A PASS 1 200 200 200",
            ])
            header = subprocess.run(["bcftools", "view", "-h", str(root / "cg.vcf.gz")], capture_output=True, text=True).stdout
            self.assertIn("##CG_CALLED_GENDER=FEMALE", header)
            self.assertIn('##FILTER=<ID=VQLOW', header)
            self.assertTrue((root / "cg.vcf.gz.tbi").exists())
            account = json.loads((root / "cg.json").read_text())
            self.assertEqual(account["snv_records_written"], 6)
            self.assertEqual(account["variant_loci_not_snv_dropped"], {"del": 1})


if __name__ == "__main__":
    unittest.main()
