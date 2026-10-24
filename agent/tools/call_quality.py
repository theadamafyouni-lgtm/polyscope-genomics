#!/usr/bin/env python3
"""call_quality.py -- a sequencing file's own call-quality fields, reported; the agent's policy,
applied.

Mechanical, two steps (read-and-reason, A6):

  facts   reads the person's VCF as received and reports what quality evidence it carries and
          how it is spread: the FILTER values it uses and declares, QUAL, and every numeric
          one-value INFO and FORMAT field (DP, GQ and whatever else the caller wrote), each as
          counts and quantiles -- FORMAT fields over the called genotypes, with how many called
          genotypes lack the field, since a policy testing a field a call lacks fails that call.
          It says nothing about what is good enough: that is the agent's policy for this file.

  apply   sets to missing, in the normalized VCF every stage reads, each genotype whose record
          fails the agent's policy -- a bcftools filter expression -- and keeps every record. A
          missing genotype is then what it is anywhere else: not typed, imputed where the panel
          can, never scored as reference. The file before the policy is kept beside it, and the
          policy can be applied once.

Usage:
  python3 tools/call_quality.py facts --run <RUN_ID> --input <the person's VCF> \\
      --out runs/<RUN_ID>/door/call_quality_facts.json
  python3 tools/call_quality.py apply --run <RUN_ID> --vcf runs/<RUN_ID>/door/normalized.vcf.gz \\
      --include 'FILTER="PASS" && FMT/GQ>=20 && FMT/DP>=10' \\
      --out runs/<RUN_ID>/door/call_quality.json
"""
import argparse
from array import array
import gzip
import os
import re
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import lib_run  # noqa: E402

BCFTOOLS = "bcftools"
QUANTILES = (1, 5, 10, 25, 50, 75, 90, 95, 99)
DECLARED = re.compile(r'##(FILTER|INFO|FORMAT)=<ID=([^,>]+)(?:,Number=([^,>]+))?(?:,Type=([^,>]+))?')
DESCRIPTION = re.compile(r'Description="([^"]*)"')


def spread(values):
    if not len(values):
        return {}
    import numpy as np
    v = np.frombuffer(values, dtype=np.float64)
    out = {"min": float(v.min()), "max": float(v.max()), "mean": float(v.mean())}
    out.update({"p%02d" % q: float(x) for q, x in zip(QUANTILES, np.percentile(v, QUANTILES))})
    return out


def number(text):
    try:
        x = float(text)
    except ValueError:
        return None
    return x if x == x and abs(x) != float("inf") else None


def facts(args):
    declared = {"FILTER": {}, "INFO": {}, "FORMAT": {}}
    filters, qual = {}, array("d")
    n_records = n_called = qual_missing = 0
    info, info_seen = {}, {}
    fmt, fmt_with, fmt_without = {}, {}, {}
    sample = None
    opener = gzip.open if open(args.input, "rb").read(2) == b"\x1f\x8b" else open
    with opener(args.input, "rt", encoding="utf-8", errors="replace") as fh:
        for line in fh:
            if line.startswith("##"):
                m = DECLARED.match(line.rstrip("\n"))
                if m:
                    kind, ident, num, typ = m.groups()
                    d = DESCRIPTION.search(line)
                    desc = d[1] if d else None
                    declared[kind][ident] = (desc if kind == "FILTER" else
                                             {"number": num, "type": typ, "description": desc})
                continue
            if line.startswith("#CHROM"):
                names = line.rstrip("\n").split("\t")[9:]
                if args.sample:
                    sample = 9 + names.index(args.sample)
                elif len(names) == 1:
                    sample = 9
                else:
                    raise SystemExit("call_quality.py: the VCF has %d samples; name one with --sample" % len(names))
                continue
            f = line.rstrip("\n").split("\t")
            n_records += 1
            for value in f[6].split(";"):
                filters[value] = filters.get(value, 0) + 1
            q = number(f[5])
            if q is None:
                qual_missing += 1
            else:
                qual.append(q)
            for item in f[7].split(";"):
                key, _, value = item.partition("=")
                spec = declared["INFO"].get(key)
                if value and spec and spec["number"] == "1" and spec["type"] in ("Integer", "Float"):
                    x = number(value)
                    if x is not None:
                        info.setdefault(key, array("d")).append(x)
                        info_seen[key] = info_seen.get(key, 0) + 1
            if sample is None or len(f) <= sample:
                continue
            keys, values = f[8].split(":"), f[sample].split(":")
            gt = values[keys.index("GT")] if "GT" in keys and keys.index("GT") < len(values) else "."
            if not gt or any(a in (".", "") for a in re.split(r"[/|]", gt)):
                continue
            n_called += 1
            for key, spec in declared["FORMAT"].items():
                if spec["number"] != "1" or spec["type"] not in ("Integer", "Float"):
                    continue
                i = keys.index(key) if key in keys else -1
                x = number(values[i]) if 0 <= i < len(values) else None
                if x is None:
                    fmt_without[key] = fmt_without.get(key, 0) + 1
                else:
                    fmt.setdefault(key, array("d")).append(x)
                    fmt_with[key] = fmt_with.get(key, 0) + 1
    numeric_format = [k for k, s in declared["FORMAT"].items() if s["number"] == "1" and s["type"] in ("Integer", "Float")]
    result = {
        "tool": "call_quality.py facts",
        "generated_utc": lib_run.utcnow(),
        "input": args.input,
        "n_records": n_records,
        "n_called": n_called,
        "declared": declared,
        "filter_values": dict(sorted(filters.items(), key=lambda kv: -kv[1])),
        "qual": dict(spread(qual), n_records_with_it=len(qual), n_missing=qual_missing),
        "info": {k: dict(spread(v), n_records_with_it=info_seen[k]) for k, v in sorted(info.items())},
        "format": {k: dict(spread(fmt.get(k, array("d"))), n_called_with_it=fmt_with.get(k, 0),
                           n_called_without_it=fmt_without.get(k, 0)) for k in sorted(numeric_format)},
        "what_this_does_not_do": [
            "say which calls are reliable, or what a field's value should be -- the policy is the agent's",
            "read multi-value fields (AD, PL) -- their presence is in `declared`",
            "check that what the header declares is what the records carry",
        ],
    }
    lib_run.emit(result, args.out)
    if args.run:
        lib_run.ledger_append(args.run, {"kind": "facts", "stage": "door", "tool": "call_quality.py facts",
                                         "artifact": args.out})


def sh(cmd):
    done = subprocess.run(cmd, capture_output=True, text=True)
    if done.returncode:
        raise RuntimeError("%s failed: %s" % (" ".join(cmd[:3]), done.stderr.strip()[-2000:]))
    return done.stdout


def counts(vcf):
    records = int(sh([BCFTOOLS, "index", "-n", vcf]).strip())
    called = sum(1 for _ in sh([BCFTOOLS, "view", "-H", "-g", "^miss", vcf]).splitlines())
    return records, called


def apply(args):
    vcf = args.vcf
    if not vcf.endswith(".vcf.gz"):
        raise SystemExit("call_quality.py: --vcf is the bgzipped, indexed normalized VCF")
    before = vcf[:-len(".vcf.gz")] + ".before_call_quality.vcf.gz"
    if os.path.lexists(before):
        raise SystemExit("call_quality.py: a call-quality policy was already applied to %s (the file "
                         "before it is %s); it is applied once, not stacked" % (vcf, before))
    n_records, n_called = counts(vcf)
    os.rename(vcf, before)
    os.rename(vcf + ".tbi", before + ".tbi")
    try:
        sh([BCFTOOLS, "filter", "-i", args.include, "-S", ".", before, "-Oz", "-o", vcf])
        sh([BCFTOOLS, "index", "-f", "-t", vcf])
        n_after, n_called_after = counts(vcf)
        if n_after != n_records:
            raise RuntimeError("the policy dropped records (%d of %d kept)" % (n_after, n_records))
    except (RuntimeError, ValueError) as exc:
        for path in (vcf, vcf + ".tbi"):
            if os.path.exists(path):
                os.remove(path)
        os.rename(before, vcf)
        os.rename(before + ".tbi", vcf + ".tbi")
        raise SystemExit("call_quality.py: the policy was not applied, and %s is as it was: %s" % (vcf, exc))
    result = {
        "tool": "call_quality.py apply",
        "generated_utc": lib_run.utcnow(),
        "vcf": vcf,
        "before_policy": before,
        "include": args.include,
        "how": "bcftools filter -i <include> -S . : a record whose genotype fails it keeps its place, "
               "its genotype set to missing",
        "bcftools_version": lib_run.tool_version([BCFTOOLS, "--version"]),
        "n_records": n_records,
        "n_called_before": n_called,
        "n_called_after": n_called_after,
        "n_calls_made_missing": n_called - n_called_after,
    }
    lib_run.emit(result, args.out)
    if args.run:
        lib_run.ledger_append(args.run, {"kind": "facts", "stage": "door", "tool": "call_quality.py apply",
                                         "n_calls_made_missing": result["n_calls_made_missing"],
                                         "artifact": args.out})


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    f = sub.add_parser("facts")
    f.add_argument("--run")
    f.add_argument("--input", required=True, help="the person's VCF as received, plain or gzipped")
    f.add_argument("--sample", help="the sample, where the VCF has more than one")
    f.add_argument("--out", required=True)
    a = sub.add_parser("apply")
    a.add_argument("--run")
    a.add_argument("--vcf", required=True, help="the normalized VCF every stage reads")
    a.add_argument("--include", required=True,
                   help="no default: the agent's policy, a bcftools expression a kept call passes")
    a.add_argument("--out", required=True)
    args = ap.parse_args()
    (facts if args.cmd == "facts" else apply)(args)


if __name__ == "__main__":
    main()
