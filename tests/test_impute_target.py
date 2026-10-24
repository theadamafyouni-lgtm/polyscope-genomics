"""Synthetic identity test: a chromosome-only target must not change Beagle's output.

No patient data, SSH connection or pipeline run is used. Set BEAGLE_JAR if the
reference-data jar is outside the checkout; the test always uses six threads.
"""
import gzip
import os
from pathlib import Path
import random
import shutil
import subprocess
import sys
import tempfile
import unittest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "agent" / "tools"))
import impute_joint  # noqa: E402

BEAGLE = Path(os.environ.get("BEAGLE_JAR", REPO / "agent/refdata/bin/beagle.jar"))


def command(*args):
    result = subprocess.run([str(a) for a in args], capture_output=True, text=True, timeout=60)
    if result.returncode:
        raise AssertionError(f"{args}: exit {result.returncode}\n{result.stdout}\n{result.stderr}")
    return result


def write_vcf(root, name, samples, rows):
    raw = root / (name + ".vcf")
    header = ["##fileformat=VCFv4.2", "##contig=<ID=1>", "##contig=<ID=22>",
              '##FORMAT=<ID=GT,Number=1,Type=String,Description="Genotype">',
              "#CHROM\tPOS\tID\tREF\tALT\tQUAL\tFILTER\tINFO\tFORMAT\t" + "\t".join(samples)]
    raw.write_text("\n".join(header + rows) + "\n")
    output = root / (name + ".vcf.gz")
    command("bcftools", "view", "-Oz1", "-o", output, raw)
    command("bcftools", "index", "-t", output)
    return output


def row(chrom, pos, genotypes):
    return f"{chrom}\t{pos}\t.\tA\tG\t.\tPASS\t.\tGT\t" + "\t".join(genotypes)


def body(path):
    with gzip.open(path, "rt") as source:
        return [line.rstrip("\n") for line in source if not line.startswith("#")]


class Arguments(unittest.TestCase):
    def test_local_threads_default_is_four(self):
        # Six is what the skill passes and what the gb10 requires; the default stays 4.
        required = [f"--{flag}=x" for flag in (
            "run", "target", "patient-id", "holdout-samples", "reference-samples", "phase3-dir",
            "map-dir", "typed-positions", "chromosomes", "seed", "out-dir")]
        self.assertEqual(impute_joint.parse_args(required).threads, "4")
        self.assertEqual(impute_joint.parse_args(required + ["--threads", "6"]).threads, "6")


@unittest.skipUnless(shutil.which("bcftools"), "bcftools is required")
class ChromosomeTargetIdentity(unittest.TestCase):
    @unittest.skipUnless(shutil.which("java") and BEAGLE.is_file(), "java and BEAGLE_JAR are required")
    def test_beagle_output_identical_after_chromosome_cut(self):
        rng = random.Random(193)
        with tempfile.TemporaryDirectory(prefix="impute-identity-") as directory:
            root = Path(directory)
            names = [f"REFERENCE_{i:02}" for i in range(64)]
            hold_names = names[:8]
            panel_rows, patient_rows, hold_rows = [], [], []
            # Recombining copies of eight synthetic haplotypes, with all sites polymorphic.
            donors = [(i % 8, (i * 3 + 1) % 8) for i in range(len(names))]
            for i in range(300):
                alleles = [rng.randrange(2) for _ in range(8)]
                alleles[0], alleles[1] = 0, 1
                genotypes = [f"{alleles[a]}|{alleles[b]}" for a, b in donors]
                pos = 1_000_000 + i * 1000
                panel_rows.append(row("22", pos, genotypes))
                if i % 3 == 0:
                    patient_rows.append(row("22", pos, [genotypes[17].replace("|", "/")]))
                    hold_rows.append(row("22", pos, [g.replace("|", "/") for g in genotypes[:8]]))
            patient_rows = [row("1", 1000 + i * 100, ["0/1"]) for i in range(12)] + patient_rows
            panel = write_vcf(root, "panel", names, panel_rows)
            patient = write_vcf(root, "patient", ["PATIENT"], patient_rows)
            hold = write_vcf(root, "hold", [impute_joint.HOLDOUT_PREFIX + n for n in hold_names], hold_rows)
            hold_list = root / "holdout.txt"
            hold_list.write_text("\n".join(hold_names) + "\n")
            gmap = root / "map.txt"
            gmap.write_text("22 . 0 1000000\n22 . 3 1300000\n")

            old = root / "whole-target.vcf.gz"
            command("bcftools", "merge", patient, hold, "-Oz1", "--threads", "6", "-o", old)
            cut = root / "chromosome-target.vcf.gz"
            impute_joint.merge_chromosome_target(str(patient), str(hold), "22", str(cut), "6")
            old_body, cut_body = body(old), body(cut)
            self.assertEqual({line.split("\t", 1)[0] for line in old_body}, {"1", "22"})
            self.assertEqual({line.split("\t", 1)[0] for line in cut_body}, {"22"})
            self.assertEqual([line for line in old_body if line.startswith("22\t")], cut_body)

            outputs = []
            for label, target in (("whole", old), ("cut", cut)):
                prefix = root / label
                command("java", "-Xmx512m", "-jar", BEAGLE, f"gt={target}", f"ref={panel}",
                        f"map={gmap}", f"out={prefix}", "chrom=22", "nthreads=6",
                        "seed=20261008", "gp=true", "impute=true", f"excludesamples={hold_list}")
                outputs.append(body(Path(str(prefix) + ".vcf.gz")))
            self.assertEqual(len(outputs[0]), 300)
            self.assertEqual(outputs[0], outputs[1], "Beagle record bytes changed after slicing")
            self.assertTrue(all(line.split("\t")[8] == "GT:DS:GP" for line in outputs[0]))
            self.assertTrue(all("DR2=" in line.split("\t")[7] for line in outputs[0]))
            self.assertTrue(all(len(line.split("\t")) == 18 for line in outputs[0]))
            print("IDENTITY: chromosome-only upload target; 300 sites, 9 samples; "
                  "GT/DS/GP/DR2/IMP and all record bytes identical; threads=6", flush=True)


if __name__ == "__main__":
    unittest.main()
