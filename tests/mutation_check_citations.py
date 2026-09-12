#!/usr/bin/env python3
"""Prove the citation gate's tests actually defend it.

    python3 tests/mutation_check_citations.py

For each check in gate/citations.py: neuter it, run the suite, and require the
suite to go RED. A test that still passes with the mechanism removed is not
testing the mechanism — it is decoration that reports green forever.

Two of the mutants are not checks but SHAPES the extractor recognises, and they
are here because a blind extractor is the worst failure this gate can have. A
deleted check reports a finding it should not have missed; a blind extractor
reports "0 attributed quotations — clean" and hands out a green badge over a
pool nobody ever looked at. That is not hypothetical: before the bullet shape
was added, this gate found exactly zero quotations in the Zen edition's eight
haiku and exited 0.

Exit 0 when every mutant was killed; 1 when any survived; 2 when this script
itself could not run (the same contract as the gate).

The file is restored from an in-memory copy in a `finally`, never with
`git checkout`: this repo may hold uncommitted work, and a checkout to undo a
mutation would take that work with it. The restore is then verified — a mutation
runner that leaves the file mutated has done more harm than the bug it hunted.

This file is shared byte-for-byte with the other conduct-harness repos that
carry gate/citations.py.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
GATE = ROOT / "gate" / "citations.py"

# (name, exact text to replace, replacement)
#
# Each `old` string is unique in the file and carries enough context that it
# cannot collide with a `def` line or a docstring mention of the same name.
MUTANTS = [
    ("CHECK 1 unsourced-quote",
     "coverage, quote_findings = check_quotes_resolve(sources)",
     "checked, quote_findings = 0, []"),
    ("CHECK 2 incomplete-provenance",
     "findings += check_provenance_complete(sources)",
     "pass"),
    ("CHECK 3 anachronism",
     "findings += check_anachronism(sources)",
     "pass"),
    ("CHECK 4 pd-claim",
     "findings += check_pd_status(sources)",
     "pass"),
    # SHAPE B: stop recognising the bullet form, so pool bullets fall back into
    # the delimiter-requiring flowing path. This is the exact state the gate was
    # in when it read the Zen pool and found nothing.
    ("SHAPE B undelimited bullet",
     '        bullet = body.startswith("- ")',
     "        bullet = False"),
    # SHAPE A: walk the delimited spans left to right instead of right to left,
    # which hands back the romanisation of a composite epigraph as the quotation.
    ("SHAPE A rightmost span wins",
     "for cand in reversed(list(_SPAN.finditer(body))):",
     "for cand in list(_SPAN.finditer(body)):"),
    # SHAPE C: keep recognising the attribution line, but never emit the pair.
    ("SHAPE C attribution on its own line",
     "                    out.append((lineno, quote, body.lstrip(\"—– \").strip()))",
     "                    pass"),
]


def run_suite() -> bool:
    """True when the suite is green."""
    result = subprocess.run(
        [sys.executable, "-m", "pytest", str(ROOT / "tests"), "-q", "-x", "--no-header"],
        capture_output=True, text=True, cwd=ROOT, check=False)
    return result.returncode == 0


def main() -> int:
    original = GATE.read_text(encoding="utf-8")

    if not run_suite():
        print("the suite is RED before any mutation — fix that first", file=sys.stderr)
        return 2

    survivors: list[str] = []
    try:
        for name, old, new in MUTANTS:
            if original.count(old) != 1:
                print(f"  ?? {name}: anchor appears {original.count(old)} times in the "
                      f"gate — the mutation list is stale, so this script is "
                      f"measuring nothing")
                survivors.append(f"{name} (stale)")
                continue
            GATE.write_text(original.replace(old, new, 1), encoding="utf-8")
            if run_suite():
                print(f"  SURVIVED  {name} — removed it and the suite stayed green")
                survivors.append(name)
            else:
                print(f"  killed    {name}")
    finally:
        GATE.write_text(original, encoding="utf-8")

    if GATE.read_text(encoding="utf-8") != original:
        print("the gate file was NOT restored cleanly", file=sys.stderr)
        return 2

    if survivors:
        print(f"\n{len(survivors)} mutant(s) survived: {', '.join(survivors)}")
        return 1
    print(f"\nall {len(MUTANTS)} mutants killed; gate restored and verified")
    return 0


if __name__ == "__main__":
    sys.exit(main())
