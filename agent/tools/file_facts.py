#!/usr/bin/env python3
"""file_facts.py -- mechanical census of a delimited text file with a comment header.

This is the extractor behind the `read-and-reason` skill. It is deliberately generic:
it knows about lines, columns, delimiters and counts. It knows nothing about genomes.

There is NO build detection here, NO assay classification here, and no table of
reference-assembly coordinates anywhere in this file. It reports the header verbatim
and counts what is in the columns; the agent reads that and reasons (ARCH-5, INTAKE-1,
SCO-1, SEL-13). The same tool serves the patient's file at the door and the scoring
model file in select -- one capability, two inputs (ARCH-3).

Reads plain text, .gz, or a single member inside a .zip.

Usage:
  # first pass: see the header and the column shape
  python3 tools/file_facts.py --input sample/patient.array-23andme.zip

  # second pass: the agent, having read the header, asks for specific censuses
  python3 tools/file_facts.py --input sample/patient.array-23andme.zip \
      --token-census-columns 3 --group-column 1 --value-column 2 \
      --key-columns 1,2 --lookup-column 0 --lookup-values rs3131972,rs6681049
"""
import argparse
import collections
import gzip
import io
import os
import sys
import zipfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import lib_run  # noqa: E402


def open_text(path, member=None):
    """Return a text stream for a plain / gzipped / zipped file."""
    if path.endswith(".zip"):
        zf = zipfile.ZipFile(path)
        names = [i.filename for i in zf.infolist() if not i.is_dir()]
        if member is None:
            if len(names) != 1:
                raise SystemExit(f"zip holds {len(names)} members; pass --member (one of {names})")
            member = names[0]
        return io.TextIOWrapper(zf.open(member), encoding="utf-8", errors="replace"), member
    if path.endswith(".gz"):
        return io.TextIOWrapper(gzip.open(path, "rb"), encoding="utf-8", errors="replace"), None
    return open(path, encoding="utf-8", errors="replace"), None


def parse_int_list(s):
    return [int(x) for x in s.split(",")] if s else []


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--input", required=True)
    ap.add_argument("--member", help="member name when --input is a zip with several files")
    ap.add_argument("--comment-prefix", default="#")
    ap.add_argument("--delimiter", default="\t", help=r"field delimiter; use '\t' for tab (default)")
    ap.add_argument("--max-header-lines", type=int, default=200)
    ap.add_argument("--sample-data-lines", type=int, default=5,
                    help="verbatim first N data lines")
    ap.add_argument("--tail-data-lines", type=int, default=2,
                    help="verbatim last N data lines")
    ap.add_argument("--token-census-columns", default="",
                    help="0-based columns to count distinct values in, comma separated")
    ap.add_argument("--token-census-max", type=int, default=40,
                    help="report full counts up to this cardinality, else top-N plus cardinality")
    ap.add_argument("--token-census-by-group", action="store_true",
                    help="cross --token-census-columns with --group-column")
    ap.add_argument("--group-column", type=int, default=None,
                    help="0-based column to group by (e.g. a chromosome column)")
    ap.add_argument("--value-column", type=int, default=None,
                    help="0-based numeric column to summarise per group (e.g. a position column)")
    ap.add_argument("--key-columns", default="",
                    help="0-based columns forming a key, for duplicate counting")
    ap.add_argument("--lookup-column", type=int, default=None,
                    help="0-based column to look values up in")
    ap.add_argument("--lookup-values", default="",
                    help="comma separated values to fetch whole rows for")
    ap.add_argument("--run")
    ap.add_argument("--out")
    args = ap.parse_args()

    delim = args.delimiter.replace("\\t", "\t")
    census_cols = parse_int_list(args.token_census_columns)
    key_cols = parse_int_list(args.key_columns)
    lookups = set(v for v in args.lookup_values.split(",") if v)

    stream, member = open_text(args.input, args.member)

    header_lines = []
    sample_rows = []
    tail_rows = collections.deque(maxlen=max(args.tail_data_lines, 0))
    n_data = 0
    colcount_census = collections.Counter()
    token_census = {c: collections.Counter() for c in census_cols}
    groups = {}
    by_group = collections.defaultdict(collections.Counter)
    key_seen = collections.Counter()
    found = {}
    numeric_ok = collections.defaultdict(lambda: True)
    numeric_min, numeric_max = {}, {}
    header_done = False

    with stream:
        for raw in stream:
            line = raw.rstrip("\n").rstrip("\r")
            if not header_done and line.startswith(args.comment_prefix):
                if len(header_lines) < args.max_header_lines:
                    header_lines.append(line)
                continue
            if line == "":
                continue
            header_done = True
            fields = line.split(delim)
            n_data += 1
            colcount_census[len(fields)] += 1
            if len(sample_rows) < args.sample_data_lines:
                sample_rows.append(fields)
            if args.tail_data_lines:
                tail_rows.append(fields)

            for c in census_cols:
                if c < len(fields):
                    token_census[c][fields[c]] += 1
                    if args.token_census_by_group and args.group_column is not None \
                            and args.group_column < len(fields):
                        by_group[(fields[args.group_column], c)][fields[c]] += 1

            if args.group_column is not None and args.group_column < len(fields):
                g = fields[args.group_column]
                entry = groups.setdefault(g, {"rows": 0, "value_min": None, "value_max": None,
                                              "value_non_numeric": 0})
                entry["rows"] += 1
                if args.value_column is not None and args.value_column < len(fields):
                    try:
                        v = int(fields[args.value_column])
                    except ValueError:
                        entry["value_non_numeric"] += 1
                    else:
                        if entry["value_min"] is None or v < entry["value_min"]:
                            entry["value_min"] = v
                        if entry["value_max"] is None or v > entry["value_max"]:
                            entry["value_max"] = v

            if key_cols:
                key_seen[tuple(fields[c] if c < len(fields) else "" for c in key_cols)] += 1

            if args.lookup_column is not None and lookups and args.lookup_column < len(fields):
                v = fields[args.lookup_column]
                if v in lookups and v not in found:
                    found[v] = fields

            # cheap numeric range per column, only while every value so far parsed
            for i, val in enumerate(fields):
                if not numeric_ok[i]:
                    continue
                try:
                    v = int(val)
                except ValueError:
                    numeric_ok[i] = False
                    continue
                numeric_min[i] = v if i not in numeric_min else min(numeric_min[i], v)
                numeric_max[i] = v if i not in numeric_max else max(numeric_max[i], v)

    facts = {
        "tool": "file_facts.py",
        "generated_utc": lib_run.utcnow(),
        "input": {"path": args.input, "member": member, "delimiter_used": repr(delim)},
        "comment_header": {
            "prefix": args.comment_prefix,
            "n_lines_captured": len(header_lines),
            "truncated": len(header_lines) >= args.max_header_lines,
            "lines_verbatim": header_lines,
        },
        "shape": {
            "n_data_rows": n_data,
            "column_count_census": {str(k): v for k, v in sorted(colcount_census.items())},
        },
        "first_data_rows_verbatim": sample_rows,
        "last_data_rows_verbatim": list(tail_rows),
        "numeric_column_ranges": {
            str(i): {"min": numeric_min[i], "max": numeric_max[i]}
            for i in sorted(numeric_min) if numeric_ok[i]
        },
    }

    if census_cols:
        out = {}
        for c, counter in token_census.items():
            if len(counter) <= args.token_census_max:
                out[str(c)] = {"cardinality": len(counter), "counts": dict(counter.most_common())}
            else:
                out[str(c)] = {"cardinality": len(counter),
                               "top": dict(counter.most_common(args.token_census_max))}
        facts["token_census"] = out

    if by_group:
        facts["token_census_by_group"] = {
            f"{g}|col{c}": dict(counter.most_common())
            for (g, c), counter in sorted(by_group.items(), key=lambda kv: (len(kv[0][0]), kv[0]))
        }

    if groups:
        facts["group_summary"] = {
            "group_column": args.group_column,
            "value_column": args.value_column,
            "groups": {k: groups[k] for k in sorted(groups, key=lambda x: (len(x), x))},
        }

    if key_cols:
        dupes = {"|".join(k): v for k, v in key_seen.items() if v > 1}
        facts["duplicate_keys"] = {
            "key_columns": key_cols,
            "n_distinct_keys": len(key_seen),
            "n_keys_appearing_more_than_once": len(dupes),
            "n_extra_rows_from_duplicates": sum(v - 1 for v in dupes.values()),
            "examples": dict(list(sorted(dupes.items()))[:20]),
        }

    if lookups:
        facts["lookups"] = {
            "column": args.lookup_column,
            "found": found,
            "not_found": sorted(lookups - set(found)),
        }

    lib_run.emit(facts, args.out)
    if args.run:
        lib_run.ledger_append(args.run, {
            "kind": "facts", "stage": "door", "tool": "file_facts.py",
            "input": args.input, "artifact": args.out,
        })


if __name__ == "__main__":
    main()
