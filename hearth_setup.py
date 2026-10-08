#!/usr/bin/env python3
"""
hearth_setup.py — One-file bootstrapper for running hearth.py on a second machine.

Run:  python hearth_setup.py

It will:
  1. Install Python dependencies (playwright, playwright-stealth)
  2. Download and install the Chromium browser for Playwright
  3. Create your download folder
  4. Prompt you for aa_secret (or you can paste aa_secret.txt yourself)
  5. Remind you to put your half of aa_links.txt in the folder
  6. Print the exact command to start hearth.py
"""

import os
import sys
import subprocess
import platform

REQUIRED_PACKAGES = [
    "playwright",
    "playwright-stealth",
]

def run(cmd, check=True, shell=True):
    print(f"  $ {cmd}")
    result = subprocess.run(cmd, shell=shell, capture_output=True, text=True)
    if result.stdout.strip():
        print(result.stdout.strip())
    if result.returncode != 0 and check:
        print(f"  [!] FAILED: {result.stderr.strip()}")
        return False
    return True

def main():
    print("=" * 60)
    print("  hearth.py — Second Laptop Setup")
    print("=" * 60)
    print()

    # 1) Check Python version
    v = sys.version_info
    print(f"[1/5] Python version: {v.major}.{v.minor}.{v.micro}")
    if v.major < 3 or (v.major == 3 and v.minor < 8):
        print("  [!] Python 3.8+ required.")
        sys.exit(1)
    print("  OK")
    print()

    # 2) Install pip packages
    print("[2/5] Installing Python packages...")
    for pkg in REQUIRED_PACKAGES:
        run(f"{sys.executable} -m pip install {pkg}")
    print()

    # 3) Install Playwright browser
    print("[3/5] Installing Chromium for Playwright...")
    run(f"{sys.executable} -m playwright install chromium")
    run(f"{sys.executable} -m playwright install-deps chromium")
    print()

    # 4) Download folder
    print("[4/5] Setting up download folder...")
    if platform.system() == "Windows":
        default_dir = os.path.join(os.path.expanduser("~"), "Downloads", "hearth_books")
    else:
        default_dir = os.path.join(os.path.expanduser("~"), "hearth_books")

    dl_dir = input(f"  Download folder [{default_dir}]: ").strip()
    if not dl_dir:
        dl_dir = default_dir
    dl_dir = os.path.abspath(dl_dir)
    os.makedirs(dl_dir, exist_ok=True)
    print(f"  Created: {dl_dir}")
    print()

    # 5) aa_secret
    print("[5/5] API key setup...")
    secret_path = os.path.join(dl_dir, "aa_secret.txt")
    if os.path.exists(secret_path):
        print(f"  aa_secret.txt already exists at {secret_path}")
    else:
        key = input("  Paste your AA_SECRET key (or press Enter to skip and add later): ").strip()
        if key:
            with open(secret_path, "w", encoding="utf-8") as f:
                f.write(key)
            print(f"  Saved to {secret_path}")
        else:
            print(f"  Skipped. Put your key in: {secret_path}")
    print()

    # Remind about aa_links.txt
    links_path = os.path.join(dl_dir, "aa_links.txt")
    print("=" * 60)
    print("  SETUP COMPLETE")
    print("=" * 60)
    print()
    print("  Remaining manual steps:")
    print(f"  1. Copy your half of aa_links.txt to:")
    print(f"     {links_path}")
    print()
    print(f"  2. Copy hearth.py to this machine (same folder or anywhere)")
    print()
    print(f"  3. Run with:")

    hearth_path = "hearth.py"
    if platform.system() == "Windows":
        print(f'     python {hearth_path} text "{dl_dir}"')
    else:
        print(f'     python3 {hearth_path} text "{dl_dir}"')
    print()
    print("  Optional flags:")
    print('     --reset-quota     Reset fast download quota counter')
    print('     --all-books       Download all languages (skip Turkish filter)')
    print()
    print("  Files this creates in your download folder:")
    print("     aa_links.txt            Your book URLs (you provide this)")
    print("     aa_secret.txt           API key for fast downloads")
    print("     completed.txt           Tracks finished books (auto)")
    print("     libgen_manual_queue.txt Libgen-only books for manual download (auto)")
    print("     fast_quota.json         Fast download counter (auto)")
    print()

if __name__ == "__main__":
    main()
