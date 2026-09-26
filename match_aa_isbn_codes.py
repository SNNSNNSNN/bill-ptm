#!/usr/bin/env python3
"""Match a book list's ISBNs against Anna's Archive's aa_isbn13_codes index.

    python match_aa_isbn_codes.py BOOKS.csv AA_ISBN13_CODES.benc [--out PATH]

BOOKS.csv must have an isbn13 column (digits only, 13 characters). The .benc
file is Anna's Archive's own compact ISBN-availability export -- found in the
codes_benc/ directory of their aa_derived_mirror_metadata torrents. Its byte
layout isn't documented by Anna's Archive; decoding here was confirmed against
the open-source decoder at https://github.com/xymaxim/allisbns and validated
empirically (rows already carrying an OpenLibrary ID hit the 'ol' catalog at
97%, rows without one at 0.2%).

Format: a bencoded dict of catalog name -> run-length-encoded bitmap over
ISBN-12 (ISBN-13 with its check digit dropped), spanning 978000000000 to
979999999999. Each catalog's value is a flat sequence of little-endian int32
run lengths, alternating present/absent/present/..., starting at 978000000000.

'md5' means Anna's Archive already holds a file for that ISBN. The other
~29 catalogs (oclc, ol, gbooks, isbndb, goodreads, hathi, ia, duxiu_ssid, ...)
are bibliographic sources that merely know the ISBN exists, no file guaranteed.

This index only gives per-ISBN presence per catalog -- no MD5 hash, file path,
or torrent name. It cannot say *which* file or torrent holds a book; that
needs a separate Anna's Archive metadata dump keyed by MD5.
"""
import csv
import struct
import sys
from pathlib import Path

import numpy as np

FIRST_ISBN = 978_000_000_000  # first ISBN-12: ISBN-13 with the check digit dropped
OUT_DEFAULT = Path("output/aa_match_results.csv.gz")


def read_catalog_offsets(path):
    """{catalog_name: (byte_offset, byte_length)} for every key, without reading values."""
    offsets = {}
    with open(path, "rb") as f:
        assert f.read(1) == b"d", "not a bencoded dict"
        while True:
            b = f.read(1)
            if b == b"e":
                break
            key = f.read(int(_read_int_prefix(f, b))).decode()
            length = int(_read_int_prefix(f))
            offsets[key] = (f.tell(), length)
            f.seek(length, 1)
    return offsets


def _read_int_prefix(f, first=b""):
    """Reads bencode's `<digits>:` length prefix, already past any leading digit."""
    buf = first
    while True:
        b = f.read(1)
        if b == b":":
            return buf
        buf += b


def load_codes(path, offset, length):
    with open(path, "rb") as f:
        f.seek(offset)
        data = f.read(length)
    return np.array(struct.unpack(f"<{length // 4}I", data), dtype=np.int64)


def contains(codes, isbn12, valid):
    """Boolean array: is each isbn12 present (a filled run) in this catalog?"""
    cumsums = FIRST_ISBN + np.cumsum(codes)
    inside = valid & (isbn12 <= int(cumsums[-1]) - 1)
    segment = np.clip(np.searchsorted(cumsums, isbn12, side="right"), 0, len(cumsums) - 1)
    return inside & (segment % 2 == 0)  # even segment index = a "present" run


def match(books_csv, codes_benc, out_path=OUT_DEFAULT):
    catalog_offsets = read_catalog_offsets(codes_benc)
    catalogs = list(catalog_offsets)

    with open(books_csv, encoding="utf-8-sig") as f:
        rows = list(csv.DictReader(f))

    isbn13 = [row["isbn13"].strip() for row in rows]
    valid = np.array([s.isdigit() and len(s) == 13 for s in isbn13])
    isbn12 = np.array([int(s[:12]) if v else -1 for s, v in zip(isbn13, valid)], dtype=np.int64)

    hits = np.zeros((len(catalogs), len(rows)), dtype=bool)
    for i, cat in enumerate(catalogs):
        codes = load_codes(codes_benc, *catalog_offsets[cat])
        hits[i] = contains(codes, isbn12, valid)

    Path(out_path).parent.mkdir(parents=True, exist_ok=True)
    import gzip
    opener = gzip.open if str(out_path).endswith(".gz") else open
    with opener(out_path, "wt", newline="", encoding="utf-8") as out:
        w = csv.writer(out)
        w.writerow(["isbn13", "title", "authors", "in_md5", "num_catalogs", "matched_catalogs"])
        md5_row = catalogs.index("md5")
        for j, row in enumerate(rows):
            if not valid[j]:
                continue
            matched = [catalogs[i] for i in range(len(catalogs)) if hits[i, j]]
            w.writerow([isbn13[j], row.get("title", ""), row.get("authors", ""),
                        "yes" if hits[md5_row, j] else "no", len(matched), ";".join(matched)])

    return rows, catalogs, hits, valid


def main():
    argv = sys.argv[1:]
    out_path = OUT_DEFAULT
    if "--out" in argv:
        i = argv.index("--out")
        out_path = Path(argv[i + 1])
        argv = argv[:i] + argv[i + 2:]
    if len(argv) != 2:
        sys.exit(__doc__)

    books_csv, codes_benc = argv
    rows, catalogs, hits, valid = match(books_csv, codes_benc, out_path)

    n_valid = int(valid.sum())
    print(f"{len(rows)} rows, {n_valid} with a clean 13-digit isbn13\n")
    print(f"{'catalog':22s} {'hits':>8s}  {'rate':>7s}")
    for cat, count in sorted(zip(catalogs, hits.sum(axis=1)), key=lambda x: -x[1]):
        print(f"{cat:22s} {int(count):8d}  {count / n_valid:6.2%}")

    any_hit = hits.any(axis=0)
    md5_hit = hits[catalogs.index("md5")]
    print(f"\nany catalog:  {int(any_hit.sum()):8d}  {any_hit.sum() / n_valid:6.2%}")
    print(f"md5 (has file): {int(md5_hit.sum()):6d}  {md5_hit.sum() / n_valid:6.2%}")
    print(f"no catalog:   {int((valid & ~any_hit).sum()):8d}  {(valid & ~any_hit).sum() / n_valid:6.2%}")
    print(f"\n-> {out_path}")


if __name__ == "__main__":
    main()
