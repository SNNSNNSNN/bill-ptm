#!/usr/bin/env python3
"""
setup_second_laptop.py
Run this FIRST on the second machine. It installs everything hearth.py needs.
"""
if __name__ != "__main__":
    raise SystemExit("Run this file directly, don't import it.")

import os, sys, subprocess, platform

print("=" * 60)
print("  hearth.py - Second Laptop Setup")
print("=" * 60)

v = sys.version_info
print(f"\n[1/5] Python {v.major}.{v.minor}.{v.micro}")
if v.major < 3 or v.minor < 8:
    print("  Need Python 3.8+"); sys.exit(1)
print("  OK")

print("\n[2/5] Installing packages...")
for pkg in ["playwright", "playwright-stealth"]:
    print(f"  Installing {pkg}...")
    subprocess.run([sys.executable, "-m", "pip", "install", pkg],
                   capture_output=True, text=True)
print("  Done")

print("\n[3/5] Installing Chromium browser...")
subprocess.run([sys.executable, "-m", "playwright", "install", "chromium"],
               capture_output=True, text=True)
if platform.system() == "Linux":
    subprocess.run([sys.executable, "-m", "playwright", "install-deps", "chromium"],
                   capture_output=True, text=True)
print("  Done")

print("\n[4/5] Download folder...")
if platform.system() == "Windows":
    default = os.path.join(os.path.expanduser("~"), "Downloads", "hearth_books")
else:
    default = os.path.join(os.path.expanduser("~"), "hearth_books")

dl = input(f"  Folder [{default}]: ").strip() or default
dl = os.path.abspath(dl)
os.makedirs(dl, exist_ok=True)
print(f"  Ready: {dl}")

print("\n[5/5] API key...")
sf = os.path.join(dl, "aa_secret.txt")
if os.path.exists(sf):
    print(f"  Already exists: {sf}")
else:
    k = input("  Paste AA_SECRET (Enter to skip): ").strip()
    if k:
        with open(sf, "w") as f: f.write(k)
        print(f"  Saved: {sf}")
    else:
        print(f"  Add later: {sf}")

lf = os.path.join(dl, "aa_links.txt")
py = "python" if platform.system() == "Windows" else "python3"

print("\n" + "=" * 60)
print("  DONE. Now:")
print("=" * 60)
print(f"\n  1. Put your half of aa_links.txt here:")
print(f"     {lf}")
print(f"\n  2. Put hearth.py in this folder (or anywhere)")
print(f"\n  3. Run:")
print(f'     {py} hearth.py text "{dl}"')
print(f"\n  Flags:  --reset-quota  --all-books")
print()
