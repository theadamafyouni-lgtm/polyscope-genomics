#!/usr/bin/env python3
"""Build the parts of agent/refdata that are derived rather than downloaded, and check
each one against the sha256 of the working box's copy.

    python3 build_refdata.py --plan      # what it would do; builds nothing, hashes nothing
    python3 build_refdata.py             # build whatever is missing, checking as it goes
    python3 build_refdata.py --check     # sha256 every file this script is answerable for

fetch_refdata.py downloads, into agent/refdata and agent/refdata-downloads/. This
derives, from what it downloaded. Run it second, after `fetch_refdata.py --verify`
reports no problems. Both read refdata_recipe.json.

It never overwrites, moves or deletes a file it did not create in this run. Output that is
already present is checked, not rebuilt. Output that is present with the wrong content is
reported BAD and left alone: that step stops, the other steps still run, and the exit
status is 1. What to do about it is left to you. Files are written to a .part file or a
.build directory and renamed into place only after they match; the one exception is the
FASTA's .fai and .gzi, which htslib writes in place (a wrong one is reported BAD). A .part
or .build left by an interrupted run stops its step, naming it, before anything is read.

THE STEPS, IN ORDER

  manifest        agent/refdata/MANIFEST.json is checked, never built: it is tracked in git,
                  and it must stay the 32-record file the published runs had in every clone.
  plink2          bin/plink2, the plink2 member of plink2_linux_x86_64_20250116.zip.
                  PLINK v2.0.0-a.6.6LM 64-bit Intel (16 Jan 2025): the binary every
                  published run used, and the one agent/tools/ancestry.py calls. Made
                  executable, since a mode-644 plink2 is the costliest mistake this project
                  has made (setup.sh has the story).
  hgdp_1kgp       ancestry-ref/hgdp_1kgp/, the four GRCh37 members of
                  pgsc_HGDP+1kGP_v1.tar.zst (PGS Catalog, published 2023-10-05). The GRCh38
                  half and the tarball's other files are not extracted. Needs zstd, about
                  15 GB free, and a read of the whole tarball.
  hgdp_panel      ancestry-ref/hgdp_1kgp.panel: the .psam re-columned to
                  sample / pop / super_pop. ancestry.py build-ref --panel needs it.
  phase3_markers  ancestry-ref/phase3/chrN.vcf.gz.done, the empty completion markers
                  agent/tools/leaveout_dr2.sh looks for. Written only after the chromosome's
                  .vcf.gz and .tbi match the recipe's sha256; an existing marker is ok only
                  while both files are there at their recorded sizes.
  fasta           fasta/GRCh37.fa.gz, .fai and .gzi: Ensembl's gzip re-compressed with
                  `bgzip -@ 4` (the tabix package), then indexed by htslib through bcftools.
                  The uncompressed sequence is checked separately, so a bgzip that
                  compresses differently is reported as that and not as different data.
                  htslib 1.19 (Ubuntu 24.04) reproduces the working box's file bit for bit;
                  which bgzip the Mac used is not recorded.
  maps            maps/, the 27 members of plink.GRCh37.map.zip.
  jre             jre/, the 259 files under Contents/Home of the macOS aarch64 Temurin
                  21.0.11+10 JRE the Mac staged. The published runs had it in every clone.
                  It does not run on Linux, and no selection run starts Java.
  hgdp_basis      ancestry-ref/hgdp_basis/, from agent/tools/ancestry.py build-ref with the
                  parameters in the original's reference_build.json. About 10 min and 9 GB.
  sites_index     panel/sites_index.bin, which the select stage reads. Its builder,
                  agent/tools/panel_index.py, is gitignored here and kept private with the
                  benchmark data.
                  Without it the step reports the index missing and says where to get it.
  panel_snv       panel-snv/chrN.snv.vcf.gz and .tbi, N = 1..22: the phase3 VCFs filtered to
                  biallelic SNVs with exact duplicates collapsed, all 2,504 samples -- the
                  reference agent/tools/impute_joint.py --panel-dir reads, dropping each run's
                  hold-out at read time. `bcftools view --no-version -m2 -M2 -v snps |
                  bcftools norm --no-version -d exact -Oz1`, then `bcftools index -t`.
                  bcftools/htslib 1.19 (Ubuntu 24.04) reproduces the working box's files bit
                  for bit (checked on chr22). Another version may compress differently: the
                  step then STOPS on the first chromosome, leaving its .part files named, and
                  that is a different compression, not necessarily a different panel. About
                  6 h of CPU and 16 GB, one chromosome at a time, so several hours of wall
                  clock.
  liftover        bin/liftOver is checked, never built. setup.sh downloads UCSC's current
                  build, and the working box's copy matches no public build found, so any
                  other copy reports NOT-REPRODUCED.

WHAT CANNOT COME OUT IDENTICAL, AND IS REPORTED RATHER THAN HIDDEN

The working box's hgdp_basis was built on macOS (plink2 a.7.1 M1). No published run read
it: each built its own basis with this same script, binary and input, and for the 129
published runs that used these parameters the run manifests record sha256 values equal to
this script's Linux rebuild (SETUP.md section 8). Against the Mac copy, every data file
matches by sha256, prune.prune.in included, except two: ref_pca.eigenvec (1 of 78,840
printed values) and ref_pca.eigenvec.allele (about 36 of 9.2 million) differ in the sixth
significant digit. eigenvec.allele also changes with --threads and with the binary's SIMD
level, so a machine with another CPU count gives a sha256 nobody has seen. Such a file is
accepted as LINUX only if it matches a fingerprint of the Mac one: identical row IDs and
order, and 60 per-PC checksums within 1e-5 (every Linux rebuild seen is within 6e-8; one
printed value off by more than 1e-5 fails it). The logs, reference_build.json and
sites_index.json carry timestamps, a host and tool versions. All of these are shown as
LINUX or TEXT, not as ok, and make the exit status 2 instead of 0, as does NOT-REPRODUCED.

Not built here: ancestry-ref/build/ and build_p3chr1/ (derived from one patient's
positions; removed from every run clone) and the staging logs. SETUP.md section 8 has the
full list, and what to copy off a working box before wiping it.

EXIT STATUS

  0  every file this script is answerable for is present and identical
  1  something is missing, wrong, or failed to build
  2  everything identical except what the section above says cannot be (LINUX, TEXT,
     NOT-REPRODUCED)
"""

import argparse
import fcntl
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tarfile
import zipfile

REPO = os.path.dirname(os.path.abspath(__file__))
AGENT = os.path.join(REPO, "agent")
RECIPE = os.path.join(REPO, "refdata_recipe.json")
ANCESTRY_PY = os.path.join(AGENT, "tools", "ancestry.py")
PANEL_INDEX_PY = os.path.join(AGENT, "tools", "panel_index.py")
# ancestry.py calls this binary whatever --refdata says, so it is checked before use.
ANCESTRY_PLINK2 = os.path.join(AGENT, "refdata", "bin", "plink2")

HGDP_STEM = "GRCh37_HGDP+1kGP_ALL"

# ancestry.py build-ref, with the parameters recorded in hgdp_basis/reference_build.json.
BASIS_ARGS = ["--maf", "0.05", "--geno", "0.02",
              "--prune-window", "200", "--prune-step", "50", "--prune-r2", "0.2",
              "--pcs", "20", "--exclude-palindromic", "yes"]

# sha256 and size of every derived file not recorded in the recipe, from the working box's
# agent/refdata on 2026-09-24.
LEGACY_MANIFEST = ("268928a692386e10f2055bd0dcddd9b2d26fbdfc8b1d09cf5ba8fd28b4309cbe", 12369)
PANEL = ("7baa5d96cb6ddb8a3ccac5b361b054b95ed788043944c80ec665ea4d1f501235", 68200)
# sha256 of the uncompressed FASTA, which does not depend on the bgzip build.
FASTA_STREAM_SHA256 = "3a3872e7bdd1532fdbfbc3afd70c04ff8a12a109319ae050c91d962f32e16a4f"
# panel/sites_index. The .json carries a timestamp and the bcftools version, so only the
# .bin can match after a rebuild.
SITES_INDEX_BIN = ("29d3a34eb30647aed44c573373a90c1b48e915fd52920eb68b13dbcc749129a0", 392181824)
SITES_INDEX_JSON = "5e834f077c6403e5cbd8882790163639797b2db9e31f90aced7acdb19426c76b"
# panel-snv: {chromosome: ((sha256, bytes) of chrN.snv.vcf.gz, (sha256, bytes) of its .tbi)},
# from the working box on 2026-10-08, bcftools/htslib 1.19.
PANEL_SNV = {
    1: (("86ae988b37d2326f39094bc0900146096c03961c6516693e7d74db2b0e731f7b", 1282272335),
         ("1671c8f358db6148cb46f0b94ac5a95a2db4af3a56046d9b4abb346acd34aedf", 205980)),
    2: (("f855a1f3965c588cbec845eb05c12be3b9534d292d92cf5f198c8bfcca078866", 1383778211),
         ("55ce1568286cf03ca32eca451b11f1292c6bc73ca73331ce00b7b362b0f00927", 218580)),
    3: (("ea0dad33073254152d6afe3e055f22651f7581ed0e3d6793ccab7a664b0a787c", 1167723917),
         ("88b94be7652878852f57e09b92f966c55ab54e944de0a4beee690e0545d509d9", 179524)),
    4: (("3f3f37d623bc350ab3270491d1b82e2b605ad2e613b2a271d63fb4de3f3bfa9b", 1182787089),
         ("734fcc64985fa6ee6faa0acf82c7942cbd2476cd90e4aef549de5db4cfa898e4", 172423)),
    5: (("ed58b7dca27336b78004b6d73fe4b7ed8992a52792f54615b5604a8a74ed95c7", 1040995512),
         ("34ca900ffbdc825c0bb90086b66376d90089a3e43fdeafc0488a8fcc7d5ee201", 162728)),
    6: (("83c9739eb57c4c5783182b928f453d82696d19413ae41a2478b1716afd547350", 1059377579),
         ("f782fefe551d5bdc3b099c096963ce8cca8c6dcd8001d50e12b5dc707503bf65", 154081)),
    7: (("bae9180f28e812030a4ad0ab1dddbb62c168a62567bc126bb91d7e3fd8e2456f", 962595416),
         ("1fe5a6d42aeb107f15542595611b73f98d187fb34a9391535ead4481208e59ff", 143774)),
    8: (("019f73e5fb8b2765e39a9f734b219a26805766e2d2d45706812a596ed9427e41", 916624631),
         ("2c22a4928513e82dfd00ca9a3fc07f9b998c186c0bd4dd8416bcd9a4aaa30645", 131983)),
    9: (("e25c8381e7fd96e0aeb38aabe4a970edcb2029ef4d624586aebbbc9ad54510df", 713167898),
         ("d9db5bfcf7626d1fc4276d54b70af518c1106a2c0da3c0feec9ea95b066036a2", 109113)),
    10: (("654904cc5031aa0376b3a9f5597e839a3c3f6fd442bdafce449353216099f793", 820048454),
         ("5ac48f3c226f92796651cfda81400c3a00ba58b4a6f4d66554167a9dd5564228", 120266)),
    11: (("997c7e30dec71024a7d06a00eabddf4d6c8bbeeaa44a7a242e2eda865904eaa3", 812366413),
         ("b5e6046d61f35675633e6642f11eeea36b96550549903c2a9f2db3eea6de1768", 120441)),
    12: (("1bc207ac1cb3b179c6f6844bd18ddedf2258546936c78534f87865a6257948e2", 778913231),
         ("e653ef645f80dc56f72ccd8323860a89015d08031b4a59630f94f07327f24eec", 119306)),
    13: (("31e81096a1887a9379bf4ed05935eb910f5b7fdc1d728380a3b7bf551df6ceb3", 586387281),
         ("ff68376a5d2e60e7a2d86e8b0391c9a231a47c93d5b84f2157e289d880378c7e", 86708)),
    14: (("f9f23e0045a7c07c8974c1d74df0695245c93d405a078366d41bb9af013d82d5", 533145575),
         ("3a36e3c5e2dc9872609a1aa2d52079508a25e22550a8ef8eab073e7ca10127f0", 80570)),
    15: (("e8645bb8d377fcb8becbca7dfcf175c0231ecae6af327b1764f056ebbc9571e3", 481789520),
         ("37cf5142b37ee2840a68720a0385cb9461d90ad7fd21ef5f9cfd22c5fda0e1d3", 74301)),
    16: (("6cd70c070801b665795f6f3c1880a89dd73617f783e7ccfa10dfc77a87641d42", 525260449),
         ("a826eab97df363822fda33dea34afe0cb05feb3fa2cbc0c9a26999596d677b6c", 72370)),
    17: (("64350ff3e5185f0e7b2c82066dbc02facd3b55f7490853815dd46e3096921f8e", 453761726),
         ("cbd2ed5af01f94a2fb6876c6e162d2b0ae91be9e9f1c422ce926eb6c9e568bfa", 71032)),
    18: (("aac719307394514bb14d71016621b94bc9034d8fbb513bb14ca31560522469f8", 462741154),
         ("f3c855707317cd38b2f52a894757bb276dd6a208229b6d2b86c72b66f75753be", 68411)),
    19: (("166458ffb3e92ecba0d0f60bda720273ebf1833d64fbb5eb6cb9c85cd2b3410b", 378771757),
         ("4aebdae1300ad6b286e0156cdf45c8f957bfa2c3e73fc6d4125cd0c256b951c1", 50576)),
    20: (("9f6c4c054f7e25241832634b46f624d82e6542410bc26aebf7c6c4b130f19f9c", 362420233),
         ("b27cbd70825d56e682f597c3a4207a5559af09bcd4a40b90a362ca3d52311bc7", 54051)),
    21: (("2fcb90183457d17466d4cc7df956d03f2aad77c6845e7f907e63e596b48ba149", 231039556),
         ("0c68d22239a231728da20a70ceb0085e207a048fc6b8082440455866dd86a3a5", 32659)),
    22: (("eb4c47c6d0e0b5ae92b07b9043919bdfdf7758b56d351906d97ef11f9b06f317", 226294758),
         ("d298a6dd1b084b809c6bca22fde4167ac7fcb4042604d27b749766ae2acb07b3", 32375)),
}
# The working box's liftOver. Checked only: see step_liftover.
LIFTOVER = ("6529f91135d579f18d1d86681810cdd0150bf6d7531457baa79a6632706368c0", 30663104)

BASIS = "ancestry-ref/hgdp_basis"
# Must match exactly, on any machine.
BASIS_EXACT = {
    "nonpalindromic.ids":
        ("0bb446117581491c93898221aba8ee93e33fbc3c873d0ef3bbe9ab75ccc13f2c", 62486734),
    "prune.prune.in":
        ("64a05450cb77fb3e974c8b16312d0a7f3d0639794e7ffb6807600f30b61f039e", 2679865),
    "prune.prune.out":
        ("f0189bf6f0e7b6d8d8eb854c22a79ed0c27bfcdbd44170bab863819f5b89b7aa", 59806869),
    "ref_raw.pgen":
        ("32447bda05ea9208edff6f79f229689a4e2790c1e3eb0a8ca1dcc00c8f3e6737", 3811010841),
    "ref_raw.pvar":
        ("b6f974016007a126e2830aa30260066f4d7636f9da1fd7182857d74dc3ff9833", 919031698),
    "ref_raw.psam":
        ("c8bc08292123545a6d7724a0090a4dbe2b7fe3367835dc3d6f234c6eeaf7de3f", 120281),
    "ref_nonpal.pgen":
        ("69a14addbed85f2475e16f0d919e56aee151fd607ac82019344337024e1dacd2", 3242060016),
    "ref_nonpal.pvar":
        ("a2bc180b803f24bd7f4a0435a7ba996912e584f4a876a10cacde0d1b1fb5fa56", 776869929),
    "ref_nonpal.psam":
        ("c8bc08292123545a6d7724a0090a4dbe2b7fe3367835dc3d6f234c6eeaf7de3f", 120281),
    "ref_pruned.pgen":
        ("7af418db8141a3ac57700bd0326ad3ff9fa11bf1b76ffbfc60a1e77f38d42e99", 176466421),
    "ref_pruned.pvar":
        ("53fa83bea6de6f162c90f137709ed3b192f1509e0f4b0470c985dc69369c4a0b", 33409934),
    "ref_pruned.psam":
        ("c8bc08292123545a6d7724a0090a4dbe2b7fe3367835dc3d6f234c6eeaf7de3f", 120281),
    "ref_pca.afreq":
        ("32267816d6824e2ed61676449ea9ed8a7eb4dc8bb025c31d43a31ecc15eca988", 7427593),
    "ref_pca.eigenval":
        ("1af3ea6b6c1666844de4fb339dc3b4f647ac2237d81e14bd97678653e1c3e387", 156),
    "sample_labels.json":
        ("d571a93c517f7cd1c1301678b67f33eab37eb29588c3add73f3ca03900690f6f", 245572),
}
# The working box's (macOS) hash, and every hash a Linux rebuild has been seen to give.
# The same hash comes out of more than one configuration, so these say where each was
# seen, not which configuration a file came from.
BASIS_PCA = {
    "ref_pca.eigenvec": (
        "6052ec7aadca876961b3a46202af4cee95aac6031c5d792d01b339e691ecbb45", {
            "c013b0435da05688f84c5c8c43fe6dfcd141921782f40be9913776f4291d0a21":
                "a known Linux rebuild: every Linux build, version and thread count tried, "
                "and the per-run basis of 129 published runs",
        }),
    "ref_pca.eigenvec.allele": (
        "63102c2926e27f84529f43999d5d1f44e32a70ba3bb7c26eb0808a403d94dd73", {
            "075564827110662d01884b01bef544dd42932f3fc2942d6c7ceed9ff4191f9e3":
                "a known Linux rebuild, seen from the 64-bit build at 8 threads: this "
                "repo's plink2 on an 8-CPU box",
            "e8eaad1e8ae94f3362d9d93d087d18503ba7bb4deb79b0250aec048e923c2f84":
                "a known Linux rebuild, seen from AVX2 builds at 8 threads and the 64-bit "
                "build at 1, 2, 16 and 32 threads",
            "388bcb026677dc0c0e37925429bf4f943e118a09595e749854ba1fa7dd3afaa8":
                "a known Linux rebuild, seen from the 64-bit build at 7 threads",
            "0882b77596cc7088d493096867d12a1840c1137c4ab03b7ac20f7b9ea5dbb5e6":
                "a known Linux rebuild, seen from the AVX2 build at 7 threads and the "
                "64-bit build at 4 and 6",
            "20ce8a5e9633c69c6b6e2f806b52d6e9cfddde7ef7816dc9e84a596ffd917fd3":
                "a known Linux rebuild, seen from the AVX2 build at 1 thread",
        }),
}
# A fingerprint of each of the working box's PCA files, for a Linux rebuild whose sha256
# has not been seen before: (number of ID columns, sha256 of the ID columns of every line,
# (per-PC sum, and two per-PC sums weighted by a fixed function of the line number)).
# Row IDs and order must be identical, and each of the 60 sums within PCA_TOL of the
# original. Every Linux rebuild seen so far is within 6e-8, and any single printed value
# that is off by more than PCA_TOL moves the plain sum by more than PCA_TOL.
PCA_TOL = 1e-5
BASIS_PCA_FINGERPRINT = {
    "ref_pca.eigenvec": (1, "b4ef21ede233fb6e8f8f1878304de0e95f39cd8dc2c5542c9cdb61deaff66e80", (
        (0.00013140799999037875, 0.0030647297000080433, 0.0022844163081903596, 0.003662371129983289, 0.012753132689999154, -0.017842379248986837, 0.0006800663410053568, 0.016050068210004667, -0.0035966615710046335, -0.0016770601489963458, -0.0011920710749966264, -0.0030542263599979413, 0.004678062606003008, 0.0029780264659957645, 0.004011417095988594, -0.0006264927203971406, 0.0008647878149954215, -0.001058842619999401, -0.0011968300900006318, 0.0030140789099994533),
        (0.03873535334339974, -0.05848850854217838, -0.021257440179826136, -0.03491726015268598, -0.0179490688947613, 0.23283652307692412, 0.2691213749365026, 0.10549532285786488, -0.3096363260051652, -0.1114147249002143, 0.017358925341932757, -0.01050457608130119, 0.14887871674403141, 0.09407187905210927, 0.23100528099513076, 0.0581419830375164, -0.2754995704110455, 0.2076219382803635, -0.146351433032403, -0.28980178095898596),
        (0.05764329915828525, 0.07675551247818614, 0.09378184127208405, -0.05485607550302634, -0.09029629793831097, -0.06453745475430737, -0.06549295802765968, -0.049575077761168325, 0.1027718540339155, 0.06426104861063106, 0.07419826916136461, 0.2587922440143343, -0.061390473165492905, -0.08456011841310433, 0.17513721829242138, -0.29393578718469965, -0.10620951581349025, -0.0628270011094544, 0.022444410642940584, 0.036310156174556525),
    )),
    "ref_pca.eigenvec.allele": (5, "db3c06115c36a046492b2cb927193fe4b50f2e8749c5fd461a267b5d4a2d3c9d", (
        (0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0),
        (135.216767498562, -66.58771914220465, 8.98313651282822, 215.24519491972538, 14.465145573267959, -11.316642717947094, 20.18123472644655, 11.649214448442365, 88.8656374955636, 142.63346165881507, 114.95132613526648, 8.89467380758614, 120.10444836436653, 111.27639602187203, -97.34797204221208, 35.75944054474461, 59.172350711432344, -193.5547833358803, -56.334713113541, -161.46715737586587),
        (27.47315707445751, -163.05788693901934, -45.265162337885904, 209.5228252484633, 41.66586898316707, -1.3356744902117774, -41.28846411511859, -188.08222688817716, 14.734140218110515, 62.660956116198726, 44.3456642588771, -57.40478371669992, 38.196843753601996, -90.78347339352615, -85.22525345606212, 45.17174084340193, 57.41173274179857, 77.71487283097109, -8.066392913644716, 44.95710284602805),
    )),
}
# Carry timestamps, a host, paths and a version banner, so they cannot match after a
# rebuild. These are the working box's copies, which were path-edited on 2026-09-23.
BASIS_TEXT = {
    "reference_build.json": "269e95c453b6e05a44d836cf7f48955a3b87c36d0f72a30c43ed45da11ce8661",
    "plink.log": "08dbd18e9b54a4c89c66591124dd401f413bccf004cc3d4455db4ee9f65a5fcf",
    "ref_raw.log": "27c4d66c3d5bf866bb0be3a4bb789e53762439c6b521b4261454d5ce842a41b3",
    "ref_nonpal.log": "416701c1aecdf874258747d13ff245bcae252263b26711d74b03e5181b6baa30",
    "prune.log": "fa9f2e2b1bf213274b0c1ff70b69a8b12b5d42063064571afd5e368c735b4962",
    "ref_pruned.log": "1c58cc0adc6983463664bee4604494458cd0b66ab6b33651b39c68e484c15fbd",
    "ref_pca.log": "2c2b133e66159a0c624c11f18a848f5c150a86107978e222590b2e6d3494ab41",
}

GB = 1000 ** 3


class Stop(Exception):
    """A step cannot go on. The message says why and what was left behind."""


def sha256_of(path):
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 22), b""):
            h.update(chunk)
    return h.hexdigest()


def state(path, sha, size):
    """ok / missing / BAD. Hashes the file unless the size already disagrees."""
    if not os.path.isfile(path):
        return "missing"
    if size is not None and os.path.getsize(path) != size:
        return "BAD"
    return "ok" if sha256_of(path) == sha else "BAD"


def need_free(path, nbytes, what):
    d = path
    while not os.path.isdir(d):
        d = os.path.dirname(d)
    free = shutil.disk_usage(d).free
    if free < nbytes:
        raise Stop("%s needs about %.0f GB free under %s; there is %.1f GB."
                   % (what, nbytes / GB, d, free / GB))


def need_tools(*names):
    missing = [n for n in names if shutil.which(n) is None]
    if missing:
        raise Stop("needs %s on PATH (apt install %s)" % (
            ", ".join(missing),
            " ".join(sorted({"bgzip": "tabix", "tabix": "tabix"}.get(n, n) for n in missing))))


def refuse_stale(*paths):
    """A .part or .build left by an interrupted run: name it, before reading anything."""
    stale = [p for p in paths if os.path.lexists(p)]
    if stale:
        raise Stop("left by an interrupted run: %s. Delete it and rerun." % ", ".join(stale))


def short(rel):
    """A recipe path as the other labels show it: relative to the refdata root."""
    return rel[len("refdata/"):] if rel.startswith("refdata/") else rel


class Refdata:
    def __init__(self, root, recipe):
        self.root = os.path.abspath(root)
        if not os.path.isfile(recipe):
            sys.exit("no recipe at %s. It is tracked in this repository; restore it with "
                     "git checkout refdata_recipe.json" % recipe)
        with open(recipe) as fh:
            self.arts = {a["key"]: a for a in json.load(fh).get("artifacts") or []}
        self.failed = set()

    def p(self, rel):
        """A recipe path. refdata/... is under the refdata root; anything else (the
        refdata-downloads/ archives) sits beside it, as agent/ holds both."""
        if rel.startswith("refdata/"):
            return os.path.join(self.root, rel[len("refdata/"):])
        return os.path.join(os.path.dirname(self.root), rel)

    def entry(self, key, *fields):
        a = self.arts.get(key)
        if a is None or any(f not in a for f in fields):
            raise Stop("refdata_recipe.json has no %r record%s; this script needs the "
                       "recipe of 2026-09-24 or later."
                       % (key, " with " + ", ".join(fields) if fields else ""))
        return a

    def archive(self, key):
        """The fetched archive for recipe record `key`, hash-checked before use."""
        a = self.entry(key)
        path = self.p(a["path"])
        if not os.path.isfile(path):
            raise Stop("%s is not here. Fetch it first:  python3 fetch_refdata.py" % a["path"])
        print("  checking %s against the recipe (%.1f GB) ..."
              % (os.path.basename(path), os.path.getsize(path) / GB))
        if state(path, a["sha256"], a.get("bytes")) != "ok":
            raise Stop("%s does not match the recipe's sha256. Not using it." % path)
        return path


def write_verified(dst, chunks, sha, size, label):
    """Stream chunks to dst.part, check sha256 and size, then rename into place. A .part
    this call created is removed if anything goes wrong, interruption included."""
    tmp = dst + ".part"
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    h = hashlib.sha256()
    n = 0
    out = open(tmp, "xb")   # outside the try: a .part someone else made is not ours
    try:
        with out:
            for buf in chunks:
                out.write(buf)
                h.update(buf)
                n += len(buf)
    except BaseException:
        os.unlink(tmp)
        raise
    if n != size or h.hexdigest() != sha:
        os.unlink(tmp)
        raise Stop("%s: got %d bytes with sha256 %s; expected %d bytes with sha256 %s. "
                   "Nothing was put in place." % (label, n, h.hexdigest(), size, sha))
    os.rename(tmp, dst)


def report(rows):
    for st, label in rows:
        print("  %-8s %s" % (st, label))


def refuse_if_bad(rows, what):
    if any(st == "BAD" for st, _ in rows):
        raise Stop("%s: a file is present with the wrong content. Nothing was overwritten; "
                   "move it aside yourself if you want it rebuilt." % what)


def unpack_members(rd, mode, key, opener):
    """Shared by plink2, maps and jre: members of an archive, each to its dest.
    opener(archive_path) yields (member_name, file_object) for the members wanted."""
    a = rd.entry(key)
    if "members" not in a and "member" not in a:
        raise Stop("refdata_recipe.json's %r record lists no members; this script needs "
                   "the recipe of 2026-09-24 or later." % key)
    members = a["members"] if "members" in a else {a["member"]["name"]: a["member"]}
    if mode == "plan":
        n = sum(os.path.exists(rd.p(m["dest"])) for m in members.values())
        what = short(next(iter(members.values()))["dest"]) if len(members) == 1 else \
            "%s  (%d of %d members present)" % (os.path.dirname(short(
                next(iter(members.values()))["dest"])) + "/", n, len(members))
        return [("present" if n == len(members) else "planned", what)]
    rows = [(state(rd.p(m["dest"]), m["sha256"], m["bytes"]), short(m["dest"]))
            for m in members.values()]
    if mode == "check" or all(st == "ok" for st, _ in rows):
        return rows
    refuse_if_bad(rows, key)
    todo = {n for (st, _), n in zip(rows, members) if st == "missing"}
    refuse_stale(*(rd.p(members[n]["dest"]) + ".part" for n in todo))
    for name, src in opener(rd.archive(key), todo):
        m = members[name]
        write_verified(rd.p(m["dest"]), iter(lambda: src.read(1 << 22), b""),
                       m["sha256"], m["bytes"], short(m["dest"]))
        todo.discard(name)
    if todo:
        raise Stop("%s: the archive has no member named %s" % (key, sorted(todo)))
    return [(state(rd.p(m["dest"]), m["sha256"], m["bytes"]), short(m["dest"]))
            for m in members.values()]


def zip_opener(path, names):
    with zipfile.ZipFile(path) as zf:
        for name in sorted(names):
            with zf.open(name) as src:
                yield name, src


def tar_opener(path, names):
    with tarfile.open(path, "r:*") as tf:
        for ti in tf:
            if ti.isfile() and ti.name in names:
                yield ti.name, tf.extractfile(ti)


# --------------------------------------------------------------------------- steps
# Each step takes (rd, mode) with mode in plan / build / check and returns a list of
# (state, label) rows. state is ok, missing, BAD, LINUX, TEXT, NOT-REPRODUCED, present or
# planned.

def step_manifest(rd, mode):
    rel = "MANIFEST.json"
    path = rd.p("refdata/" + rel)
    if mode == "plan":
        return [("present" if os.path.exists(path) else "missing", rel + "  (checked, never built)")]
    st = state(path, *LEGACY_MANIFEST)
    if st == "ok":
        return [(st, rel)]
    return [(st, rel + "  (must be the published runs' copy: git checkout "
                       "agent/refdata/MANIFEST.json)")]


def step_plink2(rd, mode):
    rows = unpack_members(rd, mode, "plink2", zip_opener)
    if mode == "plan":
        return rows
    dst = rd.p(rd.entry("plink2", "member")["member"]["dest"])
    if rows[0][0] == "ok" and not os.access(dst, os.X_OK):
        if mode == "check":
            return [("BAD", rows[0][1] + "  (right bytes, not executable)")]
        os.chmod(dst, 0o755)   # a mode change, not a content overwrite
        return [("ok", rows[0][1] + "  (made executable)")]
    return rows


def step_hgdp_1kgp(rd, mode):
    a = rd.entry("hgdp_1kgp_tarball", "members")
    members = a["members"]
    dsts = {name: rd.p(m["dest"]) for name, m in members.items()}
    if mode == "plan":
        return [("present" if os.path.exists(d) else "planned", short(members[n]["dest"]))
                for n, d in dsts.items()]
    rows = [(state(dsts[n], m["sha256"], m["bytes"]), short(m["dest"]))
            for n, m in members.items()]
    if mode == "check" or all(st == "ok" for st, _ in rows):
        return rows
    refuse_if_bad(rows, "hgdp_1kgp")
    todo = {n for (st, _), n in zip(rows, members) if st == "missing"}
    refuse_stale(*(dsts[n] + ".part" for n in todo))
    need_tools("zstd")
    need_free(dsts[next(iter(todo))], sum(members[n]["bytes"] for n in todo) + GB,
              "extracting hgdp_1kgp")
    tarball = rd.archive("hgdp_1kgp_tarball")
    print("  extracting %d member(s); this reads through the whole tarball" % len(todo))
    zstd = subprocess.Popen(["zstd", "-dc", "--", tarball], stdout=subprocess.PIPE)
    got = set()
    try:
        with tarfile.open(fileobj=zstd.stdout, mode="r|") as tf:
            for ti in tf:
                if ti.name not in todo or not ti.isfile():
                    continue
                m = members[ti.name]
                src = tf.extractfile(ti)
                write_verified(dsts[ti.name], iter(lambda: src.read(1 << 22), b""),
                               m["sha256"], m["bytes"], short(m["dest"]))
                os.utime(dsts[ti.name], (ti.mtime, ti.mtime))
                got.add(ti.name)
                print("    %-40s extracted, sha256 matches" % ti.name)
                if got == todo:
                    break
    finally:
        zstd.stdout.close()
        zstd.kill()
        zstd.wait()
    if got != todo:
        raise Stop("hgdp_1kgp: the tarball has no member named %s" % sorted(todo - got))
    return [(state(dsts[n], m["sha256"], m["bytes"]), short(m["dest"]))
            for n, m in members.items()]


def panel_bytes(psam):
    """hgdp_1kgp.panel from the .psam: IID, Population, SuperPop under a new header.

    Equivalent to  awk 'BEGIN{FS=OFS="\\t"} NR==1{print "sample","pop","super_pop";next}
    {print $1,$4,$3}'  over the .psam, which is how the original was reproduced."""
    out = []
    with open(psam, newline="") as fh:
        header = fh.readline().rstrip("\n").split("\t")
        if header[:4] != ["#IID", "SEX", "SuperPop", "Population"]:
            raise Stop("%s: unexpected header %r" % (psam, header))
        out.append("sample\tpop\tsuper_pop\n")
        for line in fh:
            f = line.rstrip("\n").split("\t")
            out.append("%s\t%s\t%s\n" % (f[0], f[3], f[2]))
    return "".join(out).encode()


def step_hgdp_panel(rd, mode):
    rel = "ancestry-ref/hgdp_1kgp.panel"
    dst = rd.p("refdata/" + rel)
    if mode == "plan":
        return [("present" if os.path.exists(dst) else "planned", rel)]
    rows = [(state(dst, *PANEL), rel)]
    if mode == "check" or rows[0][0] == "ok":
        return rows
    refuse_if_bad(rows, "hgdp_panel")
    refuse_stale(dst + ".part")
    m = rd.entry("hgdp_1kgp_tarball", "members")["members"][HGDP_STEM + ".psam"]
    psam = rd.p(m["dest"])
    if state(psam, m["sha256"], m["bytes"]) != "ok":
        raise Stop("hgdp_panel: %s is missing or not the published .psam; run the "
                   "hgdp_1kgp step first" % psam)
    write_verified(dst, [panel_bytes(psam)], PANEL[0], PANEL[1], rel)
    return [("ok", rel + "  (written)")]


def step_phase3_markers(rd, mode):
    rows = []
    for n in range(1, 23):
        vcf, tbi = rd.entry("phase3_chr%d" % n), rd.entry("phase3_chr%d_tbi" % n)
        done_rel = "ancestry-ref/phase3/chr%d.vcf.gz.done" % n
        done = rd.p("refdata/" + done_rel)
        if os.path.exists(done):
            gone = [short(e["path"]) for e in (vcf, tbi)
                    if not os.path.isfile(rd.p(e["path"]))
                    or os.path.getsize(rd.p(e["path"])) != e["bytes"]]
            if os.path.getsize(done) != 0:
                rows.append(("BAD", done_rel + "  (not empty)"))
            elif gone and mode != "plan":
                rows.append(("BAD", done_rel + "  (vouches for %s, which is missing or the "
                             "wrong size)" % ", ".join(gone)))
            else:
                rows.append(("ok", done_rel))
            continue
        if mode == "plan":
            rows.append(("planned", done_rel))
            continue
        if mode == "check":
            rows.append(("missing", done_rel))
            continue
        # The original fetch ran a size-proportional step before each marker; this hashes.
        bad = [short(e["path"]) for e in (vcf, tbi)
               if state(rd.p(e["path"]), e["sha256"], e["bytes"]) != "ok"]
        if bad:
            rows.append(("missing", done_rel + "  (not written: %s missing or wrong; "
                         "fetch_refdata.py --verify)" % ", ".join(bad)))
            continue
        if os.path.getmtime(rd.p(tbi["path"])) < os.path.getmtime(rd.p(vcf["path"])):
            print("  note: chr%d .tbi is older than its .vcf.gz; htslib will warn "
                  "'index file is older than the data file'. The contents are correct." % n)
        with open(done, "x"):
            pass
        rows.append(("ok", done_rel + "  (written)"))
    return rows


def step_fasta(rd, mode):
    ys = rd.entry("fasta", "yields")["yields"]
    fa_y, fai_y, gzi_y = ys["GRCh37.fa.gz"], ys["GRCh37.fa.gz.fai"], ys["GRCh37.fa.gz.gzi"]
    ylist = [fa_y, fai_y, gzi_y]
    fa = rd.p(fa_y["dest"])
    if mode == "plan":
        return [("present" if os.path.exists(rd.p(y["dest"])) else "planned", short(y["dest"]))
                for y in ylist]
    rows = [(state(rd.p(y["dest"]), y["sha256"], y["bytes"]), short(y["dest"])) for y in ylist]
    if mode == "check" or all(st == "ok" for st, _ in rows):
        return rows
    refuse_if_bad(rows, "fasta")
    indexes = [rd.p(y["dest"]) for y in (fai_y, gzi_y)]
    if sum(os.path.exists(p) for p in indexes) == 1:
        # htslib writes both indexes together; with one already there it would not.
        raise Stop("fasta: only one of .fai and .gzi is present. Move it aside and rerun.")
    need_tools("gunzip", "bgzip", "bcftools")
    if not os.path.exists(fa):
        tmp = fa + ".part"
        refuse_stale(tmp)
        src = rd.archive("fasta")
        os.makedirs(os.path.dirname(fa), exist_ok=True)
        print("  gunzip -c | bgzip -@ 4 -c  (a few minutes)")
        stream = hashlib.sha256()
        gz = bg = None
        out = open(tmp, "xb")   # outside the try: a .part someone else made is not ours
        try:
            with out:
                gz = subprocess.Popen(["gunzip", "-c", src], stdout=subprocess.PIPE)
                bg = subprocess.Popen(["bgzip", "-@", "4", "-c"], stdin=subprocess.PIPE,
                                      stdout=out)
                for chunk in iter(lambda: gz.stdout.read(1 << 22), b""):
                    stream.update(chunk)
                    bg.stdin.write(chunk)
                bg.stdin.close()
                rc = (gz.wait(), bg.wait())
        except BaseException:
            for proc in (gz, bg):
                if proc is not None:
                    proc.kill()
                    proc.wait()
            os.unlink(tmp)
            raise
        if rc != (0, 0) or stream.hexdigest() != FASTA_STREAM_SHA256:
            os.unlink(tmp)
            raise Stop("fasta: the uncompressed sequence does not match (sha256 %s, exit "
                       "codes %s). Nothing was put in place." % (stream.hexdigest(), rc))
        got = sha256_of(tmp)
        if got != fa_y["sha256"] or os.path.getsize(tmp) != fa_y["bytes"]:
            os.unlink(tmp)
            raise Stop("fasta: the sequence is identical, but this bgzip compresses it "
                       "differently (sha256 %s, expected %s). htslib 1.19 (Ubuntu 24.04) "
                       "reproduces it. Nothing was put in place." % (got, fa_y["sha256"]))
        os.rename(tmp, fa)
        print("  fasta/GRCh37.fa.gz written; sequence and compression both match")
    if not any(os.path.exists(p) for p in indexes):
        # htslib writes .fai and .gzi itself the first time something opens the FASTA,
        # which is how the originals were made. They are written in place.
        empty_vcf = b"##fileformat=VCFv4.2\n#CHROM\tPOS\tID\tREF\tALT\tQUAL\tFILTER\tINFO\n"
        subprocess.run(["bcftools", "norm", "-f", fa, "-Ov", "-o", os.devnull, "-"],
                       input=empty_vcf, check=True, capture_output=True)
    return [(state(rd.p(y["dest"]), y["sha256"], y["bytes"]), short(y["dest"])) for y in ylist]


def step_maps(rd, mode):
    return unpack_members(rd, mode, "maps", zip_opener)


def step_jre(rd, mode):
    return unpack_members(rd, mode, "jre", tar_opener)


def pca_fingerprint(path, n_id):
    """(sha256 of the ID columns of every line, (per-PC sums, and two weighted sums))."""
    ids = hashlib.sha256()
    sums = None
    with open(path) as fh:
        for i, line in enumerate(fh):
            f = line.rstrip("\n").split("\t")
            ids.update(("\t".join(f[:n_id]) + "\n").encode())
            if i == 0:
                sums = [[0.0] * (len(f) - n_id) for _ in range(3)]
                continue
            a = ((i * 2654435761) % 65536) / 65536.0 - 0.5
            b = ((i * 40503 + 12345) % 65521) / 65521.0 - 0.5
            s, w1, w2 = sums
            for j, x in enumerate(f[n_id:]):
                v = float(x)
                s[j] += v
                w1[j] += a * v
                w2[j] += b * v
    return ids.hexdigest(), sums


def near_original(path, name):
    """True if a PCA file equals the working box's up to last-digit rounding."""
    n_id, ids_want, sums_want = BASIS_PCA_FINGERPRINT[name]
    try:
        ids_got, sums_got = pca_fingerprint(path, n_id)
    except (ValueError, IndexError, TypeError) as exc:
        return False, "unreadable as a plink2 PCA file (%s)" % exc
    if ids_got != ids_want:
        return False, "row IDs or their order differ from the original"
    if len(sums_got[0]) != len(sums_want[0]):
        return False, "a different number of PCs"
    worst = max(abs(g - w) for G, W in zip(sums_got, sums_want) for g, w in zip(G, W))
    if worst > PCA_TOL:
        return False, ("differs from the original by more than rounding "
                       "(largest checksum difference %.3g > %g)" % (worst, PCA_TOL))
    return True, ("identical row IDs and order; 60 per-PC checksums within %.1g (largest "
                  "difference %.2g)" % (PCA_TOL, worst))


def basis_rows(d):
    """Check a hgdp_basis directory. EXACT files: ok/missing/BAD. The two PCA files: ok if
    they equal the working box's macOS output; LINUX if they equal a known Linux output,
    or match the original's fingerprint within rounding; else BAD. Logs and
    reference_build.json: ok if unchanged, else TEXT."""
    rows = []
    for name, (sha, size) in sorted(BASIS_EXACT.items()):
        rows.append((state(os.path.join(d, name), sha, size), "%s/%s" % (BASIS, name)))
    for name, (original, linux) in sorted(BASIS_PCA.items()):
        path = os.path.join(d, name)
        label = "%s/%s" % (BASIS, name)
        if not os.path.isfile(path):
            rows.append(("missing", label))
            continue
        got = sha256_of(path)
        if got == original:
            rows.append(("ok", label))
        elif got in linux:
            rows.append(("LINUX", "%s  (%s; differs from the Mac-built original in the "
                                  "6th significant digit)" % (label, linux[got])))
        else:
            near, why = near_original(path, name)
            if near:
                rows.append(("LINUX", "%s  (sha256 %s... not seen before, but equal to the "
                                      "Mac-built original within rounding: %s)"
                                      % (label, got[:12], why)))
            else:
                rows.append(("BAD", "%s  (sha256 %s: %s)" % (label, got, why)))
    for name, sha in sorted(BASIS_TEXT.items()):
        path = os.path.join(d, name)
        label = "%s/%s" % (BASIS, name)
        if not os.path.isfile(path):
            rows.append(("missing", label))
        elif sha256_of(path) == sha:
            rows.append(("ok", label))
        else:
            rows.append(("TEXT", label + "  (timestamps, host, version banner)"))
    return rows


def step_hgdp_basis(rd, mode):
    d = rd.p("refdata/" + BASIS)
    stage = d + ".build"
    if mode == "plan":
        if os.path.exists(d):
            return [("present", BASIS + "/")]
        return [("planned", "agent/tools/ancestry.py build-ref into %s.build/, then "
                            "rename to %s/" % (BASIS, BASIS))]
    if os.path.exists(d) or mode == "check":
        return basis_rows(d) if os.path.exists(d) else [("missing", BASIS + "/")]
    upstream = rd.failed & {"plink2", "hgdp_1kgp", "hgdp_panel"}
    if upstream:
        raise Stop("not run, because %s stopped earlier in this run" % ", ".join(sorted(upstream)))
    refuse_stale(stage)
    m = rd.entry("plink2", "member")["member"]
    if state(ANCESTRY_PLINK2, m["sha256"], m["bytes"]) != "ok" \
            or not os.access(ANCESTRY_PLINK2, os.X_OK):
        raise Stop("ancestry.py runs %s, which is not the pinned, executable plink2 (%s). "
                   "Run the plink2 step against agent/refdata first."
                   % (ANCESTRY_PLINK2, m["version"]))
    members = rd.entry("hgdp_1kgp_tarball", "members")["members"]
    wrong = [short(x["dest"]) for x in members.values()
             if not os.path.isfile(rd.p(x["dest"])) or os.path.getsize(rd.p(x["dest"])) != x["bytes"]]
    if state(rd.p("refdata/ancestry-ref/hgdp_1kgp.panel"), *PANEL) != "ok":
        wrong.append("ancestry-ref/hgdp_1kgp.panel")
    if wrong:
        raise Stop("hgdp_basis: inputs missing or wrong: %s" % ", ".join(wrong))
    need_free(stage, 10 * GB, "building hgdp_basis")
    cmd = [sys.executable, ANCESTRY_PY, "build-ref",
           "--ref-pfile", rd.p("refdata/ancestry-ref/hgdp_1kgp/" + HGDP_STEM),
           "--panel", rd.p("refdata/ancestry-ref/hgdp_1kgp.panel")] + BASIS_ARGS + \
          ["--out-dir", stage, "--keep-intermediates"]  # BASIS_EXACT checks ref_raw/ref_nonpal
    print("  " + " ".join(cmd))
    print("  (about 10 minutes on 8 CPUs)")
    try:
        subprocess.run(cmd, check=True, env=dict(os.environ, PYTHONDONTWRITEBYTECODE="1"))
    except BaseException:
        # This run created the stage directory; a half-built one only blocks the next run.
        shutil.rmtree(stage, ignore_errors=True)
        raise
    rows = basis_rows(stage)
    if any(st in ("BAD", "missing") for st, _ in rows):
        report(rows)
        raise Stop("hgdp_basis: built into %s, but the files above marked BAD or missing "
                   "do not match. It was NOT moved into place." % stage)
    os.rename(stage, d)
    return rows


def step_sites_index(rd, mode):
    """panel/sites_index.bin, which the select stage reads (weight_coverage.py)."""
    rel = "panel/sites_index.bin"
    sha, size = SITES_INDEX_BIN
    dst = rd.p("refdata/" + rel)
    js = rd.p("refdata/panel/sites_index.json")
    no_script = ("  (not built: agent/tools/panel_index.py is not in this repository. Copy "
                 "it in from the benchmark data, which is kept private, and rerun)")
    if mode == "plan":
        if os.path.exists(dst):
            return [("present", rel)]
        return [("planned", rel)] if os.path.isfile(PANEL_INDEX_PY) else [("missing", rel + no_script)]
    stem = rd.p("refdata/panel/sites_index.build")
    rows = [(state(dst, sha, size), rel)]
    if os.path.isfile(js):
        same = sha256_of(js) == SITES_INDEX_JSON
        rows.append(("ok" if same else "TEXT", "panel/sites_index.json"
                     + ("" if same else "  (timestamp, bcftools version)")))
    elif rows[0][0] != "missing":
        # weight_coverage.py opens the .json before the .bin; one without the other is broken
        rows.append(("missing", "panel/sites_index.json  (the .bin is here without it)"))
    if rows[0][0] == "missing" and not os.path.isfile(PANEL_INDEX_PY):
        return [("missing", rel + no_script)] + rows[1:]
    if mode == "check" or all(st in ("ok", "TEXT") for st, _ in rows):
        return rows
    if rows[0][0] == "ok":
        refuse_stale(stem + ".bin", stem + ".json")
        raise Stop("sites_index: the .json is missing beside a correct .bin. Move the .bin "
                   "aside and rerun to rebuild both.")
    refuse_if_bad(rows, "sites_index")
    if os.path.exists(js):
        raise Stop("sites_index: the .json is here without its .bin. Move it aside.")
    s = rd.entry("sites_vcf")
    sites = rd.p(s["path"])
    if state(sites, s["sha256"], s["bytes"]) != "ok":
        raise Stop("sites_index: %s is missing or does not match the recipe" % sites)
    refuse_stale(stem + ".bin", stem + ".json")
    need_tools("bcftools")
    # Run from agent/ with the path the original was built with, so the .json records
    # the same sites_vcf string when building into the live tree.
    live = rd.root == os.path.join(AGENT, "refdata")
    sites_arg = "refdata/panel/1kg.phase3.v5b.sites.b37.vcf.gz" if live else sites
    print("  python3 tools/panel_index.py --sites %s  (a few minutes)" % sites_arg)
    try:
        subprocess.run([sys.executable, PANEL_INDEX_PY, "--sites", sites_arg, "--out", stem],
                       check=True, cwd=AGENT, env=dict(os.environ, PYTHONDONTWRITEBYTECODE="1"))
    except BaseException:
        for p in (stem + ".bin", stem + ".json"):
            if os.path.exists(p):
                os.unlink(p)
        raise
    if state(stem + ".bin", sha, size) != "ok":
        raise Stop("sites_index: rebuilt %s.bin does not match (sha256 %s). It was NOT "
                   "moved into place." % (stem, sha256_of(stem + ".bin")))
    # .json first: an interruption between the two then leaves a .json without its .bin,
    # which the next run refuses, rather than a .bin that looks complete.
    os.rename(stem + ".json", js)
    os.rename(stem + ".bin", dst)
    same = sha256_of(js) == SITES_INDEX_JSON
    return [("ok", rel + "  (built)"),
            ("ok" if same else "TEXT", "panel/sites_index.json  (timestamp, bcftools version)")]


def step_panel_snv(rd, mode):
    rows = []
    for n in range(1, 23):
        rel = "panel-snv/chr%d.snv.vcf.gz" % n
        dst = rd.p("refdata/" + rel)
        tbi = dst + ".tbi"
        (vsha, vsize), (tsha, tsize) = PANEL_SNV[n]
        if mode == "plan":
            rows.append(("present" if os.path.exists(dst) else "planned", rel))
            continue
        refuse_stale(dst + ".part", tbi + ".part")  # named before anything is read
        have = [(state(dst, vsha, vsize), rel), (state(tbi, tsha, tsize), rel + ".tbi")]
        if mode == "check" or all(st == "ok" for st, _ in have) or any(
                st == "BAD" for st, _ in have):
            rows.extend(have)
            continue
        if os.path.exists(dst) or os.path.exists(tbi):
            # One of the pair without the other: reported, not rebuilt. Move the one that is
            # here aside and rerun to build both.
            rows.extend(have)
            continue
        src = rd.entry("phase3_chr%d" % n)
        if state(rd.p(src["path"]), src["sha256"], src["bytes"]) != "ok":
            rows.append(("missing", rel + "  (not built: %s missing or wrong; "
                         "fetch_refdata.py --verify)" % short(src["path"])))
            continue
        need_tools("bcftools")
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        print("  chr%d: bcftools view -m2 -M2 -v snps | norm -d exact  (minutes to half an hour)" % n)
        view = norm = None
        try:
            view = subprocess.Popen(["bcftools", "view", "--no-version", "-m2", "-M2", "-v",
                                     "snps", rd.p(src["path"]), "-Ou"], stdout=subprocess.PIPE)
            norm = subprocess.Popen(["bcftools", "norm", "--no-version", "-d", "exact", "-Oz1",
                                     "-o", dst + ".part"], stdin=view.stdout)
            view.stdout.close()  # so view sees SIGPIPE, not a blocked write, if norm dies
            if norm.wait() != 0 or view.wait() != 0:
                raise Stop("chr%d: bcftools view | norm failed" % n)
            subprocess.run(["bcftools", "index", "-t", "-o", tbi + ".part", dst + ".part"],
                           check=True)
        except BaseException:
            for proc in (view, norm):
                if proc is not None and proc.poll() is None:
                    proc.kill()
                    proc.wait()
            for p in (dst + ".part", tbi + ".part"):
                if os.path.exists(p):
                    os.unlink(p)
            raise
        got = [state(dst + ".part", vsha, vsize), state(tbi + ".part", tsha, tsize)]
        if got != ["ok", "ok"]:
            raise Stop("chr%d: rebuilt panel does not match (sha256 %s); left as %s.part. "
                       "A bcftools/htslib other than 1.19 compresses differently."
                       % (n, sha256_of(dst + ".part"), rel))
        os.rename(tbi + ".part", tbi)
        os.rename(dst + ".part", dst)
        rows.extend([("ok", rel + "  (built)"), ("ok", rel + ".tbi  (built)")])
    return rows


def step_liftover(rd, mode):
    """bin/liftOver: checked, never built. setup.sh downloads UCSC's current build, which
    changed on 2026-09-09; the working box's copy matches no public build found."""
    rel = "bin/liftOver"
    path = rd.p("refdata/" + rel)
    if mode == "plan":
        return [("present" if os.path.exists(path) else "missing", rel + "  (checked, never built)")]
    st = state(path, *LIFTOVER)
    if st == "BAD":
        return [("NOT-REPRODUCED", rel + "  (not the working box's liftOver; no public copy "
                 "of that one is known. SETUP.md section 8)")]
    if st == "missing":
        return [(st, rel + "  (setup.sh downloads one)")]
    return [(st, rel)]


STEPS = [("manifest", step_manifest), ("plink2", step_plink2), ("hgdp_1kgp", step_hgdp_1kgp),
         ("hgdp_panel", step_hgdp_panel), ("phase3_markers", step_phase3_markers),
         ("fasta", step_fasta), ("maps", step_maps), ("jre", step_jre),
         ("hgdp_basis", step_hgdp_basis), ("sites_index", step_sites_index),
         ("panel_snv", step_panel_snv), ("liftover", step_liftover)]


class Args(argparse.ArgumentParser):
    def error(self, message):
        # argparse exits 2 on a usage error, and 2 means something else here.
        self.print_usage(sys.stderr)
        sys.exit("%s: error: %s" % (self.prog, message))


def main():
    ap = Args(description=__doc__.split("\n")[0],
              formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--plan", action="store_true", help="report only; build and hash nothing")
    ap.add_argument("--check", action="store_true",
                    help="sha256 every file this script is answerable for; build nothing")
    ap.add_argument("--only", metavar="STEP[,STEP]",
                    help="run only these steps: " + ", ".join(n for n, _ in STEPS))
    ap.add_argument("--refdata", default=os.path.join(AGENT, "refdata"),
                    help="refdata root (default agent/refdata). For rebuilding somewhere "
                         "other than the live tree; archives are looked for in "
                         "refdata-downloads/ beside it.")
    ap.add_argument("--recipe", default=RECIPE, help="default: refdata_recipe.json")
    args = ap.parse_args()
    mode = "plan" if args.plan else "check" if args.check else "build"
    steps = STEPS
    if args.only:
        want = args.only.split(",")
        unknown = set(want) - {n for n, _ in STEPS}
        if unknown:
            ap.error("unknown step(s): %s" % ", ".join(sorted(unknown)))
        steps = [(n, f) for n, f in STEPS if n in want]

    rd = Refdata(args.refdata, args.recipe)
    if mode == "build":
        # One build at a time per tree. The lock lives in refdata-downloads/, beside the
        # root, because anything inside agent/refdata would be copied into run clones.
        lockdir = rd.p("refdata-downloads")
        os.makedirs(lockdir, exist_ok=True)
        lock = open(os.path.join(lockdir, ".build_refdata.lock"), "a")
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            sys.exit("another build_refdata.py is building this tree; wait for it.")
    print("refdata    %s" % rd.root)
    print("mode       %s" % mode)
    counts = {}
    for name, step in steps:
        print("\n[%s]" % name)
        try:
            rows = step(rd, mode)
        except (Stop, subprocess.CalledProcessError, OSError, KeyError) as exc:
            print("  STOPPED: %s" % exc)
            rd.failed.add(name)
            continue
        report(rows)
        for st, _ in rows:
            counts[st] = counts.get(st, 0) + 1
        if any(st in ("BAD", "missing") for st, _ in rows):
            rd.failed.add(name)

    print()
    print("  ".join("%s %d" % kv for kv in sorted(counts.items())))
    if mode == "plan":
        print("plan only. nothing built, nothing hashed.")
        return 0
    if rd.failed:
        print("not ok in: %s" % ", ".join(sorted(rd.failed)))
    if rd.failed or counts.get("BAD") or counts.get("missing"):
        return 1
    if counts.get("LINUX") or counts.get("TEXT") or counts.get("NOT-REPRODUCED"):
        print("identical to the working box's copy except what cannot be reproduced here "
              "(LINUX, TEXT, NOT-REPRODUCED above); see the docstring and SETUP.md section 8.")
        return 2
    print("every file identical to the working box's copy.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
