# split_links.py - Take the last 100K lines from aa_links.txt for the second laptop.
# Run on laptop 1:  python split_links.py
import os

SOURCE = r"C:\Users\sinan\Desktop\AAdownloads\books\aa_links.txt"
DEST   = r"C:\Users\sinan\Desktop\AA_DOWNLOADS2\aa_links.txt"
TAKE   = 100_000

with open(SOURCE, "r", encoding="utf-8") as f:
    lines = [l for l in f if l.strip()]

total = len(lines)
if total <= TAKE:
    split_at = total // 2
    print(f"File has {total} lines (under {TAKE}), splitting in half at {split_at}")
else:
    split_at = total - TAKE
    print(f"File has {total} lines, taking last {TAKE}")

keep = lines[:split_at]
move = lines[split_at:]

os.makedirs(os.path.dirname(DEST), exist_ok=True)

with open(SOURCE, "w", encoding="utf-8") as f:
    for line in keep:
        f.write(line if line.endswith("\n") else line + "\n")

with open(DEST, "w", encoding="utf-8") as f:
    for line in move:
        f.write(line if line.endswith("\n") else line + "\n")

print(f"Laptop 1: {len(keep)} lines  ->  {SOURCE}")
print(f"Laptop 2: {len(move)} lines  ->  {DEST}")
print("Done.")
