"""Transport boundary checks; no patient data or real harness executions."""

import bz2
import gzip
import io
import stat
import struct
import tempfile
import unittest
import zipfile
from unittest.mock import patch

from demo_site import uploads


ARRAY = b"# Example genotype data\nrs1\t1\t101\tAA\nrs2\tX\t102\t--\n"
VCF = (b"##fileformat=VCFv4.2\n#CHROM\tPOS\tID\tREF\tALT\tQUAL\tFILTER\tINFO\tFORMAT\tSAMPLE\n"
       b"1\t101\trs1\tA\tG\t.\tPASS\t.\tGT\t0/1\n")
# A Complete Genomics var file's layout, trailing empty columns included; synthetic loci.
CG_VAR = (b"#ASSEMBLY_ID\tGS000000000-ASM\n#GENOME_REFERENCE\tNCBI build 37\n#TYPE\tVAR-ANNOTATION\n"
          b">locus\tploidy\tallele\tchromosome\tbegin\tend\tvarType\treference\talleleSeq\tvarScoreVAF\t"
          b"varScoreEAF\tvarQuality\thapLink\txRef\n"
          b"1\t2\tall\tchr1\t0\t10000\tno-ref\t=\t?\t\t\t\t\t\n"
          b"2\t2\t1\tchr1\t10000\t10001\tsnp\tA\tG\t120\t118\tVQHIGH\t\tdbsnp.100:rs1\n")
CG_MASTERVAR = (b"#TYPE\tVAR-OLPL\n>locus\tploidy\tchromosome\tbegin\tend\tzygosity\tvarType\treference\t"
                b"allele1Seq\tallele2Seq\n1\t2\tchrX\t200\t201\thet-ref\tsnp\tC\tC\tT\n")


def zipped(content=ARRAY, name="genome.txt", mode=None, extra=False):
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        if mode is None:
            archive.writestr(name, content)
        else:
            info = zipfile.ZipInfo(name)
            info.create_system = 3
            info.external_attr = mode << 16
            archive.writestr(info, content)
        if extra:
            archive.writestr("another.txt", ARRAY)
    return out.getvalue()


class UploadTests(unittest.TestCase):
    def test_plain_zip_gzip_preserve_genome_bytes(self):
        for data in (ARRAY, gzip.compress(ARRAY), zipped(), zipped(name="folder/file.txt")):
            with self.subTest(data=data[:8]):
                self.assertEqual(uploads.validate_upload(data), (ARRAY, ".txt"))
        self.assertEqual(uploads.validate_upload(gzip.compress(VCF)), (VCF, ".vcf"))

    def test_split_allele_table_and_utf8_header(self):
        data = "# Génome\r\nrsid\tchromosome\tposition\tallele1\tallele2\r\nrs1\t1\t101\tA\tA\r\n".encode()
        self.assertEqual(uploads.validate_upload(data), (data, ".txt"))

    def test_upload_limit_checked_before_decompression(self):
        with patch.object(uploads, "MAX_UPLOAD_BYTES", 8):
            with self.assertRaisesRegex(uploads.UploadError, "uploaded.*size limit"):
                uploads.validate_upload(ARRAY)

    def test_expanded_limits_plain_zip_gzip_and_multiple_gzip_members(self):
        payload = ARRAY * 10
        with patch.object(uploads, "MAX_EXPANDED_BYTES", len(ARRAY)):
            for data in (payload, zipped(payload), gzip.compress(payload),
                         gzip.compress(ARRAY) + gzip.compress(ARRAY)):
                with self.subTest(kind=data[:4]), self.assertRaisesRegex(uploads.UploadError, "expanded.*size limit"):
                    uploads.validate_upload(data)
            self.assertEqual(uploads.validate_upload(gzip.compress(ARRAY)), (ARRAY, ".txt"))

    def test_zip_names_types_and_member_count(self):
        for name in ("../genome.txt", "/genome.txt", "folder/../genome.txt",
                     "folder\\genome.txt", "C:/genome.txt", "folder//genome.txt", "./genome.txt"):
            with self.subTest(name=name), self.assertRaises(uploads.UploadError):
                uploads.validate_upload(zipped(name=name))
        for data in (zipped(mode=stat.S_IFLNK | 0o777), zipped(mode=stat.S_IFIFO | 0o600),
                     zipped(extra=True), zipped(name="folder/", content=b"")):
            with self.subTest(data=data[:4]), self.assertRaises(uploads.UploadError):
                uploads.validate_upload(data)

    def test_encrypted_zip_rejected(self):
        data = bytearray(zipped())
        central = data.index(b"PK\x01\x02")
        struct.pack_into("<H", data, central + 8, 1)
        struct.pack_into("<H", data, 6, 1)
        with self.assertRaisesRegex(uploads.UploadError, "Encrypted"):
            uploads.validate_upload(bytes(data))

    def test_truncated_corrupted_and_nested_containers(self):
        corrupted = bytearray(gzip.compress(ARRAY))
        corrupted[-8] ^= 0xFF
        for data in (gzip.compress(ARRAY)[:-5], zipped()[:-10], bytes(corrupted),
                     gzip.compress(gzip.compress(ARRAY)), zipped(gzip.compress(ARRAY))):
            with self.subTest(data=data[:4]), self.assertRaises(uploads.UploadError):
                uploads.validate_upload(data)

    def test_bzip2_single_and_multi_stream(self):
        self.assertEqual(uploads.validate_upload(bz2.compress(ARRAY)), (ARRAY, ".txt"))
        self.assertEqual(uploads.validate_upload(bz2.compress(VCF[:60]) + bz2.compress(VCF[60:])), (VCF, ".vcf"))
        for data in (bz2.compress(ARRAY)[:-6], bz2.compress(bz2.compress(ARRAY))):
            with self.subTest(data=data[-4:]), self.assertRaises(uploads.UploadError):
                uploads.validate_upload(data)
        with patch.object(uploads, "MAX_EXPANDED_BYTES", len(ARRAY) - 1):
            with self.assertRaisesRegex(uploads.UploadError, "expanded.*size limit"):
                uploads.validate_upload(bz2.compress(ARRAY))

    def test_complete_genomics_var_and_mastervar_files(self):
        for data in (CG_VAR, CG_MASTERVAR, bz2.compress(CG_VAR), gzip.compress(CG_MASTERVAR), zipped(CG_VAR)):
            with self.subTest(data=data[:8]):
                self.assertEqual(uploads.validate_upload(data)[1], ".tsv")
        self.assertEqual(uploads.validate_upload(CG_VAR), (CG_VAR, ".tsv"))

    def test_complete_genomics_layout_is_held_to(self):
        for data in (
                CG_VAR.replace(b"\tchr1\t0\t", b"\tchr1\tzero\t"),             # begin not a number
                CG_VAR.replace(b"\tchr1\t0\t", b"\tcontig9\t0\t"),             # not a chromosome
                CG_VAR + b"3\t2\tall\tchr1\t1\t2\tref\t=\t=\t\t\t\t\t\t\t\n",   # more columns than named
                CG_VAR.replace(b"\tvarType\t", b"\tkind\t"),                   # not a CG header
                ARRAY + CG_VAR,                                                # header after records
                CG_VAR + VCF,                                                  # a second header
        ):
            with self.subTest(data=data[-20:]), self.assertRaises(uploads.UploadError):
                uploads.validate_upload(data)

    def test_a_header_comes_once_before_its_records(self):
        record = VCF.splitlines(keepends=True)[-1]
        for data in (ARRAY + VCF, VCF + VCF, VCF + record.replace(b"\t", b" ")):  # a VCF's columns are tabs
            with self.subTest(data=data[-20:]), self.assertRaises(uploads.UploadError):
                uploads.validate_upload(data)

    def test_one_line_is_bounded(self):
        with patch.object(uploads, "MAX_LINE_BYTES", 64):
            with self.assertRaisesRegex(uploads.UploadError, "line longer"):
                uploads.validate_upload(b"rs1\t1\t101\t" + b"A" * 100)
            self.assertEqual(uploads.validate_upload(ARRAY * 3)[1], ".txt")

    def test_zip64_accepted_and_large_directories_refused(self):
        out = io.BytesIO()
        with patch.object(zipfile, "ZIP64_LIMIT", 0):  # ZIP64 end records, as a member over 4 GiB gets
            with zipfile.ZipFile(out, "w", compression=zipfile.ZIP_DEFLATED) as archive:
                with archive.open("genome.vcf", "w", force_zip64=True) as member:
                    member.write(VCF)
        data = bytearray(out.getvalue())
        end = data.rindex(b"PK\x05\x06")
        struct.pack_into("<2HL", data, end + 8, 0xFFFF, 0xFFFF, 0xFFFFFFFF)  # counts in ZIP64 only
        self.assertEqual(uploads.validate_upload(bytes(data)), (VCF, ".vcf"))
        record = data.rindex(b"PK\x06\x06")
        struct.pack_into("<Q", data, record + 32, 2)  # two members, said in the ZIP64 record
        with self.assertRaisesRegex(uploads.UploadError, "exactly one regular file"):
            uploads.validate_upload(bytes(data))
        data = bytearray(zipped())
        end = data.rindex(b"PK\x05\x06")
        struct.pack_into("<L", data, end + 12, 1 << 20)  # a central directory claimed at 1 MiB
        with self.assertRaisesRegex(uploads.UploadError, "exactly one regular file"):
            uploads.validate_upload(bytes(data))

    def test_streams_from_a_file_into_a_file(self):
        with tempfile.TemporaryFile() as source, tempfile.TemporaryFile() as target:
            source.write(gzip.compress(VCF * 1))
            self.assertEqual(uploads.expand_upload(source, target), ".vcf")
            target.seek(0)
            self.assertEqual(target.read(), VCF)
        with tempfile.TemporaryFile() as empty, tempfile.TemporaryFile() as target:
            with self.assertRaisesRegex(uploads.UploadError, "Choose a genome file"):
                uploads.expand_upload(empty, target)

    def test_arbitrary_binary_and_nongenome_text_rejected(self):
        for data in (b"", b"\x7fELF\x00" + ARRAY, b"hello world\n", b"<html>hello</html>",
                     b"# comments only\n", ARRAY + b"\x00", b"#!/bin/sh\necho hello\n"):
            with self.subTest(data=data[:8]), self.assertRaises(uploads.UploadError):
                uploads.validate_upload(data)


if __name__ == "__main__":
    unittest.main()
