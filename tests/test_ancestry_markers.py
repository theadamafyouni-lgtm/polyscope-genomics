"""ancestry.py project on a small synthetic reference: the shared-marker count the ancestry skill
reads is the markers the PCA used, not the records the person's file has on the marker set."""
import json
from pathlib import Path
import random
import subprocess
import sys
import tempfile
import unittest

REPO = Path(__file__).resolve().parents[1]
TOOL = REPO / "agent" / "tools" / "ancestry.py"
PLINK2 = REPO / "agent" / "refdata" / "bin" / "plink2"


def vcf(path, samples, sites, genotypes):
    with open(path, "w") as fh:
        fh.write("##fileformat=VCFv4.2\n##contig=<ID=1>\n##contig=<ID=2>\n"
                 '##FORMAT=<ID=GT,Number=1,Type=String,Description="Genotype">\n'
                 "#CHROM\tPOS\tID\tREF\tALT\tQUAL\tFILTER\tINFO\tFORMAT\t" + "\t".join(samples) + "\n")
        for (chrom, pos), row in zip(sites, genotypes):
            fh.write("%s\t%d\t.\tA\tG\t.\tPASS\t.\tGT\t%s\n" % (chrom, pos, "\t".join(row)))


@unittest.skipUnless(PLINK2.exists(), "the staged plink2 is not here")
class SharedMarkersTest(unittest.TestCase):
    def test_the_shared_count_is_the_markers_the_pca_used(self):
        rng = random.Random(7)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        sites = [(1 + i // 600, 1000 + 1000 * i) for i in range(1200)]
        pops = {"AAA": "EUR", "BBB": "AFR", "CCC": "EAS"}
        freq = {p: [min(0.95, max(0.05, rng.random())) for _ in sites] for p in pops}
        samples = ["%s%02d" % (p, i) for p in pops for i in range(30)]
        call = lambda q: ["0/0", "0/1", "1/1"][(rng.random() < q) + (rng.random() < q)]
        vcf(root / "ref.vcf", samples, sites, [[call(freq[s[:3]][j]) for s in samples] for j in range(len(sites))])
        (root / "ref.panel").write_text("sample\tpop\tsuper_pop\n" + "".join(
            "%s\t%s\t%s\n" % (s, s[:3], pops[s[:3]]) for s in samples))
        build = subprocess.run([sys.executable, str(TOOL), "build-ref", "--ref-vcf", str(root / "ref.vcf"),
                                "--panel", str(root / "ref.panel"), "--maf", "0.01", "--geno", "0.1",
                                "--prune-window", "50", "--prune-step", "5", "--prune-r2", "0.99",
                                "--pcs", "4", "--exclude-palindromic", "no", "--out-dir", str(root / "ref")],
                               capture_output=True, text=True)
        self.assertEqual(build.returncode, 0, build.stderr[-2000:])
        pruned = [tuple(l.split("\t")[:2]) for l in (root / "ref" / "ref_pruned.pvar").read_text().splitlines()
                  if not l.startswith("#")]
        # The person's file has a record at every reference marker, but a genotype at only a
        # third of them: the rest are no-calls, kept as missing genotypes.
        person = [call(freq["AAA"][j]) if j % 3 == 0 else "./." for j in range(len(sites))]
        vcf(root / "person.vcf", ["P"], sites, [[g] for g in person])
        project = subprocess.run([sys.executable, str(TOOL), "project", "--ref-dir", str(root / "ref"),
                                  "--patient-vcf", str(root / "person.vcf"), "--pcs", "4", "--k-neighbours", "10",
                                  "--out", str(root / "projection.json")], capture_output=True, text=True)
        self.assertEqual(project.returncode, 0, project.stderr[-2000:])
        result = json.loads((root / "projection.json").read_text())
        called = {"%s:%d" % s for s, g in zip(sites, person) if g != "./."}
        used = sum("%s:%s" % p in called for p in pruned)
        self.assertEqual(result["n_markers_used_in_augmented_pca"], used)
        self.assertEqual(result["n_markers_shared_with_reference"], used)
        self.assertEqual(result["marker_set"]["present_on_this_array"], len(pruned))
        self.assertLess(used, len(pruned) / 2)


if __name__ == "__main__":
    unittest.main()
