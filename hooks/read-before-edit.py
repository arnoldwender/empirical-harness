#!/usr/bin/env python3
"""The Empirical Harness — read-before-edit, at the moment of the edit.

A Claude Code `PreToolUse` hook for `Edit`, `Write` and `MultiEdit`. Before the
tool runs, it asks the order gate's question about THIS file in THIS session:
was the file read — all of it — before it is changed? If not, it tells the
agent so, in the tool result, and lets the edit through. It warns; it does not
block. See hooks/README.md for the wiring and the README for why.

    "hooks": {"PreToolUse": [{"matcher": "Edit|Write|MultiEdit", "hooks": [
        {"type": "command", "command": "python3 /abs/path/to/empirical-harness/hooks/read-before-edit.py",
         "timeout": 10}]}]}

WHY A HOOK AND NOT ONLY THE GATE
--------------------------------
`gate/read_order.py` judges a session after the fact. That is the right place
for a verdict and the wrong place for a correction: by the time the gate runs,
the edit made on a half-read file has been made. The rule it enforces was
measured broken in 66 of 72 real sessions whose standing instructions state it
in so many words (README, "Measured before it was believed"). A rule that is
written down and broken nine times out of ten needs a mechanism at the moment
of the act, not a fourth copy of the sentence.

WHAT IT DOES
------------
1. Reads the hook payload from stdin: `tool_name`, `tool_input.file_path`,
   `transcript_path`, `session_id`, `cwd`.
2. Ignores tools other than Edit/Write/MultiEdit, files that do not exist yet
   (a Write that creates is not an edit), and files exempted in
   `.conduct/read-order-allow.txt` of the working directory — a glob with a
   written reason, as the gate requires; a glob with no reason is not honoured.
3. Takes from the session transcript only the rows that mention the file, and
   only those AFTER the last context compaction: what was read before a
   compaction is no longer in the agent's context, so for this rule it was not
   read.
4. Converts them with the gate's own Claude Code adapter (`from_claude_transcript`)
   and replays the gate's predicate for that one file: do the windows of `Read`
   cover 1..total, or did this session create or overwrite the file itself?
5. If not: prints `{"hookSpecificOutput": {"hookEventName": "PreToolUse",
   "additionalContext": "..."}}` and exits 0. Claude Code adds that text to
   the agent's context alongside the tool result (hooks reference, "PreToolUse
   decision control", read 2026-09-17). The permission flow is not touched.
6. Appends one receipt line per run to `READ_ORDER_RECEIPTS` (default
   `~/.local/state/empirical-harness/read-order-receipts.jsonl`; `off` disables):
   `{ts, session, tool, path, verdict, seen, total, reads, edits, ms}`. The
   receipts are the instrument: whether the warning fires, how often, and
   whether the rate falls over time is a count over this file, not a feeling.

WHERE IT DIVERGES FROM THE GATE, AND THE EVIDENCE FOR EACH
----------------------------------------------------------
The gate's `replay` is not reused. Three things happen here that the gate does
not do, because a live hook sees things a post-hoc judge does not:

* Windows are REMAPPED through the session's own edits, exactly. The gate drops
  every window when a file changes length, and as a judge it is right to. Live,
  that punishes the behaviour the rule asks for: "read 1-120 → edit → read
  121-915" would warn again on the next edit. A first version of this hook kept
  the old windows unchanged instead, and review refuted it with one sequence:
  read 1-50 of 100 → delete the first 50 → read 26-50 of the 50 that remain →
  covered by arithmetic, lines 1-25 of the current file never seen. The exact
  answer is in the transcript: every Edit result carries `structuredPatch`
  (unified-diff hunks with ' ', '-', '+' lines; measured on 48 of 48 Edits in
  one session, 35 of them with no `originalFile`). With it, each old line maps
  to its new position, deleted lines disappear, and added lines — written by
  this session — count as seen. An edit with no patch invalidates the windows,
  as the gate does: without evidence nothing is kept.
* The phantom last line is proven against the DISK. This runtime counts the
  empty segment after a file's final newline as a line (`totalLines` = newlines
  + 1, measured 1,462 of 1,462). The gate corrects it only where a later Edit's
  `originalFile` proves it; at the first edit there is no such proof, and a
  careful block read would come out "912 of 913". Here the file is on disk: if
  it ends in a newline and has exactly `total - 1` real lines, the phantom is
  proven for this version and removed. Otherwise the total is left alone.
* An OUTSIDE change is detected by mtime. The log cannot see `sed -i`, a
  formatter or another session. Every transcript row carries a `timestamp`; if
  the file's mtime is later than the last event this session logged for it, the
  file changed underneath the reading — "stale", even after a full read.
  With one grace: the transcript is written with a LAG. Measured on Claude Code
  2.1.274: with seven Edits in one turn, the result of Edit n (timestamp +1.1 s)
  was not yet in the file when the PreToolUse of Edit n+1 fired (+3 s); all
  seven receipts saw the same edit count. So an mtime newer than the last logged
  event may be this session's own edit from three seconds ago. The hook's own
  previous receipt for the same file and session, written synchronously, tells
  the two apart: an mtime within `OWN_EDIT_GRACE_S` of it is that edit.

WHAT IT DOES NOT SEE
--------------------
* A file read or rewritten through the shell (`cat`, `grep`, `sed -i`, a
  script): the transcript keeps the command, not what arrived. A shell read is
  no read; a shell rewrite shows up as an outside change.
* A Read and an Edit of the same file in the SAME turn: the Read's result may
  not be in the transcript yet (the lag above), so the hook may warn once too
  often. Assumed, and visible in the receipts.
* `NotebookEdit`: a Read of a notebook reports no coverage, so it would warn
  every time. Left out on purpose.
* Any runtime other than Claude Code. Its transcript is the only shape that was
  measured, so it is the only one this hook reads.

MODES AND FAIL-OPEN
-------------------
`READ_ORDER_HOOK_MODE=warn` (default) injects the text and exits 0.
`READ_ORDER_HOOK_MODE=block` writes it to stderr and exits 2, which Claude Code
treats as a denial. Block is shipped so the switch exists; it is not the
default, because the measured base rate is nine sessions in ten and a guard
that stops nine sessions in ten is uninstalled by the eleventh.
Any error of the hook's own — a transcript it cannot parse, a gate it cannot
import — is a receipt with `verdict: error` and exit 0. This hook is never the
reason a session cannot proceed.

Tests: tests/test_read_before_edit_hook.py · mutants: tests/mutation_check_read_before_edit.py
"""
from __future__ import annotations

import datetime as _dt
import fnmatch
import importlib.util
import json
import os
import pathlib
import re
import sys
import tempfile
import time

HERE = pathlib.Path(__file__).resolve().parent
GATE = pathlib.Path(os.environ.get("READ_ORDER_GATE") or HERE.parent / "gate" / "read_order.py")


def _default_receipts() -> str:
    state = os.environ.get("XDG_STATE_HOME") or os.path.join(os.path.expanduser("~"), ".local", "state")
    return os.path.join(state, "empirical-harness", "read-order-receipts.jsonl")


RECEIPTS = os.environ.get("READ_ORDER_RECEIPTS") or _default_receipts()
MODE = os.environ.get("READ_ORDER_HOOK_MODE", "warn")            # warn | block
TOOLS = {"Edit", "Write", "MultiEdit"}
WARNS = {"never-read", "partial", "no-total", "stale"}
# mutation-anchor: WARNS
MTIME_TOLERANCE_S = 2.0
# mutation-anchor: MTIME_TOLERANCE_S
OWN_EDIT_GRACE_S = 5.0
# mutation-anchor: OWN_EDIT_GRACE_S
RECEIPT_TAIL_BYTES = 262_144
COMPACT_MARK = "isCompactSummary"
# mutation-anchor: COMPACT_MARK
# The runtime writes compact JSON (`"isCompactSummary":true`); a transcript rewritten by
# another tool may carry a space after the colon. Both spellings count.
COMPACT_RE = re.compile(r'"isCompactSummary"\s*:\s*true')


# --- receipts ----------------------------------------------------------------

def receipt(**row: object) -> None:
    """One JSON line per run. Paths and counts only — never file contents."""
    if RECEIPTS == "off":
        return
    try:
        pathlib.Path(RECEIPTS).parent.mkdir(parents=True, exist_ok=True)
        row = {"ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), **row}
        with open(RECEIPTS, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
    except Exception:  # noqa: BLE001 — a receipt never brings the hook down
        pass


def last_own_receipt(session: str, path: str) -> float | None:
    """Epoch of this session's last receipt for this file — only the verdicts that precede
    a real edit. Reads the tail of the receipts file, not all of it."""
    if RECEIPTS == "off":
        return None
    try:
        with open(RECEIPTS, "rb") as fh:
            fh.seek(0, os.SEEK_END)
            fh.seek(max(0, fh.tell() - RECEIPT_TAIL_BYTES))
            tail = fh.read().decode("utf-8", errors="replace")
    except OSError:
        return None
    for raw in reversed(tail.split("\n")):
        if session not in raw or path not in raw:
            continue
        try:
            r = json.loads(raw)
        except ValueError:
            continue                                   # a line cut by the tail seek
        if (r.get("session") == session and r.get("path") == path
                and r.get("verdict") in ("ok", "partial", "never-read", "stale", "no-total")):
            try:
                return _dt.datetime.fromisoformat(str(r["ts"]).replace("Z", "+00:00")).timestamp()
            except (KeyError, ValueError):
                return None
    return None


# --- the gate ----------------------------------------------------------------

def load_gate(allowlist: pathlib.Path):
    """Import the gate by path. `HARNESS_ROOT=/` BEFORE the import: the gate fixes its
    root when it loads, and `rel()` has to spell the transcript's paths and the payload's
    path the same way."""
    os.environ["HARNESS_ROOT"] = "/"
    spec = importlib.util.spec_from_file_location("empirical_read_order_gate", GATE)
    mod = importlib.util.module_from_spec(spec)
    # Registered BEFORE execution: the gate uses `from __future__ import annotations` and
    # its dataclasses resolve annotations by looking the module up in sys.modules.
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    mod.ALLOWLIST = allowlist
    return mod


# --- the transcript ----------------------------------------------------------

def rows_for(transcript: pathlib.Path, needle: str) -> tuple[pathlib.Path, pathlib.Path | None]:
    """Copy to a scratch file ONLY the rows that mention the file. The gate's adapter reads
    a whole transcript (tens of MB, possibly); one file is what matters here. Rows are split
    at "\\n" while iterating (never `splitlines()`, which also breaks at U+2028).

    Returns (rows after the last context compaction, rows before it or None). The rows
    before only serve the warning text: what this session did with the file back then."""
    kept: list[tuple[int, str]] = []
    last_compact = 0
    with transcript.open(encoding="utf-8", errors="replace") as fh:
        for n, line in enumerate(fh, 1):
            if COMPACT_MARK in line and COMPACT_RE.search(line):
                last_compact = n
            if needle in line:
                kept.append((n, line))

    def dump(rows: list[tuple[int, str]]) -> pathlib.Path:
        tmp = tempfile.NamedTemporaryFile("w", suffix=".jsonl", delete=False, encoding="utf-8")
        with tmp:
            for _, line in rows:
                tmp.write(line if line.endswith("\n") else line + "\n")
        return pathlib.Path(tmp.name)

    after = dump([r for r in kept if r[0] > last_compact])
    before = dump([r for r in kept if r[0] <= last_compact]) if last_compact else None
    return after, before


def evidence_by_line(tmp: pathlib.Path) -> dict[int, dict]:
    """Per line of the scratch file: the row's `ts` (epoch) and, for an Edit result, its
    `structuredPatch` hunks. The gate's adapter numbers its events by that same line
    (`Event.n`), so this adds what the adapter does not carry."""
    out: dict[int, dict] = {}
    for n, raw in enumerate(tmp.read_text(encoding="utf-8", errors="replace").split("\n"), 1):
        if not raw.strip():
            continue
        try:
            row = json.loads(raw)
        except ValueError:
            continue
        info: dict = {}
        ts = row.get("timestamp")
        if isinstance(ts, str):
            try:
                info["ts"] = _dt.datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()
            except ValueError:
                pass
        tur = row.get("toolUseResult")
        if isinstance(tur, dict) and isinstance(tur.get("structuredPatch"), list):
            info["patch"] = tur["structuredPatch"]
        if info:
            out[n] = info
    return out


# --- the predicate -----------------------------------------------------------

def remap(windows: list[tuple[int, int]], hunks: list) -> list[tuple[int, int]] | None:
    """Windows in the file's coordinates BEFORE an edit → coordinates AFTER it, exactly.

    Walks every hunk line by line (' ' context: old line x becomes new line y and is kept
    only if a window held it; '-' deleted: gone; '+' added: written by this session, counts
    as seen). Outside the hunks, the accumulated shift applies. Returns None when the patch
    is not the shape expected; the caller then invalidates the windows."""
    seen_old: set[int] = set()
    for a, b in windows:
        seen_old.update(range(a, b + 1))
    new_seen: set[int] = set()
    old_pos = 1          # next old line not yet consumed
    new_pos = 1          # next new line not yet emitted
    try:
        for h in sorted(hunks, key=lambda h: int(h["oldStart"])):
            old_start, new_start = int(h["oldStart"]), int(h["newStart"])
            lines = h["lines"]
            if not isinstance(lines, list):
                return None
            # A hunk with no old lines (pure insertion) starts at the line BEFORE it.
            if int(h.get("oldLines", 0)) == 0:
                old_start += 1
            if int(h.get("newLines", 0)) == 0:
                new_start += 1
            shift = new_pos - old_pos
            for x in range(old_pos, old_start):
                if x in seen_old:
                    new_seen.add(x + shift)
            old_pos, new_pos = old_start, new_start
            for ln in lines:
                tag = ln[:1]
                if tag == " ":
                    if old_pos in seen_old:
                        new_seen.add(new_pos)
                    old_pos += 1
                    new_pos += 1
                elif tag == "-":
                    old_pos += 1
                elif tag == "+":
                    new_seen.add(new_pos)
                    new_pos += 1
                elif tag == "\\":       # "\\ No newline at end of file"
                    continue
                else:
                    return None
    except (KeyError, TypeError, ValueError):
        return None
    shift = new_pos - old_pos
    for x in sorted(seen_old):
        if x >= old_pos:
            new_seen.add(x + shift)
    out: list[tuple[int, int]] = []
    for x in sorted(new_seen):
        if out and out[-1][1] == x - 1:
            out[-1] = (out[-1][0], x)
        else:
            out.append((x, x))
    return out
# mutation-anchor: remap


def real_lines(content: str) -> int:
    """Lines a text really has: the "\\n"-separated segments, not counting an empty last one."""
    if not content:
        return 0
    return content.count("\n") + (0 if content.endswith("\n") else 1)


def prove_phantom(rows: list, path: str, disk: str) -> None:
    """Drop the phantom line from reads whose `total` is exactly (real lines + 1) while the
    file on disk ends in a newline. Only then: same version, exact proof."""
    if not disk.endswith("\n"):
        return
    real = real_lines(disk)
    for _, r in rows:
        if r.get("op") != "read" or r.get("path") != path or r.get("full"):
            continue
        start, lines, total = r.get("start", 1), r.get("lines"), r.get("total")
        if type(total) is int and type(start) is int and type(lines) is int and total == real + 1:
            r["total"] = real
            r["lines"] = max(0, min(lines, real - start + 1))
# mutation-anchor: prove_phantom


def replay_one(events, ro, path: str, evidence: dict[int, dict]) -> dict:
    """The predicate, for ONE file. See the header: exact remapping, no leniency."""
    st = {"windows": [], "total": None, "known": False, "reads": 0, "own_change": False,
          "stale": False, "edits": 0, "delta_known": True, "last_ts": None}
    for ev in events:
        if ev.path != path:
            continue
        info = evidence.get(ev.n, {})
        if "ts" in info:
            st["last_ts"] = info["ts"]
        if ev.op == ro.READ:
            st["reads"] += 1
            if ev.full:
                st["known"], st["own_change"], st["delta_known"] = True, False, True
                continue
            if ev.total != st["total"] and st["total"] is not None and ev.total is not None:
                if not st["own_change"]:
                    # nothing this session did explains it: the earlier reading is of another file
                    st["windows"], st["known"], st["stale"] = [], False, True
                elif not st["delta_known"] or abs(ev.total - st["total"]) > 1:
                    # own edits without a patch, or the remap does not add up: keep nothing
                    st["windows"] = []
                # |difference| <= 1 with a complete remap: the phantom line; the windows hold
            st["own_change"], st["delta_known"] = False, True
            st["total"] = ev.total
            if ev.lines:
                st["windows"].append((ev.start, ev.start + ev.lines - 1))
            if ro.covers(st["windows"], st["total"]):
                st["known"], st["stale"] = True, False
        elif ev.op == ro.CREATE:
            st["known"] = True
        else:
            st["edits"] += 1
            st["own_change"] = True
            if ev.op == ro.WRITE:
                st["known"], st["windows"] = True, []
                continue
            patch = info.get("patch")
            mapped = remap(st["windows"], patch) if isinstance(patch, list) else None
            if mapped is None:
                st["delta_known"] = False          # invalidated at the next read
            else:
                st["windows"] = mapped
                if st["total"] is not None:
                    st["total"] += sum(int(h.get("newLines", 0)) - int(h.get("oldLines", 0))
                                       for h in patch)
                if st["known"] is False and ro.covers(st["windows"], st["total"]):
                    st["known"] = True
    return st


def changed_outside(target: str, st: dict, prev_receipt: float | None) -> bool:
    """Did the file change on disk AFTER the last event this session logged for it, in a way
    that an own edit whose result has not reached the transcript yet does not explain?"""
    if st["last_ts"] is None:
        return False
    try:
        mtime = os.path.getmtime(target)
    except OSError:
        return False
    if mtime <= st["last_ts"] + MTIME_TOLERANCE_S:
        return False
    if prev_receipt is not None and mtime <= prev_receipt + OWN_EDIT_GRACE_S:
        return False                                   # the edit that followed the last receipt
    return True


def verdict_for(st: dict, ro, outside: bool) -> tuple[str, int, int | None]:
    seen = ro.lines_seen(st["windows"])
    if outside:
        return "stale", seen, st["total"]
    if st["known"]:
        return "ok", seen, st["total"]
    if st["stale"]:
        return "stale", seen, st["total"]
    if not st["reads"]:
        return "never-read", 0, None
    if st["total"] is None:
        return "no-total", seen, None
    return "partial", seen, st["total"]


# --- the warning -------------------------------------------------------------

def safe_name(path: str) -> str:
    """The base name without what could break the warning's markdown or smuggle text shaped
    like an instruction (backticks, line breaks, control characters). The agent already saw
    the name in its own tool input; this is depth, not a boundary."""
    base = os.path.basename(path)
    return re.sub(r"[`\r\n\t\x00-\x1f\x7f]", "?", base)[:120]


def message(verdict: str, path: str, seen: int, total: int | None, windows,
            before_ops: list[str]) -> str:
    base = safe_name(path)
    if verdict == "never-read":
        if before_ops:
            done = ", ".join(sorted({{"read": "read", "edit": "edited", "write": "overwrote",
                                     "create": "created"}.get(o, o) for o in before_ops}))
            cause = (f"you are about to edit `{base}`, and everything this session did with it "
                     f"({done}) happened BEFORE the last context compaction. It is no longer in "
                     "your context, so for this rule it was not read")
        else:
            cause = (f"you are about to edit `{base}` with no `Read` of it in this session. A "
                     "grep, a `cat` or a `sed` through the shell does not count: the log keeps "
                     "the command, not what arrived")
    elif verdict == "stale":
        cause = (f"`{base}` changed on disk AFTER the last thing you did with it in this session, "
                 "outside the log (a shell command, a formatter, another session). What you read "
                 "no longer describes this file")
    elif verdict == "no-total":
        cause = (f"the only reads of `{base}` report no length (pdf, notebook, multi-part): "
                 "there is no record of you having seen all of it")
    else:
        vis = ", ".join(f"{a}-{b}" for a, b in sorted(windows)[:6]) or "none"
        cause = (f"you are about to edit `{base}` having read {seen} of {total} lines in this "
                 f"session (windows: {vis}). What the window left out may hold the invariant "
                 "this change breaks — that is exactly how an invariant was once copied in half")
    return (f"read-order: {cause}. Rule: read the file in full before you change it. Read what is "
            "missing (consecutive blocks until every line was seen) and edit again. If a full "
            "read is genuinely not the point (a generated file, a log), add a glob with a "
            "`# reason` to .conduct/read-order-allow.txt. Warning mode: this edit is NOT blocked.")


def ops_before(ro, before: pathlib.Path | None, target: str) -> list[str]:
    """What this session did with the file BEFORE the compaction (warning text only)."""
    if before is None:
        return []
    try:
        rows, _ = ro.from_claude_transcript(before)
        return [r["op"] for _, r in rows
                if isinstance(r.get("path"), str) and os.path.realpath(r["path"]) == target]
    except Exception:  # noqa: BLE001 — help text; if it cannot be had, it is left out
        return []


# --- main --------------------------------------------------------------------

def main() -> int:
    t0 = time.time()
    payload = json.loads(sys.stdin.read() or "{}")
    tool = payload.get("tool_name")
    if tool not in TOOLS:
        return 0
    given = payload.get("tool_input") or {}
    target = given.get("file_path") or ""
    if not target:
        return 0
    cwd = payload.get("cwd") or os.getcwd()
    if not os.path.isabs(target):
        target = os.path.join(cwd, target)
    target = os.path.normpath(target)
    session = str(payload.get("session_id") or "")[:8]
    common = {"session": session, "tool": tool, "path": target, "mode": MODE}

    if not os.path.isfile(target):
        receipt(verdict="create", **common)          # Write creates; Edit will fail by itself
        return 0
    transcript = pathlib.Path(str(payload.get("transcript_path") or ""))
    if not transcript.is_file():
        receipt(verdict="no-transcript", **common)
        return 0

    allow = pathlib.Path(os.environ.get("READ_ORDER_ALLOWLIST")
                         or os.path.join(cwd, ".conduct", "read-order-allow.txt"))
    ro = load_gate(allow)
    for glob in ro.load_allowlist():
        if fnmatch.fnmatch(target, glob) or fnmatch.fnmatch(os.path.basename(target), glob):
            receipt(verdict="allow", glob=glob, **common)
            return 0

    after, before = rows_for(transcript, os.path.basename(target))
    try:
        rows, _ = ro.from_claude_transcript(after)
        evidence = evidence_by_line(after)
    finally:
        after.unlink(missing_ok=True)
    # One spelling per file: `/tmp/x` and `/private/tmp/x` are the same file on macOS, and
    # the agent may read through one and edit through the other. `rel()` normalises; it
    # does not resolve links.
    target = os.path.realpath(target)
    for _, r in rows:
        if isinstance(r.get("path"), str) and r["path"]:
            r["path"] = os.path.realpath(r["path"])
    path = ro.rel(target)
    try:
        disk = pathlib.Path(target).read_text(encoding="utf-8", errors="replace")
    except OSError:
        disk = ""
    prove_phantom(rows, target, disk)      # rows still carry absolute paths; `rel` is parse_events'
    events, _ = ro.parse_events(rows)
    st = replay_one(events, ro, path, evidence)
    outside = changed_outside(target, st, last_own_receipt(session, target))
    verdict, seen, total = verdict_for(st, ro, outside)
    compacted = before is not None
    before_ops = ops_before(ro, before, target) if (compacted and verdict == "never-read") else []
    if before is not None:
        before.unlink(missing_ok=True)
    ms = int((time.time() - t0) * 1000)
    receipt(verdict=verdict, seen=seen, total=total, reads=st["reads"], edits=st["edits"],
            compacted=compacted, outside=outside, ms=ms, **common)
    if verdict not in WARNS:
        return 0
    text = message(verdict, target, seen, total, st["windows"], before_ops)
    if MODE == "block":
        sys.stderr.write(text.replace("Warning mode: this edit is NOT blocked.",
                                      "Block mode: this edit was not applied.") + "\n")
        return 2
    print(json.dumps({"hookSpecificOutput": {"hookEventName": "PreToolUse",
                                             "additionalContext": text}}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:  # noqa: BLE001 — fail open on purpose: the hook is never the blocker
        receipt(verdict="error", error=f"{type(exc).__name__}: {exc}"[:200])
        sys.exit(0)
