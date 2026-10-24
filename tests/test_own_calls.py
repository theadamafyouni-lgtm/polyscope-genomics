"""own_calls.py: a whole genome's own calls at a model's positions, for scoring without
imputation where the file says what it read. A variant call is the door's (a call the policy set
missing stays missing); a reference call is a position inside the Complete Genomics var file's
reference loci, every allele called reference, written 0/0 with the genome's base; anything else
is not called, never reference (JC-7). Synthetic genome, end to end through match_variants.py
and score_plink2.py."""
import gzip
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

REPO = Path(__file__).resolve().parents[1]
TOOLS = REPO / "agent" / "tools"
PLINK2 = REPO / "agent" / "refdata" / "bin" / "plink2"

# chr1, 0-based half-open loci: reference [0,100); a het SNV at 100 (A>G); no-call [101,150);
# reference [150,300); nothing for [300,319); a hom SNV at 319 the quality policy failed.
VAR = """#ASSEMBLY_ID\tGS000000000-ASM
#FORMAT_VERSION\t2.5
#TYPE\tVAR-ANNOTATION

>locus\tploidy\tallele\tchromosome\tbegin\tend\tvarType\treference\talleleSeq\tvarScoreVAF\tvarScoreEAF\tvarFilter\thapLink\txRef
1\t2\tall\tchr1\t0\t100\tref\t=\t=\t\t\t\t\t
2\t2\t1\tchr1\t100\t101\tref\tA\tA\t90\t90\tPASS\t\t
2\t2\t2\tchr1\t100\t101\tsnp\tA\tG\t90\t90\tPASS\t\t
3\t2\tall\tchr1\t101\t150\tno-call\t=\t?\t\t\t\t\t
4\t2\tall\tchr1\t150\t300\tref\t=\t=\t\t\t\t\t
5\t2\t1\tchr1\t319\t320\tsnp\tC\tT\t5\t5\tVQLOW\t\t
5\t2\t2\tchr1\t319\t320\tsnp\tC\tT\t5\t5\tVQLOW\t\t
6\t2\tall\tchrX\t0\t500\tref\t=\t=\t\t\t\t\t
"""
MODEL = """#pgs_id=PGS000000
chr_name\tchr_position\teffect_allele\tother_allele\teffect_weight\thm_chr\thm_pos\thm_inferOtherAllele
1\t50\tG\tA\t0.5\t1\t50\t
1\t101\tG\tA\t0.4\t1\t101\t
1\t120\tC\tT\t0.3\t1\t120\t
1\t200\tT\tC\t0.2\t1\t200\t
1\t310\tA\tG\t0.1\t1\t310\t
1\t320\tT\tC\t0.05\t1\t320\t
X\t100\tA\tG\t0.9\tX\t100\t
"""


def genome(length=500):
    seq = ["C"] * length
    for pos, base in ((50, "A"), (101, "A"), (200, "T"), (320, "C")):
        seq[pos - 1] = base
    return "".join(seq)


class OwnCalls(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.d = Path(tmp.name)
        (self.d / "genome.var.tsv").write_text(VAR)
        with gzip.open(self.d / "model.txt.gz", "wt") as fh:
            fh.write(MODEL)
        with gzip.open(self.d / "GRCh37.fa.gz", "wt") as fh:
            seq = genome()
            fh.write(">1 dna:chromosome\n" + "\n".join(seq[i:i + 60] for i in range(0, len(seq), 60)) + "\n")
            fh.write(">X dna:chromosome\n" + "A" * 500 + "\n")
        vcf = ("##fileformat=VCFv4.2\n##contig=<ID=1,length=500>\n##contig=<ID=X,length=500>\n"
               '##FORMAT=<ID=GT,Number=1,Type=String,Description="Genotype">\n'
               "#CHROM\tPOS\tID\tREF\tALT\tQUAL\tFILTER\tINFO\tFORMAT\tPERSON\n"
               "1\t101\t.\tA\tG\t.\tPASS\t.\tGT\t0/1\n"
               "1\t320\t.\tC\tT\t.\tVQLOW\t.\tGT\t./.\n")
        (self.d / "normalized.vcf").write_text(vcf)
        subprocess.run(["bcftools", "view", "-Oz", "-o", str(self.d / "normalized.vcf.gz"), str(self.d / "normalized.vcf")], check=True)
        subprocess.run(["bcftools", "index", "-t", str(self.d / "normalized.vcf.gz")], check=True)

    def own_calls(self):
        out = self.d / "own_calls"
        done = subprocess.run([sys.executable, str(TOOLS / "own_calls.py"),
                               "--scorefile", str(self.d / "model.txt.gz"), "--chr-col", "hm_chr", "--pos-col", "hm_pos",
                               "--effect-allele-col", "effect_allele", "--other-allele-col", "other_allele",
                               "--effect-weight-col", "effect_weight", "--calls", str(self.d / "normalized.vcf.gz"),
                               "--cg-var", str(self.d / "genome.var.tsv"), "--fasta", str(self.d / "GRCh37.fa.gz"),
                               "--out-dir", str(out)], capture_output=True, text=True)
        self.assertEqual(done.returncode, 0, done.stderr)
        return out, json.loads((out / "own_calls.json").read_text())

    def test_reference_only_where_the_file_called_it_and_never_where_it_did_not(self):
        out, facts = self.own_calls()
        records = subprocess.run(["bcftools", "query", "-f", "%CHROM\t%POS\t%REF\t%ALT\t[%GT]\t[%DS]\n", str(out / "calls.vcf.gz")],
                                 capture_output=True, text=True, check=True).stdout.splitlines()
        self.assertEqual(records, ["1\t50\tA\tG\t0/0\t0", "1\t101\tA\tG\t0/1\t1", "1\t200\tT\tC\t0/0\t0"])
        self.assertEqual(facts["positions"], {"called_variant": 1, "called_reference": 2, "call_failed": 1,
                                              "not_called": 2, "alleles_unplaceable": 0})
        self.assertEqual(facts["left_out_of_coverage"], {"X": 1})
        self.assertAlmostEqual(facts["abs_beta"]["total_autosomal"], 1.55)
        self.assertAlmostEqual(facts["abs_beta"]["called"], 1.1)
        self.assertAlmostEqual(facts["abs_beta"]["fraction_called"], 1.1 / 1.55)
        self.assertEqual([locus["pos"] for locus in facts["largest_not_called"]][:2], [120, 310])
        self.assertIn("agent", facts["does_not_decide"])

    @unittest.skipUnless(PLINK2.exists(), "plink2 is in refdata/bin on the build host")
    def test_its_calls_score_through_the_same_tools_as_imputed_genotypes(self):
        out, _ = self.own_calls()
        run = "own-calls-test"
        runs = REPO / "agent" / "runs" / run
        self.addCleanup(shutil.rmtree, runs, True)
        score = self.d / "score"
        subprocess.run([sys.executable, str(TOOLS / "match_variants.py"), "--run", run,
                        "--scorefile", str(self.d / "model.txt.gz"), "--chr-col", "hm_chr", "--pos-col", "hm_pos",
                        "--effect-allele-col", "effect_allele", "--other-allele-col", "other_allele",
                        "--effect-weight-col", "effect_weight", "--imputed-dir", str(out),
                        "--typed-vcf", str(out / "calls.vcf.gz"), "--palindromic-policy", "exclude",
                        "--r2-bar", "0.8", "--out-dir", str(score)], check=True, capture_output=True)
        rows = (score / "plink_score_input.tsv").read_text().splitlines()
        self.assertEqual(rows, ["ID\tEA\tBETA", "1:50\tG\t0.5", "1:101\tG\t0.4", "1:200\tT\t0.2"])
        done = subprocess.run([sys.executable, str(TOOLS / "score_plink2.py"), "--run", run,
                               "--score-input", str(score / "plink_score_input.tsv"), "--imputed-dir", str(out),
                               "--out-dir", str(score)], capture_output=True, text=True)
        self.assertEqual(done.returncode, 0, done.stderr[-2000:])
        result = json.loads((score / "raw_score.json").read_text())
        self.assertAlmostEqual(result["raw_score"], 0.8)   # 0 x 0.5 + 1 x 0.4 + 2 x 0.2


if __name__ == "__main__":
    unittest.main()
