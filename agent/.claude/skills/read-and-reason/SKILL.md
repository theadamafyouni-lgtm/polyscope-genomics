---
name: read-and-reason
description: Classify a genomic file and read its provenance from the file's own content — assay class, vendor/chip, declared genome build, and what an absent position will mean downstream. Use at the door on the patient's file (INTAKE-1/INTAKE-2), and in select on a PGS Catalog scoring file (SEL-13). Invoke before any stage that depends on knowing what kind of file it is holding.
---

# read-and-reason

One capability, two inputs (`ARCH-3`). Classifying an arbitrary genomic file from its own
content is the same reasoning whether the file is the patient's or the model's, so this skill
runs at the door on the patient's file and again in `select` on the scoring file.

**You do the reasoning. The tools only count things.** There is no build-detector script, no
assay-classifier script, and no coordinate table anywhere in `tools/`. If you find yourself
wanting one, that is the signal you are about to hardcode interpretation (`ARCH-5`).

---

## A. At the door, on the patient's file

Order is fixed: **hygiene → classify → normalize → restore ALT.**
Classify comes before normalize because a conversion cannot preserve provenance it does not know.

### A1. Hygiene (`INTAKE-4`)

```bash
python3 tools/intake_hygiene.py --input <file> --run <RUN_ID> \
    --out runs/<RUN_ID>/door/hygiene.json
```

Read the output and apply the rule yourself:

- **Any container, then check for the damage.** `.zip`, `.gz` and raw uncompressed text are all
  accepted. The risk that raw text carries is real: an export opened in a spreadsheet silently
  mangles base-pair positions into scientific notation and reformats genotypes. But that is
  **testable, not a reason to refuse unseen** — a mangled position will not parse as an integer,
  and a reformatted genotype will not match the vendor's token set. So read the rows and judge:
  if positions parse and the genotype tokens look like the vendor's, the file was not mangled and
  there is nothing to stop on. If they do not, **that** is your stop, and you can say exactly what
  is wrong rather than refusing a container.

  Container is not evidence of corruption. Corruption is.

  `normalize.py` reads the container from the file's own magic bytes rather than its extension,
  so a `.txt` that is really a zip is handled correctly and the container it found lands in
  provenance. Note the container in your judgment either way.
- **Line endings.** Both CRLF and LF are handled. Note which is present; it belongs in provenance.
- **Row count.** You judge plausibility, against what you know a vendor export of this kind
  contains. The tool reports the number and never comments on it. An implausible count is a
  corruption or modification signal — treat it as grounds to look harder, not as an automatic stop.
- **Checksum.** The tool computes a SHA-256. Note honestly that no vendor checksum ships with the
  archive, so there is nothing external to verify against; the hash is an audit trail, not a
  verification.

### A2. Classify (`INTAKE-1`)

```bash
# first pass: header verbatim plus column shape
python3 tools/file_facts.py --input <file> --run <RUN_ID> \
    --out runs/<RUN_ID>/door/file_facts.json
```

Read `comment_header.lines_verbatim` in full before anything else. Then ask for whatever census
you need for the call you are about to make — the tool takes column arguments, so you choose what
to count:

```bash
python3 tools/file_facts.py --input <file> \
    --token-census-columns <genotype col> \
    --group-column <chrom col> --value-column <pos col> \
    --key-columns <chrom col>,<pos col> \
    --out runs/<RUN_ID>/door/file_facts_census.json
```

**The judgment: which of WGS / WES / genotyping-array / gVCF is this?** It matters because
absence means something different in each — array: unmeasured; WES: outside the capture;
WGS: depends how it was called; gVCF: resolvable from the file itself. Guessing wrong makes
absence mean the wrong thing at every stage after this one.

Signals worth weighing. No single one is proof; the conjunction is
a **heuristic**, and there is no validated, benchmarked classifier for this decision:

- a fixed, sparse marker set (hundreds of thousands of rows, not millions) at non-contiguous positions
- an explicit no-call token rather than a missing-genotype field
- SNP-only content, no indels
- no sequencing-style per-genotype quality (`DP`, `GQ`, `PL`)
- no reference-confidence apparatus (`<NON_REF>` / `<*>` ALT, `INFO/END` blocks, `##GVCFBlock`)
- genotypes stated as alleles rather than as differences from a reference
- duplicate positions under different marker IDs — a known consumer-export residue

**If you cannot classify confidently, that is a `stop`, not a guess** (`JC-1`). Say what was
ambiguous. Note that "confidently" has no established basis here — record your confidence as
your own assessment, not as a validated threshold.

**Watch for the fifth class.** A pseudo-array text file *down-converted from WGS or WES* looks
like an array but its absent positions were guessed, so its hom-ref and no-call calls are
unreliable. If the row set looks array-shaped but the header, marker IDs, or position spacing
suggest it was derived from sequencing, detect it and refuse rather than treating it as an array.

**A Complete Genomics var file** names its columns on a `>locus ploidy allele chromosome begin
end varType ...` line under a `#` header (`#GENOME_REFERENCE`, `#FORMAT_VERSION`, and the vendor's
own `#CALLED_GENDER`, which is evidence to weigh with the counts in A3b, not a fact). It is a
whole-genome call set, WGS, that no other tool here reads as it is: convert it, and use the VCF
for A3b, A6 and A5 -- the file as received is the var file, its VCF is what the tools read:

```bash
python3 tools/cg_to_vcf.py --run <RUN_ID> --input <the var file> --sample-id <name> \
    --out runs/<RUN_ID>/door/cg_converted.vcf.gz
```

It writes the single-base SNVs with each allele's scores and filter, and says in
`cg_converted.json` what it did not write: reference and no-call stretches (so an absent position
is unknown, as on any variant-only VCF) and indels and substitutions. A large genome takes
minutes.

**For array-origin input, also read vendor and chip generation from the `#` header.** Different
chips carry different markers, so this is needed downstream for the strand file (`SCO-4`) and the
reference panel.

### A3. The build (`SCO-1`) — reason it out, never read it off a filename

The declared build is a claim the file makes about itself. Find it in the header and **quote the
line**. Then check the claim against the file's own coordinates rather than taking it on trust:

```bash
python3 tools/file_facts.py --input <file> \
    --lookup-column <id col> --lookup-values <a few marker IDs you independently know> \
    --group-column <chrom col> --value-column <pos col>
```

Two independent cross-checks:

1. **Named markers.** Choose several markers whose GRCh37 *and* GRCh38 coordinates you know
   independently, look up their positions in this file, and see which build they match. Choose
   markers where the two builds differ enough to be decisive.
2. **Chromosome extents.** `group_summary` gives the maximum position seen per chromosome. The
   assemblies have different chromosome lengths; a maximum position exceeding a chromosome's
   length in one build rules that build out.

If the header's declaration and the coordinates disagree, **the coordinates win and you say so** —
a header can be stale or hand-edited, positions cannot. If you cannot resolve the build, that is
a `stop`: every downstream match depends on it.

Record the build with the evidence that settled it, not just the answer.

### A3b. The sex chromosomes — count, then judge

`select` needs chromosomal sex for a trait tied to one sex, and only the person's file can say
it. Count what the file shows on X and Y, from the file **as received** (the normalized VCF
drops haploid X and Y calls, and on the VCF route X and Y altogether), naming the
pseudoautosomal regions of the build you just settled:

```bash
python3 tools/sex_evidence.py --run <RUN_ID> --input <the person's file> \
    --chrom-col <col> --pos-col <col> --genotype-col <col> --no-call-token=<the file's> \
    --par X:60001-2699520 --par X:154931044-155260560 \
    --par Y:10001-2649520 --par Y:59034050-59363566 \
    --out runs/<RUN_ID>/door/sex_evidence.json
```

Those regions are GRCh37's; GRCh38's are X:10001-2781479, X:155701383-156030895,
Y:10001-2781479 and Y:56887903-57217415. A VCF needs no columns. Two allele columns go in
`--allele-cols`.

The tool counts; you judge. An XX genome typically shows heterozygous calls across X outside
the pseudoautosomal regions, at a share of diploid calls not far below the autosomes', and few
or no Y calls; an XY genome shows almost no heterozygous X calls there (a handful of genotyping
errors) and many called Y markers. Record `chromosomal_sex` in your classification as `XX`,
`XY` or `unclear`, with the counts that settled it. **Unclear is an answer**: a pattern that fits
neither -- heterozygous X with called Y, or neither -- can come from a sex-chromosome variation,
mosaicism, a file that is not this person's, or a vendor's quirk. Say what the file shows, never
name a condition from it, and leave it for `select` to ask about if the trait makes it matter.
This is a fact about chromosomes, not about who the person is.

### A4. Record the classification

Write a judgment and file it:

```bash
python3 tools/record.py --run <RUN_ID> --stage door --name classification --judgment - <<'JSON'
{
  "decision": "...what you concluded...",
  "outcome": "proceed",
  "assay_class": "...",
  "vendor": "...", "chip_generation": "...",
  "genome_build": {"value": "...", "declared_in_header": "...quoted line...",
                   "verified_by": ["...cross-check and result..."]},
  "absence_means": "...what a missing position will mean downstream, per this class...",
  "chromosomal_sex": {"judged": "XX|XY|unclear", "from": "runs/<RUN_ID>/door/sex_evidence.json",
                      "why": "...the counts that settled it..."},
  "confidence": "...yours, with what would change it...",
  "reasons": ["..."],
  "evidence": [{"source": "runs/<RUN_ID>/door/file_facts.json", "field": "..."}]
}
JSON
```

### A5. Normalize (`INTAKE-2`) and restore ALT (`INTAKE-3`)

See `.claude/skills/score/SKILL.md` for how the normalized file is consumed. The conversion is
run by `tools/normalize.py`, which is mechanical; **you** supply the build, the reference FASTA
and the conversion path, and the provenance record is written from your classification.

**A VCF can contradict its own header, and reconciling that is your call, not the tool's.**
Two conditions, both measured on real vendor exports, and both stop bcftools before any stage
sees the file. `normalize.py` takes a policy for each; neither has a default, so you state what
you did even when you did nothing.

**Declared sample columns exceeding the data.** One vendor export declares `unknown` and
`Sample1` in `#CHROM` and writes ten fields per data row, so bcftools stops at the first record
with "Number of columns ... does not match the number of samples". You can see this before
running anything: `file_facts.py` puts the `#CHROM` line in `comment_header.lines_verbatim` and
the observed width in `shape.column_count_census`. Compare the two.

> **Identify the live column by where the data is, never by which name reads like a sample id.**
> In that file the genotype sits under `unknown` and the phantom is `Sample1` -- the name that
> looks real is the empty one. Guessing from the names picks the wrong column, and picking the
> wrong column scores a genotype that is not this person's, with no error anywhere.

`--header-sample-repair truncate-to-data-columns` rewrites the `#CHROM` line to the observed
width and copies every data row through untouched. `none` leaves the file as it is.

**An INFO field whose value count contradicts its own declaration.** The same export declares
`INFO/AB` with `Number=2` and writes one value; `bcftools norm` stops and suggests `--force`.
**Do not reach for `--force`** -- it carries forward data the file itself says is wrong.
`--info-policy drop-all` removes the INFO block instead. That is an enumerable loss and it is
recorded: this path is consumed for `GT`, and no stage downstream reads INFO. It *would* be
load-bearing for a gVCF, where `DP` and `END` live there, which is part of why gVCF is a
separate route.

**Two repairs on one vendor's file is the current count.** If a third condition turns up in the
same export, refusing it is the better answer than a third repair -- a file that contradicts
itself in three places is not one you can honestly hand a person a number from. Say that rather
than accumulating patches.

Two things the conversion must not do: erase where the file came from, and silently resolve
strand. A 23andMe txt converted to a VCF *looks* like a VCF but has array semantics — downstream
stages read the provenance record, never the file extension. Strand ambiguity at A/T and C/G
markers is **deferred to matching time** (`SCO-4`), not resolved here.

The conversion is lossy in enumerable ways, and the provenance record names them: REF/ALT inferred
from an out-of-band FASTA, indels dropped, no-calls become `./.`. Because bcftools' reference-aware
path and PLINK's observation-only path produce **different VCFs from the same bytes**, the
conversion path itself is provenance and is recorded by name and version.

### A6. Sequencing calls: their quality, your policy

A called genotype is not a reliable one: a non-PASS, depth-one call gets full typed credit unless
something stops it. An array call carries no per-call quality to judge it by, so this step is for
sequencing files -- WGS, WES, gVCF -- and for an array you skip it and say why. Read the quality
evidence the file carries, from the file as received:

```bash
python3 tools/call_quality.py facts --run <RUN_ID> --input <the person's VCF> \
    --out runs/<RUN_ID>/door/call_quality_facts.json
```

Set the policy for this file -- which FILTER values pass, what GQ, DP or QUAL a call needs -- from
what it actually carries and how the values are spread, then apply it to the normalized VCF:

```bash
python3 tools/call_quality.py apply --run <RUN_ID> --vcf runs/<RUN_ID>/door/normalized.vcf.gz \
    --include '<your policy, as a bcftools expression>' \
    --out runs/<RUN_ID>/door/call_quality.json
```

A call that fails it becomes missing in the file every stage reads: not typed, imputed where the
panel can, never scored as reference. So a stricter policy moves weight from typed to imputed or
unresolved, and the coverage gate sees that.

- **No threshold is given here, and none is derived.** GQ >= 20, DP >= 10 and FILTER=PASS are
  conventions, not measurements: say which you used and why they fit this file's spread.
- **A field the file lacks fails every call tested on it** (`n_called_without_it`). Test what
  the file has, or say why a call without the field should be missing.
- **What normalization drops cannot be tested at `apply`**: `--info-policy drop-all` removes
  INFO. Read INFO in the facts, and decide before normalizing whether the policy needs it.

Record it as a judgment (`--stage door --name call_quality`): the policy, why it fits this file,
and the counts it changed (`n_calls_made_missing` in `call_quality.json`).

---

## B. In select, on a PGS Catalog scoring file (`SEL-13`)

**Read the header of the file you actually downloaded, every time. Never cache a column layout
per PGS ID.** A live sweep found the schema differs between builds for 85.2% of scores, so a
layout learned once is wrong later.

```bash
python3 tools/file_facts.py --input <scoring file> --comment-prefix '#' \
    --out runs/<RUN_ID>/select/scorefile_facts_<PGS_ID>.json
```

- Only **six columns are universal**: `effect_allele`, `hm_chr`, `hm_pos`, `hm_rsID`, `hm_source`,
  `hm_inferOtherAllele`. That is the whole contract.
- Read `hm_chr` / `hm_pos`. **Never** `chr_name` / `chr_position` — hundreds of models lack them.
- Many files have no `other_allele`; `hm_inferOtherAllele` is what you get instead, and matching
  has to cope with an inferred other-allele rather than assume a stated one.
- The `#` header also carries the provenance to capture per `DATA-6`: `#pgs_id`, `#format_version`,
  `#genome_build`, `#HmPOS_build`, `#HmPOS_date`, `#variants_number`, `#weight_type`. `#HmPOS_date`
  is the field that dates the position mapping — the part that can change between downloads even
  when the published weights do not.
- Check `#weight_type`. If the model is not an additive weighted sum — dosage-per-genotype,
  recessive, dominant, interaction, diplotype, haplotype — a weighted-sum computation cannot
  compute it (`SCO-9`). That fork is unresolved; surface it and pick an additive model rather
  than resolving it silently.

**You** decide the column mapping from what the header actually says, and you pass that mapping
explicitly to the downstream tools. No tool guesses a column layout.
