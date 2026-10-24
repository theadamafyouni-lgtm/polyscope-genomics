#!/usr/bin/env python3
"""ancestry.py -- reference PCA, bias-corrected projection, and distances (ANC-1, ANC-6).

Mechanical. It computes coordinates and distances and reports them. It does NOT assign an
ancestry group, does not apply a posterior cutoff, and does not decide whether a distance is
close enough. Those are the agent's (ANC-1's classifier step), and the reason they are not
here is that the cutoff `ANC-1` refers to is recorded by R5 as NOT ESTABLISHED in the retrieved
literature -- it lives in gnomAD production docs. Inventing one in a script would be exactly
the hardcoded interpretation ARCH-5 forbids.

Two projections are produced, on purpose:
  * simple projection -- query genotypes times reference loadings. Known to be biased:
    shrinkage pulls the query toward the origin, i.e. toward the centre of the reference
    cloud, and the bias is DIRECTIONAL, distorting exactly the non-European and admixed
    queries FRAME-3 says the product serves worst.
  * ADP (data augmentation, decomposition, Procrustes) -- the LASER/TRACE correction, which
    Zhang 2020 reports as unbiased. Its cost is one extra PCA per query, which is irrelevant
    here because there is exactly one query (R5 makes this point explicitly).
ANC-6 requires the corrected one. Both are reported so the shrinkage is visible, not asserted.

Usage:
  python3 tools/ancestry.py build-ref --ref-vcf refdata/ancestry-ref/1kg....vcf.gz \
      --panel refdata/ancestry-ref/integrated_call_samples_v3.20130502.ALL.panel \
      --maf 0.05 --geno 0.02 --prune-window 200 --prune-step 50 --prune-r2 0.2 \
      --pcs 20 --exclude-palindromic yes --out-dir refdata/ancestry-ref/build

  python3 tools/ancestry.py project --run <RUN_ID> --ref-dir refdata/ancestry-ref/build \
      --patient-vcf runs/<RUN_ID>/door/normalized.vcf.gz --k-neighbours 50 \
      --out runs/<RUN_ID>/ancestry/projection.json
"""
import argparse
import collections
import csv
import os
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import lib_run  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PLINK2 = os.path.join(ROOT, "refdata", "bin", "plink2")


def sh(cmd, log=None):
    print("+ " + " ".join(str(c) for c in cmd), file=sys.stderr)
    proc = subprocess.run([str(c) for c in cmd], capture_output=True, text=True)
    if log:
        with open(log, "a") as fh:
            fh.write(" ".join(str(c) for c in cmd) + "\n" + proc.stdout + proc.stderr + "\n")
    if proc.returncode != 0:
        raise SystemExit(f"ancestry.py: failed: {' '.join(str(c) for c in cmd)}\n{proc.stderr[-4000:]}")
    return proc


# --------------------------------------------------------------------------- build-ref
def _deviation_note(labels):
    """State what this reference is and is not, from the panel rather than from memory.

    ANC-1 specifies HGDP + 1000 Genomes with an AoU/gnomAD random forest over
    AFR/AMR/EAS/MID/EUR/SAS/OTH. Two things can deviate: which groups the panel
    actually carries, and whether a trained classifier exists. The second is always
    true here -- there is no RF and no posterior cutoff, because R5 records that
    cutoff as NOT ESTABLISHED and inventing one in a script is the hardcoded
    interpretation ARCH-5 forbids. The first is now read off the panel.
    """
    groups = sorted({(v or {}).get("super_pop") for v in labels.values()} - {None})
    expected = {"AFR", "AMR", "EAS", "EUR", "MID", "SAS", "CSA"}
    missing = sorted(expected - set(groups) - {"SAS", "CSA"})
    note = (f"Panel carries {len(groups)} groups: {', '.join(groups)}. "
            "No trained random forest and no posterior cutoff, so no OTH bin "
            "(R5 records the AoU/gnomAD cutoff as NOT ESTABLISHED). ")
    if missing:
        note += f"ANC-1 groups absent from this panel: {', '.join(missing)}. "
    if "CSA" in groups and "SAS" not in groups:
        note += ("This panel uses CSA (Central and South Asian) where ANC-1 says SAS "
                 "(South Asian); CSA is the wider group. ")
    note += "See OPEN_QUESTIONS Q3."
    return note


def cmd_build_ref(args):
    out = args.out_dir
    os.makedirs(out, exist_ok=True)
    log = os.path.join(out, "plink.log")
    # The reference genotypes arrive either as a VCF or as a plink2 pgen fileset.
    # pgen is read natively; `vzs` is required because the .pvar ships zstd-compressed.
    # Everything after this line is identical for both, because plink2 normalises to
    # pgen immediately either way.
    if getattr(args, "ref_pfile", None):
        src = ["--pfile", args.ref_pfile, "vzs"]
        ref_source = args.ref_pfile
    else:
        src = ["--vcf", args.ref_vcf]
        ref_source = args.ref_vcf
    base = [PLINK2] + src + ["--autosome", "--snps-only", "just-acgt",
            "--max-alleles", "2", "--set-all-var-ids", "@:#", "--rm-dup", "exclude-all"]
    filters = ["--maf", str(args.maf), "--geno", str(args.geno)]
    sh(base + filters + ["--make-pgen", "--out", os.path.join(out, "ref_raw")], log)

    if args.exclude_palindromic == "yes":
        # A/T and C/G markers cannot be strand-resolved from alleles alone; excluding them
        # from the ancestry marker set is a QC choice the agent made, recorded here.
        keep = os.path.join(out, "nonpalindromic.ids")
        with open(os.path.join(out, "ref_raw.pvar")) as fh, open(keep, "w") as kf:
            n_excl = 0
            for line in fh:
                if line.startswith("#"):
                    continue
                f = line.split("\t")
                alleles = {f[3].upper(), f[4].upper()}
                if alleles in ({"A", "T"}, {"C", "G"}):
                    n_excl += 1
                    continue
                kf.write(f[2] + "\n")
        sh([PLINK2, "--pfile", os.path.join(out, "ref_raw"), "--extract", keep,
            "--make-pgen", "--out", os.path.join(out, "ref_nonpal")], log)
        stem = os.path.join(out, "ref_nonpal")
    else:
        n_excl = 0
        stem = os.path.join(out, "ref_raw")

    sh([PLINK2, "--pfile", stem, "--indep-pairwise", args.prune_window, args.prune_step,
        args.prune_r2, "--out", os.path.join(out, "prune")], log)
    sh([PLINK2, "--pfile", stem, "--extract", os.path.join(out, "prune.prune.in"),
        "--make-pgen", "--out", os.path.join(out, "ref_pruned")], log)
    sh([PLINK2, "--pfile", os.path.join(out, "ref_pruned"), "--freq",
        "--pca", "allele-wts", str(args.pcs), "--out", os.path.join(out, "ref_pca")], log)

    n_pruned = sum(1 for line in open(os.path.join(out, "ref_pruned.pvar")) if not line.startswith("#"))
    labels = {}
    with open(args.panel) as fh:
        rd = csv.DictReader(fh, delimiter="\t")
        for row in rd:
            labels[row["sample"]] = {"pop": row.get("pop"), "super_pop": row.get("super_pop")}
    lib_run.write_json(os.path.join(out, "sample_labels.json"), labels)

    meta = {
        "tool": "ancestry.py build-ref",
        "generated_utc": lib_run.utcnow(),
        "reference_source": ref_source,
        "panel_file": args.panel,
        "plink2_version": lib_run.tool_version([PLINK2, "--version"]),
        "parameters_chosen_by_agent": {
            "maf": args.maf, "geno": args.geno, "pcs": args.pcs,
            "prune": [args.prune_window, args.prune_step, args.prune_r2],
            "exclude_palindromic": args.exclude_palindromic,
        },
        "n_palindromic_excluded": n_excl,
        "n_markers_after_pruning": n_pruned,
        "n_reference_samples": len(labels),
        "superpop_counts": dict(collections.Counter(v["super_pop"] for v in labels.values())),
        # Derived from the panel that was actually read, not asserted. The previous
        # version hardcoded "1000 Genomes only ... five superpopulations and no MID",
        # which was written into every projection artifact and would have gone false
        # and silent the moment the panel changed.
        "deviation_note": _deviation_note(labels),
    }
    lib_run.write_json(os.path.join(out, "reference_build.json"), meta)
    if not args.keep_intermediates:
        # ref_raw and ref_nonpal exist only to produce ref_pruned and nonpalindromic.ids;
        # nothing after build-ref reads them (project and the reference provider use
        # ref_pruned and ref_pca.*). Their .pgen/.pvar are ~8.6 GB for the HGDP+1kGP basis,
        # in every run that builds its own, which filled this box mid-run. The .psam and .log
        # files stay, so what was built is still on record.
        for stem in ("ref_raw", "ref_nonpal"):
            for ext in (".pgen", ".pvar"):
                path = os.path.join(out, stem + ext)
                if os.path.exists(path):
                    os.remove(path)
    return meta


# --------------------------------------------------------------------------- genotype IO
def read_bed(stem):
    """Read a PLINK1 .bed/.bim/.fam into a float32 matrix of allele counts (NaN = missing).

    Variant-major, 2 bits per genotype: 00=hom A1, 01=missing, 10=het, 11=hom A2.
    Mechanical decoding, no interpretation."""
    import numpy as np
    ids = [l.split()[1] for l in open(stem + ".fam")]
    variants = [l.split("\t")[1] for l in open(stem + ".bim")]
    n, m = len(ids), len(variants)
    lut = np.array([0.0, np.nan, 1.0, 2.0], dtype=np.float32)
    per_variant = (n + 3) // 4
    with open(stem + ".bed", "rb") as fh:
        magic = fh.read(3)
        if magic[:2] != b"\x6c\x1b":
            raise SystemExit(f"ancestry.py: {stem}.bed is not a PLINK1 .bed")
        if magic[2] != 1:
            raise SystemExit("ancestry.py: sample-major .bed not supported")
        raw = np.frombuffer(fh.read(), dtype=np.uint8)
    raw = raw.reshape(m, per_variant)
    codes = np.empty((m, per_variant * 4), dtype=np.uint8)
    for k in range(4):
        codes[:, k::4] = (raw >> (2 * k)) & 0b11
    G = lut[codes[:, :n]].T          # samples x variants
    return G, ids, variants


def augmented_pca(G_ref, ref_ids, q_row, npcs):
    """PCA over reference + query together (the 'decomposition' in ADP).

    Computed in sample space: with ~2.5k samples the Gram matrix is small and its
    eigendecomposition is exact, so no randomised SVD and no approximation is involved.
    Missing genotypes are mean-filled FOR THE PCA ONLY -- this is a projection, not a score, so
    JC-2's no-fabrication rule is not in play here; nothing from this path reaches the PRS.

    Returns RAW eigenvectors, deliberately NOT scaled by sqrt(eigenvalue). The Procrustes step
    below fits ONE global scale rho against plink2's ref_pca.eigenvec, and plink writes that file
    unscaled -- eigenvalues go to ref_pca.eigenval separately, and every eigenvec column has norm
    exactly 1. A single rho can only reconcile the two sides if every PC needs the same factor,
    so both sides have to carry the same convention.

    Scaling this side alone by sqrt(eigenvalue) imposes a per-PC factor spanning about 10x. (For
    this reference the sqrt of ref_pca.eigenval runs 14.13 on PC1 down to 1.38 on PC20; the
    factors that actually operate come from the augmented decomposition and are query-dependent,
    but the spread is the same.) rho then splits the difference -- measured on the recorded runs
    it lands near 0.14-0.15 -- which leaves PC1 roughly 2x too large and PC5-PC20 roughly 5x too
    small. The largest factor error is on the trailing PCs; the consequential one is on PC1 and
    PC2, where the ancestry signal is and where the residual reached ~1.0 reference sd, i.e. worse
    than predicting the group mean on that axis.

    TRACE and FRAPOSA instead scale BOTH sides. That is a different fit, not an equivalent one --
    a rotation does not commute with a non-scalar diagonal -- and on the runs measured here it is
    better still on PC1-PC4 than scaling neither. It is a larger change than this one and has not
    been adopted."""
    import numpy as np
    G = np.vstack([G_ref, q_row[None, :]])
    ids = list(ref_ids) + ["__QUERY__"]
    # Columns the QUERY lacks are dropped outright. Filling them with the reference mean would
    # place the query at the reference centroid for those markers, manufacturing exactly the
    # centre-ward bias ADP exists to remove.
    keep = ~np.isnan(G[-1, :])
    G = G[:, keep]
    mean = np.nanmean(G, axis=0)
    inds = np.where(np.isnan(G))
    G[inds] = np.take(mean, inds[1])   # residual reference-side missingness only
    p = mean / 2.0
    sd = np.sqrt(2 * p * (1 - p))
    sd[sd == 0] = 1.0
    Z = (G - mean) / sd
    C = (Z @ Z.T) / Z.shape[1]
    vals, vecs = np.linalg.eigh(C)
    order = np.argsort(vals)[::-1][:npcs]
    return vecs[:, order], ids


# --------------------------------------------------------------------------- project
def read_eigenvec(path):
    ids, rows = [], []
    with open(path) as fh:
        header = [h.lstrip("#") for h in fh.readline().rstrip("\n").split("\t")]
        iid = header.index("IID")
        pcs = [i for i, h in enumerate(header) if h.startswith("PC")]
        for line in fh:
            f = line.rstrip("\n").split("\t")
            ids.append(f[iid])
            rows.append([float(f[i]) for i in pcs])
    return ids, rows


def cmd_project(args):
    import numpy as np

    ref = args.ref_dir
    out_dir = os.path.dirname(os.path.abspath(args.out))
    os.makedirs(out_dir, exist_ok=True)
    work = os.path.join(out_dir, "work")
    os.makedirs(work, exist_ok=True)
    log = os.path.join(work, "plink.log")

    # 1. patient restricted to the reference marker set, same variant-ID convention
    sh([PLINK2, "--vcf", args.patient_vcf, "--autosome", "--snps-only", "just-acgt",
        "--max-alleles", "2", "--set-all-var-ids", "@:#", "--rm-dup", "exclude-all",
        "--extract", os.path.join(ref, "prune.prune.in"),
        "--make-pgen", "--out", os.path.join(work, "patient")], log)
    # Records the person's file has on the reference marker set, called or not: a no-call is
    # kept as a missing genotype, and it is not a marker the projection can use.
    n_records = sum(1 for line in open(os.path.join(work, "patient.pvar")) if not line.startswith("#"))

    # 1b. The SHARED marker set: reference markers this array actually carries, with a usable ALT
    #     and a non-degenerate reference frequency. Everything below runs on exactly this set.
    #
    #     This matters more than it looks. A consumer array carries only a fraction of an
    #     LD-pruned reference marker set, and if the query's absent markers are filled with the
    #     reference mean, the query is dragged toward the centre of the reference cloud by
    #     construction -- a far larger version of the very shrinkage ANC-6 exists to remove. So
    #     absent markers are DROPPED from the projection, never filled.
    af = {}
    with open(os.path.join(ref, "ref_pca.afreq")) as fh:
        head = [h.lstrip("#") for h in fh.readline().rstrip("\n").split("\t")]
        i_id, i_af = head.index("ID"), head.index("ALT_FREQS")
        for line in fh:
            f = line.rstrip("\n").split("\t")
            try:
                af[f[i_id]] = float(f[i_af])
            except ValueError:
                pass
    shared, dropped_zero_af, dropped_no_alt = [], 0, 0
    with open(os.path.join(work, "patient.pvar")) as fh:
        for line in fh:
            if line.startswith("#"):
                continue
            f = line.rstrip("\n").split("\t")
            vid, alt = f[2], f[4]
            if alt == "." or not alt:
                dropped_no_alt += 1
                continue
            a = af.get(vid)
            if a is None or a <= 0.0 or a >= 1.0:
                dropped_zero_af += 1
                continue
            shared.append(vid)
    shared_path = os.path.join(work, "shared.ids")
    with open(shared_path, "w") as fh:
        fh.write("\n".join(shared) + "\n")

    # 2. simple projection: query genotypes x reference loadings (the biased one).
    #    plink2's --score output is on its own scale, so comparing it to the reference eigenvec
    #    would measure the units, not the bias. Calibrate first: run the identical recipe over the
    #    REFERENCE samples, whose true coordinates are known, and fit one scale factor per PC.
    #    Both runs are restricted to the shared set, or the calibration would be fit on a
    #    different marker set from the one the query was projected with.
    def project(pfile, out_stem):
        sh([PLINK2, "--pfile", pfile, "--extract", shared_path,
            "--read-freq", os.path.join(ref, "ref_pca.afreq"),
            "--score", os.path.join(ref, "ref_pca.eigenvec.allele"), "2", "5",
            "header-read", "no-mean-imputation", "variance-standardize",
            "--score-col-nums", f"6-{5 + args.pcs}",
            "--out", out_stem], log)
        ids, rows = [], []
        with open(out_stem + ".sscore") as fh:
            header = [h.lstrip("#") for h in fh.readline().rstrip("\n").split("\t")]
            score_cols = [i for i, h in enumerate(header) if h.endswith("_AVG")]
            if not score_cols:
                score_cols = [i for i, h in enumerate(header) if h.endswith("_SUM")]
            iid = header.index("IID")
            for line in fh:
                f = line.rstrip("\n").split("\t")
                ids.append(f[iid])
                rows.append([float(f[i]) for i in score_cols])
        return ids, np.array(rows)

    _, Q_raw = project(os.path.join(work, "patient"), os.path.join(work, "patient_proj"))
    ref_proj_ids, R_proj = project(os.path.join(ref, "ref_pruned"), os.path.join(work, "ref_proj"))

    # 3. ADP: augment the reference with the query, re-run PCA, Procrustes back (ANC-6).
    #    plink2's non-concatenating --pmerge is still unimplemented, and a VCF-level merge would
    #    silently drop markers wherever REF/ALT disagree. So the augmented PCA is done here:
    #    both sides are exported to PLINK .bed on the SAME variant set in the SAME order, and the
    #    sample-space Gram matrix is accumulated in marker blocks (n x n is only ~2.5k square, so
    #    this is exact and cheap -- no randomised SVD, no approximation).
    sh([PLINK2, "--pfile", os.path.join(ref, "ref_pruned"), "--extract", shared_path,
        "--make-bed", "--out", os.path.join(work, "ref_bed")], log)
    sh([PLINK2, "--pfile", os.path.join(work, "patient"), "--extract", shared_path,
        "--make-bed", "--out", os.path.join(work, "patient_bed")], log)

    G_ref, ref_bed_ids, ref_bed_vars = read_bed(os.path.join(work, "ref_bed"))
    G_q, _q_ids, q_vars = read_bed(os.path.join(work, "patient_bed"))

    # align the query's markers onto the reference's variant order
    qpos = {v: i for i, v in enumerate(q_vars)}
    cols = [qpos.get(v, -1) for v in ref_bed_vars]
    q_row = np.full(len(ref_bed_vars), np.nan, dtype=np.float32)
    for j, c in enumerate(cols):
        if c >= 0:
            q_row[j] = G_q[0, c]
    n_query_markers = int(np.sum(~np.isnan(q_row)))

    aug_pcs_all, aug_ids = augmented_pca(G_ref, ref_bed_ids, q_row, args.pcs)

    ref_ids, ref_pcs = read_eigenvec(os.path.join(ref, "ref_pca.eigenvec"))
    R = np.array(ref_pcs)
    A = aug_pcs_all

    # per-PC scale, fit on reference samples whose true coordinates are known
    pos = {s: i for i, s in enumerate(ref_ids)}
    order = [pos[s] for s in ref_proj_ids if s in pos]
    keep = [i for i, s in enumerate(ref_proj_ids) if s in pos]
    P, T = R_proj[keep], R[order]
    scale = np.array([
        (P[:, k] @ T[:, k]) / (P[:, k] @ P[:, k]) if (P[:, k] @ P[:, k]) else 1.0
        for k in range(min(P.shape[1], T.shape[1]))
    ])
    simple_pcs = (Q_raw[0][:len(scale)] * scale).tolist()

    # shrinkage measured on the reference itself: after calibration a global scale is 1 by
    # construction, so the bias shows up as distant samples being pulled in more than central
    # ones. Reported by distance decile rather than asserted from the literature.
    Pc = P[:, :len(scale)] * scale
    true_norm = np.linalg.norm(T[:, :len(scale)], axis=1)
    proj_norm = np.linalg.norm(Pc, axis=1)
    deciles = np.percentile(true_norm, [10, 50, 90])
    shrinkage_profile = {
        "inner_decile_mean_ratio": float(np.mean(
            proj_norm[true_norm <= deciles[0]] / true_norm[true_norm <= deciles[0]])),
        "median_band_mean_ratio": float(np.mean(
            proj_norm[(true_norm > deciles[0]) & (true_norm <= deciles[2])] /
            true_norm[(true_norm > deciles[0]) & (true_norm <= deciles[2])])),
        "outer_decile_mean_ratio": float(np.mean(
            proj_norm[true_norm >= deciles[2]] / true_norm[true_norm >= deciles[2]])),
        "how_measured": "reference samples projected through the same plink2 --score recipe and "
                        "compared to their true PCA coordinates, after a per-PC scale fit.",
        "what_it_does_and_does_not_show": "These samples were IN the PCA, so this is an in-sample "
                        "reprojection and the ratios should sit near 1.0. Treat it as a check that "
                        "the per-PC scale calibration is correct, NOT as a measurement of "
                        "shrinkage. Nothing in this artifact measures the out-of-sample shrinkage "
                        "for the query. Doing so would need the query's true coordinate, which "
                        "does not exist for someone outside the reference; measuring it at all "
                        "means holding reference samples out and reprojecting them, which is a "
                        "validation exercise, not something a per-patient run can do. "
                        "projection_shrinkage_ratio_for_this_query is NOT that measurement -- see "
                        "shrinkage_note.",
    }
    idx = {s: i for i, s in enumerate(aug_ids)}
    common = [s for s in ref_ids if s in idx]
    A_ref = A[[idx[s] for s in common]]
    R_ref = R[[ref_ids.index(s) for s in common]]

    # Procrustes: find scale rho, rotation Q, translation t mapping A_ref onto R_ref
    Ac, Rc = A_ref - A_ref.mean(0), R_ref - R_ref.mean(0)
    U, S, Vt = np.linalg.svd(Ac.T @ Rc)
    Q = U @ Vt
    rho = S.sum() / (Ac ** 2).sum()
    t = R_ref.mean(0) - rho * (A_ref.mean(0) @ Q)

    query_rows = [i for i, s in enumerate(aug_ids) if s == "__QUERY__"]
    if len(query_rows) != 1:
        raise SystemExit(f"ancestry.py: expected exactly 1 query row in the augmented set, "
                         f"found {len(query_rows)}")
    adp_pcs = (rho * (A[query_rows[0]] @ Q) + t).tolist()

    # 4. distances -- reported, never thresholded
    labels = lib_run.read_json(os.path.join(ref, "sample_labels.json"))
    by_pop = collections.defaultdict(list)
    for i, s in enumerate(ref_ids):
        sp = (labels.get(s) or {}).get("super_pop")
        if sp:
            by_pop[sp].append(R[i])

    q = np.array(adp_pcs)
    q_simple = np.array(simple_pcs)
    dist = {}
    for sp, rows in sorted(by_pop.items()):
        M = np.array(rows)
        centroid = M.mean(0)
        within = np.linalg.norm(M - centroid, axis=1)
        dist[sp] = {
            "n_reference_samples": len(rows),
            "distance_to_centroid_adp": float(np.linalg.norm(q - centroid)),
            "distance_to_centroid_simple_projection": float(np.linalg.norm(q_simple - centroid)),
            "within_group_distance_mean": float(within.mean()),
            "within_group_distance_sd": float(within.std()),
            "within_group_distance_p95": float(np.percentile(within, 95)),
            "patient_distance_in_sd_units": float(
                (np.linalg.norm(q - centroid) - within.mean()) / within.std()) if within.std() else None,
        }

    d_all = np.linalg.norm(R - q, axis=1)
    order = np.argsort(d_all)[:args.k_neighbours]
    neigh = collections.Counter((labels.get(ref_ids[i]) or {}).get("super_pop") for i in order)
    neigh_pop = collections.Counter((labels.get(ref_ids[i]) or {}).get("pop") for i in order)

    shrinkage = float(np.linalg.norm(q_simple) / np.linalg.norm(q)) if np.linalg.norm(q) else None

    result = {
        "tool": "ancestry.py project",
        "generated_utc": lib_run.utcnow(),
        "patient_vcf": args.patient_vcf,
        "reference_build": lib_run.read_json(os.path.join(ref, "reference_build.json")),
        # The markers the projection actually had: on the reference's pruned set, with a usable
        # ALT and frequency, and CALLED in this file. The coverage check reads this over
        # marker_set.reference_pruned_markers; counting records instead let a file of no-calls
        # look as covered as a file of genotypes.
        "n_markers_shared_with_reference": n_query_markers,
        "n_markers_used_in_augmented_pca": n_query_markers,
        "marker_set": {
            "reference_pruned_markers": len(af),
            "present_on_this_array": n_records,
            "present_on_this_array_means": "records on the reference marker set, called or not",
            "dropped_alt_unrestorable": dropped_no_alt,
            "dropped_degenerate_reference_frequency": dropped_zero_af,
            "shared_set_used": len(shared),
            "called_in_shared_set": n_query_markers,
            "note": "Absent markers are dropped from the projection, never mean-filled: filling "
                    "would drag the query toward the reference centroid, a larger version of the "
                    "shrinkage ANC-6 exists to remove.",
        },
        "pcs_used": args.pcs,
        "patient_pcs_simple_projection": simple_pcs,
        "patient_pcs_adp_corrected": adp_pcs,
        "projection_shrinkage_ratio_for_this_query": shrinkage,
        "shrinkage_note": "||calibrated simple|| / ||ADP-corrected|| for THIS query. Read it as a "
                          "post-hoc DIAGNOSTIC, not a correction factor: it is computed after "
                          "patient_pcs_adp_corrected is already final, it is applied to nothing, "
                          "and nothing downstream reads it. It compares two estimators to each "
                          "other, and there is no ground truth for this person -- their true "
                          "coordinate would require them to have been in the reference "
                          "decomposition, which is exactly what they are not. So it cannot "
                          "identify which estimator is biased, or in which direction. Below 1 "
                          "means the simple projection placed this person nearer the reference "
                          "centre than ADP did; above 1 means further out; near 1 means the two "
                          "agree, which is not the same as either being correct. The reason ANC-6 "
                          "takes the ADP coordinate is Zhang 2020, not this number.",
        "shrinkage_profile_measured_on_reference": shrinkage_profile,
        "distance_to_superpopulations": dist,
        "nearest_reference_samples": {
            "k": args.k_neighbours,
            "by_superpopulation": dict(neigh.most_common()),
            "by_population": dict(neigh_pop.most_common()),
        },
        "what_this_does_not_do": [
            "assign an ancestry group -- that is the agent's judgment (ANC-1's classify step)",
            "apply a posterior cutoff -- R5 records the gnomAD/AoU cutoff as NOT ESTABLISHED",
            "produce an OTH bin -- no trained random forest here (OPEN_QUESTIONS Q3)",
        ],
    }
    lib_run.emit(result, args.out)
    if args.run:
        lib_run.ledger_append(args.run, {
            "kind": "facts", "stage": "ancestry", "tool": "ancestry.py project",
            "n_markers_shared": n_query_markers, "artifact": args.out,
        })
    return result


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    b = sub.add_parser("build-ref")
    src = b.add_mutually_exclusive_group(required=True)
    src.add_argument("--ref-vcf", help="reference genotypes as a VCF")
    src.add_argument("--ref-pfile", help="reference genotypes as a plink2 pgen stem, "
                                         "without the .pgen/.pvar.zst/.psam extension")
    b.add_argument("--panel", required=True)
    b.add_argument("--maf", required=True)
    b.add_argument("--geno", required=True)
    b.add_argument("--prune-window", required=True)
    b.add_argument("--prune-step", required=True)
    b.add_argument("--prune-r2", required=True)
    b.add_argument("--pcs", type=int, required=True)
    b.add_argument("--exclude-palindromic", required=True, choices=["yes", "no"])
    b.add_argument("--out-dir", required=True)
    b.add_argument("--keep-intermediates", action="store_true",
                   help="keep ref_raw/ref_nonpal .pgen/.pvar (~8.6 GB for HGDP+1kGP); by default "
                        "they are removed once ref_pruned is built, since nothing reads them")

    p = sub.add_parser("project")
    p.add_argument("--run")
    p.add_argument("--ref-dir", required=True)
    p.add_argument("--patient-vcf", required=True)
    p.add_argument("--pcs", type=int, default=20)
    p.add_argument("--k-neighbours", type=int, default=50)
    p.add_argument("--out", required=True)

    args = ap.parse_args()
    if args.cmd == "build-ref":
        meta = cmd_build_ref(args)
        print(f"reference built: {meta['n_markers_after_pruning']} markers, "
              f"{meta['n_reference_samples']} samples, superpops {meta['superpop_counts']}")
    else:
        cmd_project(args)


if __name__ == "__main__":
    main()
