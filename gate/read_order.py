#!/usr/bin/env python3
"""The Empirical Harness order gate: nothing is edited, and nothing is claimed,
before it was read in full.

    python3 gate/read_order.py --toollog .conduct/toollog.jsonl
    python3 gate/read_order.py --claude-transcript SESSION.jsonl --report HANDBACK.md
    python3 gate/read_order.py --toollog LOG.jsonl --sarif read-order.sarif

Exit codes are the contract shared by the conduct-harness family:

    0   no findings
    1   findings — something was changed, or spoken about, on a partial reading
    2   the gate itself failed

The third one is not decoration. A checker that returns 1 when it crashed reads
as "I found something"; one that returns 0 reads as "clean" and fails OPEN. A
toollog this gate cannot read is not a clean session — it is no evidence at all.

WHY THIS GATE EXISTS
--------------------
The citation gate next door asks whether a quotation traces to its source. This
one asks the same question about the agent's own work: does the change, or the
sentence in the hand-back, trace to a file that was actually read — all of it?

Two incidents, both real, neither catchable by looking at the diff:

  * A file was copied from with a first window of 110 lines and a second window
    starting at line 200. The invariant lived in lines 111-199. Nothing failed;
    the copy was simply wrong, and the window never said what it had left out.
  * A page was reported missing from a project whose README documented exactly
    where it was. The README had been searched, not read.

Both are ORDER defects. The diff is fine, the report is fluent, and the only
place the defect is visible is the sequence of tool calls: the edit came before
the read was complete, or the claim came with no complete read at all. That is
a predicate over a log, which is why it can be checked by arithmetic instead of
by opinion.

WHAT IT CHECKS
--------------
    1 edit-before-full-read   an existing file is edited or overwritten before
                              the session has read every line of it
    2 claim-on-partial-read   (--report) the hand-back speaks about a file the
                              session read only through a window
    3 claim-on-unread         (--report --strict-claims) the hand-back names a
                              file in the tree the session never opened

Rule 3 is opt-in because a report may point at a file without asserting
anything about it, and no tokenizer can tell the two apart.

THE ORDER IS THE LINE ORDER
---------------------------
A read that arrives after the edit redeems nothing. Events are judged in the
order they appear in the log, never re-sorted by a timestamp field: two tool
calls can share a millisecond, and a clock is one more thing that can be wrong.

THE TOOLLOG FORMAT
------------------
One JSON object per line. Anything a runtime can write, it can write this:

    {"op": "read",   "path": "src/a.py", "start": 1, "lines": 120, "total": 120}
    {"op": "read",   "path": "src/b.py", "full": true}
    {"op": "edit",   "path": "src/a.py"}
    {"op": "write",  "path": "src/c.py"}      # overwrote a file that existed
    {"op": "create", "path": "src/new.py"}    # wrote a file that did not

`start` is 1-based, `lines` is how many lines ARRIVED (not how many were asked
for), `total` is the length of the file at that moment. A file is read in full
when its windows, together, cover 1..total. A read that carries no `total`
proves nothing about completeness and is never rounded up to "full": when the
datum is missing, the default is not the one that means "all good".

Extra keys (`ts`, `tool`, anything else) are carried and ignored. Lines whose
`op` this gate does not judge are counted and said out loud, not dropped.

`--claude-transcript` converts a Claude Code session file into that format. The
shape it parses was MEASURED, not assumed: Claude Code 2.1.272, 2026-09-17, 40
session files, 7,433 Read calls. No other runtime's log has been measured, so
no other adapter ships.

WHAT IT DOES NOT SEE — read this before trusting it
---------------------------------------------------
  * A file read or changed through the shell (`cat`, `sed -i`, a script). The
    log records the command, not what arrived, so a shell read counts as no
    read — which errs toward a finding, not toward silence.
  * A file changed underneath the session by something else.
  * Whether the agent UNDERSTOOD what it read. Reading every line is necessary
    for the rule and nowhere near sufficient.
  * Whether a sentence in the report is a claim or a pointer. Rule 2 fires on
    the mention; a reader decides what the mention meant.

It ships report-only. It judges a session where someone points it at one; in CI
it only proves it still has teeth.
"""

from __future__ import annotations

import argparse
import fnmatch
import json
import os
import re
import sys
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# The root is overridable so the tests can point the gate at a scratch repo. A
# checker that can only ever run on itself cannot be shown to work.
ROOT = Path(os.environ.get("HARNESS_ROOT") or Path(__file__).resolve().parent.parent)
CONDUCT = Path(os.environ["CONDUCT_DIR"]) if os.environ.get("CONDUCT_DIR") else ROOT / ".conduct"
TOOLLOG = CONDUCT / "toollog.jsonl"
ALLOWLIST = CONDUCT / "read-order-allow.txt"

READ, EDIT, WRITE, CREATE = "read", "edit", "write", "create"
JUDGED_OPS = (READ, EDIT, WRITE, CREATE)

# Never walked when --strict-claims looks for files the report names.
SKIP_DIRS = {".git", ".conduct", "node_modules", "__pycache__", ".venv", "venv",
             ".pytest_cache", "dist", "build"}

# Claude Code tools that touch a file's content. Measured, see the docstring.
TRANSCRIPT_TOOLS = {"Read", "Edit", "MultiEdit", "NotebookEdit", "Write"}


class Unreadable(Exception):
    """The evidence could not be read. The gate did not judge: exit 2."""


@dataclass
class Finding:
    check: str
    message: str
    path: str = ""
    line: int = 0


@dataclass
class Event:
    n: int                     # line in the toollog; the order IS this number
    op: str
    path: str
    start: int = 1
    lines: int = 0
    total: int | None = None
    full: bool = False


@dataclass
class FileState:
    windows: list[tuple[int, int]] = field(default_factory=list)
    total: int | None = None
    known: bool = False        # read in full, or written whole by this session
    reads: int = 0
    own_change: bool = False   # the session changed it since the last read
    stale: bool = False        # was known in full, then changed with no edit in the log


# --- paths -------------------------------------------------------------------

def rel(path: str) -> str:
    """One spelling per file: relative to the root when it lives under it.

    A runtime logs absolute paths and a person writes relative ones; without
    this the same file is two files and its windows never add up.
    """
    p = Path(path)
    if not p.is_absolute():
        p = ROOT / p
    norm = Path(os.path.normpath(p))
    try:
        return norm.relative_to(Path(os.path.normpath(ROOT))).as_posix()
    except ValueError:
        return norm.as_posix()


def is_int(v: Any) -> bool:
    return type(v) is int                      # `True` is an int to isinstance


# --- loading -----------------------------------------------------------------

def parse_events(rows: list[tuple[int, dict[str, Any]]]) -> tuple[list[Event], Counter[str]]:
    """Validate neutral toollog rows. Refuses what it cannot read.

    A corrupt receipt can be skipped and reported, because the other receipts
    still mean what they meant. A corrupt line in an ORDER log cannot: the lost
    event may be the read that clears an edit or the edit that needed one, so
    every verdict after it is a guess. Refusing is the only honest output.
    """
    events: list[Event] = []
    ignored: Counter[str] = Counter()
    for n, row in rows:
        op = row.get("op")
        if op not in JUDGED_OPS:
            ignored[str(op)] += 1
            continue
        path = row.get("path")
        if not isinstance(path, str) or not path.strip():
            raise Unreadable(f"toollog line {n}: `{op}` carries no path")
        ev = Event(n, op, rel(path))
        if op == READ:
            ev.full = row.get("full") is True
            if not ev.full:
                start, lines, total = row.get("start", 1), row.get("lines"), row.get("total")
                if not is_int(start) or start < 1 or not is_int(lines) or lines < 0:
                    raise Unreadable(f"toollog line {n}: a windowed read needs integer "
                                     f"`start` >= 1 and `lines` >= 0")
                if total is not None and (not is_int(total) or total < 0):
                    raise Unreadable(f"toollog line {n}: `total` is not a line count")
                if total is not None and lines and start + lines - 1 > total:
                    # A window that overruns the file it came from is not a generous
                    # read, it is a log that contradicts itself — and the cheapest way
                    # there is to fake full coverage. Refused, not rounded down.
                    raise Unreadable(f"toollog line {n}: lines {start}-{start + lines - 1} "
                                     f"of a {total}-line file — the log contradicts itself")
                ev.start, ev.lines, ev.total = start, lines, total
        events.append(ev)
    return events, ignored


def read_jsonl(path: Path, what: str) -> list[tuple[int, dict[str, Any]]]:
    if not path.is_file():
        raise Unreadable(f"no {what} at {path} — no evidence is not a clean session")
    rows: list[tuple[int, dict[str, Any]]] = []
    # Split on "\n" and nothing else. `str.splitlines()` also breaks on U+2028 and
    # U+2029, which are legal INSIDE a JSON string — measured: 4 of 80 real session
    # files carried one, and each was refused as "not JSON" by a gate that had cut
    # the row in half itself.
    for n, raw in enumerate(path.read_text(encoding="utf-8", errors="replace").split("\n"), 1):
        if not raw.strip():
            continue
        try:
            row = json.loads(raw)
        except ValueError as exc:
            raise Unreadable(f"{what} line {n}: not JSON ({exc})") from exc
        if not isinstance(row, dict):
            raise Unreadable(f"{what} line {n}: not a JSON object")
        rows.append((n, row))
    return rows


def load_allowlist() -> list[str]:
    """Globs for files where a full read is not the point — each with its reason.

    An exemption with no reason written next to it is how an allowlist becomes
    the place findings go to disappear, so a bare glob is refused, not honoured.
    """
    if not ALLOWLIST.is_file():
        return []
    globs: list[str] = []
    for n, raw in enumerate(ALLOWLIST.read_text(encoding="utf-8").splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        glob, _, reason = line.partition("#")
        if not reason.strip():
            raise Unreadable(f"{ALLOWLIST.name} line {n}: `{glob.strip()}` has no reason "
                             f"after a `#` — an exemption nobody justified is not honoured")
        globs.append(glob.strip())
    return globs


# --- the Claude Code adapter -------------------------------------------------

def from_claude_transcript(path: Path) -> tuple[list[tuple[int, dict[str, Any]]], Counter[str]]:
    """Turn a Claude Code session file into neutral toollog rows.

    Shape measured on Claude Code 2.1.272 (2026-09-17): a `tool_use` block names
    the tool and its input; the matching `tool_result` row carries
    `toolUseResult`. For a text Read that is `file.{startLine,numLines,totalLines}`
    — and `numLines` is what ARRIVED, so a read cut short by the token cap shows
    up as the window it really was (42 of those in the measured sessions). For a
    Write it is `type: create | update`. A failed call has no dict there at all.

    The event is logged where the RESULT appears, not where the call was made:
    a read has happened when its content arrived.

    ONE NUMBER IS CORRECTED, and only where the correction is exact. This runtime
    counts the empty segment after a file's final newline as a line: measured,
    `totalLines` was the newline count plus one in 1,462 of 1,462 newline-terminated
    files. So an agent that reads a 912-line file in careful consecutive blocks is
    reported as having seen 912 of 913 — a finding against exactly the behaviour
    the rule asks for. The result of an Edit carries the file as it was
    (`originalFile`); when that text ends in a newline AND has exactly the length
    the read reported, the phantom line is proven for that version of the file and
    is taken out of that read's total. The length has to match: a proof about the
    file as it stood at one edit says nothing about a read of some other version.
    Where no edit supplies the proof — a file that was only read, or one too large
    for the runtime to record — the total is left as reported: the adapter cannot
    tell a trailing newline from an unread last line, and does not guess.
    """
    source = read_jsonl(path, "transcript")
    phantom: dict[str, set[int]] = {}
    for _, row in source:
        result = row.get("toolUseResult")
        before = result.get("originalFile") if isinstance(result, dict) else None
        if isinstance(before, str) and before.endswith("\n"):
            phantom.setdefault(str(result.get("filePath")), set()).add(before.count("\n") + 1)

    pending: dict[str, tuple[str, dict[str, Any]]] = {}
    rows: list[tuple[int, dict[str, Any]]] = []
    skipped: Counter[str] = Counter()
    for n, row in source:
        message = row.get("message")
        content = message.get("content") if isinstance(message, dict) else None
        if not isinstance(content, list):
            continue
        for block in content:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "tool_use" and block.get("name") in TRANSCRIPT_TOOLS:
                pending[str(block.get("id"))] = (block["name"], block.get("input") or {})
                continue
            if block.get("type") != "tool_result" or block.get("tool_use_id") not in pending:
                continue
            tool, given = pending.pop(block["tool_use_id"])
            result = row.get("toolUseResult")
            if block.get("is_error") or not isinstance(result, dict):
                skipped["failed call"] += 1        # it read nothing and changed nothing
                continue
            target = (result.get("filePath") or given.get("file_path")
                      or given.get("notebook_path") or "")
            event: dict[str, Any] = {"tool": tool, "path": target}
            if tool == "Read":
                event["op"] = READ
                info = result.get("file")
                if result.get("type") == "text" and isinstance(info, dict):
                    event["path"] = info.get("filePath") or target
                    start, lines, total = (info.get("startLine", 1), info.get("numLines"),
                                           info.get("totalLines"))
                    if total in phantom.get(event["path"], ()) and is_int(total) \
                            and is_int(start) and is_int(lines):
                        total -= 1                 # the empty segment after the last newline
                        lines = max(0, min(lines, total - start + 1))
                    event.update(start=start, lines=lines, total=total)
                elif result.get("type") == "image":
                    event["full"] = True           # an image is delivered whole or not at all
                else:
                    # pdf, notebook, multi-part: how much arrived was NOT MEASURED,
                    # so it is logged as a window of unknown size and proves nothing.
                    event.update(start=1, lines=0)
                    skipped[f"read of type {result.get('type')!r} proves no coverage"] += 1
            elif tool == "Write":
                event["op"] = CREATE if result.get("type") == "create" else WRITE
            else:
                event["op"] = EDIT
            rows.append((n, event))
    return rows, skipped


# --- the predicate -----------------------------------------------------------

def covers(windows: list[tuple[int, int]], total: int | None) -> bool:
    """Do these windows, together, reach every line from 1 to `total`?"""
    if total is None:
        return False
    if total == 0:
        return True
    reach = 0
    for first, last in sorted(windows):
        if first > reach + 1:
            return False
        reach = max(reach, last)
    return reach >= total


def lines_seen(windows: list[tuple[int, int]]) -> int:
    seen, reach = 0, 0
    for first, last in sorted(windows):
        first = max(first, reach + 1)
        if last >= first:
            seen += last - first + 1
            reach = last
    return seen


def replay(events: list[Event], allow: list[str]) -> tuple[dict[str, FileState], list[Finding], int]:
    """CHECK 1 — walk the log in order and ask, at every edit, what had been read.

    One finding per file, however many times it was edited: the defect is the
    unread file, and ten findings for one file is how a report stops being read.
    """
    states: dict[str, FileState] = {}
    offences: dict[str, list[Event]] = {}
    snapshot: dict[str, str] = {}
    exempted = 0
    for ev in events:
        st = states.setdefault(ev.path, FileState())
        if ev.op == READ:
            st.reads += 1
            if ev.full:
                st.known, st.own_change = True, False
                continue
            if ev.total != st.total:
                # The file is not the length it was: the earlier windows describe
                # a file that no longer exists and cannot be added to these.
                st.windows = []
                if st.total is not None and ev.total is not None and not st.own_change:
                    # ...and nothing this session did explains it. Something else
                    # rewrote the file, so having read the OLD one in full is no
                    # longer knowledge of this one. A change of the session's own
                    # making does not count: it wrote those lines, it knows them.
                    st.known, st.stale = False, True
            st.own_change = False
            st.total = ev.total
            if ev.lines:
                st.windows.append((ev.start, ev.start + ev.lines - 1))
            if covers(st.windows, st.total):
                st.known = True
        elif ev.op == CREATE:
            st.known = True                        # the session wrote every line of it
        else:
            if not st.known:
                if any(fnmatch.fnmatch(ev.path, g) for g in allow):
                    exempted += 1
                else:
                    if ev.path not in offences:
                        one_short = (st.total is not None and st.total > 1
                                     and covers(st.windows, st.total - 1))
                        snapshot[ev.path] = (
                            f"after reading {lines_seen(st.windows)} of {st.total} lines"
                            + (" (exactly one short: if this runtime counts the empty segment "
                               "after a final newline as a line, that is the line — the log "
                               "cannot say which)" if one_short else "")
                            + (" — it HAD been read in full, then changed length with no "
                               "edit in this log (something else rewrote it; if that was "
                               "this session's own shell command, the log cannot see it)"
                               if st.stale else "")
                            if st.reads and st.total is not None else
                            "after a read that never said how long the file was"
                            if st.reads else "and never read")
                    offences.setdefault(ev.path, []).append(ev)
            st.own_change = True
            if ev.op == WRITE:
                st.known = True                    # overwritten whole: it is all the session's now

    findings: list[Finding] = []
    for path, evs in offences.items():
        verb = "overwritten" if evs[0].op == WRITE else "edited"
        later = f" (+{len(evs) - 1} later change(s) to the same file)" if len(evs) > 1 else ""
        findings.append(Finding(
            "edit-before-full-read",
            f"`{path}` was {verb} at toollog line {evs[0].n} {snapshot[path]} — "
            + ("the change went into a file this session had not seen"
               if snapshot[path] == "and never read" else
               "a window does not say what it left out, and the rule you never saw still governs")
            + later,
            path, evs[0].n))
    return states, findings, exempted


# --- the report --------------------------------------------------------------

TOKEN = re.compile(r"[\w./\-]+")


def report_tokens(text: str) -> list[tuple[int, str]]:
    """Path-shaped words in the report's OWN voice.

    Fenced blocks and blockquotes are left out: a command shown as an example and
    a line quoted from someone else are not the report asserting anything.
    """
    out: list[tuple[int, str]] = []
    lines = text.split("\n")
    # Pair the fences FIRST. A fence that never closes is a typo, not a code block:
    # toggling on it would hide every sentence after it from CHECK 2, and the run
    # would print "clean" over a report it had stopped reading halfway down.
    inside: set[int] = set()
    opened: tuple[int, str] | None = None
    for i, raw in enumerate(lines):
        marker = raw.strip()[:3]
        if marker not in ("```", "~~~"):
            continue
        if opened is None:
            opened = (i, marker)
        elif marker == opened[1]:
            inside.update(range(opened[0], i + 1))
            opened = None
    for n, raw in enumerate(lines, 1):
        stripped = raw.strip()
        fenced = (n - 1) in inside
        if fenced or stripped.startswith(">"):
            continue
        for m in TOKEN.finditer(re.sub(r"https?://\S+", " ", raw)):
            tok = m.group(0).strip(".-/")
            if "." in tok or "/" in tok:
                out.append((n, tok[2:] if tok.startswith("./") else tok))
    return out


def check_claims(tokens: list[tuple[int, str]], states: dict[str, FileState]) -> list[Finding]:
    """CHECK 2 — the hand-back speaks about a file it read only through a window.

    Driven by the LOG, not by the report: the candidates are the files this
    session opened and never finished. A bare filename counts only when exactly
    one logged file answers to it — `README.md` in a session that opened three
    of them names none of them.
    """
    partial = {p: st for p, st in states.items() if st.reads and not st.known}
    names = Counter(Path(p).name for p in states)
    findings: list[Finding] = []
    for path, st in partial.items():
        base = Path(path).name
        for lineno, tok in tokens:
            if tok == path or (tok == base and names[base] == 1):
                seen = (f"{lines_seen(st.windows)} of {st.total} lines" if st.total is not None
                        else "a window of unknown size")
                findings.append(Finding(
                    "claim-on-partial-read",
                    f"the report speaks about `{path}`, which this session read only in "
                    f"part — {seen}. Consistent-with is not read-in-full",
                    path, lineno))
                break
    return findings


def check_unread_claims(tokens: list[tuple[int, str]], states: dict[str, FileState],
                        exclude: set[str]) -> list[Finding]:
    """CHECK 3 (--strict-claims) — the hand-back names a file nobody opened."""
    tree: set[str] = set()
    for base, dirs, files in os.walk(ROOT):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
        for name in files:
            tree.add((Path(base) / name).relative_to(ROOT).as_posix())
    findings: list[Finding] = []
    reported: set[str] = set()
    for lineno, tok in tokens:
        if tok not in tree or tok in exclude or tok in reported:
            continue
        st = states.get(tok)
        if st is None or (not st.reads and not st.known):
            reported.add(tok)
            findings.append(Finding(
                "claim-on-unread",
                f"the report names `{tok}`, which exists and which this session never "
                f"opened — a search measures a file, it does not read it",
                tok, lineno))
    return findings


# --- output ------------------------------------------------------------------

def to_sarif(findings: list[Finding], report_uri: str) -> dict[str, Any]:
    repo = ROOT.name
    return {
        "$schema": "https://json.schemastore.org/sarif-2.1.0.json",
        "version": "2.1.0",
        "runs": [{
            "tool": {"driver": {
                "name": f"{repo}-read-order",
                "informationUri": f"https://github.com/arnoldwender/{repo}",
                "rules": [{"id": r} for r in sorted({f.check for f in findings})],
            }},
            # `warning`, not `error`: this gate is report-only until its
            # false-positive rate has been measured on real sessions.
            "results": [{
                "ruleId": f.check,
                "level": "warning",
                "message": {"text": f.message},
                "locations": [{"physicalLocation": {
                    "artifactLocation": {
                        "uri": report_uri if f.check.startswith("claim-") else f.path},
                    "region": {"startLine": max(f.line, 1) if f.check.startswith("claim-") else 1},
                }}],
            } for f in findings],
        }],
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0].replace("\n", " "))
    src = ap.add_mutually_exclusive_group()
    src.add_argument("--toollog", metavar="PATH",
                     help=f"neutral toollog (default: {TOOLLOG.relative_to(ROOT) if TOOLLOG.is_relative_to(ROOT) else TOOLLOG})")
    src.add_argument("--claude-transcript", metavar="PATH",
                     help="a Claude Code session .jsonl, converted on the fly")
    ap.add_argument("--emit-toollog", metavar="PATH",
                    help="with --claude-transcript: also write the converted toollog")
    ap.add_argument("--report", metavar="PATH", help="the hand-back to check for claims")
    ap.add_argument("--strict-claims", action="store_true",
                    help="also flag files the report names and the session never opened")
    ap.add_argument("--sarif", metavar="PATH", help="write SARIF 2.1.0 to PATH")
    args = ap.parse_args(argv)

    try:
        skipped: Counter[str] = Counter()
        if args.emit_toollog and not args.claude_transcript:
            raise Unreadable("--emit-toollog converts a transcript; it needs --claude-transcript")
        if args.claude_transcript:
            rows, skipped = from_claude_transcript(Path(args.claude_transcript))
            if args.emit_toollog:
                Path(args.emit_toollog).write_text(
                    "".join(json.dumps(r, ensure_ascii=False) + "\n" for _, r in rows),
                    encoding="utf-8")
        else:
            rows = read_jsonl(Path(args.toollog) if args.toollog else TOOLLOG, "toollog")
        events, ignored = parse_events(rows)
        states, findings, exempted = replay(events, load_allowlist())

        report_uri = "report"
        if args.report:
            report_path = Path(args.report)
            if not report_path.is_file():
                raise Unreadable(f"no report at {args.report}")
            report_uri = report_path.name
            tokens = report_tokens(report_path.read_text(encoding="utf-8", errors="replace"))
            findings += check_claims(tokens, states)
            if args.strict_claims:
                findings += check_unread_claims(tokens, states, {rel(str(report_path))})
        elif args.strict_claims:
            raise Unreadable("--strict-claims needs a --report to read the claims from")
    except Unreadable as exc:
        print(f"gate failure: {exc}", file=sys.stderr)
        return 2
    except Exception as exc:                           # noqa: BLE001
        # Exit 2, never 1 and never 0: the gate broke, it did not judge.
        print(f"gate failure: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2

    if args.sarif:
        Path(args.sarif).write_text(json.dumps(to_sarif(findings, report_uri), indent=2),
                                    encoding="utf-8")

    # COVERAGE, on every run. "0 findings" over a log with no edits in it prints
    # exactly like "0 findings" over a careful session, and only one of them is
    # a verdict. The citation gate next door learned that the expensive way.
    ops = Counter(ev.op for ev in events)
    changed = {ev.path for ev in events if ev.op in (EDIT, WRITE)}
    print(f"read-order: {len(events)} event(s) — " + ", ".join(
        f"{ops[o]} {o}" for o in JUDGED_OPS) + f" · {len(states)} file(s), "
        f"{sum(1 for s in states.values() if s.known)} known in full")
    if ignored:
        print("  not judged: " + ", ".join(f"{n} × op {o!r}" for o, n in sorted(ignored.items())))
    for why, n in sorted(skipped.items()):
        print(f"  adapter: {n} × {why}")
    if exempted:
        print(f"  {exempted} change(s) exempted by {ALLOWLIST.name}, each with a written reason")
    if not changed:
        print("  no existing file was changed in this log — CHECK 1 judged nothing")
    for f in findings:
        where = f"report line {f.line}" if f.check.startswith("claim-") else f.path
        print(f"  FAIL [{f.check}] {where}: {f.message}")
    if findings:
        print(f"\n{len(findings)} finding(s)")
        return 1
    print("  every change, and every file the report speaks about, follows a full read")
    return 0


if __name__ == "__main__":
    sys.exit(main())
