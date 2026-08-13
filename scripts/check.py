#!/usr/bin/env python3
"""Self-check for one conduct-harness repo. No dependencies, no network, no arguments.

    python3 scripts/check.py        # from the repo root

Exits 0 if the repo keeps its own promises, 1 otherwise, printing every failure.

The codex this repo carries says "done is what the gates return". This is that gate,
turned on the repo itself: it verifies that what the documentation claims is what the
shipped files actually do.

  1 emitter      bin/<cmd> and hooks/session-start.sh run, exit 0, and print a real line
  2 rotation     the day's line is selected from the NON-BLANK pool, so no day is mute
  3 block        codex-block.md is plain text (never markdown headings), titled, 4 sections,
                 rules numbered from 1 in each, one falsifier per section
  4 single copy  CODEX.md's "## Paste-ready" is codex-block.md verbatim — the block is
                 pasted into a host AGENTS.md, and a second copy always drifts from the first
  5 quotations   every quoted line in README's "The first word" exists verbatim in the pool,
                 in the emitter's fixed banner, or recorded in the pool document
  6 pool doc     the pool document lists every line of the pool
  7 links        every relative markdown link resolves
  8 hygiene      no absolute home paths, e-mail addresses or key-shaped strings

Written in Python rather than shell on purpose: the pools carry kanji, hanzi, Arabic and
Devanagari, and sed/cut abort on them with "illegal byte sequence" under a C locale.
"""
import os
import re
import subprocess
import sys
import unicodedata

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FAILURES = []


def fail(check, msg):
    FAILURES.append((check, msg))
    print(f"  FAIL [{check}] {msg}")


def read(*parts):
    with open(os.path.join(ROOT, *parts), encoding="utf-8") as fh:
        return fh.read()


def find_one(pattern_dir, predicate):
    d = os.path.join(ROOT, pattern_dir) if pattern_dir else ROOT
    if not os.path.isdir(d):
        return None
    hits = [f for f in sorted(os.listdir(d)) if predicate(f)]
    return os.path.join(d, hits[0]) if len(hits) == 1 else None


def pool_path():
    return find_one("", lambda f: f.endswith(".txt"))


def emitter_path():
    return find_one("bin", lambda f: not f.startswith("."))


def doc_paths():
    skip = {"README.md", "CODEX.md", "EXAMPLE.md", "REFERENCE.md"}
    return [os.path.join(ROOT, f) for f in sorted(os.listdir(ROOT))
            if f.endswith(".md") and f[:1].isupper() and f not in skip]


def norm(s):
    s = unicodedata.normalize("NFKC", s)
    s = re.sub(r"[*_`>\"“”'’\[\]()]", "", s)
    return " ".join(s.split()).lower()


# --- 1 + 2 -------------------------------------------------------------------------

def check_emitter():
    emitter, pool = emitter_path(), pool_path()
    if not emitter:
        return fail("emitter", "expected exactly one file in bin/")
    if not pool:
        return fail("emitter", "expected exactly one *.txt pool at the repo root")

    for path in (emitter, os.path.join(ROOT, "hooks/session-start.sh")):
        if not os.access(path, os.X_OK):
            fail("emitter", f"{os.path.relpath(path, ROOT)} is not executable")
        run = subprocess.run(["sh", path], capture_output=True, text=True, check=False)
        if run.returncode != 0:
            fail("emitter", f"{os.path.relpath(path, ROOT)} exited "
                            f"{run.returncode}: {run.stderr.strip()[:90]}")

    lines = [l.strip() for l in read(os.path.basename(pool)).splitlines() if l.strip()]
    out = subprocess.run(["sh", emitter], capture_output=True, text=True, check=False).stdout
    if not any(norm(l)[:40] in norm(out) for l in lines):
        fail("rotation", "the emitter printed no line from the pool")

    # The count and the pick must agree on what a "line" is. Counting non-blank lines with
    # `grep -c .` while picking by absolute line number with `sed -n Np` desynchronises the
    # moment the pool holds a blank line — and the docs invite the reader to edit the pool.
    # Measured on an 8-line pool with 3 blanks: mute on 137 of 366 days, 3 entries forever
    # unreachable. The fix is to filter before selecting.
    src = read(os.path.relpath(emitter, ROOT))
    if re.search(r'sed -n "\$\{IDX\}p" "\$\w+"', src):
        fail("rotation", "the emitter picks by absolute line number; a blank line in the "
                         "pool would desynchronise it from the count — filter first, e.g. "
                         'grep . "$POOL" | sed -n "${IDX}p"')


# --- 3 + 4 -------------------------------------------------------------------------

def check_block():
    block = read("codex-block.md")
    lines = block.rstrip("\n").split("\n")

    if any(l.startswith("#") for l in lines):
        fail("block", "codex-block.md carries markdown headings; it is pasted inside the "
                      "reader's own AGENTS.md, where a '#' hijacks their heading tree")
    if not re.match(r"^THE [A-Z]+ CODEX · v1\.0", lines[0]):
        fail("block", f"first line is not the canonical title: {lines[0][:60]!r}")

    sections = [i for i, l in enumerate(lines) if re.match(r"^[IVX]+\.\s", l)]
    if len(sections) != 4:
        fail("block", f"{len(sections)} sections, expected 4")
    for n, start in enumerate(sections, 1):
        end = sections[n] if n < len(sections) else len(lines)
        nums = [int(m.group(1)) for l in lines[start:end]
                if (m := re.match(r"^\s{2}(\d+)\s", l))]
        if nums and nums != list(range(1, len(nums) + 1)):
            fail("block", f"section {n}: rules numbered {nums}, expected 1..{len(nums)}")
        if sum(1 for l in lines[start:end] if "Falsifier:" in l) != 1:
            fail("block", f"section {n}: expected exactly one 'Falsifier:' line")

    m = re.search(r"^## Paste-ready\b.*?```[a-z]*\n(.*?)```", read("CODEX.md"), re.S | re.M)
    if not m:
        fail("single-copy", "CODEX.md has no '## Paste-ready' fenced block")
    elif m.group(1).strip("\n") != block.strip("\n"):
        fail("single-copy", "CODEX.md's '## Paste-ready' differs from codex-block.md — "
                            "one of the two is already wrong for anyone who pasted it")


# --- 5 + 6 -------------------------------------------------------------------------

def check_quotations():
    pool, emitter = pool_path(), emitter_path()
    if not pool or not emitter:
        return
    haystack = norm("\n".join(
        [read(os.path.basename(pool)), read(os.path.relpath(emitter, ROOT))]
        + [open(d, encoding="utf-8").read() for d in doc_paths()]))

    section = re.search(r"^## The first word\n(.*?)(?=^## )", read("README.md"), re.S | re.M)
    if section:
        for quote in re.findall(r"^>\s*(.+)$", section.group(1), re.M):
            if quote.strip().lstrip("*_> ").startswith(("—", "–", "-")):
                continue                                  # attribution line, not a quotation
            core = norm(quote).split(" — ")[0].split(" – ")[0].rstrip(".,;:!?…")
            if len(core) >= 18 and core[:52] not in haystack:
                fail("quotations", f"README quotes a line that exists nowhere in this repo: "
                                   f"{quote.strip()[:70]}")

    docs = doc_paths()
    if not docs:
        return fail("pool-doc", "no pool document found")
    flat = " ".join(
        " ".join(re.sub(r"(?m)^\s*[->]\s?", "", open(d, encoding="utf-8").read())
                 .replace('"', "").replace("*", "").split())
        for d in docs)
    missing = [l for l in read(os.path.basename(pool)).splitlines()
               if l.strip() and " ".join(l.replace('"', "").split()) not in flat]
    if missing:
        fail("pool-doc", f"{len(missing)} pool line(s) are not listed in "
                         f"{', '.join(os.path.basename(d) for d in docs)} — "
                         f"first: {missing[0][:60]}")


# --- 7 + 8 -------------------------------------------------------------------------

def check_links_and_hygiene():
    leak = re.compile(r"/(?:Users|home)/[a-z]+|[\w.]+@(?:gmail|googlemail)\.com"
                      r"|sk-[A-Za-z0-9]{20}|ghp_[A-Za-z0-9]{20}")
    for base, dirs, files in os.walk(ROOT):
        dirs[:] = [d for d in dirs if d not in (".git", "assets")]
        for name in files:
            path = os.path.join(base, name)
            rel = os.path.relpath(path, ROOT)
            if not name.endswith((".md", ".sh", ".txt", ".cff", ".py", ".yml")):
                continue
            text = open(path, encoding="utf-8", errors="replace").read()
            for i, line in enumerate(text.splitlines(), 1):
                if leak.search(line):
                    fail("hygiene", f"{rel}:{i} looks like a private path, address or key")
            if not name.endswith(".md"):
                continue
            for m in re.finditer(r"\[[^\]]*\]\(([^)#][^)]*)\)", text):
                target = m.group(1).split("#")[0]
                if target and not target.startswith(("http", "mailto:")) \
                        and not os.path.exists(os.path.join(base, target)):
                    fail("links", f"{rel} links to {target}, which does not exist")


def main():
    print(f"checking {os.path.basename(ROOT)}")
    for fn in (check_emitter, check_block, check_quotations, check_links_and_hygiene):
        try:
            fn()
        except Exception as exc:                     # a broken check must not read as a pass
            fail("check", f"{fn.__name__} raised {type(exc).__name__}: {exc}")
    if FAILURES:
        print(f"\n{len(FAILURES)} failure(s)")
        return 1
    print("  all checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
