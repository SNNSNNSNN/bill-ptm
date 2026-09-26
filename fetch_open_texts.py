#!/usr/bin/env python3
"""Fetch full text for approved books that archive.org offers under an open
licence or as public domain, clean it, and file it as texts/<Author>/<Title>.txt.

    python fetch_open_texts.py

Anything without such a copy goes to output/missing_books.csv.gz so the
publishers can be contacted. Downloads go to a temp dir that is deleted.
"""
import csv
import re
import shutil
import tempfile
import unicodedata
from pathlib import Path

import fitz  # pymupdf
import ftfy
import pandas as pd
import requests
from tqdm import tqdm

SRC = "data/turkish_books_list1_all_isbns.csv"
CLEANED = "output/cleaned_books.csv"
TEXTS = Path("texts")
MISSING = "output/missing_books.csv.gz"

s = requests.Session()
s.headers["User-Agent"] = "bill-ptm-open-texts/1.0"

# Cyrillic/Greek letters that look like Latin ones.
HOMOGLYPHS = str.maketrans({
    "а": "a", "е": "e", "о": "o", "р": "p", "с": "c", "у": "y", "х": "x", "і": "i", "ј": "j", "ѕ": "s",
    "һ": "h", "ԁ": "d", "ԛ": "q", "ԝ": "w", "А": "A", "В": "B", "Е": "E", "К": "K", "М": "M", "Н": "H",
    "О": "O", "Р": "P", "С": "C", "Т": "T", "Х": "X", "У": "Y", "І": "I", "Ј": "J", "Ѕ": "S",
    "α": "a", "ο": "o", "ν": "v", "ι": "i", "Α": "A", "Β": "B", "Ε": "E", "Ζ": "Z", "Η": "H", "Ι": "I",
    "Κ": "K", "Μ": "M", "Ν": "N", "Ο": "O", "Ρ": "P", "Τ": "T", "Υ": "Y", "Χ": "X",
})
# Turkish text decoded as cp1252 instead of cp1254.
CP1254 = str.maketrans({"ý": "ı", "þ": "ş", "ð": "ğ", "Ý": "İ", "Þ": "Ş", "Ð": "Ğ"})
LATIN = re.compile(r"[A-Za-zÇĞİÖŞÜçğıöşüÂâÎîÛû]")
MIXED_WORD = re.compile(r"\b\w*[Ͱ-ϿЀ-ӿ]\w*\b")


def fix_word(m):
    w = m.group(0)
    return w.translate(HOMOGLYPHS) if LATIN.search(w) else w  # leave genuinely Cyrillic/Greek words


def clean(text):
    text = ftfy.fix_text(text)
    if text.count("ý") + text.count("þ") > text.count("ı") + text.count("ş"):
        text = text.translate(CP1254)
    text = MIXED_WORD.sub(fix_word, text)
    text = unicodedata.normalize("NFC", text).replace("­", "")
    text = re.sub(r"(\w)-\n\s*(\w)", r"\1\2", text)          # re-join hyphenated line breaks
    text = re.sub(r"^\s*\d{1,4}\s*$", "", text, flags=re.M)   # bare page numbers
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r" *\n *", "\n", text)
    return re.sub(r"\n{3,}", "\n\n", text).strip() + "\n"


def safe(name, fallback):
    name = re.sub(r'[\\/:*?"<>|\x00-\x1f]', "", name).strip(" .")[:120]
    return name or fallback


def is_open(md):
    lic = md.get("licenseurl", "")
    lic = " ".join(lic) if isinstance(lic, list) else lic
    if str(md.get("access-restricted-item", "")).lower() == "true":
        return False, "archive.org copy is lending-only"
    if "creativecommons.org" in lic or "publicdomain" in lic:
        return True, lic
    if md.get("possible-copyright-status") == "NOT_IN_COPYRIGHT":
        return True, "public domain (archive.org: NOT_IN_COPYRIGHT)"
    return False, "archive.org copy has no open licence"


def fetch_text(ident, files, tmp):
    names = [f["name"] for f in files]
    txt = next((n for n in names if n.endswith("_djvu.txt")), None)
    if txt:
        return s.get(f"https://archive.org/download/{ident}/{txt}", timeout=300).text
    pdf = next((n for n in names if n.lower().endswith(".pdf")), None)
    if not pdf:
        return ""
    path = Path(tmp) / "book.pdf"
    with s.get(f"https://archive.org/download/{ident}/{pdf}", timeout=600, stream=True) as r:
        r.raise_for_status()
        with path.open("wb") as fh:
            shutil.copyfileobj(r.raw, fh)
    with fitz.open(path) as doc:
        text = "\n".join(page.get_text() for page in doc)
    path.unlink()
    return text


def main():
    src = pd.read_csv(SRC, dtype=str, keep_default_na=False, encoding="utf-8-sig",
                      usecols=["isbn13", "internet_archive_downloadable_ids"])
    books = pd.read_csv(CLEANED, dtype=str, keep_default_na=False, encoding="utf-8-sig")
    ia = dict(zip(src.isbn13, src.internet_archive_downloadable_ids))
    books["ia"] = books.ISBN.map(ia).fillna("")
    books["Reason"] = "no open-access copy found"
    saved = 0
    tmp = tempfile.mkdtemp()
    try:
        for idx, row in tqdm(books[books.ia != ""].iterrows(), total=(books.ia != "").sum(), desc="archive.org"):
            reason = ""
            for ident in [x.strip() for x in row.ia.split(";") if x.strip()]:
                try:
                    m = s.get(f"https://archive.org/metadata/{ident}", timeout=60).json()
                    ok, why = is_open(m.get("metadata", {}))
                    if not ok:
                        reason = why
                        continue
                    text = fetch_text(ident, m.get("files", []), tmp)
                except (requests.RequestException, ValueError, RuntimeError) as exc:
                    reason = f"download failed: {exc}"[:120]
                    continue
                if len(text.strip()) < 500:
                    reason = "archive.org copy has no extractable text"
                    continue
                author = safe(row.Author.split(";")[0], "Unknown author")
                out = TEXTS / author / (safe(row.Title, row.ISBN) + ".txt")
                out.parent.mkdir(parents=True, exist_ok=True)
                header = (f"Title: {row.Title}\nAuthor: {row.Author}\nISBN: {row.ISBN}\n"
                          f"Source: https://archive.org/details/{ident}\nLicence: {why}\n\n")
                out.write_text(header + clean(text), encoding="utf-8")
                books.at[idx, "Reason"] = "saved"
                saved += 1
                break
            else:
                books.at[idx, "Reason"] = reason or books.at[idx, "Reason"]
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    missing = books[books.Reason != "saved"][["ISBN", "Title", "Author", "Publisher", "Publisher_Source",
                                               "Publish_Year", "Reason"]]
    missing.to_csv(MISSING, index=False, encoding="utf-8-sig", quoting=csv.QUOTE_MINIMAL)
    print(f"saved {saved} texts to {TEXTS}/, {len(missing)} missing -> {MISSING}")
    print(missing.Reason.value_counts().to_string())


if __name__ == "__main__":
    main()
