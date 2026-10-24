"""Bounded, filename-independent handling of an uploaded genome.

Only the transport container is removed. Genome records, headers and line endings
are preserved for the harness to interpret. No uploaded member name becomes a path.
The upload is read from a file and the genome streamed into another, a line at a time,
so a whole genome never sits in memory.
"""

from __future__ import annotations

import bz2
import gzip
import io
import os
import re
import stat
import struct
import zipfile
import zlib


MIB = 1024 * 1024
# Whole genomes and exomes as people receive them (input-limits.json): a consumer whole-genome
# VCF is 200-800 MB compressed, a Complete Genomics var file about 270 MB as bzip2 and 1.5-1.6 GB
# expanded (the two dev genomes), an exome VCF tens of MB. Alignments (BAM, CRAM, FASTQ) are not
# genomes the agent reads, and at 30-100 GB not uploads.
MAX_UPLOAD_BYTES = 1024 * MIB
MAX_EXPANDED_BYTES = 8 * 1024 * MIB
# No genome file has a line this long; it bounds what one line can hold in memory.
MAX_LINE_BYTES = MIB
_CHUNK = MIB
_CHROMOSOME = re.compile(r"(?:chr)?(?:[0-9]{1,2}|X|Y|XY|M|MT)", re.IGNORECASE)
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_SPLIT = re.compile(r"\s+|,")
# A Complete Genomics var or masterVar file names its columns on one '>' line.
_CG_COLUMNS = {"chromosome", "begin", "end", "varType"}
_FORMATS = "Upload a genotype table, VCF or Complete Genomics genome file."


class UploadError(ValueError):
    """An invalid, unsafe or oversized uploaded file."""


class _Layout:
    """The genome's layout, set by its first lines and held to by every line after."""

    def __init__(self):
        self.kind = None  # "table", "vcf" or "cg"
        self.records = 0
        self.first = True
        self.chromosomes = set()  # names already checked: a genome has a few dozen

    def lines(self, data: bytes):
        """Whole lines, so no UTF-8 sequence is ever cut: text and control bytes are checked
        for the block at once, the layout a line at a time."""
        if self.first:
            self.first = False
            if data.startswith(b"\xef\xbb\xbf"):
                data = data[3:]
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise UploadError("Upload a UTF-8 genome text file, or one in ZIP, gzip or bzip2.") from exc
        if _CONTROL.search(text):
            raise UploadError("The genome must contain text, without binary control bytes.")
        for line in text.split("\n"):
            self.line(line)

    def line(self, text: str):
        line = text.strip()
        if not line:
            return
        if line[0] == "#":
            if line.startswith("#CHROM\tPOS\tID\tREF\tALT\tQUAL\tFILTER\tINFO"):
                self._become("vcf")
            return
        if line[0] == ">":
            columns = line[1:].split("\t")
            if not _CG_COLUMNS <= set(columns):
                raise UploadError(_FORMATS)
            self._become("cg")
            self.width = len(columns)
            self.chromosome, self.begin, self.end = (columns.index(name) for name in ("chromosome", "begin", "end"))
            self.needed = max(self.chromosome, self.begin, self.end)
            return
        if self.kind == "cg":
            # Trailing empty columns are part of the layout, so only the line ending goes.
            fields = text.rstrip("\r").split("\t")
            if (len(fields) > self.width or len(fields) <= self.needed or not self._chromosome(fields[self.chromosome])
                    or not fields[self.begin].isdigit() or not fields[self.end].isdigit()):
                raise UploadError("The Complete Genomics file contains an invalid record layout.")
        elif self.kind == "vcf":
            fields = line.split("\t")
            if len(fields) < 8 or not fields[1].isdigit():
                raise UploadError("The VCF contains an invalid record layout.")
        else:
            self.kind = "table"
            # Split at whitespace or commas, as _SPLIT does; str.split is the same where there is no comma.
            fields = _SPLIT.split(line) if "," in line else line.split()
            if len(fields) not in (4, 5) or not fields[2].isdigit() or not self._chromosome(fields[1]):
                if [s.lower() for s in fields[:3]] == ["rsid", "chromosome", "position"]:
                    return
                raise UploadError(_FORMATS)
        self.records += 1

    def _chromosome(self, name):
        if name in self.chromosomes:
            return True
        if _CHROMOSOME.fullmatch(name) and len(self.chromosomes) < 1000:
            self.chromosomes.add(name)
            return True
        return bool(_CHROMOSOME.fullmatch(name))

    def _become(self, kind):
        # A header comes before the records it names, once.
        if self.kind is not None or self.records:
            raise UploadError(_FORMATS)
        self.kind = kind

    def finish(self):
        if not self.records:
            raise UploadError("The genome does not contain any genotype records.")
        return {"vcf": ".vcf", "cg": ".tsv"}.get(self.kind, ".txt")


def _copy(stream, destination) -> str:
    layout = _Layout()
    expanded = 0
    pending = b""
    while True:
        chunk = stream.read(_CHUNK)
        if not chunk:
            break
        expanded += len(chunk)
        if expanded > MAX_EXPANDED_BYTES:
            raise UploadError("The expanded genome exceeds the size limit.")
        destination.write(chunk)
        data = pending + chunk
        cut = data.rfind(b"\n") + 1
        if cut:
            layout.lines(data[:cut])
        pending = data[cut:]
        if len(pending) > MAX_LINE_BYTES:
            raise UploadError("The genome contains a line longer than any genome file has.")
    if pending:
        layout.lines(pending)
    return layout.finish()


def _zip_directory(source, size):
    """One member and a small central directory, read from the end record before ZipFile
    allocates anything for it. A member over 4 GiB is ZIP64; split archives are not uploads."""
    tail_size = min(size, 65557)
    source.seek(size - tail_size)
    tail = source.read(tail_size)
    end = tail.rfind(b"PK\x05\x06")
    if end < 0 or end + 22 > len(tail):
        raise UploadError("The ZIP file is incomplete or invalid.")
    _, disk, directory_disk, disk_count, count, directory_size, _, comment_size = struct.unpack(
        "<4s4H2LH", tail[end:end + 22])
    if end + 22 + comment_size != len(tail):
        raise UploadError("The ZIP file is incomplete or invalid.")
    if count == 0xFFFF or directory_size == 0xFFFFFFFF:
        locator = tail.rfind(b"PK\x06\x07", 0, end)
        if locator < 0 or locator + 20 != end:
            raise UploadError("The ZIP file is incomplete or invalid.")
        record_at = struct.unpack("<Q", tail[locator + 8:locator + 16])[0]
        source.seek(record_at)
        record = source.read(56)
        if len(record) != 56 or record[:4] != b"PK\x06\x06":
            raise UploadError("The ZIP file is incomplete or invalid.")
        disk, directory_disk, disk_count, count, directory_size = struct.unpack("<2L3Q", record[16:48])
    if disk or directory_disk or disk_count != 1 or count != 1 or directory_size > 65536:
        raise UploadError("A ZIP upload must contain exactly one regular file.")


def _copy_zip(source, size, destination) -> str:
    _zip_directory(source, size)
    source.seek(0)
    with zipfile.ZipFile(source) as archive:
        members = archive.infolist()
        if len(members) != 1:
            raise UploadError("A ZIP upload must contain exactly one regular file.")
        member = members[0]
        name = member.orig_filename
        mode = member.external_attr >> 16
        file_type = stat.S_IFMT(mode)
        if (not name or "\x00" in name or "\\" in name or ":" in name
                or name.startswith("/") or any(p in ("", ".", "..") for p in name.split("/"))
                or member.is_dir() or file_type not in (0, stat.S_IFREG)
                or member.external_attr & 0x10):
            raise UploadError("The ZIP member must be a regular file with a safe name.")
        if member.flag_bits & 1:
            raise UploadError("Encrypted ZIP files are not accepted.")
        if member.compress_type not in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED):
            raise UploadError("Use an uncompressed or deflated ZIP file.")
        if member.file_size > MAX_EXPANDED_BYTES:
            raise UploadError("The expanded genome exceeds the size limit.")
        with archive.open(member) as stream:
            return _copy(stream, destination)


def expand_upload(source, destination) -> str:
    """Stream the genome in ``source`` -- the upload as received, a seekable binary file --
    into ``destination``, a binary file, without its container. Return a trusted extension,
    or raise UploadError, having written part of it; the caller discards ``destination`` then.

    The restricted worker validates the original bytes independently of the web app.
    """
    size = source.seek(0, os.SEEK_END)
    if not size:
        raise UploadError("Choose a genome file to upload.")
    if size > MAX_UPLOAD_BYTES:
        raise UploadError("The uploaded genome exceeds the size limit.")
    source.seek(0)
    magic = source.read(4)
    source.seek(0)
    try:
        if magic.startswith(b"PK"):
            return _copy_zip(source, size, destination)
        if magic.startswith(b"\x1f\x8b"):
            # Every member of a multi-member gzip, which BGZF is.
            with gzip.GzipFile(fileobj=source) as stream:
                return _copy(stream, destination)
        if magic.startswith(b"BZh"):
            # Every stream of a multi-stream bzip2, which parallel compressors write.
            with bz2.BZ2File(source) as stream:
                return _copy(stream, destination)
        return _copy(source, destination)
    except UploadError:
        raise
    except (OSError, EOFError, ValueError, RuntimeError, zipfile.BadZipFile, zlib.error,
            NotImplementedError, struct.error) as exc:
        raise UploadError("The compressed genome is incomplete or invalid.") from exc


def validate_upload(data: bytes) -> tuple[bytes, str]:
    """Return uncompressed genome bytes and a trusted extension, or UploadError, for an upload
    small enough to hold in memory. Callers must bound ``data`` before constructing it."""
    if not isinstance(data, bytes):
        raise UploadError("The upload must be a single binary file body.")
    expanded = io.BytesIO()
    suffix = expand_upload(io.BytesIO(data), expanded)
    return expanded.getvalue(), suffix
