"""Tests for the live hook, hooks/read-before-edit.py.

Synthetic transcripts in the shape measured on Claude Code 2.1.274 (2026-09-17):
`Read` results carry `file.{filePath,numLines,startLine,totalLines}`, `Edit`
results carry `structuredPatch` (and often no `originalFile`), `Write` results
carry `type: create | update`, a failed call has `is_error` and no dict result,
every row carries a `timestamp`, and a context compaction is a user row with
`isCompactSummary: true`. Real files in a temp dir; the hook is run as a
PreToolUse subprocess with the payload on stdin — the exit code, the stdout
JSON and the receipt are the contract.

    python3 -m pytest tests/test_read_before_edit_hook.py -q
"""

from __future__ import annotations

import datetime
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

# The mutation runner points this at a mutated COPY; the real hook is never rewritten.
HOOK = Path(os.environ.get("READ_BEFORE_EDIT_HOOK_UNDER_TEST")
            or Path(__file__).resolve().parent.parent / "hooks" / "read-before-edit.py")

_ids = iter(range(1, 100_000))


# --- rows in the measured shape ----------------------------------------------

def use(uid: str, tool: str, given: dict[str, Any]) -> dict[str, Any]:
    return {"type": "assistant", "message": {"role": "assistant", "content": [
        {"type": "tool_use", "id": uid, "name": tool, "input": given}]}}


def result(uid: str, payload: Any, failed: bool = False) -> dict[str, Any]:
    block: dict[str, Any] = {"type": "tool_result", "tool_use_id": uid, "content": "…"}
    if failed:
        block["is_error"] = True
    row: dict[str, Any] = {"type": "user", "message": {"role": "user", "content": [block]}}
    if payload is not None:
        row["toolUseResult"] = payload
    return row


def read(path: str, start: int, n: int, total: int) -> list[dict[str, Any]]:
    uid = f"toolu_{next(_ids)}"
    given: dict[str, Any] = {"file_path": path}
    if start > 1:
        given["offset"] = start
    if start + n - 1 < total:
        given["limit"] = n
    return [use(uid, "Read", given),
            result(uid, {"type": "text", "file": {"filePath": path, "content": "x", "numLines": n,
                                                  "startLine": start, "totalLines": total}})]


def failed_read(path: str) -> list[dict[str, Any]]:
    uid = f"toolu_{next(_ids)}"
    return [use(uid, "Read", {"file_path": path}), result(uid, "Error: File does not exist.", failed=True)]


def edit(path: str, patch: list | None = (), tool: str = "Edit") -> list[dict[str, Any]]:
    """`patch=()` → `structuredPatch: []`; `patch=None` → no `structuredPatch` at all."""
    uid = f"toolu_{next(_ids)}"
    payload: dict[str, Any] = {"filePath": path, "oldString": "a", "newString": "b",
                               "originalFile": None, "userModified": False, "replaceAll": False}
    if patch is not None:
        payload["structuredPatch"] = list(patch)
    return [use(uid, tool, {"file_path": path, "old_string": "a", "new_string": "b"}),
            result(uid, payload)]


def write(path: str, kind: str) -> list[dict[str, Any]]:
    uid = f"toolu_{next(_ids)}"
    return [use(uid, "Write", {"file_path": path, "content": "c"}),
            result(uid, {"type": kind, "filePath": path, "content": "c", "structuredPatch": [],
                         "originalFile": None, "userModified": False})]


def hunk(old_start: int, new_start: int, lines: list[str]) -> dict[str, Any]:
    """A unified-diff hunk as the runtime records it: `lines` prefixed ' ' / '-' / '+'."""
    return {"oldStart": old_start, "newStart": new_start,
            "oldLines": sum(1 for l in lines if l[:1] in (" ", "-")),
            "newLines": sum(1 for l in lines if l[:1] in (" ", "+")), "lines": lines}


COMPACTION = {"type": "user", "isCompactSummary": True,
              "message": {"role": "user", "content": "summary"}}


def text(n: int, trailing_newline: bool = True) -> str:
    return "\n".join(f"line {i}" for i in range(1, n + 1)) + ("\n" if trailing_newline else "")


# --- the runner --------------------------------------------------------------

class Session:
    """A temp dir, a transcript being built, and one hook run per call."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.rows: list[dict[str, Any]] = []
        self.transcript = root / "session.jsonl"
        self.receipts = root / "receipts.jsonl"
        self.env: dict[str, str] = {}
        # Rows with no timestamp are dated 120 s in the FUTURE: files are written before
        # the hook runs, and an older row would read as "changed outside". The test that
        # measures exactly that sets `stamp` negative.
        self.stamp = 120

    def file(self, rel: str, content: str) -> str:
        p = self.root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
        return str(p)

    def run(self, tool: str, path: str, transcript: bool = True, raw: str | None = None,
            cwd: str | None = None) -> tuple[int, dict | None, str, dict | None]:
        if transcript:
            when = (datetime.datetime.now(datetime.timezone.utc)
                    + datetime.timedelta(seconds=self.stamp)).strftime("%Y-%m-%dT%H:%M:%S.000Z")
            for r in self.rows:
                r.setdefault("timestamp", when)
            body = raw if raw is not None else "".join(json.dumps(r, ensure_ascii=False) + "\n"
                                                       for r in self.rows)
            self.transcript.write_text(body, encoding="utf-8")
        payload = {"session_id": "test-session", "transcript_path": str(self.transcript),
                   "cwd": cwd or str(self.root), "hook_event_name": "PreToolUse",
                   "tool_name": tool, "tool_use_id": "toolu_x",
                   "tool_input": {"file_path": path, "old_string": "a", "new_string": "b"}}
        env = {**os.environ, "READ_ORDER_RECEIPTS": str(self.receipts), **self.env}
        env.pop("READ_ORDER_HOOK_MODE", None)
        env.update(self.env)
        r = subprocess.run([sys.executable, str(HOOK)], input=json.dumps(payload),
                           capture_output=True, text=True, env=env, check=False, timeout=60)
        out = json.loads(r.stdout) if r.stdout.strip() else None
        rec = None
        if self.receipts.is_file():
            rec = json.loads(self.receipts.read_text(encoding="utf-8").strip().split("\n")[-1])
        return r.returncode, out, r.stderr, rec


def warning(out: dict | None) -> str:
    return ((out or {}).get("hookSpecificOutput") or {}).get("additionalContext") or ""


@pytest.fixture
def s(tmp_path: Path) -> Session:
    return Session(tmp_path)


# --- the control -------------------------------------------------------------

def test_full_read_then_edit_is_silent(s: Session) -> None:
    """Without this, every test below could pass because the hook always warns."""
    p = s.file("a.py", text(200))
    s.rows += read(p, 1, 200, 200)
    rc, out, _, rec = s.run("Edit", p)
    assert rc == 0 and not warning(out), (rc, out)
    assert rec["verdict"] == "ok" and rec["seen"] == 200


# --- the warning -------------------------------------------------------------

def test_a_windowed_read_then_an_edit_warns_and_says_how_much_was_seen(s: Session) -> None:
    p = s.file("a.py", text(200))
    s.rows += read(p, 1, 50, 200)
    rc, out, _, rec = s.run("Edit", p)
    assert rc == 0
    assert "50 of 200 lines" in warning(out) and "NOT blocked" in warning(out)
    assert out["hookSpecificOutput"]["hookEventName"] == "PreToolUse"
    assert "permissionDecision" not in out["hookSpecificOutput"], "the permission flow is not touched"
    assert rec["verdict"] == "partial"


def test_no_read_at_all_warns(s: Session) -> None:
    p = s.file("a.py", text(10))
    rc, out, _, rec = s.run("Edit", p)
    assert rc == 0 and "no `Read` of it" in warning(out)
    assert rec["verdict"] == "never-read"


def test_a_write_that_creates_is_not_an_edit(s: Session) -> None:
    rc, out, _, rec = s.run("Write", str(s.root / "new.py"))
    assert rc == 0 and out is None
    assert rec["verdict"] == "create"


def test_overwriting_an_unread_file_warns(s: Session) -> None:
    p = s.file("a.py", text(10))
    rc, out, _, rec = s.run("Write", p)
    assert rc == 0 and warning(out) and rec["verdict"] == "never-read"


def test_two_windows_with_a_gap_warn_with_the_windows(s: Session) -> None:
    p = s.file("a.py", text(200, trailing_newline=False))
    s.rows += read(p, 1, 100, 200) + read(p, 150, 51, 200)
    _, out, _, rec = s.run("Edit", p)
    assert "1-100, 150-200" in warning(out)
    assert rec["verdict"] == "partial" and rec["seen"] == 151


def test_a_read_cut_short_by_the_token_cap_warns(s: Session) -> None:
    p = s.file("a.py", text(1000, trailing_newline=False))
    s.rows += read(p, 1, 300, 1000)
    _, out, _, rec = s.run("Edit", p)
    assert "300 of 1000 lines" in warning(out) and rec["verdict"] == "partial"


def test_a_failed_read_read_nothing(s: Session) -> None:
    p = s.file("a.py", text(10))
    s.rows += failed_read(p)
    _, out, _, rec = s.run("Edit", p)
    assert warning(out) and rec["verdict"] == "never-read"


def test_multiedit_is_judged_too(s: Session) -> None:
    p = s.file("a.py", text(10))
    _, out, _, rec = s.run("MultiEdit", p)
    assert warning(out) and rec["verdict"] == "never-read" and rec["tool"] == "MultiEdit"


# --- the phantom last line, proven on disk -----------------------------------

def test_consecutive_blocks_over_a_newline_terminated_file_are_a_full_read(s: Session) -> None:
    """199 real lines ending in a newline; the runtime reports 200; blocks 1-100 and 101-199."""
    p = s.file("a.py", text(199, trailing_newline=True))
    s.rows += read(p, 1, 100, 200) + read(p, 101, 99, 200)
    _, out, _, rec = s.run("Edit", p)
    assert not warning(out), out
    assert rec["verdict"] == "ok" and rec["total"] == 199


def test_the_same_blocks_over_a_file_with_a_real_last_line_are_one_short(s: Session) -> None:
    p = s.file("a.py", text(200, trailing_newline=False))
    s.rows += read(p, 1, 100, 200) + read(p, 101, 99, 200)
    _, out, _, rec = s.run("Edit", p)
    assert "199 of 200" in warning(out) and rec["verdict"] == "partial"


def test_an_empty_file_read_is_a_full_read(s: Session) -> None:
    p = s.file("empty.py", "")
    s.rows += read(p, 1, 0, 0)
    _, out, _, rec = s.run("Edit", p)
    assert not warning(out) and rec["verdict"] == "ok"


# --- windows through the session's own edits ---------------------------------

def test_own_edits_after_a_full_read_do_not_unknow_the_file(s: Session) -> None:
    p = s.file("a.py", text(200))
    s.rows += read(p, 1, 200, 200) + edit(p)
    _, out, _, rec = s.run("Edit", p)
    assert not warning(out) and rec["verdict"] == "ok"


def test_windows_are_remapped_through_an_insertion(s: Session) -> None:
    """Read 1-120 of 913 → insert two lines at 60 → read 121-915: every line was seen."""
    p = s.file("a.py", text(915, trailing_newline=False))
    ins = hunk(57, 57, [" a", " b", " c", "+x", "+y", " d", " e", " f"])
    s.rows += read(p, 1, 120, 913) + edit(p, patch=[ins]) + read(p, 121, 795, 915)
    _, out, _, rec = s.run("Edit", p)
    assert not warning(out), out
    assert rec["verdict"] == "ok" and rec["seen"] == 915


def test_a_deletion_does_not_launder_lines_never_seen(s: Session) -> None:
    """The sequence that refuted the first version: read 1-50 of 100, delete the first 50,
    read 26-50 of the 50 that remain. Lines 1-25 of the current file were never seen."""
    p = s.file("a.py", text(50, trailing_newline=False))
    wipe = hunk(1, 1, [f"-line {i}" for i in range(1, 51)] + [" k", " l", " m"])
    s.rows += read(p, 1, 50, 100) + edit(p, patch=[wipe]) + read(p, 26, 25, 50)
    _, out, _, rec = s.run("Edit", p)
    assert "25 of 50" in warning(out), (out, rec)
    assert rec["verdict"] == "partial" and rec["seen"] == 25


def test_an_own_edit_with_no_patch_keeps_nothing(s: Session) -> None:
    p = s.file("a.py", text(915, trailing_newline=False))
    s.rows += read(p, 1, 120, 913) + edit(p, patch=None) + read(p, 121, 795, 915)
    _, out, _, rec = s.run("Edit", p)
    assert "795 of 915" in warning(out) and rec["verdict"] == "partial"


# --- the file changed underneath the session ---------------------------------

def test_a_length_change_with_no_own_edit_in_the_log_is_stale(s: Session) -> None:
    p = s.file("a.py", text(260, trailing_newline=False))
    s.rows += read(p, 1, 200, 200) + read(p, 1, 50, 260)
    _, out, _, rec = s.run("Edit", p)
    assert "outside the log" in warning(out) and rec["verdict"] == "stale"


def test_a_newer_mtime_than_the_last_logged_event_is_stale(s: Session) -> None:
    """Same line count, other content — the case the disk proof alone cannot see."""
    p = s.file("a.py", text(199, trailing_newline=True))
    s.stamp = -120                                     # the read was two minutes BEFORE the mtime
    s.rows += read(p, 1, 199, 200)
    _, out, _, rec = s.run("Edit", p)
    assert "changed on disk AFTER" in warning(out)
    assert rec["verdict"] == "stale" and rec["outside"] is True


def test_an_own_edit_whose_result_has_not_reached_the_transcript_is_not_stale(s: Session) -> None:
    """The measured lag: this hook already ran for the file three seconds ago, and the mtime
    is that edit's. Its own receipt tells it so."""
    p = s.file("a.py", text(10))
    s.stamp = -120
    s.rows += read(p, 1, 10, 10)
    prev = (datetime.datetime.now(datetime.timezone.utc)
            - datetime.timedelta(seconds=3)).strftime("%Y-%m-%dT%H:%M:%SZ")
    s.receipts.write_text(json.dumps({"ts": prev, "session": "test-ses", "verdict": "ok",
                                      "path": os.path.realpath(p)}) + "\n", encoding="utf-8")
    _, out, _, rec = s.run("Edit", p)
    assert not warning(out) and rec["verdict"] == "ok" and rec["outside"] is False


# --- context compaction --------------------------------------------------------

def test_a_read_before_a_compaction_no_longer_counts(s: Session) -> None:
    p = s.file("a.py", text(10))
    s.rows += read(p, 1, 10, 10) + [dict(COMPACTION)]
    _, out, _, rec = s.run("Edit", p)
    assert "BEFORE the last context compaction" in warning(out)
    assert rec["verdict"] == "never-read" and rec["compacted"] is True


def test_a_file_created_before_a_compaction_says_so(s: Session) -> None:
    p = s.file("a.py", text(10))
    s.rows += write(p, "create") + [dict(COMPACTION)]
    _, out, _, rec = s.run("Edit", p)
    assert "created" in warning(out) and "no `Read` of it" not in warning(out)
    assert rec["verdict"] == "never-read"


def test_a_read_after_the_compaction_counts(s: Session) -> None:
    p = s.file("a.py", text(10))
    s.rows += read(p, 1, 3, 10) + [dict(COMPACTION)] + read(p, 1, 10, 10)
    _, out, _, rec = s.run("Edit", p)
    assert not warning(out) and rec["verdict"] == "ok" and rec["compacted"] is True


# --- paths -------------------------------------------------------------------

def test_the_same_basename_elsewhere_does_not_vouch_for_the_target(s: Session) -> None:
    other = s.file("other/a.py", text(10))
    p = s.file("a.py", text(10))
    s.rows += read(other, 1, 10, 10)
    _, out, _, rec = s.run("Edit", p)
    assert warning(out) and rec["verdict"] == "never-read"


def test_a_symlinked_spelling_and_the_real_one_are_one_file(s: Session) -> None:
    p = s.file("real/a.py", text(10))
    link = s.root / "link"
    os.symlink(s.root / "real", link)
    s.rows += read(str(link / "a.py"), 1, 10, 10)
    _, out, _, rec = s.run("Edit", p)
    assert not warning(out) and rec["verdict"] == "ok"


def test_a_relative_file_path_is_resolved_against_cwd(s: Session) -> None:
    p = s.file("sub/a.py", text(10))
    s.rows += read(p, 1, 10, 10)
    _, out, _, rec = s.run("Edit", "sub/a.py", cwd=str(s.root))
    assert not warning(out) and rec["verdict"] == "ok"


# --- the allowlist -----------------------------------------------------------

def test_an_exemption_with_a_reason_is_honoured(s: Session) -> None:
    p = s.file("gen/big.lock", text(10))
    (s.root / ".conduct").mkdir()
    (s.root / ".conduct" / "read-order-allow.txt").write_text("*.lock   # generated, never hand-read\n",
                                                             encoding="utf-8")
    rc, out, _, rec = s.run("Edit", p)
    assert rc == 0 and out is None and rec["verdict"] == "allow" and rec["glob"] == "*.lock"


def test_an_exemption_with_no_reason_is_not_honoured_and_the_hook_fails_open(s: Session) -> None:
    p = s.file("gen/big.lock", text(10))
    (s.root / ".conduct").mkdir()
    (s.root / ".conduct" / "read-order-allow.txt").write_text("*.lock\n", encoding="utf-8")
    rc, out, err, rec = s.run("Edit", p)
    assert rc == 0 and out is None, (rc, out, err)
    assert rec["verdict"] == "error" and "no reason" in rec["error"]


# --- fail-open ---------------------------------------------------------------

def test_other_tools_are_ignored_without_a_receipt(s: Session) -> None:
    p = s.file("a.py", text(10))
    rc, out, _, rec = s.run("Bash", p)
    assert rc == 0 and out is None and rec is None


def test_notebook_edit_is_left_out(s: Session) -> None:
    p = s.file("a.ipynb", "{}")
    rc, out, _, rec = s.run("NotebookEdit", p)
    assert rc == 0 and out is None and rec is None


def test_a_missing_transcript_is_a_receipt_not_a_crash(s: Session) -> None:
    p = s.file("a.py", text(10))
    rc, out, _, rec = s.run("Edit", p, transcript=False)
    assert rc == 0 and out is None and rec["verdict"] == "no-transcript"


def test_a_corrupt_row_that_mentions_the_file_fails_open(s: Session) -> None:
    p = s.file("a.py", text(10))
    raw = "".join(json.dumps(r) + "\n" for r in read(p, 1, 10, 10)) + f'{{"broken": "{p}"\n'
    rc, out, err, rec = s.run("Edit", p, raw=raw)
    assert rc == 0 and out is None, (rc, out, err)
    assert rec["verdict"] == "error" and "not JSON" in rec["error"]


def test_a_missing_gate_fails_open_with_a_receipt(s: Session) -> None:
    p = s.file("a.py", text(10))
    s.env["READ_ORDER_GATE"] = str(s.root / "no-such-gate.py")
    rc, out, _, rec = s.run("Edit", p)
    assert rc == 0 and out is None and rec["verdict"] == "error"


def test_block_mode_exits_2_with_the_text_on_stderr(s: Session) -> None:
    p = s.file("a.py", text(10))
    s.env["READ_ORDER_HOOK_MODE"] = "block"
    rc, out, err, rec = s.run("Edit", p)
    assert rc == 2 and out is None and "Block mode" in err
    assert rec["mode"] == "block"


def test_receipts_can_be_switched_off(s: Session) -> None:
    p = s.file("a.py", text(10))
    s.env["READ_ORDER_RECEIPTS"] = "off"
    rc, out, _, _ = s.run("Edit", p)
    assert rc == 0 and warning(out)
    assert not s.receipts.exists()


# --- the text ----------------------------------------------------------------

def test_a_hostile_file_name_is_neutralised_in_the_warning(s: Session) -> None:
    p = s.file("a`b\nc.py", text(10))
    _, out, _, _ = s.run("Edit", p)
    w = warning(out)
    assert "`a?b?c.py`" in w and "\n" not in w.split("read-order: ")[1][:40]


def test_the_warning_stays_far_below_the_runtime_cap(s: Session) -> None:
    p = s.file("a.py", text(500, trailing_newline=False))
    for start in range(1, 500, 10):
        s.rows += read(p, start, 5, 500)               # fifty windows
    _, out, _, _ = s.run("Edit", p)
    assert 0 < len(warning(out)) < 2000               # the runtime caps hook output at 10,000


def test_the_receipt_carries_what_the_measurement_needs(s: Session) -> None:
    p = s.file("a.py", text(10))
    s.rows += read(p, 1, 10, 10)
    _, _, _, rec = s.run("Edit", p)
    for key in ("ts", "session", "path", "verdict", "ms", "reads", "edits", "compacted", "outside"):
        assert key in rec, (key, rec)
