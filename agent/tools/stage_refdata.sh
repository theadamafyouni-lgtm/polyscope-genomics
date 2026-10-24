#!/usr/bin/env bash
# stage_refdata.sh -- fetch the external artifacts the slice needs, and record what was fetched.
#
# MECHANICAL ONLY. This script downloads bytes and records identifiers. It makes no
# judgment about the data (ARCH-5). Every artifact lands in refdata/ with its URL,
# size, sha256 and retrieval timestamp captured in refdata/MANIFEST.json, per DATA-6
# (do not version-pin; record what was used at run time) and R6's capture contract
# (record from the object that travelled with the result).
#
# Idempotent and resumable: an artifact already present with a recorded sha256 is skipped.
#
# Usage:  bash tools/stage_refdata.sh [artifact ...]
#         bash tools/stage_refdata.sh              # everything
#         bash tools/stage_refdata.sh plink2 jre   # named subset
set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
REF="$ROOT/refdata"
LOG="$REF/staging.log"
MAN="$REF/MANIFEST.json"
mkdir -p "$REF"/{bin,jre,maps,panel,fasta,ancestry-ref,tmp}

log() { printf '[%s] %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*" | tee -a "$LOG"; }

# record <key> <path> <url>  -- append one artifact record to the manifest fragment dir
record() {
  local key="$1" path="$2" url="$3"
  local sha size
  sha=$(shasum -a 256 "$path" | awk '{print $1}')
  size=$(wc -c < "$path" | tr -d ' ')
  mkdir -p "$REF/tmp/records"
  cat > "$REF/tmp/records/$key.json" <<EOF
{"key":"$key","path":"${path#$ROOT/}","url":"$url","sha256":"$sha","bytes":$size,"retrieved_utc":"$(date -u +%Y-%m-%dT%H:%M:%SZ)"}
EOF
  log "recorded $key ($size bytes, sha256 ${sha:0:16}...)"
}

have() { [ -s "$1" ] && [ -f "$REF/tmp/records/$2.json" ]; }

# Refuse to run two writers against the same destination. A concurrent resume silently produces
# a file LARGER than the source, which then fails deep inside a pipeline rather than here.
lock() {
  local dest="$1"
  if [ -e "$dest.lock" ] && kill -0 "$(cat "$dest.lock" 2>/dev/null)" 2>/dev/null; then
    log "SKIP: another staging process (pid $(cat "$dest.lock")) is fetching $(basename "$dest")"
    return 1
  fi
  echo $$ > "$dest.lock"
}
unlock() { rm -f "$1.lock"; }

fetch() {  # fetch <url> <dest>
  local url="$1" dest="$2"
  lock "$dest" || return 1
  log "GET $url"
  curl -fL --retry 5 --retry-delay 5 --retry-all-errors -C - -o "$dest" "$url" 2>>"$LOG"
  local rc=$?
  unlock "$dest"
  [ $rc -ne 0 ] && return $rc
  verify_size "$url" "$dest" || return 1
}

# Compare the fetched size against the server's Content-Length. A mismatch in EITHER direction is
# a corrupt fetch -- larger means a bad resume appended, smaller means truncation.
verify_size() {
  local url="$1" dest="$2"
  local remote local_sz
  remote=$(curl -sIL -m 30 "$url" | awk 'tolower($1)=="content-length:"{n=$2} END{print n+0}' | tr -d '\r')
  local_sz=$(wc -c < "$dest" | tr -d ' ')
  if [ "$remote" -le 0 ]; then
    # Cannot confirm the size -> do not record it as good. Silently skipping this check is how a
    # corrupt file got recorded once already.
    log "UNVERIFIABLE: no Content-Length for $(basename "$dest"); refusing to record it"
    return 1
  fi
  if [ "$remote" != "$local_sz" ]; then
    log "CORRUPT: $(basename "$dest") is $local_sz bytes, source says $remote. Removing."
    rm -f "$dest"
    return 1
  fi
  # bgzf files must carry the EOF marker; a truncated one reads fine until it doesn't
  case "$dest" in
    *.gz|*.bgz)
      if ! bgzip -t "$dest" 2>/dev/null && ! gzip -t "$dest" 2>/dev/null; then
        log "CORRUPT: $(basename "$dest") fails a gzip/bgzf integrity check. Removing."
        rm -f "$dest"
        return 1
      fi ;;
  esac
  return 0
}

# ---------------------------------------------------------------- plink2
stage_plink2() {
  local dest="$REF/bin/plink2"
  have "$dest" plink2 && { log "plink2 present, skipping"; return 0; }
  # The "latest" alias 404s; the assets are date-stamped. Resolve the newest arm64 build
  # from the downloads page at run time rather than pinning a date (DATA-6).
  local url
  url=$(curl -s -m 30 https://www.cog-genomics.org/plink/2.0/ \
        | grep -oE 'https://s3\.amazonaws\.com/plink2-assets/[a-z0-9]+/plink2_mac_arm64_[0-9]{8}\.zip' \
        | sort -u | tail -1)
  [ -z "$url" ] && { log "ERROR: could not resolve a plink2 mac arm64 build from the downloads page"; return 1; }
  log "resolved plink2 build: $url"
  fetch "$url" "$REF/tmp/plink2.zip" || return 1
  unzip -oq "$REF/tmp/plink2.zip" -d "$REF/tmp/plink2dir" || return 1
  mv "$REF/tmp/plink2dir/plink2" "$dest" && chmod +x "$dest"
  record plink2 "$dest" "$url"
  "$dest" --version 2>&1 | head -1 | tee -a "$LOG"
}

# ---------------------------------------------------------------- java runtime (sudo-free, local)
stage_jre() {
  local marker="$REF/jre/bin/java"
  have "$marker" jre && { log "jre present, skipping"; return 0; }
  local url="https://api.adoptium.net/v3/binary/latest/21/ga/mac/aarch64/jre/hotspot/normal/eclipse"
  fetch "$url" "$REF/tmp/jre.tar.gz" || return 1
  rm -rf "$REF/tmp/jredir"; mkdir -p "$REF/tmp/jredir"
  tar xzf "$REF/tmp/jre.tar.gz" -C "$REF/tmp/jredir" || return 1
  local home
  home=$(find "$REF/tmp/jredir" -maxdepth 3 -type d -name Home | head -1)
  [ -z "$home" ] && { log "ERROR: no Contents/Home in jre tarball"; return 1; }
  rm -rf "$REF/jre"; mkdir -p "$REF/jre"
  cp -R "$home"/* "$REF/jre/"
  record jre "$REF/tmp/jre.tar.gz" "$url"
  "$marker" -version 2>&1 | head -1 | tee -a "$LOG"
}

# ---------------------------------------------------------------- beagle + unbref3
stage_beagle() {
  local bj="$REF/bin/beagle.jar" uj="$REF/bin/unbref3.jar"
  local burl="https://faculty.washington.edu/browning/beagle/beagle.27Feb25.75f.jar"
  local uurl="https://faculty.washington.edu/browning/beagle/unbref3.27Feb25.75f.jar"
  have "$bj" beagle || { fetch "$burl" "$bj" && record beagle "$bj" "$burl"; }
  have "$uj" unbref3 || { fetch "$uurl" "$uj" && record unbref3 "$uj" "$uurl"; }
}

# ---------------------------------------------------------------- genetic maps (b37)
stage_maps() {
  local marker="$REF/maps/plink.chr22.GRCh37.map"
  have "$marker" maps && { log "maps present, skipping"; return 0; }
  local url="https://bochet.gcc.biostat.washington.edu/beagle/genetic_maps/plink.GRCh37.map.zip"
  fetch "$url" "$REF/tmp/maps.zip" || return 1
  unzip -oq "$REF/tmp/maps.zip" -d "$REF/maps" || return 1
  record maps "$REF/tmp/maps.zip" "$url"
}

# ---------------------------------------------------------------- 1000G phase3 b37 imputation panel (bref3)
stage_panel() {
  local base="https://bochet.gcc.biostat.washington.edu/beagle/1000_Genomes_phase3_v5a/b37.bref3"
  local ok=1
  for c in $(seq 1 22); do
    local dest="$REF/panel/chr$c.1kg.phase3.v5a.b37.bref3"
    local url="$base/chr$c.1kg.phase3.v5a.b37.bref3"
    if have "$dest" "panel_chr$c"; then log "panel chr$c present, skipping"; continue; fi
    if fetch "$url" "$dest"; then record "panel_chr$c" "$dest" "$url"; else ok=0; log "ERROR panel chr$c"; fi
  done
  return $((1-ok))
}

# ---------------------------------------------------------------- GRCh37 reference FASTA (for bcftools --tsv2vcf)
stage_fasta() {
  local dest="$REF/fasta/GRCh37.fa.gz"
  have "$dest" fasta && [ -s "$dest.fai" ] && { log "fasta present, skipping"; return 0; }
  local url="https://ftp.ensembl.org/pub/grch37/current/fasta/homo_sapiens/dna/Homo_sapiens.GRCh37.dna.primary_assembly.fa.gz"
  fetch "$url" "$REF/tmp/GRCh37.fa.gz" || return 1
  record fasta "$REF/tmp/GRCh37.fa.gz" "$url"
  # Ensembl ships plain gzip; bcftools needs bgzip + faidx. Recompress.
  log "recompressing FASTA to bgzf (this takes a few minutes)"
  gunzip -c "$REF/tmp/GRCh37.fa.gz" | bgzip -@ 4 -c > "$dest" || return 1
  # .fai/.gzi: samtools if present, otherwise htslib builds them on first bcftools use.
  if command -v samtools >/dev/null 2>&1; then
    samtools faidx "$dest" && log "faidx built by samtools"
  else
    log "NOTE: samtools absent; htslib will build .fai/.gzi on first bcftools -f use"
  fi
  log "fasta ready: $dest"
}

# ---------------------------------------------------------------- ancestry reference (array-derived 1000G callset)
stage_ancestry_ref() {
  local vcf="$REF/ancestry-ref/1kg.omni_broad_sanger_combined.b37.vcf.gz"
  local pan="$REF/ancestry-ref/integrated_call_samples_v3.20130502.ALL.panel"
  local vurl="https://ftp.1000genomes.ebi.ac.uk/vol1/ftp/release/20130502/supporting/hd_genotype_chip/ALL.chip.omni_broad_sanger_combined.20140818.snps.genotypes.vcf.gz"
  local purl="https://ftp.1000genomes.ebi.ac.uk/vol1/ftp/release/20130502/integrated_call_samples_v3.20130502.ALL.panel"
  have "$pan" ancestry_panel || { fetch "$purl" "$pan" && record ancestry_panel "$pan" "$purl"; }
  have "$vcf" ancestry_vcf || { fetch "$vurl" "$vcf" && record ancestry_vcf "$vcf" "$vurl"; }
}

# ---------------------------------------------------------------- 1000G phase3 site list + allele frequencies
# Serves two distinct needs, both of which the spec names:
#   INTAKE-3 -- restore ALT after conversion (annotate against a dbSNP / 1000G site list),
#               so directly-typed markers are not silently dropped before imputation.
#   SEL-11   -- the select-stage coverage estimate needs "a reference panel's allele frequency".
stage_sites() {
  # Sourced from the AWS Open Data mirror rather than EBI: the EBI copy delivered ~0.5 MB/s and
  # failed a gzip integrity check twice at full size, while this mirror runs at ~7.6 MB/s.
  # PROVENANCE NOTE: this is release v5b. The Beagle imputation panel is built from v5a. Both are
  # the same 1000 Genomes phase 3 callset with later minor corrections; the difference is recorded
  # here rather than smoothed over, because which release supplied an allele is provenance.
  local vcf="$REF/panel/1kg.phase3.v5b.sites.b37.vcf.gz"
  local base="https://1000genomes.s3.amazonaws.com/release/20130502"
  local vurl="$base/ALL.wgs.phase3_shapeit2_mvncall_integrated_v5b.20130502.sites.vcf.gz"
  have "$vcf" sites_vcf || { fetch "$vurl" "$vcf" && record sites_vcf "$vcf" "$vurl"; }
  have "$vcf.tbi" sites_tbi || { fetch "$vurl.tbi" "$vcf.tbi" && record sites_tbi "$vcf.tbi" "$vurl.tbi"; }
}

# ---------------------------------------------------------------- integrity re-check
# Two different checks, at two different times, for two different failure modes:
#   at FETCH time  -- strict Content-Length match plus a gzip/bgzf integrity test, which catches a
#                     bad download (a concurrent resume once produced a file LARGER than its
#                     source, and that only surfaced deep inside a pipeline).
#   at VERIFY time -- sha256 against what was recorded, which catches anything that changed on
#                     disk afterwards. Comparing a local artifact to a remote Content-Length is
#                     the wrong check: it false-positived on the plink2 binary, whose record
#                     points at the extracted executable rather than the zip it came from.
stage_verify() {
  local bad=0
  for rec in "$REF"/tmp/records/*.json; do
    [ -e "$rec" ] || continue
    local path sha_rec sha_now
    path=$(python3 -c "import json,sys;print(json.load(open(sys.argv[1]))['path'])" "$rec")
    sha_rec=$(python3 -c "import json,sys;print(json.load(open(sys.argv[1]))['sha256'])" "$rec")
    if [ ! -s "$ROOT/$path" ]; then
      log "MISSING: $path (record dropped; re-run staging for it)"
      rm -f "$rec"; bad=1; continue
    fi
    sha_now=$(shasum -a 256 "$ROOT/$path" | awk '{print $1}')
    if [ "$sha_now" = "$sha_rec" ]; then
      log "OK: $path"
    else
      log "CHANGED SINCE RECORDED: $path (record dropped; re-run staging for it)"
      rm -f "$rec"; bad=1
    fi
  done
  return $bad
}

# ---------------------------------------------------------------- manifest assembly
write_manifest() {
  python3 - "$REF" <<'PY'
import json, os, sys, glob, datetime
ref = sys.argv[1]
recs = []
for f in sorted(glob.glob(os.path.join(ref, "tmp", "records", "*.json"))):
    with open(f) as fh:
        recs.append(json.load(fh))
out = {
    "note": "Run-time capture of external artifacts (DATA-6, R6). Not version pinning: "
            "these identifiers record what was used, they do not freeze it.",
    "assembled_utc": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    "artifacts": recs,
}
with open(os.path.join(ref, "MANIFEST.json"), "w") as fh:
    json.dump(out, fh, indent=2)
print(f"MANIFEST.json: {len(recs)} artifacts")
PY
}

main() {
  local targets=("$@")
  [ ${#targets[@]} -eq 0 ] && targets=(plink2 jre beagle maps fasta sites ancestry_ref panel)
  log "=== staging start: ${targets[*]} ==="
  for t in "${targets[@]}"; do
    case "$t" in
      plink2)        stage_plink2 ;;
      jre)           stage_jre ;;
      beagle)        stage_beagle ;;
      maps)          stage_maps ;;
      fasta)         stage_fasta ;;
      ancestry_ref)  stage_ancestry_ref ;;
      sites)         stage_sites ;;
      verify)        stage_verify ;;
      panel)         stage_panel ;;
      *) log "unknown artifact: $t" ;;
    esac
  done
  write_manifest
  log "=== staging done ==="
}

main "$@"
