#!/usr/bin/env python3
"""Prove the live hook's tests actually defend it.

    python3 tests/mutation_check_read_before_edit.py

For each mechanism in hooks/read-before-edit.py: neuter it in a scratch COPY,
run the hook's suite against the copy, and require the suite to go RED. The
real hook is never rewritten; its bytes are compared before and after anyway.

Exit 0 when every mutant was killed; 1 when any survived; 2 when this script
itself could not run.
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
HOOK = ROOT / "hooks" / "read-before-edit.py"
SUITE = ROOT / "tests" / "test_read_before_edit_hook.py"

# (name, exact text to replace, replacement). Each `old` is unique in the file.
MUTANTS = [
    ("WARNING no verdict warns",
     'WARNS = {"never-read", "partial", "no-total", "stale"}', "WARNS = set()"),
    ("PHANTOM the last line is not proven on disk",
     '    if not disk.endswith("\\n"):\n        return', "    if True:\n        return"),
    # The leniency review refuted: old windows survive an own edit unmapped.
    ("REMAP old windows are kept as they were",
     "    return out\n# mutation-anchor: remap", "    return windows\n# mutation-anchor: remap"),
    ("OUTSIDE a change on disk after the last logged event is not seen",
     "MTIME_TOLERANCE_S = 2.0", "MTIME_TOLERANCE_S = 10 ** 9"),
    ("OUTSIDE an own edit whose result lags the transcript is called stale",
     "OWN_EDIT_GRACE_S = 5.0", "OWN_EDIT_GRACE_S = 0.0"),
    ("CREATE a file that does not exist yet is judged",
     "    if not os.path.isfile(target):", "    if False:"),
    ("COMPACTION what was read before the summary still counts",
     'COMPACT_MARK = "isCompactSummary"', 'COMPACT_MARK = "never-in-a-transcript"'),
]


def run_suite(hook: Path) -> bool:
    env = {**os.environ, "READ_BEFORE_EDIT_HOOK_UNDER_TEST": str(hook)}
    result = subprocess.run(
        [sys.executable, "-m", "pytest", str(SUITE), "-q", "-x", "--no-header",
         "-p", "no:cacheprovider"],
        capture_output=True, text=True, cwd=ROOT, env=env, check=False)
    return result.returncode == 0


def main() -> int:
    original = HOOK.read_text(encoding="utf-8")
    if not run_suite(HOOK):
        print("the suite is RED before any mutation — fix that first", file=sys.stderr)
        return 2

    survivors: list[str] = []
    with tempfile.TemporaryDirectory() as scratch:
        mutant = Path(scratch) / "read-before-edit.py"
        for name, old, new in MUTANTS:
            if original.count(old) != 1:
                print(f"  ?? {name}: anchor appears {original.count(old)} times — the "
                      f"mutation list is stale, so this script is measuring nothing")
                survivors.append(f"{name} (stale)")
                continue
            mutant.write_text(original.replace(old, new, 1), encoding="utf-8")
            # The mutant must still find the gate: it lives next to the REAL hook.
            env_gate = str(ROOT / "gate" / "read_order.py")
            os.environ["READ_ORDER_GATE"] = env_gate
            if run_suite(mutant):
                print(f"  SURVIVED  {name} — removed it and the suite stayed green")
                survivors.append(name)
            else:
                print(f"  killed    {name}")
            os.environ.pop("READ_ORDER_GATE", None)

    if HOOK.read_text(encoding="utf-8") != original:
        print("the real hook file changed during the run — it must never be touched",
              file=sys.stderr)
        return 2
    if survivors:
        print(f"\n{len(survivors)} mutant(s) survived: {', '.join(survivors)}")
        return 1
    print(f"\nall {len(MUTANTS)} mutants killed; the real hook was never rewritten")
    return 0


if __name__ == "__main__":
    sys.exit(main())
