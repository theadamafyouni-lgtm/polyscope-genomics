#!/usr/bin/env python3
"""intake_hygiene.py -- mechanical facts about the container the person handed us (INTAKE-4).

Reports what the bytes ARE. Decides nothing.

It does not say "accept" or "reject", does not say whether a row count is plausible,
and does not name an assay class. INTAKE-4's rule (archives only, because a raw
uncompressed export opened in a spreadsheet silently mangles positions into scientific
notation) is applied by the `read-and-reason` skill using these facts -- not here.

Usage:
  python3 tools/intake_hygiene.py --input sample/patient.array-23andme.zip \
      [--run RUN_ID] [--out runs/RUN_ID/door/hygiene.json]
"""
import argparse
import hashlib
import os
import sys
import zipfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import lib_run  # noqa: E402

# Container signatures, by leading bytes. Reading a magic number is extraction;
# what to do about it is the skill's call.
MAGIC = [
    (b"PK\x03\x04", "zip"),
    (b"PK\x05\x06", "zip-empty"),
    (b"\x1f\x8b", "gzip"),
    (b"BZh", "bzip2"),
    (b"\xfd7zXZ\x00", "xz"),
    (b"7z\xbc\xaf\x27\x1c", "7z"),
]


def sniff_container(path):
    with open(path, "rb") as fh:
        head = fh.read(16)
    for sig, name in MAGIC:
        if head.startswith(sig):
            return name, head.hex()
    # Not a recognised archive signature. Say only that, plus whether it decodes as text.
    try:
        head.decode("utf-8")
        return "uncompressed-text-or-unknown", head.hex()
    except UnicodeDecodeError:
        return "unknown-binary", head.hex()


def line_stats(raw):
    """Count line terminators and rows without normalising anything (INTAKE-4 asks the
    door to handle both CRLF and LF; counting them is mechanical)."""
    crlf = raw.count(b"\r\n")
    cr_total = raw.count(b"\r")
    lf_total = raw.count(b"\n")
    return {
        "bytes": len(raw),
        "crlf_terminators": crlf,
        "lf_only_terminators": lf_total - crlf,
        "cr_only_terminators": cr_total - crlf,
        "total_line_terminators": lf_total + (cr_total - crlf),
        "final_byte_is_newline": raw.endswith(b"\n") or raw.endswith(b"\r"),
        "null_bytes": raw.count(b"\x00"),
    }


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--input", required=True)
    ap.add_argument("--run")
    ap.add_argument("--out")
    ap.add_argument("--max-member-bytes", type=int, default=64 * 1024 * 1024,
                    help="refuse to read a member larger than this into memory for line counting")
    args = ap.parse_args()

    path = os.path.abspath(args.input)
    container, magic_hex = sniff_container(path)

    facts = {
        "tool": "intake_hygiene.py",
        "generated_utc": lib_run.utcnow(),
        "input": {
            "path": args.input,
            "basename": os.path.basename(path),
            "bytes": os.path.getsize(path),
            "sha256": lib_run.sha256_file(path),
        },
        "container": {
            "signature_bytes_hex": magic_hex[:12],
            "detected_from_magic": container,
        },
        "members": [],
        "notes": [
            "sha256 above is computed here; no vendor-supplied checksum ships with the "
            "archive, so there is nothing external to verify it against (see OPEN_QUESTIONS.md Q1).",
        ],
    }

    if container == "zip":
        with zipfile.ZipFile(path) as zf:
            for info in zf.infolist():
                member = {
                    "name": info.filename,
                    "uncompressed_bytes": info.file_size,
                    "compressed_bytes": info.compress_size,
                    "crc32_from_central_directory": format(info.CRC, "08x"),
                    "modified": "%04d-%02d-%02dT%02d:%02d:%02d" % info.date_time,
                    "is_directory": info.is_dir(),
                }
                if not info.is_dir() and info.file_size <= args.max_member_bytes:
                    raw = zf.read(info.filename)
                    member["line_stats"] = line_stats(raw)
                    member["sha256"] = hashlib.sha256(raw).hexdigest()
                    head = raw[:4096].split(b"\n")[:3]
                    member["first_bytes_preview"] = [
                        b.decode("utf-8", "replace").rstrip("\r") for b in head
                    ]
                elif not info.is_dir():
                    member["line_stats"] = {"skipped": "member larger than --max-member-bytes"}
                facts["members"].append(member)
    else:
        facts["members"].append({
            "name": os.path.basename(path),
            "note": "not a zip container; no member list extracted",
        })

    lib_run.emit(facts, args.out)
    if args.run:
        lib_run.ledger_append(args.run, {
            "kind": "facts",
            "stage": "door",
            "tool": "intake_hygiene.py",
            "input_sha256": facts["input"]["sha256"],
            "artifact": args.out,
        })


if __name__ == "__main__":
    main()
