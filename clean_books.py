#!/usr/bin/env python3
"""Filter and tidy the Turkish ISBN list from the bill-ptm Drive folder.

    python clean_books.py --input data/turkish_books_list1_all_isbns.csv --outdir output

Stages
  1. Normalise ISBNs; drop duplicate ISBNs and bad check digits.
  2. Fill rows that lack a title: OpenLibrary search API, then Google Books,
     then the Anna's Archive metadata search page. Only search/metadata
     endpoints are requested. No book, PDF or EPUB file is ever fetched.
  3. Screen title, subtitle, series, genres, subjects, LCC/Dewey class,
     authors and description against the keyword/regex rules below.
  4. Write cleaned_books.csv, eliminated_books.csv and run_log.txt.

Lookups are cached as JSONL under --cache-dir, so an interrupted run resumes
where it stopped.
"""
import argparse
import csv
import html
import json
import logging
import os
import re
import time
import unicodedata
from collections import Counter
from pathlib import Path

import pandas as pd
import requests
from requests.adapters import HTTPAdapter, Retry
from tqdm import tqdm

log = logging.getLogger("clean_books")

# --------------------------------------------------------------------------
# Text folding: Turkish letters to ASCII, lower case, straight apostrophes.
# All rule patterns below are written against folded text.
# --------------------------------------------------------------------------
_FOLD = str.maketrans({
    "ı": "i", "İ": "i", "I": "i", "ş": "s", "Ş": "s", "ğ": "g", "Ğ": "g",
    "ç": "c", "Ç": "c", "ö": "o", "Ö": "o", "ü": "u", "Ü": "u",
    "â": "a", "Â": "a", "î": "i", "Î": "i", "û": "u", "Û": "u",
    "’": "'", "‘": "'", "`": "'", "´": "'",
})


def fold(text):
    text = unicodedata.normalize("NFKD", text.translate(_FOLD).lower())
    return "".join(c for c in text if not unicodedata.combining(c))


def squash(text):
    return re.sub(r"\s+", " ", text).strip()


# --------------------------------------------------------------------------
# Rules
#   confidence "high"   -> unambiguous term found in the title, subtitle or
#                          series, or an author who writes little else
#   confidence "review" -> the term has innocent uses, or it was found only
#                          in subjects/genres/description (OpenLibrary
#                          subject tags are crowd-sourced and noisy). Still
#                          eliminated, but marked so a person can re-admit it.
#   scope "title"       -> the term is too common in subject tags and blurbs
#                          of mainstream novels; only the title counts
#   exempt              -> if this matches anywhere in the row, the rule is
#                          ignored (refutations, prevention, law, history)
# --------------------------------------------------------------------------
SEX, ISLAM, OCCULT, HARM = "sexual", "anti_islamic", "occult", "harmful"
CATEGORY_NAMES = {
    SEX: "Adult / sexual content",
    ISLAM: "Anti-Islamic / religiously offensive",
    OCCULT: "Occultism / satanism",
    HARM: "Harmful: self-harm, violence, illegal acts, extremist ideology",
    "duplicate": "Duplicate ISBN",
    "invalid_isbn": "Invalid ISBN",
    "no_metadata": "No metadata found",
}

ISLAMIC_CONTEXT = r"islam|fikih|fikh|ilmihal|hadis|kur'?an|ahlak|mahrem|nikah|aile hayati"
PROTECTIVE = r"istismar|taciz|korun|onleme|abuse|prevention|siddet|cocuk|hukuk|\blaw\b|legal|\bsuc\b|sucu|ceza|sansur|censor"
REFUTATION = (r"iddia|cevap|yanit|reddiye|curut|asilsiz|hata yok|yoktur|degildir|"
              r"refut|response|answer|elestiri|uzerine|tartisma|yasakli")
RELIGION = (r"\btanri|\bdin\b|\bdinler|\bdini\b|\bgod\b|relig|\biman|ateiz|atheis|inanc|kur'?an|koran|seriat|islam|"
            r"muslum|musluman|muhammed|peygamber|allah|kutsal|tevrat|incil|diyanet|namaz|oruc|hadis|cennet|ayet")
FICTION = (r"fiction|\bnovels?\b|\bstories\b|\broman\b|\bromani\b|\bromanlar|\boyku|\bhikaye|juvenile|children|"
           r"fantasy|polisiye|thriller|comic|cizgi roman|\bmasal|poetry|\bsiir|harry potter|ucleme|serisi|\bseries\b")
SCHOLARLY = (r"tarih|history|historical|literatur|filoloji|\bdili\b|\bmetin|inceleme|arastirma|sozlug|ansiklopedi|"
             r"folklor|mitoloji|mytholog|antropoloji|anthropolog|sanat|sembol|astronomi|batil itikat|sosyoloji")
PRO_ISLAMIC_AUTHORS = re.compile(r"harun yahya|adnan oktar|said nursi|necip fazil|kisakurek")

# (category, confidence, scope, pattern, exempt, label)
TEXT_RULES = [
    # a) adult / sexual
    (SEX, "high", "all", r"\beroti[kcs]\w*", PROTECTIVE, "erotic"),
    (SEX, "high", "all", r"\bporno\w*", PROTECTIVE + r"|bagimlilik|addiction|kurtul", "porn"),
    (SEX, "high", "all", r"\bmustehcen\w*|\bobscen\w*", PROTECTIVE + r"|dava|translation|ceviri", "obscene"),
    (SEX, "high", "all", r"\bkama ?sutra\b|\bbdsm\b|\bsado ?-?mazo\w*|\bsadomaso\w*|\bhentai\b|\byaoi\b|\bsmut\b"
                         r"|\bnsfw\b", None, "explicit genre"),
    (SEX, "high", "all", r"\borji(?:ler\w*|si|ye|de|den)?\b|\borg(?:y|ies)\b|\borgazm\w*|\borgasm\w*", None,
     "orgy/orgasm"),
    (SEX, "high", "all", r"\bseks(?:i|in|te|ten|le|ler\w*|uel\w*|ual\w*)?\b", PROTECTIVE, "seks"),
    (SEX, "high", "all", r"\b(?:dark|adult|erotic|steamy|spicy) romance\b", None, "adult romance"),
    (SEX, "high", "all", r"\bcinsel (?:haz|doyum|fantez\w*|teknik\w*|oyun\w*|pozisyon\w*)", None,
     "cinsel haz/teknik"),
    (SEX, "high", "all", r"\bsex (?:instruction|manuals?|guide|positions)\b(?! for (?:children|youth|teenagers))"
                         r"|\bsexual (?:fantas\w*|techniques?|intercourse)\b", PROTECTIVE, "sex manual"),
    (SEX, "review", "title", r"\bsex\b(?! (?:role|differ|discrim|ratio|determin|chromosom|hormon|education))",
     PROTECTIVE + r"|\bseyi[td]\b|\bseyh\b|mahmud", "sex"),
    (SEX, "review", "title", r"\bcinsellik\w*|\bcinsel (?:yasam|iliski|birliktelik|saglik|terapi|isteksizlik|sorun"
                             r"|islev|guc|arzu|istek)\w*|\bsexuality\b|\bseksoloji\w*|\bsexolog\w*",
     PROTECTIVE + "|" + ISLAMIC_CONTEXT, "cinsellik"),
    (SEX, "review", "title", r"\bsehvet\w*|\bsevis(?:me|mek|tik|tikten)\w*", ISLAMIC_CONTEXT, "sehvet/sevisme"),
    (SEX, "review", "title", r"\bgenelev\w*|\bfahise\w*|\bbrothel\w*|\bprostitut\w*",
     PROTECTIVE + r"|" + SCHOLARLY, "prostitution"),
    (SEX, "review", "title", r"\byetiskin(?:ler)?(?: icin)? (?:icerik\w*|romant\w*|roman\w*|hikaye\w*|oyku\w*|masal\w*)"
                             r"|(?<![\d])\+ ?18(?!\d)|(?<!\d)18 ?\+(?!\s?\d)|\b18 yas (?:ustu|sinir\w*)",
     None, "yetiskin icerik / +18"),

    # b) anti-Islamic / religiously offensive
    (ISLAM, "high", "all", r"\bseytan(?:in|i)? ayetler\w*|\bsatanic verses\b", REFUTATION + r"|olayi|dosyasi|fetva",
     "Seytan Ayetleri"),
    (ISLAM, "high", "all", r"\btanri yanilgi\w*|\bgod delusion\b",
     REFUTATION + r"|yanilgisinin yanilgisi|yanilgisi yanilgisi|dawkins yanilgisi", "Tanri Yanilgisi"),
    (ISLAM, "high", "all", r"\btanri (?:buyuk|yuce) degil\w*|\bgod is not great\b", None, "Tanri Buyuk Degildir"),
    (ISLAM, "high", "all", r"\bneden musluman degilim\b|\bwhy i am not a muslim\b", None, "Neden Musluman Degilim"),
    (ISLAM, "high", "all", r"\bislam\w* fasizm\w*|\bislamofasizm\w*|\bislamo-?fascis\w*|\bislamic fascis\w*",
     REFUTATION + r"|iftira|yalani", "Islam fasizmi"),
    (ISLAM, "high", "all", r"\bislam(?:'?in|'?i)? (?:karanlik|kanli|vahset|vahsi|sapik|yalan)\w*", REFUTATION,
     "Islam'in karanlik yuzu"),
    (ISLAM, "high", "all", r"\b(?:kur'?an|koran|quran)(?:'?[a-z]+)? (?:celiski\w*|yalan(?:lar\w*)?\b|uydurma\w*"
                           r"|hatalar\w*|yanlislar\w*|safsata\w*|carpit\w*)|\bcontradictions? in the (?:quran|koran)\b",
     REFUTATION, "Kuran'daki celiskiler"),
    (ISLAM, "high", "all", r"\bdin(?:ler)? (?:bir )?(?:afyon|hastalik|zehir|yalan|virus)\w*|\bdinin (?:olumu|sonu)\b"
                           r"|\bend of faith\b", REFUTATION, "din bir afyon/yalan"),
    (ISLAM, "high", "all", r"\bdinsizlik (?:propaganda|el kitab|rehber)\w*|\bateizm propaganda\w*", None,
     "ateizm propagandasi"),
    (ISLAM, "review", "title", r"\bateiz\w*|\bateist\w*|\batheis\w*|\bantiteiz\w*|\bdinsizlik\w*|\bagnostis\w*",
     REFUTATION + r"|cokus|cikmaz|iflas|\bsonu\b|yanilgi|mantiksiz|batakl|acmaz|tuzak|karsi|iman|ispat|delil|"
     r"kur'?an|islam|allah|hakikat|risale|varmis|problem|sorun|baglaminda|kavram|felsefe|philosoph|apologetic|"
     r"controversial literature|critique|" + SCHOLARLY, "ateizm"),

    # c) occultism / satanism
    (OCCULT, "high", "all", r"\bsatani(?:zm|st|sm|c)\w*",
     r"verses|ayetler|controversial literature|elestiri|tehlike|mucadele|kusatma|yok etti|kur'?an|islam",
     "satanism"),
    (OCCULT, "high", "all", r"\bseytan(?:a|in)? (?:tapan|tapin\w*|kilise\w*|incil\w*)|\bchurch of satan\b",
     r"tehlike|elestiri|korun|mucadele", "seytana tapma"),
    (OCCULT, "high", "all", r"\bkara buyu\w*|\bblack magic\w*|\bnecronomicon\b|\bgrimoire\w*|\bcin cagirma\w*"
                            r"|\bruh cagirma\w*|\bbuyu (?:yapma|kitabi|teknik\w*|rehber\w*|formul\w*|tarif\w*)",
     r"korun|kurtul|tedavi|rukye|dua\b|dualari|islam|kur'?an|hadis|ayet|sifa|bozma", "kara buyu"),
    (OCCULT, "review", "all", r"\bokk?ult\w*|\boccult\w*|\bthelema\w*|\bwicca\w*|\bcadilik\w*|\bwitchcraft\b",
     r"controversial literature|elestiri", "okultizm"),
    (OCCULT, "review", "title", r"\btarot\b|\bfalcilik\b|\bfal (?:bakma\w*|kitab\w*|sanat\w*|rehber\w*)"
                                r"|\bkahve fali\w*|\bmedyum\w*|\bspiritizm\w*|\bkabb?ala\w*|\bastroloji\w*|\bastrology\b"
                                r"|\bzodyak\w*|\bzodiac\b|\bhoroscope\w*|\byildizname\w*"
                                r"|\bburc(?:lar|unuz|lara)? (?:ve|yorum\w*|rehber\w*|fali)",
     r"controversial literature|elestiri", "tarot/astroloji/fal"),

    # c) harmful: self-harm, violence, illegal acts, extremist ideology
    (HARM, "high", "all", r"\bintihar (?:rehber\w*|yontem\w*|kilavuz\w*|el kitab\w*)"
                          r"|\bsuicide (?:guide|manual|methods?|handbook)\b|\bhow to (?:kill yourself|commit suicide)\b",
     r"onleme|prevention", "suicide guide"),
    (HARM, "high", "all", r"\bbomba (?:yapim\w*|imal\w*)|\bpatlayici (?:yapim\w*|imal\w*)"
                          r"|\banars?ist(?:'?in)? yemek kitab\w*|\banarchist cookbook\b|\bsilah (?:yapim\w*|imal\w*)"
                          r"|\bimprovised (?:explosive|munition)\w*", r"sanayi|savunma|" + SCHOLARLY,
     "bomb/weapon making"),
    (HARM, "high", "all", r"\b(?:uyusturucu|esrar|kenevir|cannabis|marijuana|marihuana|kokain|eroin|metamfetamin"
                          r"|methamphetamine)\w* (?:yetistir\w*|uret\w*|yapim\w*|grow\w*|cultivat\w*)"
                          r"|\bgrow(?:ing)? (?:cannabis|marijuana)\b", r"mucadele|onleme|prevention|hukuk|\blaw\b",
     "drug production"),
    (HARM, "high", "all", r"\bmein kampf\b", None, "Mein Kampf"),
    (HARM, "review", "all", r"\bkendine zarar\w*|\bself-?harm\w*|\bself-?injur\w*",
     r"onleme|prevention|terap\w*|therap\w*|tedavi|yardim", "self-harm"),
    (HARM, "review", "title", r"\bnihiliz\w*|\bnihilism\w*|\bnihilist\w*",
     r"elestiri|asma|karsi|critique|overcom|kurtulus", "nihilism"),
    (HARM, "review", "title", r"\bpsikedelik\w*|\bpsychedeli(?!a\b)\w*|\blsd\b",
     r"rock|muzik|music|anatolian|" + SCHOLARLY, "psychedelics"),
]

# Subject headings, matched per entry ("Heading -- Subdivision"). Always
# "review": the headings come from OpenLibrary and are often wrong.
# (category, pattern, row-level exempt, label)
SUBJECT_RULES = [
    (ISLAM, r"^(?:islam|koran|qur'?an|muhammad|muhammed|prophet|hadith|sunnah)\b.*-- ?controversial literature",
     None, "LoC: Islam -- Controversial literature"),
    (ISLAM, r"^religion\b.*-- ?controversial literature", None, "LoC: Religion -- Controversial literature"),
    (OCCULT, r"^(?:satanism|black magic|devil worship)\b", FICTION, "LoC: Satanism"),
    (OCCULT, r"^(?:occultism|magic|witchcraft|astrology|divination|tarot|spiritualism|fortune-telling|demonology"
             r"|cabala|kabbalah)\s*(?:--|$)", FICTION, "LoC: occult subject"),
    (SEX, r"^(?:erotic\w*|sex instruction|sex manuals|sexual fantasies|pornograph\w*)\b(?!.* for (?:children|youth))",
     PROTECTIVE, "LoC: erotic/sex instruction"),
]

# Authors whose Turkish-market output is dominated by one flagged theme.
# (category, pattern, require, label): when `require` is set and does not
# match the title/subjects, the hit drops to "review" (or is skipped for
# authors marked skip_otherwise).
AUTHOR_RULES = [
    (ISLAM, r"\bturan dursun\b", RELIGION, False, "author: Turan Dursun"),
    (ISLAM, r"\bilhan arsel\b", RELIGION, False, "author: Ilhan Arsel"),
    (ISLAM, r"\bibn (?:warraq|varrak)\b", RELIGION, False, "author: Ibn Warraq"),
    (ISLAM, r"\bhamed abdel-? ?samad\b", RELIGION, False, "author: Hamed Abdel-Samad"),
    (ISLAM, r"\bali sina\b", RELIGION, True, "author: Ali Sina"),
    (ISLAM, r"\bayaan hirsi ali\b", RELIGION, False, "author: Ayaan Hirsi Ali"),
    (ISLAM, r"\b(?:richard dawkins|christopher hitchens|sam harris|daniel c?\.? ?dennett)\b", RELIGION, True,
     "author: New Atheism"),
    (OCCULT, r"\banton (?:szandor )?la ?vey\b|\baleister crowley\b", None, False, "author: LaVey/Crowley"),
    (HARM, r"\badolf hitler\b", None, False, "author: Adolf Hitler"),
]
AUTHOR_HIGH = {"author: Turan Dursun", "author: Ilhan Arsel", "author: Ibn Warraq", "author: Hamed Abdel-Samad",
               "author: LaVey/Crowley", "author: Adolf Hitler"}

# Library classification ranges (curated by librarians, so kept as given).
LCC_RULES = [  # (class letters, low, high, category, confidence, label)
    ("hq", 450, 472.99, SEX, "high", "LCC HQ450-472 (erotica)"),
    ("hq", 21, 32.99, SEX, "review", "LCC HQ21-32 (sex instruction)"),
    ("bf", 1546, 1550.99, OCCULT, "high", "LCC BF1546-1550 (satanism)"),
    ("bf", 1228, 1999.99, OCCULT, "review", "LCC BF1228-1999 (occult sciences)"),
]
DEWEY_RULES = [  # (prefix, category, confidence, label)
    ("306.7", SEX, "review", "Dewey 306.7 (sexual relations)"),
    ("613.96", SEX, "review", "Dewey 613.96 (sex hygiene)"),
    ("133", OCCULT, "review", "Dewey 133 (occultism)"),
]

_TEXT = [(c, conf, scope, re.compile(p), re.compile(e) if e else None, lab)
         for c, conf, scope, p, e, lab in TEXT_RULES]
_TEXT_ANY = re.compile("|".join(f"(?:{r[3]})" for r in TEXT_RULES))
_SUBJ = [(c, re.compile(p), re.compile(e) if e else None, lab) for c, p, e, lab in SUBJECT_RULES]
_AUTH = [(c, re.compile(p), re.compile(r) if r else None, skip, lab) for c, p, r, skip, lab in AUTHOR_RULES]
_LCC = re.compile(r"^\s*([a-z]{1,3})\s*(\d+(?:\.\d+)?)")
_FICTION, _SCHOLARLY = re.compile(FICTION), re.compile(SCHOLARLY)
CATEGORY_ORDER = {SEX: 0, ISLAM: 1, OCCULT: 2, HARM: 3}


def screen(title, meta, desc, subjects, authors, lcc, dewey):
    """Screen one row (all arguments folded). Returns [(category, confidence, label, matched, field)].

    title: title | subtitle | series      meta: genres, subjects, category
    """
    hits = []
    everything = f"{title} | {meta} | {desc}"
    for field, text in (("title", title), ("subjects/genres", meta), ("description", desc)):
        if not text or not _TEXT_ANY.search(text):
            continue
        for cat, conf, scope, pat, exempt, label in _TEXT:
            if scope == "title" and field != "title":
                continue
            m = pat.search(text)
            if m and not (exempt and exempt.search(everything)):
                hits.append((cat, conf if field == "title" else "review", label, m.group(0), field))
    for entry in subjects:
        for cat, pat, exempt, label in _SUBJ:
            if pat.search(entry) and not (exempt and exempt.search(everything)):
                hits.append((cat, "review", label, entry, "subjects"))
    if authors:
        for cat, pat, require, skip_otherwise, label in _AUTH:
            m = pat.search(authors)
            if not m:
                continue
            on_topic = require is None or require.search(f"{title} | {meta}")
            if on_topic or not skip_otherwise:
                conf = "high" if (on_topic and label in AUTHOR_HIGH) else "review"
                hits.append((cat, conf, label, m.group(0), "authors"))
    for code in lcc:
        m = _LCC.match(code)
        if not m:
            continue
        letters, num = m.group(1), float(m.group(2))
        for cls, lo, hi, cat, conf, label in LCC_RULES:
            if letters == cls and lo <= num <= hi:
                hits.append((cat, conf, label, code.strip(), "lcc"))
                break
    for code in dewey:
        code = code.replace("/", "").strip()
        for prefix, cat, conf, label in DEWEY_RULES:
            if code.startswith(prefix):
                hits.append((cat, conf, label, code, "dewey"))
    if not hits:
        return hits

    # Context adjustments.
    if PRO_ISLAMIC_AUTHORS.search(authors):
        hits = [h for h in hits if h[0] not in (ISLAM, OCCULT)]
    fiction = _FICTION.search(f"{title} | {meta}")
    scholarly = _SCHOLARLY.search(f"{title} | {meta}")
    adjusted = []
    for cat, conf, label, matched, field in hits:
        if cat == OCCULT and not label.startswith("author"):
            if conf == "review" and scholarly:
                continue  # history of astrology, Ottoman divination texts, folklore
            if fiction:
                conf = "review"  # fantasy/thriller novels, not occult practice
        adjusted.append((cat, conf, label, matched, field))
    return adjusted


# --------------------------------------------------------------------------
# ISBN helpers
# --------------------------------------------------------------------------
def check_digit13(first12):
    return str((10 - sum(int(c) * (3 if i % 2 else 1) for i, c in enumerate(first12)) % 10) % 10)


def valid13(isbn):
    return len(isbn) == 13 and isbn.isdigit() and check_digit13(isbn[:12]) == isbn[12]


def to13(code):
    code = re.sub(r"[^0-9Xx]", "", code)
    if len(code) == 10:
        return "978" + code[:9] + check_digit13("978" + code[:9])
    return code if len(code) == 13 else ""


# --------------------------------------------------------------------------
# Metadata enrichment. Every request below targets a search or metadata
# endpoint; responses are parsed as JSON/HTML text and nothing is saved
# except the extracted metadata fields.
# --------------------------------------------------------------------------
UA = "bill-ptm-metadata-cleaner/1.0 (bibliographic metadata lookup)"


def http_session(retry_on_429=True):
    statuses = [500, 502, 503, 504] + ([429] if retry_on_429 else [])
    s = requests.Session()
    s.headers["User-Agent"] = UA
    s.mount("https://", HTTPAdapter(max_retries=Retry(
        total=6, connect=6, read=6, backoff_factor=2, status_forcelist=statuses, allowed_methods=["GET"])))
    return s


class Cache:
    """Append-only JSONL: one {"isbn": ..., "meta": {...} | null} per line."""

    def __init__(self, path):
        self.data = {}
        if path.exists():
            with path.open(encoding="utf-8") as fh:
                for line in fh:
                    if line.strip():
                        rec = json.loads(line)
                        self.data[rec["isbn"]] = rec["meta"]
        self.fh = path.open("a", encoding="utf-8")

    def put(self, isbn, meta):
        self.data[isbn] = meta
        self.fh.write(json.dumps({"isbn": isbn, "meta": meta}, ensure_ascii=False) + "\n")

    def close(self):
        self.fh.close()


OL_URL = "https://openlibrary.org/search.json"
OL_FIELDS = "title,subtitle,author_name,subject,publisher,language,isbn"


def enrich_openlibrary(isbns, cache, batch=300, delay=1.0):
    s = http_session()
    todo = [i for i in isbns if i not in cache.data]
    log.info("OpenLibrary: %d ISBNs to query (%d already cached)", len(todo), len(isbns) - len(todo))
    failed = 0
    for start in tqdm(range(0, len(todo), batch), desc="OpenLibrary", unit="batch"):
        chunk = todo[start:start + batch]
        want, found = set(chunk), {}
        try:
            r = s.get(OL_URL, params={"q": "isbn:(" + " OR ".join(chunk) + ")", "fields": OL_FIELDS,
                                      "limit": 1000}, timeout=180)
            r.raise_for_status()
            docs = r.json().get("docs", [])
        except (requests.RequestException, ValueError) as exc:
            failed += 1
            log.warning("OpenLibrary batch at %d failed, left uncached for the next run: %s", start, exc)
            continue
        for doc in docs:
            meta = {
                "title": doc.get("title", ""), "subtitle": doc.get("subtitle", ""),
                "authors": "; ".join(doc.get("author_name", [])[:10]),
                "subjects": "; ".join(doc.get("subject", [])[:40]),
                "publisher": (doc.get("publisher") or [""])[0],
                "language": "; ".join(doc.get("language", [])),
                "source": "openlibrary_search",
            }
            for code in doc.get("isbn", []):
                code13 = to13(code)
                if code13 in want and code13 not in found:
                    found[code13] = meta
        for i in chunk:
            cache.put(i, found.get(i))
        cache.fh.flush()
        time.sleep(delay)
    if failed:
        log.warning("OpenLibrary: %d batches failed; re-run the script to retry them", failed)


GB_URL = "https://www.googleapis.com/books/v1/volumes"
GB_FIELDS = "items(volumeInfo(title,subtitle,authors,publisher,language,categories,description))"


def enrich_google(isbns, cache, api_key=None, delay=0.3):
    s = http_session(retry_on_429=False)
    todo = [i for i in isbns if i not in cache.data]
    if not todo:
        return
    base = {"fields": GB_FIELDS, "maxResults": 1}
    if api_key:
        base["key"] = api_key
    for i in tqdm(todo, desc="Google Books", unit="isbn"):
        try:
            r = s.get(GB_URL, params={**base, "q": f"isbn:{i}"}, timeout=60)
        except requests.RequestException as exc:
            log.warning("Google Books request failed for %s: %s", i, exc)
            continue
        if r.status_code in (403, 429):
            log.warning("Google Books: HTTP %d (%s). Stage stopped; set GOOGLE_BOOKS_API_KEY with a raised quota "
                        "to use it.", r.status_code, r.json().get("error", {}).get("message", "")[:120]
                        if r.headers.get("content-type", "").startswith("application/json") else "")
            return
        if not r.ok:
            continue
        items = r.json().get("items") or []
        meta = None
        if items:
            v = items[0].get("volumeInfo", {})
            meta = {"title": v.get("title", ""), "subtitle": v.get("subtitle", ""),
                    "authors": "; ".join(v.get("authors", [])), "publisher": v.get("publisher", ""),
                    "language": v.get("language", ""), "genres": "; ".join(v.get("categories", [])),
                    "description": v.get("description", ""), "source": "google_books"}
        cache.put(i, meta)
        cache.fh.flush()
        time.sleep(delay)


AA_HOST = "https://annas-archive.gl"
AA_RECORD_LINK = re.compile(r'href="/(?:isbndb|oclc|gbooks|goodreads|ol|edsebk|libby|nexusstc|md5)/[^"]+"')


def aa_blocked(resp):
    head = resp.text[:3000]
    return resp.status_code in (403, 429, 503) or "DDoS-Guard" in head or "<title>Loading" in head


def aa_parse_title(page):
    """Title of the first result card on a search page, or ''."""
    page = page.replace("<!--", "").replace("-->", "")  # results are shipped inside comments
    m = AA_RECORD_LINK.search(page)
    if not m:
        return ""
    h3 = re.search(r"<h3[^>]*>(.*?)</h3>", page[m.end():m.end() + 6000], re.S)
    return squash(html.unescape(re.sub(r"<[^>]+>", " ", h3.group(1)))) if h3 else ""


def enrich_annas_archive(isbns, cache, delay=2.0):
    """Anna's Archive METADATA search only: GET /search, read the result title.

    Never requests /md5/, /fast_download/, /slow_download/ or any file URL.
    """
    s = http_session(retry_on_429=False)
    todo = [i for i in isbns if i not in cache.data]
    if not todo:
        return
    try:
        probe = s.get(f"{AA_HOST}/search", params={"index": "meta", "q": todo[0]}, timeout=60)
    except requests.RequestException as exc:
        log.warning("Anna's Archive (%s) unreachable: %s. Stage skipped.", AA_HOST, exc)
        return
    if aa_blocked(probe):
        log.warning("Anna's Archive (%s): HTTP %d, bot-protection page returned. Stage skipped; "
                    "%d ISBNs left unresolved.", AA_HOST, probe.status_code, len(todo))
        return
    for i in tqdm(todo, desc="Anna's Archive meta", unit="isbn"):
        try:
            r = s.get(f"{AA_HOST}/search", params={"index": "meta", "q": i}, timeout=60)
        except requests.RequestException as exc:
            log.warning("Anna's Archive request failed for %s: %s", i, exc)
            continue
        if aa_blocked(r):
            log.warning("Anna's Archive started blocking after some requests; stage stopped.")
            return
        title = aa_parse_title(r.text) if r.ok else ""
        cache.put(i, {"title": title, "source": "annas_archive_meta"} if title else None)
        cache.fh.flush()
        time.sleep(delay)


# --------------------------------------------------------------------------
# Output shaping
# --------------------------------------------------------------------------
def join_nonempty(*parts):
    return "; ".join(p for p in parts if p)


def tidy(df):
    inferred = df["publisher_inferred_from_prefix"].str.replace(r"\s*\(\d+ of \d+ known books?\)$", "", regex=True)
    has_pub = df["publisher"] != ""
    out = pd.DataFrame({
        "ISBN": df["isbn13"],
        "ISBN10": df["isbn10"],
        "Title": df["title"],
        "Subtitle": df["subtitle"],
        "Author": df["authors"],
        "Contributors": df["contributors"],
        "Genre": [join_nonempty(g, f) for g, f in zip(df["genres"], df["fiction_or_type"])],
        "Category": df["broad_category"],
        "Subjects": df["subjects"],
        "Publisher": df["publisher"].where(has_pub, inferred),
        "Publisher_Source": ["record" if p else ("isbn_prefix" if i else "") for p, i in zip(has_pub, inferred)],
        "Publish_Year": df["publish_year"],
        "Publish_Place": df["publish_place"],
        "Language": df["language"],
        "Pages": df["pages"],
        "Format": df["format"],
        "Series": df["series"],
        "Edition": df["edition"],
        "Translated_From": df["translated_from"],
        "Description": df["description"],
        "Dewey": df["dewey"],
        "LCC": df["lcc"],
        "OCLC": df["oclc_numbers"],
        "OpenLibrary_Work": df["openlibrary_work"],
        "Goodreads_ID": df["goodreads_ids"],
        "Wikidata": df["wikidata"],
        "Metadata_Source": df["metadata_sources"],
    }, index=df.index)
    return out


# --------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--input", default="data/turkish_books_list1_all_isbns.csv")
    ap.add_argument("--outdir", default="output")
    ap.add_argument("--cache-dir", default="data/cache")
    ap.add_argument("--skip-enrich", action="store_true", help="screen only; no network lookups")
    ap.add_argument("--ol-delay", type=float, default=1.0, help="seconds between OpenLibrary batches")
    ap.add_argument("--aa-delay", type=float, default=2.0, help="seconds between Anna's Archive searches")
    args = ap.parse_args()

    outdir, cache_dir = Path(args.outdir), Path(args.cache_dir)
    outdir.mkdir(parents=True, exist_ok=True)
    cache_dir.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
                        handlers=[logging.StreamHandler(), logging.FileHandler(outdir / "run_log.txt", "w", "utf-8")])
    t0 = time.time()

    # 1. Load and normalise --------------------------------------------------
    df = pd.read_csv(args.input, dtype=str, keep_default_na=False, encoding="utf-8-sig")
    n_in = len(df)
    log.info("Loaded %d rows, %d columns from %s", n_in, df.shape[1], args.input)
    for col in tqdm(df.columns, desc="Normalising", unit="col"):
        df[col] = df[col].map(squash)
    df["isbn13"] = df["isbn13"].str.replace(r"[^0-9Xx]", "", regex=True)

    elim = {}  # index -> (category, confidence, reason, matched_terms)

    df["_valid"] = df["isbn13"].map(valid13)
    df["_key"] = df["isbn13"].str[:12]
    df["_titled"] = df["title"] != ""
    ranked = df.sort_values(["_key", "_valid", "_titled"], ascending=[True, False, False], kind="stable")
    keeper = ranked.drop_duplicates("_key", keep="first").set_index("_key")["isbn13"]
    for idx, row in ranked[ranked.duplicated("_key", keep="first")].iterrows():
        kept = keeper[row["_key"]]
        why = ("Duplicate ISBN" if kept == row["isbn13"] else
               f"Duplicate of ISBN {kept}: same first 12 digits, this copy has a wrong check digit")
        elim[idx] = ("duplicate", "n/a", why, "")
    for idx, row in df[~df["_valid"] & ~df.index.isin(list(elim))].iterrows():
        exp = check_digit13(row["isbn13"][:12]) if row["isbn13"][:12].isdigit() else "?"
        elim[idx] = ("invalid_isbn", "n/a", f"Invalid ISBN-13 check digit (expected {exp})", "")
    n_dup = sum(1 for v in elim.values() if v[0] == "duplicate")
    log.info("ISBN checks: %d duplicates, %d invalid check digits", n_dup, len(elim) - n_dup)

    # 2. Enrich rows without a title ---------------------------------------
    enrich_stats = Counter()
    missing = df.index[(df["title"] == "") & ~df.index.isin(list(elim))]
    log.info("Rows without a title: %d", len(missing))
    if not args.skip_enrich and len(missing):
        isbns = df.loc[missing, "isbn13"].tolist()
        stages = [
            ("openlibrary", lambda todo, c: enrich_openlibrary(todo, c, delay=args.ol_delay)),
            ("google_books", lambda todo, c: enrich_google(todo, c, os.environ.get("GOOGLE_BOOKS_API_KEY"))),
            ("annas_archive", lambda todo, c: enrich_annas_archive(todo, c, delay=args.aa_delay)),
        ]
        resolved = {}
        for name, run in stages:
            todo = [i for i in isbns if i not in resolved]
            if not todo:
                break
            cache = Cache(cache_dir / f"{name}.jsonl")
            run(todo, cache)
            cache.close()
            hits = {i: cache.data[i] for i in todo if cache.data.get(i) and cache.data[i].get("title")}
            resolved.update(hits)
            enrich_stats[f"{name}_queried"] = sum(1 for i in todo if i in cache.data)
            enrich_stats[f"{name}_resolved"] = len(hits)
            log.info("%s: %d queried, %d resolved", name, enrich_stats[f"{name}_queried"], len(hits))
        by_isbn = pd.Series(df.index, index=df["isbn13"])
        for isbn, meta in resolved.items():
            idx = by_isbn[isbn]
            for field in ("title", "subtitle", "authors", "subjects", "publisher", "language", "genres", "description"):
                if meta.get(field) and not df.at[idx, field]:
                    df.at[idx, field] = squash(meta[field])
            df.at[idx, "metadata_sources"] = join_nonempty(df.at[idx, "metadata_sources"], meta["source"])
        log.info("Enrichment filled titles for %d of %d rows", len(resolved), len(missing))

    # 3. Screen ------------------------------------------------------------
    rule_hits = Counter()
    todo = df.index[~df.index.isin(list(elim))]
    cols = ["title", "subtitle", "series", "genres", "subjects", "subject_places_people_times",
            "broad_category", "fiction_or_type", "description", "authors", "contributors", "lcc", "dewey"]
    for idx, *vals in tqdm(df.loc[todo, cols].itertuples(name=None), total=len(todo), desc="Screening", unit="row"):
        (title, subtitle, series, genres, subjects, spt, broad, ftype, desc, authors, contrib, lcc, dewey) = vals
        hits = screen(fold(" | ".join((title, subtitle, series))),
                      fold(" | ".join((genres, subjects, spt, broad, ftype))),
                      fold(desc),
                      [fold(s).strip() for s in subjects.split(";") if s.strip()],
                      fold(join_nonempty(authors, contrib)),
                      [fold(s) for s in lcc.split(";") if s.strip()],
                      [s for s in dewey.split(";") if s.strip()])
        if hits:
            hits.sort(key=lambda h: (h[1] != "high", CATEGORY_ORDER[h[0]]))
            cat, conf = hits[0][0], hits[0][1]
            reasons = []
            for h in hits:
                rule_hits[h[2]] += 1
                note = f"{CATEGORY_NAMES[h[0]]}: {h[2]} ('{h[3]}' in {h[4]})"
                if note not in reasons:
                    reasons.append(note)
            elim[idx] = (cat, conf, " | ".join(reasons[:4]), "; ".join(sorted({h[3] for h in hits}))[:300])
        elif not title:
            elim[idx] = ("no_metadata", "n/a",
                         "No title or metadata found (OpenLibrary, Google Books, Anna's Archive); cannot be screened",
                         "")

    # 4. Write ---------------------------------------------------------------
    tidy_df = tidy(df)
    gone = pd.Index(list(elim))
    cleaned = tidy_df.drop(index=gone).sort_values("ISBN")
    eliminated = tidy_df.loc[gone].copy()
    eliminated.insert(1, "Elimination_Category", [CATEGORY_NAMES[elim[i][0]] for i in gone])
    eliminated.insert(2, "Confidence", [elim[i][1] for i in gone])
    eliminated.insert(3, "Elimination_Reason", [elim[i][2] for i in gone])
    eliminated.insert(4, "Matched_Terms", [elim[i][3] for i in gone])
    eliminated = eliminated.sort_values(["Elimination_Category", "Confidence", "ISBN"])
    # Bare ISBNs with no metadata get their own file; they were never screened.
    bare = eliminated["Elimination_Category"] == CATEGORY_NAMES["no_metadata"]
    no_isbns = tidy_df.loc[eliminated.index[bare]].sort_values("ISBN")
    no_isbns = no_isbns.loc[:, (no_isbns != "").any()]
    eliminated = eliminated[~bare]

    cleaned_path, elim_path = outdir / "cleaned_books.csv", outdir / "eliminated_books.csv"
    bare_path = outdir / "NO ISBNS.csv"
    cleaned.to_csv(cleaned_path, index=False, encoding="utf-8-sig", quoting=csv.QUOTE_MINIMAL)
    eliminated.to_csv(elim_path, index=False, encoding="utf-8-sig", quoting=csv.QUOTE_MINIMAL)
    no_isbns.to_csv(bare_path, index=False, encoding="utf-8-sig", quoting=csv.QUOTE_MINIMAL)

    # Summary ----------------------------------------------------------------
    by_cat = Counter((elim[i][0], elim[i][1]) for i in gone)
    log.info("=" * 66)
    log.info("Input rows:            %8d", n_in)
    log.info("Cleaned (approved):    %8d  -> %s", len(cleaned), cleaned_path)
    log.info("Eliminated:            %8d  -> %s", len(eliminated), elim_path)
    log.info("No metadata:           %8d  -> %s", len(no_isbns), bare_path)
    for key in ("openlibrary", "google_books", "annas_archive"):
        if f"{key}_queried" in enrich_stats or f"{key}_resolved" in enrich_stats:
            log.info("  enrich %-14s queried %7d  resolved %6d", key,
                     enrich_stats[f"{key}_queried"], enrich_stats[f"{key}_resolved"])
    log.info("Eliminated by category / confidence:")
    for (cat, conf), n in sorted(by_cat.items(), key=lambda kv: -kv[1]):
        log.info("  %-62s %-6s %7d", CATEGORY_NAMES[cat], conf, n)
    log.info("Rule hits (a row can hit several):")
    for label, n in rule_hits.most_common():
        log.info("  %-45s %6d", label, n)
    log.info("Elapsed: %.1f min", (time.time() - t0) / 60)


if __name__ == "__main__":
    main()
