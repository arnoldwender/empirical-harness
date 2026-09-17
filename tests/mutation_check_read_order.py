#!/usr/bin/env python3
"""Prove the order gate's tests actually defend it.

    python3 tests/mutation_check_read_order.py

For each rule in gate/read_order.py: neuter it, run the gate's suite, and require
the suite to go RED. A test that still passes with the mechanism removed is not
testing the mechanism — it is decoration that reports green forever.

Three of the mutants are not rules but REFUSALS: the places where the gate says
"I cannot judge this" instead of guessing. They are here because a gate that
guesses fails open in the quietest way there is — it skips the line it could not
read, prints a verdict over what was left, and the verdict looks like any other.

Exit 0 when every mutant was killed; 1 when any survived; 2 when this script
itself could not run (the same contract as the gate).

The file is restored from an in-memory copy in a `finally`, never with
`git checkout`: this repo may hold uncommitted work, and a checkout to undo a
mutation would take that work with it. The restore is then verified.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
GATE = ROOT / "gate" / "read_order.py"
SUITE = ROOT / "tests" / "test_read_order_gate.py"

# (name, exact text to replace, replacement). Each `old` is unique in the file.
MUTANTS = [
    # THE predicate: judge the reads first, whatever order they arrived in. With
    # this in place a read AFTER the edit clears it, and the gate checks that a
    # file was read at some point — which every careless session also satisfies.
    ("ORDER reads are judged where they happened",
     "    for ev in events:\n        st = states.setdefault(ev.path, FileState())",
     "    for ev in sorted(events, key=lambda e: e.op != READ):\n"
     "        st = states.setdefault(ev.path, FileState())"),
    ("CHECK 1 edit-before-full-read",
     "                    offences.setdefault(ev.path, []).append(ev)",
     "                    pass"),
    # A gap between two windows is the incident the gate exists for.
    ("COVERAGE a gap between windows is not a full read",
     "        if first > reach + 1:\n            return False",
     "        if False:\n            return False"),
    ("COVERAGE a read with no total proves nothing",
     "    if total is None:\n        return False",
     "    if total is None:\n        return True"),
    ("COVERAGE windows of a file that changed length are dropped",
     "                st.windows = []",
     "                pass"),
    ("CHECK 1 an overwrite is a change too",
     '        elif ev.op == CREATE:\n            st.known = True',
     '        elif ev.op in (CREATE, WRITE):\n            st.known = True'),
    ("CHECK 2 claim-on-partial-read",
     "            findings += check_claims(tokens, states)",
     "            pass"),
    ("CHECK 2 a fence or a blockquote is not the report's own voice",
     '        if fenced or stripped.startswith(">"):\n            continue',
     "        if False:\n            continue"),
    ("CHECK 2 a bare filename two files answer to names neither",
     "if tok == path or (tok == base and names[base] == 1):",
     "if tok == path or tok == base:"),
    ("CHECK 3 claim-on-unread",
     "                findings += check_unread_claims(tokens, states, {rel(str(report_path))})",
     "                pass"),
    ("REFUSAL a line that is not JSON",
     '            raise Unreadable(f"{what} line {n}: not JSON ({exc})") from exc',
     "            continue"),
    ("LOADING a row is cut at a newline and nowhere else",
     'errors="replace").split("\\n"), 1):',
     'errors="replace").splitlines(), 1):'),
    ("REFUSAL an exemption with no reason",
     "        if not reason.strip():",
     "        if False:"),
    ("REFUSAL a missing toollog is not a clean session",
     '        raise Unreadable(f"no {what} at {path} — no evidence is not a clean session")',
     "        return []"),
    ("ADAPTER a failed call read nothing and changed nothing",
     '            if block.get("is_error") or not isinstance(result, dict):',
     "            if False:"),
    # Both directions are tested: with the correction removed a careful reader is
    # flagged, and with it applied blindly a real last line is forgiven.
    ("ADAPTER the phantom last line is dropped only where an edit proves it",
     '                    if event["path"] in phantom and is_int(total) and total > 0 \\',
     '                    if is_int(total) and total > 0 \\'),
    ("ADAPTER the phantom last line is dropped at all",
     "                        total -= 1                 # the empty segment after the last newline",
     "                        pass"),
    ("ADAPTER a Write is a create only when its result says so",
     '                event["op"] = CREATE if result.get("type") == "create" else WRITE',
     '                event["op"] = CREATE'),
]


def run_suite() -> bool:
    """True when the order gate's suite is green."""
    result = subprocess.run(
        [sys.executable, "-m", "pytest", str(SUITE), "-q", "-x", "--no-header"],
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
