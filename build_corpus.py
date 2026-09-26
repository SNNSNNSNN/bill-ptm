#!/usr/bin/env python3
"""Turn cleaned_books + texts/ into an AI-ready Turkish corpus.

    python build_corpus.py

Phase 1  audit every file already in texts/: non-Turkish files are deleted and
         their books culled from the cleaned list; Turkish ones are cleaned.
Phase 2  look up the remaining ISBNs for an openly licensed or public-domain
         copy (archive.org, Open Library, Google Books) and download it.
Phase 3  clean each text and wrap it in a <doc><metadata>..</metadata><content>
         block.

Progress lives in pipeline_checkpoint.db, so a rerun skips finished work and
retries only the lookups that failed or never ran.
"""
import csv
import html
import re
import sqlite3
import sys
import tempfile
import time
import unicodedata
import zipfile
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

import fasttext
import fitz  # pymupdf
import ftfy
import pandas as pd
import requests

from fetch_open_texts import CP1254, HOMOGLYPHS, is_open, safe

BOOKS = Path("output/cleaned_books.csv.gz")
TEXTS = Path("texts")
DB = Path("pipeline_checkpoint.db")
CULLED = Path("culled_books.csv")
FAILED = Path("failed_downloads.csv")
LID_MODEL = Path("data/lid.176.bin")
LID_URL = "https://dl.fbaipublicfiles.com/fasttext/supervised-models/lid.176.bin"

MAX_TRIES = 5            # per request; waits 2, 4, 8, 16 s between tries
TRIP_AFTER = 3           # consecutive failed requests before a source is dropped for the run
OL_BATCH = 50            # ISBNs per Open Library request
GOOGLE_MAX_YEAR = 1955   # Google Books only gives files for public-domain scans

s = requests.Session()
s.headers["User-Agent"] = "bill-ptm-corpus/1.0 (research corpus; open-licence texts only)"


def log(msg):
    print(f"{datetime.now():%H:%M:%S} {msg}", flush=True)


# ---------------------------------------------------------------- network

class SourceDown(Exception):
    pass


def get(url, **kw):
    """GET with exponential backoff on 429/5xx and connection errors."""
    kw.setdefault("timeout", 60)
    err = None
    for i in range(MAX_TRIES):
        try:
            r = s.get(url, **kw)
            if r.status_code in (429, 500, 502, 503, 504):
                err = f"HTTP {r.status_code}"
                wait = r.headers.get("Retry-After", "")
                wait = int(wait) if wait.isdigit() else 2 ** (i + 1)
            else:
                r.raise_for_status()
                return r
        except requests.HTTPError as exc:
            raise SourceDown(str(exc)[:200]) from exc   # 4xx other than 429: retrying won't help
        except requests.RequestException as exc:
            err, wait = f"{type(exc).__name__}: {exc}"[:200], 2 ** (i + 1)
        if i < MAX_TRIES - 1:
            time.sleep(min(wait, 60))
    raise SourceDown(err)


class Breaker:
    """Stops calling a source after TRIP_AFTER consecutive failures."""

    def __init__(self, name):
        self.name, self.fails, self.down = name, 0, None

    def ok(self):
        self.fails = 0

    def fail(self, why):
        self.fails += 1
        if self.fails >= TRIP_AFTER and not self.down:
            self.down = why
            log(f"{self.name}: {TRIP_AFTER} failures in a row, skipping it for this run ({why})")


# ---------------------------------------------------------------- checkpoint

def open_db():
    db = sqlite3.connect(DB)
    db.executescript("""
        CREATE TABLE IF NOT EXISTS books (
            isbn TEXT PRIMARY KEY, status TEXT NOT NULL, detail TEXT, path TEXT, updated TEXT);
        CREATE TABLE IF NOT EXISTS lookups (
            isbn TEXT, source TEXT, result TEXT, updated TEXT, PRIMARY KEY (isbn, source));
    """)
    return db


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def mark(db, isbn, status, detail="", path=""):
    db.execute("INSERT OR REPLACE INTO books VALUES (?,?,?,?,?)", (isbn, status, detail, str(path), now()))


def mark_lookup(db, isbn, source, result):
    db.execute("INSERT OR REPLACE INTO lookups VALUES (?,?,?,?)", (isbn, source, result, now()))


def append_csv(path, rows, cols):
    if not rows:
        return
    new = not path.exists()
    with path.open("a", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=cols, extrasaction="ignore")
        if new:
            w.writeheader()
        w.writerows(rows)


# ---------------------------------------------------------------- language id

class Lid:
    def __init__(self):
        if not LID_MODEL.exists():
            LID_MODEL.parent.mkdir(parents=True, exist_ok=True)
            log(f"downloading {LID_URL}")
            with get(LID_URL, stream=True, timeout=600) as r, LID_MODEL.open("wb") as fh:
                for chunk in r.iter_content(1 << 20):
                    fh.write(chunk)
        fasttext.FastText.eprint = lambda *a: None
        self.m = fasttext.load_model(str(LID_MODEL))

    def label(self, text):
        # Call the C++ binding directly: the Python wrapper breaks under numpy 2.
        (prob, lab), = self.m.f.predict(text, 1, 0.0, "strict") or [(0.0, "__label__und")]
        return lab.replace("__label__", ""), float(prob)

    def vote(self, text, chunks=24, size=1500):
        """Language share across evenly spaced chunks, weighted by letters."""
        text = re.sub(r"\s+", " ", text)
        if len(text) <= size * chunks:
            parts = [text[i:i + size] for i in range(0, len(text), size)]
        else:
            step = (len(text) - size) // (chunks - 1)
            parts = [text[i * step:i * step + size] for i in range(chunks)]
        votes = Counter()
        for p in parts:
            letters = sum(c.isalpha() for c in p)
            if letters < 100:
                continue
            lang, prob = self.label(p)
            votes[lang] += letters * prob
        total = sum(votes.values()) or 1
        return {k: v / total for k, v in votes.most_common()}


# ---------------------------------------------------------------- cleaning

# Mojibake that survives ftfy when UTF-8 Turkish went through cp1252 and was stripped of bytes.
MOJIBAKE = {"Ä±": "ı", "Ä°": "İ", "ÅŸ": "ş", "Åž": "Ş", "ÄŸ": "ğ", "Äž": "Ğ",
            "Ã¼": "ü", "Ãœ": "Ü", "Ã¶": "ö", "Ã–": "Ö", "Ã§": "ç", "Ã‡": "Ç"}
MOJIBAKE_RE = re.compile("|".join(map(re.escape, MOJIBAKE)))
LATIN = re.compile(r"[A-Za-zÇĞİÖŞÜçğıöşüÂâÎîÛû]")
MIXED_WORD = re.compile(r"\b\w*[Ͱ-ϿЀ-ӿ]\w*\b")
INVISIBLE = re.compile("[\u00ad\u200b-\u200f\u2060\ufeff\ufffc]")
SPACES = re.compile("[ \t\u00a0\u2000-\u200a\u202f\u3000]+")
CONTROL = re.compile(r"[\x00-\x08\x0b\x0e-\x1f\x7f]")
TAG = re.compile(r"</?[A-Za-z][A-Za-z0-9]*(?:\s[^<>]*)?/?>")
PAGE_NO = re.compile(r"^[-–—.\s]*(?:(?:sayfa|page|s\.|p\.)\s*)?(?:\d{1,4}|[ivxlc]{1,6})[-–—.\s]*$", re.I)
TERMINAL = tuple(".!?…:;\"”»’')]")
BOILERPLATE = re.compile(
    r"isbn|©|copyright|telif|tüm hakları|bütün hakları|her hakkı|all rights reserved|creative commons"
    r"|sertifika no|yayıncı sertifika|matbaa|baskı ve cilt|basım yeri|printed in|baskı\s*:|basım\s*:"
    r"|www\.|https?://|e-?posta|e-?mail|@|\btel\s*[:.]|\bfaks|\bfax|kapak tasarım|sayfa düzeni|dizgi"
    r"|genel yayın yönetmeni|yayın koordinatör|yayına hazırlayan|adres\s*:|kütüphane.{0,30}katalog"
    r"|lisans|licen[cs]e|iletişim\s*:|kapak görsel|\d+\.\s*(?:baskı|basım)"
    r"|yayın no|bisac|anahtar kelimeler|konu başlık|publication number",
    re.I)


def fix_word(m):
    w = m.group(0)
    return w.translate(HOMOGLYPHS) if LATIN.search(w) else w  # leave genuine Cyrillic/Greek words


def fix_chars(text):
    text = ftfy.fix_text(text)
    text = MOJIBAKE_RE.sub(lambda m: MOJIBAKE[m.group(0)], text)
    if text.count("ý") + text.count("þ") > text.count("ı") + text.count("ş"):
        text = text.translate(CP1254)
    text = unicodedata.normalize("NFC", text)           # s + U+0327 -> ş, g + U+0306 -> ğ, ...
    text = text.replace("i̇", "i").replace("ı̇", "i")  # stray dot from lowercasing İ
    text = MIXED_WORD.sub(fix_word, text)                 # Cyrillic с а е х о inside Latin words
    text = INVISIBLE.sub("", text)
    return CONTROL.sub("", text.replace("\f", "\n").replace("\r\n", "\n").replace("\r", "\n"))


def strip_markup(text):
    if len(TAG.findall(text[:200000])) > 20:
        text = re.sub(r"(?is)<(script|style|head)\b.*?</\1>", " ", text)
        text = re.sub(r"(?i)<br\s*/?>|</(p|div|h\d|li|tr)>", "\n", text)
        text = TAG.sub(" ", text)
    return html.unescape(text)


def header_key(line):
    return re.sub(r"\d+", "", line).strip(" .-–—|").lower()


def drop_lines(lines):
    """Remove page numbers, running heads and OCR debris."""
    keys = Counter(header_key(ln) for ln in lines if 0 < len(ln) <= 80)
    heads = {k for k, n in keys.items() if n >= 5 and k and not k.endswith(TERMINAL) and LATIN.search(k)}
    out = []
    for ln in lines:
        if not ln:
            out.append(ln)
            continue
        letters = sum(c.isalpha() for c in ln)
        if (PAGE_NO.match(ln)
                or letters < 3
                or letters < 0.5 * len(ln.replace(" ", ""))
                or (len(ln) <= 80 and header_key(ln) in heads)):
            continue
        out.append(ln)
    return out


def reflow(lines):
    """Rebuild paragraphs: page breaks and hard line wraps inside a paragraph are joined."""
    blocks, cur = [], []
    for ln in lines + [""]:
        if ln:
            cur.append(ln)
        elif cur:
            blocks.append(cur)
            cur = []
    if not blocks:
        return []
    widths = sorted(len(ln) for b in blocks for ln in b)
    full = 0.6 * widths[len(widths) // 2]
    paras = [blocks[0]]
    for b in blocks[1:]:
        last = paras[-1][-1]
        if last.endswith("-") or b[0][:1].islower() or (len(last) >= full and not last.endswith(TERMINAL)):
            paras[-1].extend(b)
        else:
            paras.append(b)
    out = []
    for p in paras:
        text = p[0]
        for ln in p[1:]:
            if text.endswith("-") and ln[:1].islower() and text[-2:-1].isalpha():
                text = text[:-1] + ln            # re-join a word hyphenated across lines
            else:
                text += " " + ln
        out.append(text)
    return out


def drop_boilerplate(paras):
    """Drop short copyright, licence and imprint paragraphs from the front and back matter."""
    total = sum(map(len, paras)) or 1
    out, pos = [], 0
    for p in paras:
        edge = pos < 0.1 * total or pos > 0.97 * total
        pos += len(p)
        if edge and len(p) < 800 and BOILERPLATE.search(p):
            continue
        out.append(p)
    return out


def clean(text):
    text = strip_markup(fix_chars(text))
    lines = [SPACES.sub(" ", ln).strip() for ln in text.split("\n")]
    paras = drop_boilerplate(reflow(drop_lines(lines)))
    text = "\n\n".join(p for p in paras if p)
    return text.replace("</content>", "").replace("</doc>", "")


def doc(body, title, author, isbn, extra=()):
    esc = lambda v: re.sub(r"[<>]", "", str(v)).strip()
    meta = [f"<title>{esc(title)}</title>", f"<author>{esc(author)}</author>", f"<isbn>{esc(isbn)}</isbn>",
            "<language>tr</language>"] + [f"<{k}>{esc(v)}</{k}>" for k, v in extra if v]
    return "<doc>\n<metadata>\n" + "\n".join(meta) + "\n</metadata>\n<content>\n" + body + "\n</content>\n</doc>\n"


# ---------------------------------------------------------------- reading files

def read_any(path):
    suf = path.suffix.lower()
    if suf == ".pdf":
        with fitz.open(path) as d:
            return "\n".join(page.get_text() for page in d)
    if suf == ".epub":
        with zipfile.ZipFile(path) as z:
            parts = sorted(n for n in z.namelist() if n.lower().endswith((".xhtml", ".html", ".htm")))
            return "\n".join(strip_markup(z.read(n).decode("utf-8", "replace")) for n in parts)
    return path.read_bytes().decode("utf-8", "replace")


def split_existing(raw):
    """Return (header fields, body) for texts written by fetch_open_texts.py or by this script."""
    if raw.lstrip().startswith("<doc>"):
        meta = dict(re.findall(r"<(\w+)>(.*?)</\1>", raw.split("<content>", 1)[0]))
        body = raw.split("<content>", 1)[-1].rsplit("</content>", 1)[0]
        return meta, body
    head, _, body = raw.partition("\n\n")
    meta = {k.lower(): v.strip() for k, v in re.findall(r"^(\w+): (.*)$", head, re.M)}
    if "isbn" not in meta:
        return {}, raw
    meta["license"] = meta.pop("licence", "")
    return meta, body


def norm_isbn(x):
    d = re.sub(r"[^0-9Xx]", "", str(x)).upper()
    if len(d) == 10:
        core = "978" + d[:9]
        check = (10 - sum(int(c) * (1 if i % 2 == 0 else 3) for i, c in enumerate(core)) % 10) % 10
        return core + str(check)
    return d if len(d) == 13 else ""


def text_path(row):
    author = safe(row["Author"].split(";")[0], "Unknown author")
    return TEXTS / author / (safe(row["Title"], row["ISBN"]) + ".txt")


# ---------------------------------------------------------------- phase 1

def phase1(books, db, lid):
    log("phase 1: auditing existing files in texts/")
    by_isbn = {r["ISBN"]: r for r in books.to_dict("records")}
    culled, done = [], 0
    files = sorted(p for p in TEXTS.rglob("*") if p.suffix.lower() in (".txt", ".pdf", ".epub"))
    for path in files:
        raw = read_any(path)
        meta, body = split_existing(raw)
        isbn = norm_isbn(meta.get("isbn", ""))
        if raw.startswith("<doc>") and db.execute(
                "SELECT 1 FROM books WHERE isbn=? AND status='completed'", (isbn,)).fetchone():
            done += 1
            continue                     # cleaned on an earlier run; cleaning twice would eat paragraphs
        row = by_isbn.get(isbn)
        fixed = fix_chars(strip_markup(body))
        share = lid.vote(fixed)
        top = next(iter(share), "und")
        summary = ", ".join(f"{k} {v:.0%}" for k, v in list(share.items())[:3])
        if share.get("tr", 0) < 0.5:
            reason = "Existing file non-Turkish"
            culled.append({**(row or {"ISBN": isbn, "Title": meta.get("title", path.stem),
                                       "Author": meta.get("author", path.parent.name)}),
                           "Reason": reason, "Detected_Language": summary, "File": str(path)})
            path.unlink()
            if not any(path.parent.iterdir()):
                path.parent.rmdir()
            if isbn:
                mark(db, isbn, "culled", f"{reason} ({summary})", path)
            log(f"  cull  {top:>3}  {path}  [{summary}]")
            continue
        title = row["Title"] if row else meta.get("title", path.stem)
        author = row["Author"] if row else meta.get("author", path.parent.name)
        out = path.with_suffix(".txt")
        out.write_text(doc(clean(body), title, author, isbn,
                           [("source", meta.get("source")), ("license", meta.get("license"))]), encoding="utf-8")
        if out != path:
            path.unlink()
        if isbn:
            mark(db, isbn, "completed", f"existing file ({summary})", out)
        done += 1
        log(f"  keep   tr  {out}  [{summary}]")
    db.commit()
    if culled:
        cols = list(books.columns) + ["Reason", "Detected_Language", "File"]
        append_csv(CULLED, culled, cols)
        books = books[~books.ISBN.isin({c["ISBN"] for c in culled})]
        write_books(books)
    log(f"phase 1: kept {done}, culled {len(culled)}")
    return books


def write_books(books):
    tmp = BOOKS.with_suffix(".tmp.gz")
    books.to_csv(tmp, index=False, encoding="utf-8-sig", compression="gzip")
    tmp.replace(BOOKS)
    log(f"  {BOOKS} now has {len(books)} rows")


# ---------------------------------------------------------------- phase 2 sources

def archive_index():
    """ISBN-13 -> archive.org identifiers, for every item that carries an ISBN and an open licence."""
    idx = {}
    for q in ("isbn:[* TO *] AND licenseurl:*creativecommons.org*",
              "isbn:[* TO *] AND licenseurl:*publicdomain*",
              "isbn:[* TO *] AND possible-copyright-status:NOT_IN_COPYRIGHT"):
        cursor = None
        while True:
            params = {"q": q, "fields": "identifier,isbn", "count": 10000}
            if cursor:
                params["cursor"] = cursor
            d = get("https://archive.org/services/search/v1/scrape", params=params, timeout=120).json()
            for it in d.get("items", []):
                vals = it.get("isbn", [])
                for v in [vals] if isinstance(vals, str) else vals:
                    for part in re.split(r"[;,\s]+", v):
                        if (k := norm_isbn(part)):
                            idx.setdefault(k, []).append(it["identifier"])
            cursor = d.get("cursor")
            if not cursor:
                break
    return idx


def openlibrary_batch(isbns):
    """ISBN -> archive.org identifier for books Open Library shows in full."""
    keys = ",".join(f"ISBN:{i}" for i in isbns)
    d = get("https://openlibrary.org/api/books", params={"bibkeys": keys, "format": "json", "jscmd": "viewapi"}).json()
    found = {}
    for k, v in d.items():
        if v.get("preview") == "full" and "archive.org/details/" in v.get("preview_url", ""):
            found[k.split(":", 1)[1]] = v["preview_url"].rsplit("/details/", 1)[1].split("/")[0]
    return found


def google_lookup(isbn):
    """(download url, licence note) for a public-domain Google Books scan, or None."""
    d = get("https://www.googleapis.com/books/v1/volumes", params={"q": f"isbn:{isbn}"}).json()
    for item in d.get("items", []):
        acc = item.get("accessInfo", {})
        if acc.get("accessViewStatus") != "FULL_PUBLIC_DOMAIN":
            continue
        for fmt in ("epub", "pdf"):
            link = acc.get(fmt, {}).get("downloadLink")
            if link:
                return link, "public domain (Google Books FULL_PUBLIC_DOMAIN)"
    return None


def archive_text(ident, tmp):
    """(raw text, licence) for an openly licensed archive.org item, or (None, reason)."""
    m = get(f"https://archive.org/metadata/{ident}").json()
    ok, why = is_open(m.get("metadata", {}))
    if not ok:
        return None, why
    names = [f["name"] for f in m.get("files", [])]
    txt = next((n for n in names if n.endswith("_djvu.txt")), None)
    if txt:
        return get(f"https://archive.org/download/{ident}/{txt}", timeout=300).text, why
    for ext in (".epub", ".pdf"):
        name = next((n for n in names if n.lower().endswith(ext)), None)
        if name:
            return download_text(f"https://archive.org/download/{ident}/{name}", tmp, ext), why
    return None, "archive.org item has no text, epub or pdf"


def download_text(url, tmp, ext):
    path = Path(tmp) / f"book{ext}"
    with get(url, stream=True, timeout=600) as r, path.open("wb") as fh:
        for chunk in r.iter_content(1 << 20):
            fh.write(chunk)
    try:
        return read_any(path)
    finally:
        path.unlink(missing_ok=True)


# ---------------------------------------------------------------- phase 2 + 3

def save_download(db, lid, row, raw, source, licence):
    """Language-check, clean and write one downloaded text. Returns 'completed' or 'culled'."""
    fixed = fix_chars(strip_markup(raw))
    share = lid.vote(fixed)
    summary = ", ".join(f"{k} {v:.0%}" for k, v in list(share.items())[:3])
    if share.get("tr", 0) < 0.5:
        mark(db, row["ISBN"], "culled", f"Downloaded text non-Turkish ({summary})")
        return "culled", summary
    body = clean(raw)
    if len(body) < 500:
        mark_lookup(db, row["ISBN"], source, "no usable text")
        return None, "no usable text"
    out = text_path(row)
    if out.exists():                     # another edition (same title and author) is already saved
        other = re.search(r"<isbn>(.*?)</isbn>", out.read_text(encoding="utf-8")[:2000])
        if other and other.group(1) != row["ISBN"]:
            mark(db, row["ISBN"], "completed", f"same title and author as ISBN {other.group(1)}; one copy kept", out)
            return "completed", summary
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(doc(body, row["Title"], row["Author"], row["ISBN"], [("source", source), ("license", licence)]),
                   encoding="utf-8")
    mark(db, row["ISBN"], "completed", f"downloaded ({summary})", out)
    return "completed", summary


def phase2(books, db, lid):
    log("phase 2: looking up remaining ISBNs")
    settled = {i for (i,) in db.execute("SELECT isbn FROM books WHERE status IN ('completed','culled','no_open_copy')")}
    answered = {}
    for isbn, src in db.execute("SELECT isbn, source FROM lookups"):
        answered.setdefault(isbn, set()).add(src)
    todo = books[~books.ISBN.isin(settled)]
    rows = {r["ISBN"]: r for r in todo.to_dict("records")}
    years = pd.to_numeric(todo.Publish_Year, errors="coerce")
    google_set = set(todo.ISBN[years <= GOOGLE_MAX_YEAR])
    log(f"  {len(rows)} ISBNs to check ({len(settled)} already settled)")

    brk = {n: Breaker(n) for n in ("archive.org", "openlibrary", "googlebooks")}
    failures = {}          # isbn -> [reason, ...]
    candidates = {}        # isbn -> [(source, kind, ref), ...]

    # archive.org: one bulk index instead of one query per ISBN
    try:
        idx = archive_index()
        log(f"  archive.org index: {len(idx)} openly licensed ISBNs, "
            f"{sum(i in idx for i in rows)} of ours")
        for isbn in rows:
            if isbn in idx and "archive.org" not in answered.get(isbn, ()):
                candidates.setdefault(isbn, []).extend(("archive.org", "ia", x) for x in idx[isbn])
            elif isbn not in idx:
                answered.setdefault(isbn, set()).add("archive.org")   # rebuilt every run, so not stored
    except SourceDown as exc:
        brk["archive.org"].down = str(exc)
        log(f"  archive.org index failed: {exc}")

    # Open Library: batched view-availability lookups
    ol_todo = [i for i in rows if "openlibrary" not in answered.get(i, ())]
    for n in range(0, len(ol_todo), OL_BATCH):
        if brk["openlibrary"].down:
            break
        batch = ol_todo[n:n + OL_BATCH]
        try:
            found = openlibrary_batch(batch)
            brk["openlibrary"].ok()
        except SourceDown as exc:
            brk["openlibrary"].fail(str(exc))
            for i in batch:
                failures.setdefault(i, []).append(f"openlibrary: {exc}")
            continue
        for i in batch:
            if i in found:
                candidates.setdefault(i, []).append(("openlibrary", "ia", found[i]))
            else:
                mark_lookup(db, i, "openlibrary", "no full view")
                answered.setdefault(i, set()).add("openlibrary")
        if n // OL_BATCH % 20 == 0:
            db.commit()
            log(f"  openlibrary {n + len(batch)}/{len(ol_todo)}")
    db.commit()

    # Google Books: per-ISBN, only where a public-domain scan is plausible
    for i in [i for i in google_set if "googlebooks" not in answered.get(i, ())]:
        if brk["googlebooks"].down:
            break
        try:
            hit = google_lookup(i)
            brk["googlebooks"].ok()
        except SourceDown as exc:
            brk["googlebooks"].fail(str(exc))
            failures.setdefault(i, []).append(f"googlebooks: {exc}")
            continue
        if hit:
            candidates.setdefault(i, []).append(("googlebooks", "url", hit))
        else:
            mark_lookup(db, i, "googlebooks", "no public-domain scan")
            answered.setdefault(i, set()).add("googlebooks")
    db.commit()

    # download and clean every candidate
    saved = culled = 0
    culled_rows = []
    with tempfile.TemporaryDirectory() as tmp:
        for isbn, cands in candidates.items():
            row, result = rows[isbn], None
            for source, kind, ref in cands:
                try:
                    if kind == "ia":
                        raw, licence = archive_text(ref, tmp)
                        src_url = f"https://archive.org/details/{ref}"
                    else:
                        (url, licence), src_url = ref, ref[0]
                        raw = download_text(url, tmp, ".epub" if "epub" in url else ".pdf")
                except Exception as exc:  # network, bad pdf/epub, bad json
                    failures.setdefault(isbn, []).append(f"{source} download: {exc}"[:200])
                    continue
                if not raw or len(raw.strip()) < 500:
                    mark_lookup(db, isbn, source, licence or "no extractable text")
                    continue
                result, summary = save_download(db, lid, row, raw, src_url, licence)
                if result:
                    break
            if result == "completed":
                saved += 1
                failures.pop(isbn, None)
                log(f"  saved  {isbn}  {text_path(row)}  [{summary}]")
            elif result == "culled":
                culled += 1
                failures.pop(isbn, None)
                culled_rows.append({**row, "Reason": "Downloaded text non-Turkish", "Detected_Language": summary})
            else:
                for source, _, _ in cands:
                    answered.setdefault(isbn, set()).add(source)
            db.commit()

    # rows every live source answered with "nothing open" are settled for good
    needed = {"archive.org", "openlibrary"}
    for isbn in rows:
        want = needed | ({"googlebooks"} if isbn in google_set else set())
        if isbn not in failures and want <= answered.get(isbn, set()):
            if db.execute("SELECT 1 FROM books WHERE isbn=?", (isbn,)).fetchone() is None:
                mark(db, isbn, "no_open_copy", "no openly licensed copy at any source")
    for isbn, why in failures.items():
        mark(db, isbn, "failed", "; ".join(why)[:500])
    db.commit()

    fail_rows = [{"ISBN": i, "Title": rows[i]["Title"], "Author": rows[i]["Author"],
                  "Reason": "; ".join(w)[:500], "Time": now()} for i, w in failures.items()]
    append_csv(FAILED, fail_rows, ["ISBN", "Title", "Author", "Reason", "Time"])
    if culled_rows:
        append_csv(CULLED, culled_rows, list(books.columns) + ["Reason", "Detected_Language", "File"])
        books = books[~books.ISBN.isin({r["ISBN"] for r in culled_rows})]
        write_books(books)

    pending = len(rows) - saved - culled - len(failures) - sum(
        1 for i in rows if db.execute("SELECT status FROM books WHERE isbn=?", (i,)).fetchone() == ("no_open_copy",))
    log(f"phase 2: saved {saved}, culled {culled}, failed {len(failures)}, "
        f"left for a later run {pending}")
    for name, b in brk.items():
        if b.down:
            log(f"  {name} was unreachable this run: {b.down}")
    return books


def main():
    books = pd.read_csv(BOOKS, dtype=str, keep_default_na=False, encoding="utf-8-sig")
    log(f"{BOOKS}: {len(books)} rows")
    db = open_db()
    lid = Lid()
    books = phase1(books, db, lid)
    if "--audit-only" not in sys.argv:
        phase2(books, db, lid)
    db.close()


if __name__ == "__main__":
    main()
