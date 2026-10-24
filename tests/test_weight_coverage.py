"""weight_coverage.py on synthetic scoring files: what counts toward coverage and the 5% check."""
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

REPO = Path(__file__).resolve().parents[1]
TOOL = REPO / "agent" / "tools" / "weight_coverage.py"
# Every spelling of a sex chromosome or the mitochondrion a scoring file may use.
NONAUTOSOMAL = ("X", "chrX", "x", "23", "Y", "chrY", "24", "XY", "25", "MT", "M", "chrM", "chrMT", "26")


class Model:
    """A scoring file, and what the person's run made of each position: for measure, the dosage
    table match_variants writes; for estimate, the person's VCF and the panel's sites index."""

    def __init__(self, root):
        self.root = Path(root)
        self.rows = []      # (chrom, pos, weight)
        self.dosages = []   # (chrom, pos, r2, source, status)
        self.typed = []     # (chrom, pos): called in the person's file
        self.panel = []     # (chrom, pos): in the imputation panel

    def row(self, chrom, pos, weight, dosage=None, typed=False, panel=False):
        self.rows.append((chrom, pos, weight))
        if dosage:
            self.dosages.append((chrom, pos) + dosage)
        if typed:
            self.typed.append((chrom, pos))
        if panel:
            self.panel.append((chrom, pos))
        return self

    def run(self, mode):
        scorefile = self.root / "model.txt"
        scorefile.write_text("#pgs_id=PGS000000\nhm_chr\thm_pos\teffect_weight\n" + "".join(
            "%s\t%s\t%s\n" % row for row in self.rows))
        args = [sys.executable, str(TOOL), mode, "--scorefile", str(scorefile), "--chr-col", "hm_chr",
                "--pos-col", "hm_pos", "--effect-weight-col", "effect_weight",
                "--high-beta-fraction", "0.05", "--out", str(self.root / ("%s.json" % mode))]
        if mode == "measure":
            dosages = self.root / "dosages.tsv"
            dosages.write_text("chrom\tpos\tr2\tsource\tstatus\n" + "".join(
                "%s\t%s\t%s\t%s\t%s\n" % d for d in self.dosages))
            args += ["--dosage-tsv", str(dosages), "--r2-bar", "0.80"]
        else:
            vcf = self.root / "person.vcf"
            vcf.write_text("##fileformat=VCFv4.2\n" + "".join(
                "##contig=<ID=%s>\n" % c for c in sorted({c for c, _ in self.typed})) +
                '##FORMAT=<ID=GT,Number=1,Type=String,Description="Genotype">\n'
                "#CHROM\tPOS\tID\tREF\tALT\tQUAL\tFILTER\tINFO\tFORMAT\tP\n" + "".join(
                "%s\t%s\t.\tA\tG\t.\tPASS\t.\tGT\t0/1\n" % t for t in self.typed))
            contigs = [str(i) for i in range(1, 23)]
            offsets, lengths = {}, {}
            at = 0
            for c in contigs:
                offsets[c], lengths[c] = at, 100000
                at += 100001
            bits = bytearray(at // 8 + 1)
            for c, p in self.panel:
                i = offsets[c] + p
                bits[i >> 3] |= 1 << (i & 7)
            (self.root / "panel.bin").write_bytes(bytes(bits))
            (self.root / "panel.json").write_text(json.dumps(
                {"contig_offsets": offsets, "contig_lengths": lengths, "sites_vcf": "synthetic"}))
            args += ["--patient-vcf", str(vcf), "--panel-index", str(self.root / "panel"),
                     "--imputed-contigs", "1-22"]
        done = subprocess.run(args, capture_output=True, text=True, cwd=REPO)
        if done.returncode:
            raise AssertionError(done.stderr[-2000:])
        return json.loads((self.root / ("%s.json" % mode)).read_text())


TYPED = (1.0, "typed", "resolved")


class CoverageTest(unittest.TestCase):
    def model(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        return Model(tmp.name)

    def high(self, result):
        return [(h["chrom"], h["pos"]) for h in result["high_beta_unresolved_loci"]]

    def test_sex_chromosome_and_mitochondrial_weight_is_in_neither_side_of_coverage(self):
        """The review's case (A1): autosomal weight 90.2 resolved and 4.8 unresolved at one locus,
        and five unresolved loci of weight 1 off the autosomes. Counted in the total they made
        coverage 0.902 and hid the locus at 4.8%; over the autosomes it is 90.2/95, and the locus
        is 5.05% of them, which the 5% check must name."""
        for spelling in NONAUTOSOMAL:
            for mode in ("measure", "estimate"):
                with self.subTest(chrom=spelling, mode=mode):
                    model = self.model().row("1", 100, 90.2, TYPED, typed=True, panel=True)
                    model.row("2", 200, -4.8)  # not in the panel: no route reaches it
                    for i in range(5):
                        model.row(spelling, 300 + i, 1.0)
                    result = model.run(mode)
                    self.assertAlmostEqual(result["total_abs_beta"], 95.0)
                    self.assertAlmostEqual(result["resolved_weight_fraction"], 90.2 / 95)
                    self.assertEqual(self.high(result), [("2", 200)])
                    self.assertAlmostEqual(result["high_beta_unresolved_loci"][0]["share_of_total_abs_beta"], 4.8 / 95)
                    left_out = result["left_out_of_coverage"]
                    self.assertEqual((left_out["n_model_rows"], left_out["abs_beta"]), (5, 5.0))
                    self.assertAlmostEqual(left_out["share_of_model_abs_beta"], 5 / 100)

    def test_appending_rows_off_the_autosomes_changes_nothing_about_the_autosomes(self):
        for mode in ("measure", "estimate"):
            with self.subTest(mode=mode):
                base = self.model().row("1", 100, 0.5, TYPED, typed=True, panel=True)
                base.row("2", 200, 0.25, (0.95, "imputed", "resolved"), panel=True).row("3", 300, 0.25)
                before = base.run(mode)
                for chrom in ("X", "Y", "MT", "XY"):
                    base.row(chrom, 400, 3.0, TYPED, typed=False)
                after = base.run(mode)
                for key in ("total_abs_beta", "resolved_weight_fraction", "unresolved_weight_fraction"):
                    self.assertEqual(before[key], after[key])
                self.assertEqual(self.high(before), self.high(after))
                # And all autosomal weight resolved is full coverage, whatever else the model has.
                full = self.model().row("1", 100, 0.5, TYPED, typed=True, panel=True).row("X", 400, 9.0)
                self.assertEqual(full.run(mode)["resolved_weight_fraction"], 1.0)

    def test_rows_at_one_locus_are_added_up_for_the_five_percent_check(self):
        """The review's case (A2): two unresolved rows of 3 at one position are one locus at 6%,
        not two at 3%. Signs and alleles do not matter, an excluded row makes its locus
        unresolved, and rows with no usable position are never lumped together."""
        for mode in ("measure", "estimate"):
            for second in (3.0, -3.0):
                with self.subTest(mode=mode, second=second):
                    model = self.model().row("1", 100, 94.0, TYPED, typed=True, panel=True)
                    result = model.row("3", 300, 3.0).row("3", 300, second).run(mode)
                    self.assertAlmostEqual(result["resolved_weight_fraction"], 0.94)
                    (locus,) = result["high_beta_unresolved_loci"]
                    self.assertEqual((locus["chrom"], locus["pos"], locus["n_model_rows"]), ("3", 300, 2))
                    self.assertAlmostEqual(locus["abs_beta"], 6.0)
                    self.assertAlmostEqual(locus["share_of_total_abs_beta"], 0.06)
                    self.assertEqual(result["largest_single_locus_shares"][1]["n_model_rows"], 2)
        # Two alleles at one position, one matched and one not: the locus is unresolved, all 6%.
        model = self.model().row("1", 100, 94.0, TYPED)
        model.row("3", 300, 3.0, (1.0, "typed", "resolved")).row("3", 300, 3.0, (1.0, "typed", "allele_mismatch"))
        self.assertEqual(self.high(model.run("measure")), [("3", 300)])
        # Rows without a usable position are each their own locus: 3% and 3%, no veto.
        model = self.model().row("1", 100, 94.0, TYPED, typed=True, panel=True)
        result = model.row("4", "", 3.0).row("4", "", 3.0).run("measure")
        self.assertEqual(result["high_beta_unresolved_loci"], [])
        self.assertAlmostEqual(result["unresolved_weight_fraction"], 0.06)


if __name__ == "__main__":
    unittest.main()
