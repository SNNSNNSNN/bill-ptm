#!/usr/bin/env python3
"""Write output/wanted_books.csv.gz: every book we want but have no text for.

    python build_wanted_list.py [--bare "path/to/ONLY ISBNS.csv"]

Rows are the approved books in output/cleaned_books.csv.gz without a finished
text, plus the bare ISBNs that have no metadata and were never screened. Each
row keeps every column we hold for it, and Screened/Status/Detail say why we
lack a text. build_corpus.py calls update() after each run.
"""
import re
import sqlite3
import sys
from pathlib import Path

import pandas as pd

BOOKS = Path("output/cleaned_books.csv.gz")
WANTED = Path("output/wanted_books.csv.gz")
MISSING = Path("output/missing_books.csv.gz")   # fetch_open_texts.py archive.org results
DB = Path("pipeline_checkpoint.db")
LEAD = ["ISBN", "Screened", "Status", "Detail"]


def read(path):
    return pd.read_csv(path, dtype=str, keep_default_na=False, encoding="utf-8-sig")


def db_status(db):
    """ISBN -> (status, detail) from the build_corpus checkpoint."""
    names = {"failed": "lookup failed", "no_open_copy": "no open copy"}
    out = {i: (names.get(s, s), re.sub(r" for url: \S+", "", d)) for i, s, d in db.execute("SELECT isbn, status, detail FROM books")}
    for i, src, res in db.execute("SELECT isbn, source, result FROM lookups"):
        out.setdefault(i, ("no open copy", f"{src}: {res}"))
    return out


def update(db, bare=None):
    books = read(BOOKS)
    old = read(WANTED) if WANTED.exists() else pd.DataFrame(columns=LEAD)
    if bare is None:
        bare = old[old.Screened == "no"].drop(columns=["Screened", "Status", "Detail"])
    notes = dict(zip(old.ISBN, zip(old.Status, old.Detail)))
    if MISSING.exists():
        m = read(MISSING)
        for i, why in zip(m.ISBN, m.Reason):
            if why != "no open-access copy found":
                notes.setdefault(i, ("no open copy", why))
    notes.update(db_status(db))

    books = books[[notes.get(i, ("",))[0] not in ("completed", "culled") for i in books.ISBN]]
    default = ("not found yet", "no openly licensed copy on archive.org; Open Library and Google Books not checked yet")
    status = [notes.get(i, default) for i in books.ISBN]
    books.insert(1, "Screened", "yes")
    books.insert(2, "Status", [s for s, _ in status])
    books.insert(3, "Detail", [d for _, d in status])
    bare = bare.assign(Screened="no", Status="no metadata",
                       Detail="bare ISBN with no title or author; never screened")
    wanted = pd.concat([books, bare], ignore_index=True)[list(books.columns)].fillna("")
    wanted.to_csv(WANTED, index=False, encoding="utf-8-sig", compression="gzip")
    return wanted


def main():
    bare = None
    if "--bare" in sys.argv:
        bare = read(sys.argv[sys.argv.index("--bare") + 1])
    with sqlite3.connect(DB) as db:
        wanted = update(db, bare)
    print(f"{len(wanted)} rows -> {WANTED}")
    print(wanted.groupby(["Screened", "Status"]).size().to_string())


if __name__ == "__main__":
    main()
