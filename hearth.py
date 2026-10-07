#!/usr/bin/env python3
# hearth.py - Fully Automated Anna's Archive Downloader v3.3-final-SECURED-v4-LIBGEN-SPLIT
# Fixes Applied:
#   - FIXED: downloads_path removed (Playwright API compatibility)
#   - FIXED: trigger_download_and_save uses file polling for completion detection
#   - FIXED: Full 25-min Libgen wait with actual file size monitoring
#   - FIXED: 3-cancel strike rule for interrupted downloads
#   - FIXED: Auto-resume cancelled downloads
#   - FIXED: File verification before marking complete
#   - FIXED: Recovery logic with strict MD5 matching
#   - FIXED: Windows Downloads folder fallback
#   - NEW: Libgen URLs extracted to separate file for manual processing
#   - PRESERVED: All retry, Turkish filter, Libgen 3306, IPFS logic

import os
import sys
import json
import time
import re
import signal
import random
import shutil
from datetime import datetime
from urllib.parse import urlparse, quote, unquote
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError

# ==========================
# STEALTH MODULE SAFETY
# ==========================
STEALTH_AVAILABLE = False
stealth_apply_func = None

try:
    from playwright_stealth import StealthOptions, stealth_sync
    STEALTH_AVAILABLE = True
    stealth_apply_func = stealth_sync
    print("[+] playwright-stealth imported successfully")
except ImportError:
    try:
        from playwright_stealth import Stealth
        _stealth_test = Stealth()
        if hasattr(_stealth_test, 'apply'):
            STEALTH_AVAILABLE = True
            stealth_apply_func = lambda ctx: _stealth_test.apply(ctx)
            print("[+] Legacy playwright-stealth imported")
        else:
            STEALTH_AVAILABLE = False
            print("[!] playwright-stealth installed but incompatible - skipping stealth")
    except ImportError:
        STEALTH_AVAILABLE = False
        print("[!] playwright-stealth not installed - running without stealth")
# ==========================
# GLOBAL STATE MANAGEMENT
# ==========================
shutdown_requested = False
book_counter = 0
last_health_check = None
current_targets = []
current_idx = 0
AA_SECRET = ""
last_libgen_3306_time = None
processed_in_this_session = set()
libgen_urls_found = []  # Store Libgen URLs for export

def recover_downloaded_files(download_dir, url, max_age_hours=None):
    """
    STRICT: Only recover if MD5 IN FILENAME MATCHES EXACTLY
    max_age_hours=None means no age cutoff (matches your old recovered books too)
    """
    if not os.path.isdir(download_dir):
        return None
    
    # Extract target MD5 from URL
    md5_match = re.search(r'/md5/([0-9a-fA-F]{32})', url)
    if not md5_match:
        return None
    
    target_md5 = md5_match.group(1).lower()
    if max_age_hours is not None:
        cutoff_time = time.time() - (max_age_hours * 3600)
    else:
        cutoff_time = 0  # ← no age limit
    
    candidates = []
    
    for f in os.listdir(download_dir):
        fp = os.path.join(download_dir, f)
        if not os.path.isfile(fp):
            continue
        
        if f.endswith('.part') or f.endswith('.tmp') or f.endswith('.crdownload'):
            continue
        
        try:
            mtime = os.path.getmtime(fp)
            size = os.path.getsize(fp)
            
            if size < 1024:
                continue
            
            if mtime < cutoff_time:
                continue
            
            # STRICT MATCH: Target MD5 must be IN filename
            if target_md5.lower() not in f.lower():
                continue  # SKIP this file
            
            with open(fp, 'rb') as vf:
                header = vf.read(8)
                valid = header[:4] == b'%PDF' or header[:4] == b'PK\x03\x04' or (len(header) > 60 and header[60:68] == b'BOOKMOBI') or header[:4] == b'AT&TFORM'
                if not valid:
                    continue
            
            candidates.append((f, fp, size, mtime))
        
        except Exception:
            continue
    
    if not candidates:
        return None
    
    candidates.sort(key=lambda x: x[3], reverse=True)
    best_file, best_path, best_size, _ = candidates[0]
    
    clean_title = clean_downloaded_title(best_file, url)
    base_file_name = generate_custom_filename(clean_title, url, NAME_FORMAT)
    final_name = get_unique_filename(download_dir, base_file_name)
    final_path = os.path.join(download_dir, final_name)
    
    if best_path != final_path:
        shutil.move(best_path, final_path)
    
    print(f"  [+] [RECOVERED] Verified: {final_name} ({best_size:,} bytes)")
    return clean_title, final_name

_libgen_saved = False
_libgen_saved_count = 0

def load_libgen_manual_queue():
    if os.path.exists(LIBGEN_MANUAL_FILE):
        try:
            with open(LIBGEN_MANUAL_FILE, "r", encoding="utf-8") as f:
                return [line.strip() for line in f if line.strip()]
        except Exception:
            pass
    return []

def libgen_queue_book_urls():
    urls = set()
    md5s = set()
    for line in load_libgen_manual_queue():
        if " ||| " in line:
            urls.add(line.split(" ||| ")[0].strip())
        elif line.startswith("http"):
            m = re.search(r'[?&]md5=([0-9a-fA-F]{32})', line)
            if not m:
                m = re.search(r'/md5/([0-9a-fA-F]{32})', line)
            if m:
                md5s.add(m.group(1).lower())
    return urls, md5s

def save_libgen_queue():
    global _libgen_saved, _libgen_saved_count
    if not libgen_urls_found or len(libgen_urls_found) <= _libgen_saved_count:
        return
    try:
        new_entries = libgen_urls_found[_libgen_saved_count:]
        with open(LIBGEN_MANUAL_FILE, "a", encoding="utf-8") as f:
            for entry in new_entries:
                f.write(f"{entry}\n")
        _libgen_saved_count = len(libgen_urls_found)
        print(f"\n  Libgen queue: {len(new_entries)} new entries appended ({_libgen_saved_count} total this session)")
        _libgen_saved = True
    except Exception as e:
        print(f"  [!] Error saving Libgen queue: {e}")
        
def signal_handler(sig, frame):
    global shutdown_requested
    shutdown_requested = True
    print("\n\n[⚠️] SHUTDOWN REQUESTED - Saving progress before exit...")
    save_libgen_queue()  # Save Libgen URLs on shutdown

signal.signal(signal.SIGINT, signal_handler)
signal.signal(signal.SIGTERM, signal_handler)

# ==========================
# COMMAND LINE CONFIGURATION
# ==========================
NAME_FORMAT = "full"
MAX_ATTEMPTS = 4
FORCE_RESTART = False
DOWNLOAD_DIR = ""
LIST_URL = None
TXT_MODE = False
RETRY_MODE = False

if 3 <= len(sys.argv) <= 5:
    arg1 = sys.argv[1].strip()
    DOWNLOAD_DIR = os.path.abspath(sys.argv[2].strip())

    if not os.path.isdir(DOWNLOAD_DIR) and not os.path.exists(DOWNLOAD_DIR):
        try:
            os.makedirs(DOWNLOAD_DIR, exist_ok=True)
            print(f"[✓] Created download directory: {DOWNLOAD_DIR}")
        except Exception as e:
            print(f"\n[ERROR] Cannot create download directory: {e}")
            sys.exit(1)

    if len(sys.argv) == 4 and not sys.argv[3].startswith("--"):
        optional_arg = sys.argv[3].strip().lower()
        if optional_arg.isdigit():
            MAX_ATTEMPTS = int(optional_arg)
            if MAX_ATTEMPTS < 1:
                print("\n[ERROR] Max attempts must be at least 1.")
                sys.exit(1)
        elif optional_arg in ["full", "info", "author", "title"]:
            NAME_FORMAT = optional_arg
        else:
            print(f"\n[ERROR] Invalid file naming format '{optional_arg}'.")
            sys.exit(1)

    elif len(sys.argv) >= 5 and not sys.argv[3].startswith("--"):
        parsed_format = sys.argv[3].strip().lower()
        if parsed_format not in ["full", "info", "author", "title"]:
            print(f"\n[ERROR] Invalid file naming format '{parsed_format}'.")
            sys.exit(1)
        NAME_FORMAT = parsed_format
        if not sys.argv[4].startswith("--"):
            try:
                MAX_ATTEMPTS = int(sys.argv[4].strip())
                if MAX_ATTEMPTS < 1:
                    print("\n[ERROR] Max attempts must be at least 1.")
                    sys.exit(1)
            except ValueError:
                print(f"\n[ERROR] Invalid max attempts '{sys.argv[4]}'.")
                sys.exit(1)

    if arg1.lower() == "text":
        TXT_MODE = True
    elif arg1.lower() == "retry":
        RETRY_MODE = True
    else:
        if not (arg1.startswith("http") and "annas-archive." in arg1.lower()):
            print(f"\n[ERROR] Invalid URL provided!")
            sys.exit(1)
        LIST_URL = arg1
else:
    print("Invalid arguments! Please use one of the following formats:")
    print('  python hearth.py text "<download_folder>" [filename format] [--reset-quota]')
    print('  python hearth.py retry "<download_folder>" [filename format] [--reset-quota]')
    print('  python hearth.py "https://annas-archive.XX/list/<list_id>" "<download_folder>" [--reset-quota]')
    sys.exit(1)

FILTER_TURKISH_ONLY = True
RESET_QUOTA = False

for arg in sys.argv:
    if arg.lower() in ["--all-books", "--no-filter"]:
        FILTER_TURKISH_ONLY = False
    if arg.lower() == "--reset-quota":
        RESET_QUOTA = True

# ==========================
# FILE PATH DEFINITIONS
# ==========================
TXT_FILE = os.path.join(DOWNLOAD_DIR, "aa_links.txt")
COMPLETED_FILE = os.path.join(DOWNLOAD_DIR, "completed.txt")
FAILED_FILE = os.path.join(DOWNLOAD_DIR, "failed_downloads.json")
DUPLICATES_FILE = os.path.join(DOWNLOAD_DIR, "duplicates.txt")
PROGRESS_CACHE = os.path.join(DOWNLOAD_DIR, ".hearth_filter_progress.json")
STATE_SNAPSHOT = os.path.join(DOWNLOAD_DIR, ".hearth_state.json")
FAST_HISTORY_FILE = os.path.join(DOWNLOAD_DIR, "fast_quota.json")
SECRET_FILE = os.path.join(DOWNLOAD_DIR, "aa_secret.txt")
LIBGEN_RETRY_FILE = os.path.join(DOWNLOAD_DIR, "libgen_retry_queue.txt")
NOT_TURKISH_CACHE = os.path.join(DOWNLOAD_DIR, "not_turkish_cache.json")
LIBGEN_MANUAL_FILE = os.path.join(DOWNLOAD_DIR, "libgen_manual_queue.txt")  # NEW: Libgen queue

# ==============================
# CONFIGURATION CONSTANTS
# ==============================
FAST_LIMIT = int(os.environ.get("HEARTH_FAST_LIMIT", "100"))
FAST_WINDOW_HOURS = 18
LIBGEN_DOWNLOAD_TIMEOUT_MINUTES = 25
NAVIGATION_TIMEOUT_SECONDS = 120
STUCK_DETECTION_SECONDS = 120
HEALTH_CHECK_INTERVAL_SECONDS = 1800
MIRROR_RETRY_DELAY_SECONDS = 3
MAX_BROWSER_RESTARTS = 5
MAX_DDOS_RETRIES = 2

DDOS_WAIT_FOR_RESOLUTION_SECONDS = 30
DDOS_POLL_INTERVAL_SECONDS = 3

MAX_FAIL_RETRIES_PER_BOOK = 5
MIN_TIME_BETWEEN_RETRIES_SECONDS = 60

LIBGEN_SKIP_WINDOW_MINUTES = 15
IPFS_HTTP_TIMEOUT_MS = 300000
IPFS_GATEWAYS = [
    "https://ipfs.io/ipfs/",
    "https://dweb.link/ipfs/",
    "https://gateway.pinata.cloud/ipfs/",
    "https://cloudflare-ipfs.com/ipfs/",
    "https://cf-ipfs.com/ipfs/",
]

# ==========================
# UTILITY FUNCTIONS
# ==========================
def load_aa_secret():
    env_key = os.environ.get("AA_SECRET_KEY", "").strip()
    if env_key:
        print(f"  [+] AA_SECRET loaded from environment variable")
        return env_key
    if os.path.exists(SECRET_FILE):
        try:
            with open(SECRET_FILE, "r", encoding="utf-8") as f:
                key = f.read().strip()
                if key:
                    print(f"  [+] AA_SECRET loaded from {SECRET_FILE}")
                    return key
        except Exception as e:
            print(f"  [!] Error reading secret file: {e}")
    print(f"  [!] AA_SECRET NOT FOUND - running without fast downloads!")
    return ""

def load_completed():
    if os.path.exists(COMPLETED_FILE):
        try:
            with open(COMPLETED_FILE, "r", encoding="utf-8") as f:
                return set(line.split(" ||| ")[0].strip() for line in f if line.strip())
        except Exception as e:
            print(f"[!] Warning: Error loading completed file: {e}")
            return set()
    return set()

def mark_completed(url, title):
    try:
        with open(COMPLETED_FILE, "a", encoding="utf-8") as f:
            safe_title = title.replace('\n', ' ').replace('\r', '').strip()
            f.write(f"{url} ||| {safe_title}\n")
            f.flush()
            os.fsync(f.fileno())
    except Exception as e:
        print(f"  [!] Error marking completed: {e}")

def save_failed_log(failed_items):
    try:
        with open(FAILED_FILE, "w", encoding="utf-8") as f:
            json.dump(failed_items, f, indent=4, ensure_ascii=False)
    except Exception as e:
        print(f"[!] Error saving failed log: {e}")

def load_failed_log():
    if os.path.exists(FAILED_FILE):
        try:
            with open(FAILED_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            print(f"[!] Warning: Error loading failed log: {e}")
            return []
    return []

def load_duplicates():
    if os.path.exists(DUPLICATES_FILE):
        try:
            with open(DUPLICATES_FILE, "r", encoding="utf-8") as f:
                return set(line.strip() for line in f if line.strip() and line.startswith("http"))
        except Exception:
            return set()
    return set()

def mark_duplicate(url):
    try:
        with open(DUPLICATES_FILE, "a", encoding="utf-8") as f:
            f.write(f"{url}\n")
    except Exception as e:
        print(f"  [!] Error marking duplicate: {e}")

def load_not_turkish_cache():
    if os.path.exists(NOT_TURKISH_CACHE):
        try:
            with open(NOT_TURKISH_CACHE, "r", encoding="utf-8") as f:
                data = json.load(f)
                return set(data.get("urls", []))
        except Exception:
            return set()
    return set()

def mark_not_turkish(url, lang_code):
    try:
        if os.path.exists(NOT_TURKISH_CACHE):
            with open(NOT_TURKISH_CACHE, "r", encoding="utf-8") as f:
                data = json.load(f)
        else:
            data = {"urls": [], "entries": {}}
        
        if url not in data["urls"]:
            data["urls"].append(url)
            data["entries"][url] = {
                "lang_code": lang_code,
                "timestamp": datetime.now().isoformat()
            }
            
            tmp = NOT_TURKISH_CACHE + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2)
            os.replace(tmp, NOT_TURKISH_CACHE)
            print(f"  [-] Cached as not Turkish ({lang_code}) - will skip on next run")
    except Exception as e:
        print(f"  [!] Warning: Could not cache not-Turkish URL: {e}")

def load_libgen_retry_queue():
    if os.path.exists(LIBGEN_RETRY_FILE):
        try:
            with open(LIBGEN_RETRY_FILE, "r", encoding="utf-8") as f:
                return [line.strip() for line in f if line.strip() and line.startswith("http")]
        except Exception:
            return []
    return []

def insert_failed_url_random_position(failed_url):
    try:
        if os.path.exists(TXT_FILE):
            with open(TXT_FILE, "r", encoding="utf-8") as f:
                lines = [line.strip() for line in f.readlines()]
        else:
            lines = []
        
        valid_indices = [i for i, line in enumerate(lines) if line.startswith("http")]
        
        if not valid_indices:
            with open(TXT_FILE, "a", encoding="utf-8") as f:
                f.write(f"{failed_url}\n")
            print(f"  [+] Added failed URL to empty queue")
            _update_retry_count(failed_url, 1)
            return
        
        insertion_pos = random.choice(valid_indices)
        insert_after = insertion_pos
        if insert_after < len(lines) - 1:
            insert_after = min(insert_after + random.randint(5, 20), len(lines) - 1)
        
        with open(TXT_FILE, "r", encoding="utf-8") as f:
            all_lines = f.readlines()
        
        new_line = failed_url + "\n"
        all_lines.insert(insert_after + 1, new_line)
        
        tmp_file = TXT_FILE + ".tmp"
        with open(tmp_file, "w", encoding="utf-8") as f:
            f.writelines(all_lines)
        os.replace(tmp_file, TXT_FILE)
        
        retry_count = _increment_retry_count(failed_url)
        if retry_count > MAX_FAIL_RETRIES_PER_BOOK:
            print(f"  ⚠️ Book has been retried {retry_count} times - removing from retry pool")
            return
        
        print(f"  [+] Re-queued failed URL at random position (retry #{retry_count}/{MAX_FAIL_RETRIES_PER_BOOK})")
        
    except Exception as e:
        print(f"  [!] Error inserting failed URL into queue: {e}")

def _get_retry_count(failed_url):
    failed_items = load_failed_log()
    for item in failed_items:
        if item.get("url") == failed_url:
            return item.get("retry_count", 0)
    return 0

def _increment_retry_count(failed_url):
    failed_items = load_failed_log()
    
    for i, item in enumerate(failed_items):
        if item.get("url") == failed_url:
            failed_items[i]["retry_count"] = item.get("retry_count", 0) + 1
            save_failed_log(failed_items)
            return failed_items[i]["retry_count"]
    
    failed_items.append({
        "url": failed_url,
        "retry_count": 1,
        "timestamp": datetime.now().isoformat(),
        "reason": "RE_QUEUE"
    })
    save_failed_log(failed_items)
    return 1

def _update_retry_count(failed_url, count):
    failed_items = load_failed_log()
    for i, item in enumerate(failed_items):
        if item.get("url") == failed_url:
            failed_items[i]["retry_count"] = count
            save_failed_log(failed_items)
            return

def format_elapsed_time(seconds):
    seconds = int(seconds)
    hours, remainder = divmod(seconds, 3600)
    minutes, secs = divmod(remainder, 60)
    return f"{hours}h {minutes}m {secs}s"

def sanitize_filename(filename):
    return re.sub(r'[<>:"/\\|?*]', '_', filename)

def shorten_filename_if_needed(filename, max_length=200):
    if len(filename) <= max_length:
        return filename
    base_name, ext = os.path.splitext(filename)
    truncated_base = base_name[:max_length-len(ext)-10]
    return f"{truncated_base}_truncated{ext}"

def clean_downloaded_title(filename, url):
    name = os.path.splitext(filename)[0]
    md5_hash = url.rstrip('/').split('/')[-1]
    if len(md5_hash) == 32:
        name = re.sub(md5_hash, "", name, flags=re.IGNORECASE)
    name = re.sub(r"[-_]*\s*Anna[''`\s]?s\s*Archive", "", name, flags=re.IGNORECASE)
    name = name.replace("[]", "").replace("()", "")
    return name.strip(" -_")

def generate_custom_filename(original_name, url, format_type):
    if format_type == "full":
        return sanitize_filename(original_name)
    ext = os.path.splitext(original_name)[1]
    clean_base = clean_downloaded_title(original_name, url)
    if format_type == "info":
        return sanitize_filename(f"{clean_base}{ext}")
    parts = clean_base.split(" -- ")
    if format_type == "author":
        if len(parts) >= 2:
            return sanitize_filename(f"{parts[0].strip()} - {parts[1].strip()}{ext}")
        else:
            return sanitize_filename(f"{clean_base}{ext}")
    if format_type == "title":
        return sanitize_filename(f"{parts[0].strip()}{ext}")
    return sanitize_filename(original_name)

def get_unique_filename(directory, filename):
    base_name, ext = os.path.splitext(filename)
    counter = 1
    file_path = os.path.join(directory, filename)
    while os.path.exists(file_path):
        filename = f"{base_name} ({counter}){ext}"
        file_path = os.path.join(directory, filename)
        counter += 1
    return filename

def truncate_error(error_msg, max_length=120):
    msg = str(error_msg).strip()
    if "Call log:" in msg:
        msg = msg.split("Call log:")[0].strip()
    msg = msg.replace('\n', ' ').replace('\r', '')
    if len(msg) > max_length:
        msg = msg[:max_length] + "... [truncated]"
    return msg

def check_for_page_error(page, response=None):
    error_codes = [404, 429, 500, 502, 503, 520, 521, 522, 523, 524, 525, 526, 527, 530]
    if response and response.status in error_codes:
        raise Exception(f"HTTP {response.status} returned by server")
    try:
        title = page.title().strip()
        title_lower = title.lower()
        if re.search(r'\b(404|429|500|502|503|504|520|521|522|523|524|525|526|527|530)\b', title_lower):
            raise Exception(f"Error page detected via title: '{title}'")
        text_errors = [
            "service unavailable", "temporarily unavailable",
            "bad gateway", "gateway time-out", "gateway timeout",
            "not found", "server error", "too many requests",
            "web server is down", "origin is unreachable",
            "connection timed out", "host error"
        ]
        if any(err in title_lower for err in text_errors):
            raise Exception(f"Error page detected via title: '{title}'")
    except Exception as e:
        if "Error page detected" in str(e):
            raise e

def check_ddos_block(page):
    try:
        title_lower = page.title().lower()
        body_text = page.inner_text("body").lower()[:2000]
        
        ddos_indicators = [
            "just a moment", "attention required", "cloudflare",
            "ray id:", "cf-ray", "turnstile", "ddos", "access denied"
        ]
        
        for indicator in ddos_indicators:
            if indicator in title_lower or indicator in body_text:
                return True
        return False
    except Exception:
        return False

def wait_for_ddos_to_resolve(page, max_wait_seconds=DDOS_WAIT_FOR_RESOLUTION_SECONDS, poll_interval=DDOS_POLL_INTERVAL_SECONDS):
    start_time = time.time()
    print(f"  [*] DDoS detected - waiting up to {max_wait_seconds}s for it to resolve...")
    
    while True:
        elapsed = time.time() - start_time
        if elapsed >= max_wait_seconds:
            print(f"  [!] DDoS still present after {elapsed:.1f}s - giving up")
            return False
        
        if not check_ddos_block(page):
            print(f"  [+] DDoS resolved after {elapsed:.1f}s - continuing!")
            return True
        
        try:
            title = page.title().lower()
            if "download" in title or "book" in title or "chapter" in title or "author" in title:
                print(f"  [+] Page appears to have loaded normally - continuing!")
                return True
        except Exception:
            pass
        
        page.wait_for_timeout(poll_interval * 1000)

def check_book_not_found(page, response=None):
    if response and response.status == 404:
        return True
    try:
        body_text = page.inner_text("body").lower()[:3000]
        title_lower = page.title().lower()

        not_found_indicators = [
            "could not be found",
            "page not found",
            "no files found",
            "no results found",
            "this page does not exist",
            "file not found",
            "record not found",
            "md5 not found",
            "no md5",
        ]

        for indicator in not_found_indicators:
            if indicator in body_text or indicator in title_lower:
                return True

        if re.search(r'\b404\b', title_lower):
            return True

    except Exception:
        pass
    return False

def check_out_of_fast_downloads(page):
    try:
        body_text = page.inner_text("body").lower()
        title_lower = page.title().lower()
        
        indicators = [
            "you are out of fast downloads now",
            "out of fast downloads now please use slow partner servers"
        ]
        
        for indicator in indicators:
            if indicator in body_text or indicator in title_lower:
                return True
        return False
    except Exception:
        return False

def detect_libgen_connection_limit(page):
    try:
        body_text = page.inner_text("body").lower()
        title_text = page.title().lower()
        full_text = (body_text + " " + title_text).lower()
        
        # AGGRESSIVE DETECTION - check for ANY of these patterns
        patterns = [
            "max_user_connections",
            "3306",
            "user 'libgen_get'",
            "exceeded",
            "resource",
            "could not connect",
            "database connection",
            "too many connections"
        ]
        
        # Count how many patterns match
        matches = [p for p in patterns if p in full_text]
        
        if len(matches) >= 1:
            print(f"  [+] Libgen 3306 DETECTED: {matches}")
            return True
        
        return False
    except Exception as e:
        print(f"  [!] Error detecting Libgen connection limit: {e}")
        return False

def health_check_browser(page):
    try:
        page.evaluate("() => document.title")
        return True
    except Exception:
        return False

def save_state_snapshot(idx, targets, completed_urls, failed_items):
    try:
        state = {
            "timestamp": datetime.now().isoformat(),
            "current_index": idx if idx is not None else 0,
            "total_targets": len(targets) if targets else 0,
            "completed_count": len(completed_urls),
            "failed_count": len(failed_items),
            "remaining_urls": []
        }
        if targets and idx is not None and idx < len(targets):
            state["remaining_urls"] = [t["url"] for t in targets[idx:] if t]
        tmp_file = STATE_SNAPSHOT + ".tmp"
        with open(tmp_file, "w", encoding="utf-8") as f:
            json.dump(state, f, indent=2)
        os.replace(tmp_file, STATE_SNAPSHOT)
    except Exception as e:
        print(f"[!] Warning: Could not save state snapshot: {e}")

def extract_language_from_metadata(page):
    try:
        metadata_texts = []
        for selector in ["div[data-testid='metadata']", "div[class*='metadata']", "[class*='meta']", "p", ".metadata-info"]:
            try:
                locators = page.locator(selector)
                if locators.count() > 0:
                    for i in range(locators.count()):
                        txt = locators.nth(i).inner_text()
                        metadata_texts.append(txt)
            except Exception:
                pass
        
        try:
            metadata_texts.append(page.inner_text("body"))
        except Exception:
            pass
        
        for text in metadata_texts:
            matches = re.findall(r'\[(\w{2})\]', text)
            for match in matches:
                if match.lower() == 'tr':
                    return True, 'tr', 'explicit'
            
            if re.search(r'(?:türkçe|turkish|dil:)', text, re.IGNORECASE):
                tr_matches = re.findall(r'\[tr\]', text, re.IGNORECASE)
                if tr_matches:
                    return True, 'tr', 'keyword'
            
            if re.search(r'metadata.*?(?:turkish|türkçe)', text, re.IGNORECASE):
                return True, 'tr', 'metadata'
        
        for text in metadata_texts:
            matches = re.findall(r'\[(\w{2})\]', text)
            for match in matches:
                if match.lower() in ['en', 'de', 'fr', 'es', 'ru', 'zh', 'it', 'pt']:
                    return False, match.lower(), 'explicit_other'
        
        return None, '?', 'unknown'
        
    except Exception as e:
        print(f"[!] Language extraction error: {e}")
        return None, '?', 'error'

def load_progress_cache():
    if os.path.exists(PROGRESS_CACHE):
        try:
            with open(PROGRESS_CACHE, "r", encoding="utf-8") as f:
                data = json.load(f)
                if FORCE_RESTART:
                    data["checked"] = []
                return data
        except Exception:
            pass
    return {"checked": [], "turkish_downloaded": [], "skipped_not_turkish": [], "errors": []}

def save_progress_cache(cache):
    try:
        tmp = PROGRESS_CACHE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(cache, f, indent=2)
        os.replace(tmp, PROGRESS_CACHE)
    except Exception as e:
        print(f"[!] Warning: Could not save progress cache: {e}")

def wait_for_element_or_error(page, selector, timeout_seconds=90, response=None):
    start_time = time.time()
    while True:
        try:
            loc = page.locator(selector)
            if loc.count() > 0:
                return loc.first
        except Exception:
            pass
        check_for_page_error(page, response)
        elapsed = time.time() - start_time
        if elapsed > timeout_seconds:
            is_captcha = False
            try:
                title_lower = page.title().lower()
                if "just a moment" in title_lower or "attention required" in title_lower:
                    is_captcha = True
            except Exception:
                pass
            if not is_captcha:
                raise PlaywrightTimeoutError(f"Timed out after {timeout_seconds}s waiting for '{selector}'")
        page.wait_for_timeout(1000)

def detect_connection_error(page, error_msg=None, response=None):
    if response and response.status in [429, 503, 522, 524, 502, 504]:
        return True
        
    try:
        title_lower = page.title().lower()
        body_text = page.inner_text("body").lower()[:1000]
        
        connection_error_indicators = [
            'cloudflare', 'ddos', 'attention required',
            'just a moment', 'access denied', '429',
            'ray id:', 'cf-ray', 'captcha', 'turnstile',
            'page not found', 'page unavailable', 'page inaccessible',
            "site can't be reached", 'connection timed out', 'network error',
            'ERR_', 'net::ERR_', 'refused', 'reset', 'closed',
            'could not be found', 'name not resolved', 'dns',
            '504', 'gateway time-out'
        ]
            
        for indicator in connection_error_indicators:
            if indicator in title_lower or indicator in body_text:
                return True
    except Exception:
        pass
        
    if error_msg:
        network_errors = [
            'timed out', 'connection refused', 'network error',
            'unable to connect', 'page not found',
            ' ERR_', 'net::ERR_', 'connection reset', 'connection closed',
            'could not be found', 'name not resolved', 'dns',
            '504', 'gateway'
        ]
        for err in network_errors:
            if err.lower() in error_msg.lower():
                return True
    return False

def trigger_url_download(page, dl_url, remove_referer=False):
    try:
        page.evaluate("""(args) => {
            const a = document.createElement('a');
            a.href = args.url;
            a.target = '_top';
            if (args.noRef) {
                a.rel = 'noreferrer';
            }
            document.body.appendChild(a);
            a.click();
            setTimeout(() => a.remove(), 1000);
        }""", {"url": dl_url, "noRef": remove_referer})
    except Exception as e:
        print(f"  [!] Trigger error: {e}")

def save_inline_pdf(page, md5_url):
    try:
        current_url = page.url
        if not current_url or current_url == "about:blank":
            return None

        content_type = ""
        try:
            content_type = page.evaluate("() => document.contentType || ''").lower()
        except Exception:
            pass

        is_pdf = (
            content_type == "application/pdf"
            or current_url.lower().endswith(".pdf")
        )
        if not is_pdf:
            return None

        print(f"  [*] PDF rendered in browser - downloading directly...")
        resp = page.context.request.get(current_url, timeout=300000)
        if resp.status != 200:
            return None

        body = resp.body()
        if not body or len(body) < 1024 or body[:4] != b'%PDF':
            return None

        md5_match = re.search(r'/md5/([0-9a-fA-F]{32})', md5_url)
        md5_tag = md5_match.group(1)[:8] if md5_match else "unknown"

        original_name = f"{md5_tag}.pdf"
        cd = resp.headers.get("content-disposition") or ""
        m = re.search(r'filename="?([^";]+)"?', cd)
        if m:
            original_name = unquote(m.group(1))
        elif current_url.split('/')[-1].endswith('.pdf'):
            original_name = unquote(current_url.split('/')[-1].split('?')[0])

        base_file_name = generate_custom_filename(
            clean_downloaded_title(original_name, md5_url), md5_url, NAME_FORMAT
        )
        file_name = get_unique_filename(DOWNLOAD_DIR, base_file_name)
        file_path = os.path.join(DOWNLOAD_DIR, file_name)

        with open(file_path, "wb") as f:
            f.write(body)

        print(f"  [+] Saved inline PDF: {file_name} ({len(body):,} bytes)")
        return original_name, file_name

    except Exception as e:
        print(f"  [!] Inline PDF save failed: {truncate_error(e)}")
        return None

def find_valid_link(page, text_match=None, href_match=None):
    selectors = []
    if text_match:
        selectors.append(f"a:has-text('{text_match}')")
    if href_match:
        selectors.append(f"a[href*='{href_match}']")
    if not selectors:
        return None
    combined_selector = ", ".join(selectors)
    try:
        for el in page.locator(combined_selector).all():
            h = el.get_attribute("href")
            if h and h not in ["#", "", "javascript:void(0)", "javascript:"]:
                return el
    except Exception:
        pass
    return None

def collect_download_mirrors(page, book_url):
    mirrors = []
    seen_hrefs = set()
    
    def absolutize(href):
        if href.startswith("/"):
            parsed = urlparse(page.url)
            return f"{parsed.scheme}://{parsed.netloc}{href}"
        return href
    
    try:
        fast_servers = []
        for i, el in enumerate(page.locator("a:has-text('Fast Partner Server')").all()):
            href = el.get_attribute("href")
            if href and href not in ["#", "", "javascript:void(0)", "javascript:"]:
                href = absolutize(href)
                if href not in seen_hrefs:
                    seen_hrefs.add(href)
                    label = el.inner_text().strip() or f"Fast Partner Server #{i+1}"
                    label = label.split('\n')[0].strip()
                    fast_servers.append((label, href, "fast_page", i+1))
        
        for label, href, mirror_type, pos_idx in fast_servers[1:]:
            mirrors.append((label, href, "fast_page"))
    except Exception:
        pass

    try:
        for i, el in enumerate(page.locator("a:has-text('Slow Partner Server')").all()):
            href = el.get_attribute("href")
            if href and href not in ["#", "", "javascript:void(0)", "javascript:"]:
                href = absolutize(href)
                if href not in seen_hrefs:
                    seen_hrefs.add(href)
                    label = el.inner_text().strip() or f"Slow Partner Server #{i+1}"
                    label = label.split('\n')[0].strip()
                    mirrors.append((label, href, "slow"))
    except Exception:
        pass
    
    # Extract Libgen URLs and save for manual processing
    has_libgen = False
    try:
        libgen_locator = find_valid_link(page, text_match='Libgen.li', href_match='libgen.li')
        if libgen_locator:
            href = libgen_locator.get_attribute("href")
            if href and href not in ["#", "", "javascript:void(0)", "javascript:"]:
                href = absolutize(href)
                if href not in seen_hrefs:
                    seen_hrefs.add(href)
                    libgen_urls_found.append(f"{book_url} ||| {href}")
                    has_libgen = True
                    print(f"  [+] Libgen URL saved for manual download")
    except Exception:
        pass

    # Exclude Libgen from active mirrors (they're saved separately now)
    mirrors = [m for m in mirrors if m[2] != "libgen"]

    return mirrors, has_libgen

def find_ipfs_cids(page):
    cids = []
    cid_pattern = r'(Qm[1-9A-HJ-NP-Za-km-z]{44}|baf[a-zA-Z0-9]{20,})'
    try:
        for el in page.locator("a[href*='/ipfs/']").all():
            href = el.get_attribute("href") or ""
            for m in re.finditer(cid_pattern, href):
                if m.group(1) not in cids:
                    cids.append(m.group(1))
    except Exception:
        pass
    try:
        body = page.inner_text("body")
        for m in re.finditer(r'ipfs://' + cid_pattern, body):
            if m.group(1) not in cids:
                cids.append(m.group(1))
    except Exception:
        pass
    return cids

def guess_extension_from_bytes(body):
    if not body or len(body) < 8:
        return ".bin"
    if body[:4] == b'%PDF':
        return ".pdf"
    if len(body) > 68 and body[60:68] == b'BOOKMOBI':
        return ".mobi"
    if body[:2] == b'PK':
        return ".epub"
    if body[:4] == b'DjVm':
        return ".djvu"
    return ".bin"

def download_via_ipfs(page, cids, book_url, source_label):
    t0 = time.time()
    for cid in cids:
        for gateway in IPFS_GATEWAYS:
            gw_url = gateway + cid
            try:
                resp = page.context.request.get(gw_url, timeout=IPFS_HTTP_TIMEOUT_MS)
                if resp.status != 200:
                    continue
                body = resp.body()
                if not body or len(body) < 1024:
                    continue
                cd = resp.headers.get("content-disposition") or ""
                m = re.search(r'filename="?([^";]+)"?', cd)
                if m:
                    original_name = unquote(m.group(1))
                else:
                    original_name = cid + guess_extension_from_bytes(body)
                if '.' not in original_name:
                    original_name += guess_extension_from_bytes(body)
                base_file_name = generate_custom_filename(original_name, book_url, NAME_FORMAT)
                file_name = get_unique_filename(DOWNLOAD_DIR, base_file_name)
                file_path = os.path.join(DOWNLOAD_DIR, file_name)
                with open(file_path, "wb") as f:
                    f.write(body)
                elapsed = format_elapsed_time(time.time() - t0)
                print(f"  [+] [{datetime.now().strftime('%H:%M:%S')}] Saved via IPFS: {file_name}")
                print(f"  [+] Elapsed: {elapsed}\n")
                return original_name, file_name
            except PlaywrightTimeoutError:
                print(f"  [!] IPFS gateway timeout: {gateway}")
                continue
            except Exception:
                continue
    raise Exception("ALL_IPFS_GATEWAYS_FAILED")

def load_fast_history():
    try:
        if not os.path.exists(FAST_HISTORY_FILE):
            return []
        with open(FAST_HISTORY_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
            if isinstance(data, dict) and "timestamps" in data:
                return data["timestamps"]
            elif isinstance(data, list):
                return data
            return []
    except Exception as e:
        print(f"[!] Warning: Error loading fast history: {e}")
        return []

def prune_fast_history(history):
    cutoff = time.time() - FAST_WINDOW_HOURS * 3600
    return [t for t in history if t > cutoff]

def fast_remaining():
    pruned = prune_fast_history(load_fast_history())
    remaining = max(0, FAST_LIMIT - len(pruned))
    return remaining

def record_fast_download():
    try:
        hist = prune_fast_history(load_fast_history())
        hist.append(time.time())
        history_data = [{"epoch": t, "local_time": datetime.fromtimestamp(t).strftime('%Y-%m-%d %H:%M:%S')} for t in hist]
        with open(FAST_HISTORY_FILE, "w", encoding="utf-8") as f:
            json.dump({"history": history_data, "timestamps": hist}, f, indent=4)
        print(f"  [+] Recorded fast download ({len(hist)}/{FAST_LIMIT} in window)")
    except Exception as e:
        print(f"[!] Warning: Could not record fast download: {e}")

def exhaust_fast_quota():
    print(f"\n[!] 🛑 FAST QUOTA EXHAUSTION DETECTED!")
    print(f"    Syncing local counter to 0.")
    print(f"    The {FAST_WINDOW_HOURS}-hour cooldown starts NOW: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
    try:
        hist = [time.time()] * FAST_LIMIT
        history_data = [{"epoch": t, "local_time": datetime.fromtimestamp(t).strftime('%Y-%m-%d %H:%M:%S')} for t in hist]
        with open(FAST_HISTORY_FILE, "w", encoding="utf-8") as f:
            json.dump({"history": history_data, "timestamps": hist}, f, indent=4)
    except Exception as e:
        print(f"[!] Warning: Could not save quota exhaustion: {e}")

def print_quota_diagnostics():
    print("=" * 70)
    print("🕒 FAST QUOTA DIAGNOSTICS")
    print("=" * 70)
    hist = load_fast_history()
    current_time = time.time()
    current_dt = datetime.now()
    print(f"Current Time:         {current_dt.strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"Configured LIMIT:     {FAST_LIMIT}")
    print(f"Cooldown Window:      {FAST_WINDOW_HOURS} hours")
    if not hist:
        print(f"Status:               No downloads tracked (Fresh)")
        print(f"Available Quota:      {FAST_LIMIT} / {FAST_LIMIT}")
        print("=" * 70 + "\n")
        return
    cutoff = current_time - (FAST_WINDOW_HOURS * 3600)
    active = [t for t in hist if t > cutoff]
    if not active:
        print(f"Status:               All downloads past cooldown")
        print(f"Available Quota:      {FAST_LIMIT} / {FAST_LIMIT}")
    else:
        oldest_active = min(active)
        newest_active = max(active)
        time_until_next = (oldest_active + FAST_WINDOW_HOURS * 3600) - current_time
        print(f"Downloads Tracked:    {len(hist)} total ({len(active)} active)")
        print(f"Oldest Active:        {datetime.fromtimestamp(oldest_active).strftime('%Y-%m-%d %H:%M:%S')}")
        print(f"Newest Active:        {datetime.fromtimestamp(newest_active).strftime('%Y-%m-%d %H:%M:%S')}")
        print(f"Available Quota:      {max(0, FAST_LIMIT - len(active))} / {FAST_LIMIT}")
        print(f"Next Slot Opens:      {format_elapsed_time(time_until_next)}")
    print("=" * 70 + "\n")

def extract_real_quota_from_account_page(page):
    try:
        body_text = page.inner_text("body")
        match = re.search(r'Fast downloads used \(last 18 hours\):\s*(\d+)\s*/\s*(\d+)', body_text)
        if match:
            used = int(match.group(1))
            limit = int(match.group(2))
            print(f"  ✓ AA QUOTA: {used} / {limit} used ({limit - used} remaining)")
            return used, limit
        print(f"  ✗ Could NOT find quota text")
        return None, None
    except Exception as e:
        print(f"  ✗ Error reading quota: {e}")
        return None, None

def auto_login_to_annas_archive(page, secret_key, download_dir):
    ACCOUNTS_URL = "https://annas-archive.gl/account"
    print("\n" + "=" * 70)
    print("🔐 AUTOMATIC LOGIN SEQUENCE")
    print("=" * 70)
    
    def sync_quota(used, limit):
        if used is not None and limit is not None:
            old_hist = load_fast_history()
            if len(old_hist) != used:
                print(f"  [*] Syncing tracker from {len(old_hist)} to {used} entries")
                if used > len(old_hist):
                    diff = used - len(old_hist)
                    sorted_hist = old_hist + [time.time()] * diff
                else:
                    sorted_hist = sorted(old_hist, reverse=True)[:used]
                    
                history_data = [{"epoch": t, "local_time": datetime.fromtimestamp(t).strftime('%Y-%m-%d %H:%M:%S')} for t in sorted_hist]
                with open(FAST_HISTORY_FILE, "w", encoding="utf-8") as f:
                    json.dump({"history": history_data, "timestamps": sorted_hist}, f, indent=4)

    print(f"[*] Navigating to {ACCOUNTS_URL}...")
    page.goto(ACCOUNTS_URL, wait_until="domcontentloaded", timeout=60000)
    try:
        body_text = page.inner_text("body").lower()
        if "membership:" in body_text or "account id:" in body_text or "member since" in body_text:
            print("[✓] Already logged in! Reading quota...")
            used, limit = extract_real_quota_from_account_page(page)
            sync_quota(used, limit)
            return True, (used if used is not None else 0, limit)
    except Exception as e:
        print(f"[!] Error checking login status: {e}")
        pass
        
    print("[*] Searching for secret key input...")
    try:
        secret_input = page.wait_for_selector("input[name='key'], input[type='password']", timeout=10000)
        if not secret_input:
            print("[!] Could not find input field!")
            return False, (None, None)
        secret_input.fill(secret_key)
        login_btn = page.wait_for_selector("button:has-text('Log in')", timeout=10000)
        login_btn.click()
        
        page.wait_for_load_state("networkidle", timeout=15000)
        page.wait_for_timeout(2000)
        
        body_text = page.inner_text("body").lower()
        account_indicators = ["/account" in page.url.lower(), "account id:" in body_text, "membership:" in body_text]
        
        if any(account_indicators):
            print("[✓] Login successful!")
            used, limit = extract_real_quota_from_account_page(page)
            sync_quota(used, limit)
            return True, (used if used is not None else 0, limit)
        else:
            print(f"[!] Login failed. URL: {page.url}")
            return False, (None, None)
    except PlaywrightTimeoutError:
        print("[!] Timeout waiting for login elements!")
        return False, (None, None)
    except Exception as e:
        print(f"[!] Login error: {e}")
        return False, (None, None)

def get_fast_download_urls(page, book_url):
    global AA_SECRET
    if not AA_SECRET:
        return []
    try:
        parsed = urlparse(page.url)
        md5_hash = book_url.rstrip('/').split('/')[-1]
        if '?' in md5_hash:
            md5_hash = md5_hash.split('?')[0]
        if not re.fullmatch(r'[0-9a-fA-F]{32}', md5_hash):
            print(f"  [!] Invalid MD5: {md5_hash}")
            return []
        safe_md5 = quote(md5_hash, safe='')
        api_url = f"{parsed.scheme}://{parsed.netloc}/dyn/api/fast_download.json?key={AA_SECRET}&md5={safe_md5}"
        raw = page.evaluate("""async (url) => {
            const r = await fetch(url, { method: 'GET', credentials: 'include' });
            return await r.text();
        }""", api_url)
        data = json.loads(raw)
        if not isinstance(data, dict) or data.get("error"):
            err_msg = str(data.get("error", "")).lower()
            if "run out" in err_msg or "quota" in err_msg or "exhausted" in err_msg:
                exhaust_fast_quota()
            return []
        raw_urls = []
        fallback_url = data.get("fallback_url")
        if fallback_url and isinstance(fallback_url, str) and fallback_url.startswith("http"):
            raw_urls.append(fallback_url)
        for k, v in data.items():
            if isinstance(v, str) and v.startswith("http") and v not in raw_urls:
                raw_urls.append(v)
        return raw_urls
    except Exception as e:
        print(f"  [!] API Error: {e}")
        return []

_explicitly_handled_downloads = set()

def _save_playwright_download(download, md5_url):
    _explicitly_handled_downloads.add(id(download))
    try:
        suggested = download.suggested_filename
        download.save_as(os.path.join(DOWNLOAD_DIR, suggested))
        fp = os.path.join(DOWNLOAD_DIR, suggested)
        size = os.path.getsize(fp) if os.path.exists(fp) else 0
        if size < 1024:
            return None
        clean_title = clean_downloaded_title(suggested, md5_url)
        base_file_name = generate_custom_filename(clean_title, md5_url, NAME_FORMAT)
        final_name = get_unique_filename(DOWNLOAD_DIR, base_file_name)
        final_path = os.path.join(DOWNLOAD_DIR, final_name)
        if fp != final_path:
            shutil.move(fp, final_path)
        print(f"  [+] Download COMPLETE! {final_name} ({size:,} bytes)")
        return suggested, final_name
    except Exception as e:
        print(f"  [!] save_as failed: {truncate_error(e)}")
        return None

def _setup_download_handler(page, md5_url):
    captured = []
    def on_download(download):
        try:
            suggested = download.suggested_filename
            print(f"  [*] Download event caught: {suggested}")
            download.save_as(os.path.join(DOWNLOAD_DIR, suggested))
            captured.append(suggested)
        except Exception as e:
            print(f"  [!] Background download handler error: {truncate_error(e)}")
    page.on("download", on_download)
    return captured

def trigger_download_and_save(page, trigger_action, md5_url, source_label, timeout_minutes=25, max_cancel_attempts=3):
    download_cancel_count = 0
    scan_start_time = time.time()

    try:
        t_start = datetime.now().strftime("%H:%M:%S")
        print(f"  [*] [{t_start}] Triggering {source_label} download...")

        # --- METHOD 1: Playwright expect_download() ---
        try:
            with page.expect_download(timeout=30000) as download_info:
                trigger_action()
            download = download_info.value
            print(f"  [*] Playwright intercepted download: {download.suggested_filename}")
            result = _save_playwright_download(download, md5_url)
            if result:
                elapsed = format_elapsed_time(time.time() - scan_start_time)
                print(f"  [+] [{datetime.now().strftime('%H:%M:%S')}] Elapsed: {elapsed}\n")
                return result
        except Exception as e:
            err_str = str(e).lower()
            if "timeout" in err_str or "waiting" in err_str:
                print(f"  [*] No Playwright download event in 30s, trying fallbacks...")
            else:
                print(f"  [!] expect_download error: {truncate_error(e)}")

        # --- METHOD 2: Inline PDF detection ---
        page.wait_for_timeout(2000)
        pdf_result = save_inline_pdf(page, md5_url)
        if pdf_result:
            return pdf_result

        # --- Check for mirror errors before long poll ---
        try:
            title_lower = page.title().lower()
            body_lower = ""
            try:
                body_lower = page.inner_text("body").lower()[:2000]
            except Exception:
                pass
            mirror_404_signals = [
                "404", "not found", "file not found",
                "does not exist", "no longer available",
                "has been removed", "unavailable",
            ]
            for sig in mirror_404_signals:
                if sig in title_lower or (body_lower and sig in body_lower and len(body_lower) < 1500):
                    raise Exception("MIRROR_404")
        except Exception as check_err:
            if "MIRROR_404" in str(check_err):
                raise

        # --- METHOD 3: Disk polling fallback ---
        print(f"  [*] Falling back to disk polling ({timeout_minutes} min max)...")
        existing_files = set(os.listdir(DOWNLOAD_DIR))

        while time.time() - scan_start_time < timeout_minutes * 60:
            current_files = set(os.listdir(DOWNLOAD_DIR))
            new_files = current_files - existing_files

            for f in new_files:
                fp = os.path.join(DOWNLOAD_DIR, f)
                if not os.path.isfile(fp):
                    continue
                if f.endswith('.part') or f.endswith('.tmp') or f.endswith('.crdownload'):
                    continue
                try:
                    size = os.path.getsize(fp)
                    if size < 1024:
                        continue
                    with open(fp, 'rb') as vf:
                        header = vf.read(8)
                        valid = (
                            header[:4] == b'%PDF'
                            or header[:4] == b'PK\x03\x04'
                            or header[:4] == b'AT&T'
                        )
                    if not valid:
                        continue
                    time.sleep(3)
                    new_size = os.path.getsize(fp)
                    if new_size != size:
                        print(f"  [*] File still growing: {size} -> {new_size} bytes, waiting...")
                        existing_files.add(f)
                        break
                    print(f"  [+] Download COMPLETE! Found: {f} ({new_size:,} bytes)")
                    clean_title = clean_downloaded_title(f, md5_url)
                    base_file_name = generate_custom_filename(clean_title, md5_url, NAME_FORMAT)
                    final_name = get_unique_filename(DOWNLOAD_DIR, base_file_name)
                    final_path = os.path.join(DOWNLOAD_DIR, final_name)
                    if fp != final_path:
                        shutil.move(fp, final_path)
                    elapsed = format_elapsed_time(time.time() - scan_start_time)
                    print(f"  [+] [{datetime.now().strftime('%H:%M:%S')}] Saved: {final_name}")
                    print(f"  [+] Elapsed: {elapsed}\n")
                    return f, final_name
                except Exception:
                    continue

            pdf_result = save_inline_pdf(page, md5_url)
            if pdf_result:
                return pdf_result

            if download_cancel_count >= max_cancel_attempts:
                raise Exception(f"CANCELLED_{download_cancel_count}_TIMES")

            try:
                title_lower = page.title().lower()
                body_lower = ""
                try:
                    body_lower = page.inner_text("body").lower()[:2000]
                except Exception:
                    pass
                for sig in mirror_404_signals:
                    if sig in title_lower or (body_lower and sig in body_lower and len(body_lower) < 1500):
                        raise Exception("MIRROR_404")
                if 'canceled' in title_lower or 'failed' in title_lower:
                    download_cancel_count += 1
                    print(f"  [!] Download cancelled ({download_cancel_count}/{max_cancel_attempts}), retrying...")
                    page.goto("about:blank", timeout=5000)
                    page.wait_for_timeout(3000)
                    trigger_action()
                    continue
            except Exception as page_check_err:
                if "MIRROR_404" in str(page_check_err):
                    raise

            page.wait_for_timeout(5000)

        raise Exception(f"{source_label} TIMEOUT ({timeout_minutes} mins)")

    except Exception as e:
        if "CANCELLED_" in str(e) or "TIMEOUT" in str(e) or "MIRROR_404" in str(e):
            raise e
        raise Exception(f"{source_label} download failed: {truncate_error(e)}")

def load_current_links():
    if os.path.exists(TXT_FILE):
        try:
            with open(TXT_FILE, "r", encoding="utf-8") as f:
                return [line.strip() for line in f if line.strip() and line.strip().startswith("http")]
        except Exception:
            pass
    return []

def rewrite_aa_links(active_urls):
    try:
        with open(TXT_FILE, "w", encoding="utf-8") as f:
            for url in active_urls:
                f.write(f"{url}\n")
    except Exception as e:
        print(f"[!] Error rewriting links file: {e}")

def create_archived_file(name, urls):
    try:
        filepath = os.path.join(DOWNLOAD_DIR, name)
        with open(filepath, "w", encoding="utf-8") as f:
            for url in urls:
                f.write(f"{url}\n")
    except Exception as e:
        print(f"[!] Error creating archive: {e}")

def try_mirror_download(page, book_url, mirror_label, mirror_href, mirror_type, should_skip_libgen):
    if mirror_type == "libgen":
        print(f"  [-] Libgen download disabled - URL already saved to manual queue")
        return False, False, None, None, "LIBGEN_DISABLED"
    try:
        print(f"  [*] [{datetime.now().strftime('%H:%M:%S')}] Mirror: {mirror_label}")
        response = page.goto(mirror_href, wait_until="domcontentloaded", timeout=60000)
        
        if check_ddos_block(page):
            resolved = wait_for_ddos_to_resolve(page, DDOS_WAIT_FOR_RESOLUTION_SECONDS, DDOS_POLL_INTERVAL_SECONDS)
            if not resolved:
                print(f"  [-] Mirror {mirror_label} still blocked after DDoS wait - skipping to next")
                return False, False, None, None, f"DDoS_MIRROR_SKIP:{mirror_label}"
            print(f"  [+] DDoS resolved on mirror - continuing with {mirror_label}")
        
        if mirror_type == "libgen" and detect_libgen_connection_limit(page):
            return False, False, None, None, "LIBGEN_CONNECTION_LIMIT"
        if check_out_of_fast_downloads(page):
            print(f"  [!] OUT OF FAST DOWNLOADS - switching to slow")
            return False, False, None, None, "OUT_OF_FAST_DOWNLOADS_PAGE"
        if response and response.status == 404:
            return False, False, None, None, "MIRROR_404"
        try:
            check_for_page_error(page, response)
        except Exception as check_err:
            return False, True, None, None, str(check_err)
        if check_book_not_found(page, response):
            return False, False, None, None, "MIRROR_404"
        if mirror_type == "slow":
            print(f"  [*] Waiting for download option (up to 3 mins)...")
            start_wait = time.time()
            download_btn = None
            extracted_url = None
            while time.time() - start_wait < 180:
                check_for_page_error(page, response)
                try:
                    for el in page.locator("a:has-text('Download now')").all():
                        h = el.get_attribute("href")
                        if h and h not in ["#", "", "javascript:void(0)", "javascript:"]:
                            download_btn = el
                            break
                except Exception:
                    pass
                if download_btn:
                    break
                try:
                    body_text = page.inner_text("body")
                    if "copy this URL" in body_text or "URL bar" in body_text:
                        match = re.search(r'URL bar:[\s\n]*(https?://[^\s"\'<>()]+)', body_text, re.IGNORECASE)
                        if not match:
                            match = re.search(r'(?:copy this URL|URL bar)[\s\S]{1,150}?(https?://[^\s"\'<>()]+)', body_text, re.IGNORECASE)
                        if match:
                            extracted_url = match.group(1)
                            break
                        for el in page.locator("input[type='text'], input:not([type])").all():
                            val = el.input_value()
                            if val and val.startswith("http"):
                                extracted_url = val
                                break
                        if extracted_url:
                            break
                except Exception:
                    pass
                page.wait_for_timeout(2000)
            if download_btn:
                print(f"  [*] Found 'Download now' - clicking...")
                original_name, file_name = trigger_download_and_save(
                    page, lambda: download_btn.click(force=True), book_url, mirror_label
                )
            elif extracted_url:
                print(f"  [*] Extracted URL: {extracted_url[:60]}...")
                original_name, file_name = trigger_download_and_save(
                    page, lambda: trigger_url_download(page, extracted_url, True), book_url, mirror_label
                )
            else:
                return False, True, None, None, "No download option in 3 mins"
        elif mirror_type == "libgen":
            libgen_cancel_count = 0
            max_libgen_retries = 3
            
            while libgen_cancel_count < max_libgen_retries:
                try:
                    print(f"  [*] LIBGEN MODE - waiting up to {LIBGEN_DOWNLOAD_TIMEOUT_MINUTES} mins per attempt (max {max_libgen_retries} retries)...")
                    try:
                        mirror_btn = wait_for_element_or_error(page, "a[href*='ads.php']", timeout_seconds=60, response=response)
                        with page.expect_navigation(wait_until="domcontentloaded", timeout=60000):
                            mirror_btn.click()
                    except PlaywrightTimeoutError:
                        return False, True, None, None, "Libgen button not found"
                    
                    try:
                        if detect_libgen_connection_limit(page):
                            return False, False, None, None, "LIBGEN_CONNECTION_LIMIT"
                    except Exception:
                        pass
                    
                    print(f"  [*] Waiting for GET button...")
                    try:
                        get_btn = wait_for_element_or_error(page, "a:has-text('GET'), a[href*='get.php']", timeout_seconds=60, response=response)
                    except PlaywrightTimeoutError:
                        return False, True, None, None, "Libgen GET not found"
                    
                    original_name, file_name = trigger_download_and_save(
                        page, lambda: get_btn.click(force=True), book_url, mirror_label, 
                        LIBGEN_DOWNLOAD_TIMEOUT_MINUTES, max_cancel_attempts=max_libgen_retries
                    )
                    
                    return True, False, original_name, file_name, None
                    
                except Exception as e:
                    error_msg = str(e)
                    if "CANCELLED_" in error_msg or "no_download_detected" in error_msg.lower() or "timeout" in error_msg.lower():
                        libgen_cancel_count += 1
                        print(f"  [!] Libgen attempt {libgen_cancel_count}/{max_libgen_retries} failed, retrying...")
                        page.goto("about:blank", timeout=5000)
                        page.wait_for_timeout(5000)
                        continue
                    else:
                        return False, detect_connection_error(page, error_msg, None), None, None, error_msg
            
            return False, True, None, None, f"LIBGEN_MAX_RETRIES_EXCEEDED ({libgen_cancel_count})"
        return True, False, original_name, file_name, None
    except Exception as e:
        return False, detect_connection_error(page, str(e), None), None, None, str(e)

# ==========================
# MAIN EXECUTION
# ==========================
def main():
    
    global book_counter, last_health_check, current_targets, current_idx
    global shutdown_requested, AA_SECRET, last_libgen_3306_time, processed_in_this_session
    
    AA_SECRET = load_aa_secret()
    
    if RESET_QUOTA:
        if os.path.exists(FAST_HISTORY_FILE):
            try:
                os.remove(FAST_HISTORY_FILE)
                print("\n[✓] --reset-quota: Cleared local fast download log.")
            except Exception as e:
                print(f"[!] Warning: Could not clear quota log: {e}")

    completed_urls = load_completed()
    failed_items = load_failed_log()
    progress_cache = load_progress_cache()
    duplicate_urls = load_duplicates()
    not_turkish_cached = load_not_turkish_cache()

    session_success = 0
    session_failed = 0
    session_skipped = 0
    session_not_turkish = 0
    session_fast_used = 0
    session_duplicates = 0
    session_requeued = 0
    browser_restart_count = 0
    idx = 0
    targets = []

    print("\n" + "=" * 70)
    print("~ Welcome to hearth v3.3-final-SECURED-v4-LIBGEN-SPLIT ~")
    print(f"  Features: Persistent Turkish cache, Non-blocking Libgen 3306, IPFS support")
    print(f"  NEW: Libgen URLs extracted to separate file for manual processing")
    print(f"  NEW: Failed downloads re-queued at RANDOM POSITIONS (max {MAX_FAIL_RETRIES_PER_BOOK} retries)")
    print(f"  UPDATED: Libgen download timeout = {LIBGEN_DOWNLOAD_TIMEOUT_MINUTES} minutes")
    print(f"  ENHANCED: Libgen 3306 detection with multi-indicator matching")
    print(f"  FIXED: Browser launch error protection & stealth module isolation")
    print(f"  FIXED: downloads_path removed for Playwright API compatibility")
    print(f"  FIXED: Native Playwright download API (no chrome://downloads/ scraping)")
    print(f"  FIXED: File size polling to detect completion accurately")
    print(f"  FIXED: 3-cancel retry rule for interrupted downloads")
    print(f"  FIXED: Recovery logic with strict MD5 matching")
    print(f"  FIXED: Windows Downloads folder fallback")
    print("=" * 70)
    
    if AA_SECRET:
        print(f"Member API key loaded: FAST API ENABLED")
    else:
        print("[!] No AA_SECRET - running in FREE mode.")
        
    print_quota_diagnostics()
    print("=" * 70 + "\n")
    
    print("Booting browser...")

    browser = None
    page = None
    context = None
    
    try:
        with sync_playwright() as p:
            print("[*] Step 1/5: Initializing Playwright...")
            
            print("[*] Step 2/5: Launching Chromium browser...")
            try:
                browser = p.chromium.launch(headless=False, args=["--disable-blink-features=AutomationControlled"])
                print("[✓] Browser launched successfully!")
            except Exception as launch_err:
                print(f"\n[ERROR] Browser launch failed: {launch_err}")
                print("[!] Try: python -m playwright install chromium")
                print("[!] Try: python -m playwright install-deps chromium")
                sys.exit(1)
            
            print("[*] Step 3/5: Creating browser context...")
            try:
                context = browser.new_context(accept_downloads=True, locale="en-US")
                print("[✓] Context created successfully!")
            except Exception as ctx_err:
                print(f"[ERROR] Context creation failed: {ctx_err}")
                if browser:
                    browser.close()
                sys.exit(1)
            
            if STEALTH_AVAILABLE and stealth_apply_func:
                print("[*] Step 4/5: Applying stealth module...")
                try:
                    stealth_apply_func(context)
                    print("[✓] Stealth applied successfully!")
                except Exception as stealth_err:
                    print(f"[!] Warning: Stealth failed but continuing: {stealth_err}")
            else:
                print("[*] Step 4/5: Skipping stealth (not available)")
            
            print("[*] Step 5/5: Opening new page...")
            page = context.new_page()
            def _bg_download_handler(download):
                if id(download) in _explicitly_handled_downloads:
                    return
                try:
                    name = download.suggested_filename
                    dest = os.path.join(DOWNLOAD_DIR, name)
                    download.save_as(dest)
                    print(f"  [*] Background download saved: {name}")
                except Exception:
                    pass
            page.on("download", _bg_download_handler)
            print("[✓] Page opened successfully!")
            print("")

            if AA_SECRET:
                print("[*] 🔥 Starting login sequence...")
                login_success, quota_result = auto_login_to_annas_archive(page, AA_SECRET, DOWNLOAD_DIR)
                if quota_result and quota_result[1]:
                    used, limit = quota_result
                    print(f"\n{'='*70}")
                    print(f"✅ QUOTA SYNC: {used}/{limit} used ({limit-used} remaining)")
                    print(f"{'='*70}\n")
            
            if TXT_MODE:
                current_links = load_current_links()
                if not current_links:
                    print("No valid links in aa_links.txt!")
                    return
                targets = [{"url": link, "title": "Unknown"} for link in current_links]
            elif RETRY_MODE:
                retry_urls = load_libgen_retry_queue()
                current_links = load_current_links()
                all_retry = retry_urls + current_links
                if not all_retry:
                    print("No failed items to retry!")
                    return
                targets = [{"url": link, "title": "Unknown"} for link in all_retry]
            else:
                response = page.goto(LIST_URL, wait_until="domcontentloaded", timeout=60000)
                try:
                    wait_for_element_or_error(page, "main", timeout_seconds=120, response=response)
                    page.wait_for_timeout(2000)
                except PlaywrightTimeoutError:
                    pass
                hrefs = page.eval_on_selector_all("main a[href*='/md5/']", "els => els.map(e => e.href)")
                targets = [{"url": link, "title": "Unknown"} for link in set(hrefs)]
                if len(targets) == 0:
                    print("No accessible links found!")
                    return

            libgen_queued_urls, libgen_queued_md5s = libgen_queue_book_urls()
            already_processed = completed_urls.union(not_turkish_cached).union(libgen_queued_urls)

            def _is_libgen_queued_by_md5(target_url):
                if not libgen_queued_md5s:
                    return False
                m = re.search(r'/md5/([0-9a-fA-F]{32})', target_url)
                return m and m.group(1).lower() in libgen_queued_md5s

            targets = [t for t in targets if t["url"] not in already_processed and not _is_libgen_queued_by_md5(t["url"])]

            print(f"\n[INFO] Pre-filtered targets:")
            print(f"  Already downloaded:      {len(completed_urls)}")
            print(f"  Not Turkish (cached):    {len(not_turkish_cached)}")
            print(f"  Libgen manual queue:     {len(libgen_queued_urls)} URLs + {len(libgen_queued_md5s)} MD5s")
            print(f"  Remaining to process:    {len(targets)}")
            print("=" * 70 + "\n")

            current_targets = targets

            for idx, item in enumerate(targets, 1):
                if shutdown_requested:
                    print("\n\n[⚠️] Shutdown requested - saving progress...")
                    save_libgen_queue()  # Save Libgen URLs on shutdown
                    save_state_snapshot(idx, targets, completed_urls, failed_items)
                    break

                url = item["url"]
                current_idx = idx

                if url in processed_in_this_session:
                    print(f"[{idx}/{len(targets)}] Skipping (already processed this session): {url[:50]}...")
                    session_skipped += 1
                    continue

                if last_health_check is None or (time.time() - last_health_check > HEALTH_CHECK_INTERVAL_SECONDS):
                    if not health_check_browser(page):
                        print(f"\n[!] Browser health check FAILED - restarting...")
                        try:
                            if browser:
                                browser.close()
                        except Exception:
                            pass
                        browser = p.chromium.launch(headless=False, args=["--disable-blink-features=AutomationControlled"])
                        context = browser.new_context(accept_downloads=True, locale="en-US")
                        if STEALTH_AVAILABLE and stealth_apply_func:
                            try:
                                stealth_apply_func(context)
                            except Exception:
                                pass
                        page = context.new_page()
                        page.on("download", _bg_download_handler)
                        if AA_SECRET:
                            auto_login_to_annas_archive(page, AA_SECRET, DOWNLOAD_DIR)
                        browser_restart_count += 1
                        if browser_restart_count > MAX_BROWSER_RESTARTS:
                            print(f"[!] Max restarts ({MAX_BROWSER_RESTARTS}) reached - exiting")
                            break
                    last_health_check = time.time()

                if url in completed_urls:
                    print(f"[{idx}/{len(targets)}] Skipping completed: {url[:50]}...")
                    session_skipped += 1
                    processed_in_this_session.add(url)
                    continue

                book_counter += 1
                print(f"\n[{idx}/{len(targets)}] [{datetime.now().strftime('%H:%M:%S')}] Book #{book_counter}: {url[:60]}...")
                
                failure_reason = None
                item_downloaded = False
                hit_out_of_fast_page = False
                
                should_skip_libgen = False
                if last_libgen_3306_time:
                    elapsed_since_3306 = time.time() - last_libgen_3306_time
                    if elapsed_since_3306 < LIBGEN_SKIP_WINDOW_MINUTES * 60:
                        should_skip_libgen = False
                if last_libgen_3306_time:
                    elapsed_since_3306 = time.time() - last_libgen_3306_time
                    if elapsed_since_3306 < LIBGEN_SKIP_WINDOW_MINUTES * 60:
                        should_skip_libgen = True
                        print(f"  [*] Libgen 3306 cooldown active ({format_elapsed_time(elapsed_since_3306)} elapsed)")

                try:
                    title_lower = page.title().lower()
                    if "504" in title_lower or "gateway" in title_lower or "error" in title_lower:
                        print(f"  [+] Closing error page from previous attempt...")
                        page.goto("about:blank", timeout=5000)
                except Exception:
                    pass
                
                try:
                    nav_response = page.goto(url, wait_until="domcontentloaded", timeout=NAVIGATION_TIMEOUT_SECONDS*1000)

                    if check_ddos_block(page):
                        resolved = wait_for_ddos_to_resolve(page, DDOS_WAIT_FOR_RESOLUTION_SECONDS, DDOS_POLL_INTERVAL_SECONDS)
                        if not resolved:
                            failure_reason = "DDoS_BOOK_NOT_RESOLVED"
                            session_skipped += 1
                            processed_in_this_session.add(url)
                            print(f"  [-] Book page blocked after DDoS wait - skipping")
                            continue
                        print(f"  [+] DDoS resolved - proceeding with book processing")

                    if check_book_not_found(page, nav_response):
                        print(f"  [-] 404 / NOT FOUND - book does not exist, skipping permanently")
                        processed_in_this_session.add(url)
                        session_skipped += 1
                        continue

                except Exception as nav_err:
                    print(f"  [!] Navigation error: {truncate_error(nav_err)}")
                    failure_reason = "NAVIGATION_ERROR"
                    processed_in_this_session.add(url)
                    continue

                if FILTER_TURKISH_ONLY:
                    try:
                        is_turkish, lang_code, reason = extract_language_from_metadata(page)
                        if is_turkish is False:
                            session_not_turkish += 1
                            print(f"  [-] Not Turkish ({lang_code}), skipping")
                            mark_not_turkish(url, lang_code)
                            processed_in_this_session.add(url)
                            continue
                        elif is_turkish is None:
                            print(f"  [-] Could not determine language, skipping to be safe")
                            session_not_turkish += 1
                            mark_not_turkish(url, "?")
                            processed_in_this_session.add(url)
                            continue
                    except Exception:
                        pass

                mirrors_to_try = []
                book_has_libgen = False
                try:
                    page_mirrors, book_has_libgen = collect_download_mirrors(page, url)
                    current_fast_remaining = fast_remaining()
                    print(f"  [*] Quota: {current_fast_remaining} / {FAST_LIMIT}")
                    if current_fast_remaining > 0:
                        print(f"  [*] ✅ Fast mirrors enabled ({current_fast_remaining} slots)")
                        for m in page_mirrors:
                            if m[2] == "fast_page":
                                mirrors_to_try.append(m)
                        if AA_SECRET:
                            mirrors_to_try.append(("API Fallback", None, "fast_api"))
                    else:
                        print(f"  [*] ❌ Fast mirrors skipped (tracker: {current_fast_remaining})")
                    # NOTE: Libgen mirrors are NO LONGER added here.
                    # collect_download_mirrors() extracts Libgen URLs into
                    # libgen_manual_queue.txt instead of returning them.
                    for m in page_mirrors:
                        if m[2] == "slow":
                            mirrors_to_try.append(m)
                except Exception as e:
                    print(f"  [!] Mirror collection error: {e}")
                
                try:
                    ipfs_cids = find_ipfs_cids(page)
                    if ipfs_cids:
                        mirrors_to_try.append(("IPFS Gateway", ipfs_cids, "ipfs"))
                        print(f"  [*] IPFS CIDs found: {len(ipfs_cids)}, queued as fallback")
                except Exception:
                    pass
                
                print(f"  [*] Queued mirrors: {[m[0][:30] for m in mirrors_to_try[:5]]}{'...' if len(mirrors_to_try) > 5 else ''}")

                for mirror_idx, (mirror_label, mirror_data, mirror_type) in enumerate(mirrors_to_try, 1):
                    print(f"  [Mirror {mirror_idx}/{len(mirrors_to_try)}] {mirror_label[:40]}")
                    
                    if page.url != url and not page.url.startswith("blob:"):
                        try:
                            page.goto(url, wait_until="domcontentloaded", timeout=60000)
                        except Exception:
                            pass
                    
                    if mirror_type == "fast_page":
                        if hit_out_of_fast_page:
                            print(f"  [-] Skipping fast (already hit out-of-fast)")
                            continue
                        try:
                            original_name, file_name = trigger_download_and_save(
                                page, lambda: trigger_url_download(page, mirror_data), url, mirror_label
                            )
                            file_path = os.path.join(DOWNLOAD_DIR, file_name)
                            if os.path.exists(file_path) and os.path.getsize(file_path) < 1024:
                                print(f"  [!] Small/duplicate file - logging")
                                mark_duplicate(url)
                                session_duplicates += 1
                                continue
                            record_fast_download()
                            session_fast_used += 1
                            clean_title = clean_downloaded_title(original_name, url)
                            mark_completed(url, clean_title)
                            completed_urls.add(url)
                            session_success += 1
                            processed_in_this_session.add(url)
                            item_downloaded = True
                            break
                        except Exception as e:
                            err_str = str(e)
                            if "FAST_QUOTA_EXHAUSTED" in err_str:
                                exhaust_fast_quota()
                                continue
                            if "OUT_OF_FAST_DOWNLOADS_PAGE" in err_str:
                                print(f"  [!] Out of fast - switching to slow")
                                hit_out_of_fast_page = True
                                continue
                            if "MIRROR_404" in err_str:
                                print(f"  [!] {mirror_label}: 404 / file not found on server")
                                failure_reason = "MIRROR_404"
                                continue
                            print(f"  [!] {mirror_label} failed: {truncate_error(e)}")
                            failure_reason = f"MIRROR_FAIL_{mirror_label}"

                    elif mirror_type == "slow":
                        success, conn_err, orig, fname, reason = try_mirror_download(page, url, mirror_label, mirror_data, mirror_type, should_skip_libgen)
                        if success:
                            file_path = os.path.join(DOWNLOAD_DIR, fname)
                            if os.path.exists(file_path) and os.path.getsize(file_path) < 1024:
                                print(f"  [!] Small/duplicate file - logging")
                                mark_duplicate(url)
                                session_duplicates += 1
                                continue
                            clean_title = clean_downloaded_title(orig, url)
                            mark_completed(url, clean_title)
                            completed_urls.add(url)
                            session_success += 1
                            processed_in_this_session.add(url)
                            item_downloaded = True
                            break
                        else:
                            if reason and "MIRROR_404" in reason:
                                print(f"  [!] {mirror_label}: 404 / file not found on server")
                                failure_reason = "MIRROR_404"
                            else:
                                failure_reason = f"MIRROR_FAIL_{mirror_label}_{reason}" if reason else f"MIRROR_FAIL_{mirror_label}"

                    elif mirror_type == "ipfs":
                        try:
                            cids = mirror_data
                            original_name, file_name = download_via_ipfs(page, cids, url, mirror_label)
                            file_path = os.path.join(DOWNLOAD_DIR, file_name)
                            if os.path.exists(file_path) and os.path.getsize(file_path) < 1024:
                                print(f"  [!] Small/duplicate file - logging")
                                mark_duplicate(url)
                                session_duplicates += 1
                                continue
                            clean_title = clean_downloaded_title(original_name, url)
                            mark_completed(url, clean_title)
                            completed_urls.add(url)
                            session_success += 1
                            processed_in_this_session.add(url)
                            item_downloaded = True
                            break
                        except Exception as e:
                            if "ALL_IPFS_GATEWAYS_FAILED" in str(e):
                                failure_reason = "ALL_IPFS_FAILED"
                            else:
                                failure_reason = f"IPFS_FAIL_{str(e)[:30]}"

                    elif mirror_type == "fast_api":
                        if AA_SECRET:
                            fast_urls = get_fast_download_urls(page, url)
                            for fast_url in fast_urls:
                                try:
                                    original_name, file_name = trigger_download_and_save(
                                        page, lambda u=fast_url: trigger_url_download(page, u), url, mirror_label
                                    )
                                    file_path = os.path.join(DOWNLOAD_DIR, file_name)
                                    if os.path.exists(file_path) and os.path.getsize(file_path) < 1024:
                                        print(f"  [!] Small/duplicate file - logging")
                                        mark_duplicate(url)
                                        session_duplicates += 1
                                        break
                                    record_fast_download()
                                    session_fast_used += 1
                                    clean_title = clean_downloaded_title(original_name, url)
                                    mark_completed(url, clean_title)
                                    completed_urls.add(url)
                                    session_success += 1
                                    processed_in_this_session.add(url)
                                    item_downloaded = True
                                    break
                                except Exception as api_e:
                                    if "FAST_QUOTA_EXHAUSTED" in str(api_e):
                                        exhaust_fast_quota()
                                        hit_out_of_fast_page = True
                                        break
                                    continue
                            if item_downloaded:
                                break
                        else:
                            continue

                if not item_downloaded:
                    # ===== RECOVERY: Check if file exists on disk anyway (STRICT MD5 MATCH) =====
                    recovered = recover_downloaded_files(DOWNLOAD_DIR, url)
                    if recovered:
                        original_name, file_name = recovered
                        clean_title = clean_downloaded_title(original_name, url)
                        mark_completed(url, clean_title)
                        completed_urls.add(url)
                        session_success += 1
                        processed_in_this_session.add(url)
                        print(f"  ✓ Book recovered from disk - marked as done!")
                        continue
                    else:
                        # Also check Windows Downloads folder as fallback
                        win_downloads = os.path.expanduser("~/Downloads")
                        if os.path.isdir(win_downloads):
                            found_fallback = False
                            for f in os.listdir(win_downloads):
                                fp = os.path.join(win_downloads, f)
                                if not os.path.isfile(fp):
                                    continue
                                try:
                                    if os.path.getsize(fp) < 1024:
                                        continue
                                except Exception:
                                    continue
                                md5_match = re.search(r'/md5/([0-9a-fA-F]{32})', url)
                                if md5_match and md5_match.group(1).lower() in f.lower():
                                    try:
                                        clean_title = clean_downloaded_title(f, url)
                                        base_file_name = generate_custom_filename(clean_title, url, NAME_FORMAT)
                                        final_name = get_unique_filename(DOWNLOAD_DIR, base_file_name)
                                        final_path = os.path.join(DOWNLOAD_DIR, final_name)
                                        shutil.move(fp, final_path)
                                        mark_completed(url, clean_title)
                                        completed_urls.add(url)
                                        session_success += 1
                                        processed_in_this_session.add(url)
                                        print(f"  ✓ Moved from Downloads: {final_name} - marked as done!")
                                        found_fallback = True
                                        break
                                    except Exception:
                                        pass
                            if found_fallback:
                                continue
                    
                    if book_has_libgen or failure_reason == "MIRROR_404":
                        libgen_urls_found.append(f"{url} ||| MANUAL_CHECK")
                        mark_completed(url, "LIBGEN_MANUAL")
                        completed_urls.add(url)
                        processed_in_this_session.add(url)
                        session_skipped += 1
                        if failure_reason == "MIRROR_404":
                            print(f"  [+] Mirror 404 - added to libgen_manual_queue.txt for manual check")
                        else:
                            print(f"  [+] Libgen URL in manual queue, marking done")
                    else:
                        session_failed += 1
                        processed_in_this_session.add(url)
                        print(f"\n  [!] ALL MIRRORS FAILED for: {url[:60]}...")
                        print(f"  [+] Re-queueing for retry at random position...")
                        insert_failed_url_random_position(url)
                        session_requeued += 1

            # ===== SESSION COMPLETE - SAVE LIBGEN QUEUE =====
            save_libgen_queue()

            print("\n" + "=" * 70)
            print("SESSION SUMMARY")
            print("=" * 70)
            print(f"Total books processed:     {session_success + session_failed + session_skipped}")
            print(f"Successfully downloaded:   {session_success}")
            print(f"All mirrors failed:        {session_failed} ({session_requeued} re-queued)")
            print(f"Skipped (completed/cache): {session_skipped}")
            print(f"Not Turkish (cached):      {session_not_turkish}")
            print(f"Duplicates detected:       {session_duplicates}")
            print(f"Fast downloads used:       {session_fast_used}")
            print(f"Browser restarts:          {browser_restart_count}")
            print(f"Libgen URLs exported:      {len(libgen_urls_found)} → libgen_manual_queue.txt")
            print("=" * 70 + "\n")

            save_state_snapshot(idx + 1, targets, completed_urls, failed_items)
            save_progress_cache(progress_cache)
            save_failed_log(failed_items)

    except KeyboardInterrupt:
        print("\n\n[⚠️] Interrupted by user - saving progress...")
        save_libgen_queue()
        save_state_snapshot(current_idx, current_targets or [], completed_urls, load_failed_log())
    except Exception as e:
        print(f"\n[!] Fatal error: {e}")
        import traceback
        traceback.print_exc()
        save_libgen_queue()
        save_state_snapshot(current_idx, current_targets or [], completed_urls, load_failed_log())
    finally:
        # Belt-and-suspenders: save Libgen queue no matter how we exited
        save_libgen_queue()
        try:
            if context:
                context.close()
        except Exception:
            pass
        try:
            if page:
                page.close()
        except Exception:
            pass
        try:
            if browser:
                browser.close()
        except Exception:
            pass

    print("\n[✓] Script finished. Check summary above and log files in:")
    print(f"    {DOWNLOAD_DIR}\n")
    print(f"Files created/updated:")
    print(f"  - completed.txt             : Successfully downloaded URLs")
    print(f"  - libgen_manual_queue.txt   : 🆕 Libgen URLs for MANUAL download later")
    print(f"  - failed_downloads.json    : Failures with retry counts")
    print(f"  - aa_links.txt             : Main link list (includes re-queued failures)")
    print(f"  - not_turkish_cache.json   : Cached non-Turkish book URLs")
    print(f"  - .hearth_state.json       : Session checkpoint for resume\n")

if __name__ == "__main__":
    main()
