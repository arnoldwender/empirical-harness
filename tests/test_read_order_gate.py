"""Mutation tests for the order gate.

Same treatment as the citation gate's suite: build a log that PASSES, plant the
one defect a rule exists to catch, and require the gate to go red. Every red
test has a sibling that must stay green, because a gate that fires on careful
work is removed within the week and then catches nothing at all.

    python3 -m pytest tests/test_read_order_gate.py -q

The gate is invoked as a subprocess rather than imported: the exit code is part
of the contract the conduct-harness family shares (0 clean, 1 findings, 2 the
gate itself broke), and importing would leave the contract untested.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

# The mutation runner points this at a mutated COPY. It never rewrites the real
# gate: a runner that mutates the file in place and restores it in a `finally`
# leaves the mutant on disk the day it is killed mid-run — which happened here,
# once, before this line existed.
GATE = Path(os.environ.get("READ_ORDER_GATE_UNDER_TEST")
            or Path(__file__).resolve().parent.parent / "gate" / "read_order.py")


def run(root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    env = {**os.environ, "HARNESS_ROOT": str(root)}
    env.pop("CONDUCT_DIR", None)
    return subprocess.run([sys.executable, str(GATE), *args],
                          capture_output=True, text=True, env=env, check=False)


def log(root: Path, *events: dict[str, Any], name: str = "toollog.jsonl") -> Path:
    path = root / ".conduct" / name
    path.parent.mkdir(exist_ok=True)
    path.write_text("".join(json.dumps(e) + "\n" for e in events), encoding="utf-8")
    return path


def read(path: str, start: int, lines: int, total: int | None) -> dict[str, Any]:
    ev: dict[str, Any] = {"op": "read", "path": path, "start": start, "lines": lines}
    if total is not None:
        ev["total"] = total
    return ev


def edit(path: str) -> dict[str, Any]:
    return {"op": "edit", "path": path}


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "a.py").write_text("x = 1\n", encoding="utf-8")
    return tmp_path


# --- the control -------------------------------------------------------------

def test_full_read_then_edit_passes(repo: Path) -> None:
    """Without this, every test below could pass because the gate always fails."""
    log(repo, read("src/a.py", 1, 120, 120), edit("src/a.py"))
    r = run(repo)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "follows a full read" in r.stdout


# --- CHECK 1: edit-before-full-read ------------------------------------------

def test_edit_of_a_file_never_read_is_caught(repo: Path) -> None:
    log(repo, edit("src/a.py"))
    r = run(repo)
    assert r.returncode == 1
    assert "edit-before-full-read" in r.stdout and "never read" in r.stdout


def test_edit_after_a_window_is_caught_and_says_how_much_was_seen(repo: Path) -> None:
    log(repo, read("src/a.py", 1, 110, 309), edit("src/a.py"))
    r = run(repo)
    assert r.returncode == 1
    assert "110 of 309 lines" in r.stdout


def test_a_read_that_arrives_after_the_edit_redeems_nothing(repo: Path) -> None:
    """THE order predicate. The same two events, swapped, are the control above."""
    log(repo, edit("src/a.py"), read("src/a.py", 1, 120, 120))
    assert run(repo).returncode == 1


def test_two_windows_that_meet_are_a_full_read(repo: Path) -> None:
    """The false-positive side: reading a long file in consecutive blocks is
    exactly what the rule asks for, and must not be punished."""
    log(repo, read("src/a.py", 1, 110, 309), read("src/a.py", 111, 199, 309), edit("src/a.py"))
    assert run(repo).returncode == 0


def test_two_windows_with_a_gap_are_not(repo: Path) -> None:
    """The incident this gate exists for: lines 111-199 were never seen."""
    log(repo, read("src/a.py", 1, 110, 309), read("src/a.py", 200, 110, 309), edit("src/a.py"))
    r = run(repo)
    assert r.returncode == 1
    assert "220 of 309 lines" in r.stdout


def test_a_read_with_no_total_never_proves_a_full_read(repo: Path) -> None:
    """When the datum is missing, the default is not the one that means all good."""
    log(repo, read("src/a.py", 1, 5000, None), edit("src/a.py"))
    r = run(repo)
    assert r.returncode == 1
    assert "never said how long" in r.stdout


def test_windows_of_a_file_that_changed_length_do_not_add_up(repo: Path) -> None:
    log(repo, read("src/a.py", 1, 100, 200), read("src/a.py", 101, 200, 300), edit("src/a.py"))
    assert run(repo).returncode == 1


def test_a_read_declared_full_counts(repo: Path) -> None:
    log(repo, {"op": "read", "path": "src/a.py", "full": True}, edit("src/a.py"))
    assert run(repo).returncode == 0


def test_a_file_the_session_created_may_be_edited(repo: Path) -> None:
    log(repo, {"op": "create", "path": "src/new.py"}, edit("src/new.py"))
    assert run(repo).returncode == 0


def test_overwriting_an_unread_file_is_caught(repo: Path) -> None:
    log(repo, {"op": "write", "path": "src/a.py"})
    r = run(repo)
    assert r.returncode == 1
    assert "overwritten" in r.stdout


def test_overwriting_after_a_full_read_passes_and_later_edits_too(repo: Path) -> None:
    """The file went from 40 lines to 90 because THIS session rewrote it. It wrote
    those lines; a later partial look does not un-know them."""
    log(repo, read("src/a.py", 1, 40, 40), {"op": "write", "path": "src/a.py"},
        read("src/a.py", 1, 10, 90), edit("src/a.py"))
    assert run(repo).returncode == 0


def test_a_file_that_changed_underneath_the_session_is_no_longer_known(repo: Path) -> None:
    """Read in full at 100 lines; then it is 300 and nothing this session did
    explains that. Having read the old file is not knowledge of the new one."""
    log(repo, read("src/a.py", 1, 100, 100), read("src/a.py", 1, 50, 300), edit("src/a.py"))
    r = run(repo)
    assert r.returncode == 1
    assert "50 of 300 lines" in r.stdout
    assert "HAD been read in full" in r.stdout      # says which kind of finding this is


def test_a_window_that_overruns_its_own_total_is_refused(repo: Path) -> None:
    """The cheapest way to fake full coverage is a log that contradicts itself."""
    log(repo, read("src/a.py", 1, 999999, 100), edit("src/a.py"))
    r = run(repo)
    assert r.returncode == 2
    assert "contradicts itself" in r.stderr


def test_one_finding_per_file_however_many_edits(repo: Path) -> None:
    log(repo, edit("src/a.py"), edit("src/a.py"), edit("src/a.py"))
    r = run(repo)
    assert r.returncode == 1
    assert r.stdout.count("FAIL [edit-before-full-read]") == 1
    assert "+2 later change(s)" in r.stdout


def test_absolute_and_relative_spellings_are_one_file(repo: Path) -> None:
    log(repo, read(str(repo / "src" / "a.py"), 1, 120, 120), edit("src/a.py"))
    assert run(repo).returncode == 0


# --- the allowlist -----------------------------------------------------------

def test_an_exemption_with_a_reason_is_honoured_and_said_out_loud(repo: Path) -> None:
    log(repo, edit("package-lock.json"))
    (repo / ".conduct" / "read-order-allow.txt").write_text(
        "package-lock.json  # generated; regenerated by the installer, never hand-read\n",
        encoding="utf-8")
    r = run(repo)
    assert r.returncode == 0, r.stdout
    assert "1 change(s) exempted" in r.stdout


def test_an_exemption_with_no_reason_is_refused(repo: Path) -> None:
    log(repo, edit("package-lock.json"))
    (repo / ".conduct" / "read-order-allow.txt").write_text("package-lock.json\n",
                                                            encoding="utf-8")
    r = run(repo)
    assert r.returncode == 2
    assert "no reason" in r.stderr


# --- exit 2: the gate did not judge ------------------------------------------

def test_no_toollog_is_a_gate_failure_not_a_clean_session(repo: Path) -> None:
    r = run(repo)
    assert r.returncode == 2
    assert "no evidence is not a clean session" in r.stderr


def test_a_line_that_is_not_json_is_refused(repo: Path) -> None:
    path = log(repo, read("src/a.py", 1, 120, 120), edit("src/a.py"))
    path.write_text(path.read_text(encoding="utf-8") + '{"op": "edit", "pa\n', encoding="utf-8")
    assert run(repo).returncode == 2


def test_a_line_separator_inside_a_json_string_does_not_cut_the_row(repo: Path) -> None:
    """U+2028 is legal inside a JSON string and is a line break to `splitlines()`.
    Measured: 4 of 80 real session files were refused as "not JSON" because of it."""
    path = repo / ".conduct" / "toollog.jsonl"
    path.parent.mkdir(exist_ok=True)
    rows = [{"op": "read", "path": "src/a.py", "full": True, "note": "a\u2028b"},
            {"op": "edit", "path": "src/a.py"}]
    path.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows),
                    encoding="utf-8")
    r = run(repo)
    assert r.returncode == 0, r.stderr


def test_one_line_short_is_still_a_finding_and_says_what_it_cannot_know(repo: Path) -> None:
    log(repo, read("src/a.py", 1, 912, 913), edit("src/a.py"))
    r = run(repo)
    assert r.returncode == 1
    assert "exactly one short" in r.stdout


def test_a_read_with_nonsense_numbers_is_refused(repo: Path) -> None:
    log(repo, {"op": "read", "path": "src/a.py", "start": 0, "lines": True, "total": 9})
    assert run(repo).returncode == 2


def test_an_event_with_no_path_is_refused(repo: Path) -> None:
    log(repo, {"op": "edit"})
    assert run(repo).returncode == 2


def test_ops_the_gate_does_not_judge_are_counted_not_dropped(repo: Path) -> None:
    log(repo, {"op": "bash", "command": "ls"}, read("src/a.py", 1, 9, 9), edit("src/a.py"))
    r = run(repo)
    assert r.returncode == 0
    assert "1 × op 'bash'" in r.stdout


def test_a_log_with_no_edits_says_it_judged_nothing(repo: Path) -> None:
    log(repo, read("src/a.py", 1, 9, 9))
    r = run(repo)
    assert r.returncode == 0
    assert "judged nothing" in r.stdout


# --- CHECK 2: claim-on-partial-read ------------------------------------------

def handback(repo: Path, text: str) -> str:
    path = repo / "HANDBACK.md"
    path.write_text(text, encoding="utf-8")
    return str(path)


def test_a_report_about_a_partially_read_file_is_caught(repo: Path) -> None:
    log(repo, read("src/a.py", 1, 110, 309))
    r = run(repo, "--report", handback(repo, "The invariant in src/a.py holds for every caller.\n"))
    assert r.returncode == 1
    assert "claim-on-partial-read" in r.stdout and "110 of 309" in r.stdout


def test_a_report_about_a_fully_read_file_passes(repo: Path) -> None:
    log(repo, read("src/a.py", 1, 309, 309))
    r = run(repo, "--report", handback(repo, "The invariant in src/a.py holds for every caller.\n"))
    assert r.returncode == 0, r.stdout


def test_a_path_inside_a_fence_or_a_blockquote_is_not_a_claim(repo: Path) -> None:
    log(repo, read("src/a.py", 1, 110, 309))
    text = "Ran this:\n\n```bash\npython3 src/a.py\n```\n\n> the brief said to look at src/a.py\n"
    assert run(repo, "--report", handback(repo, text)).returncode == 0


def test_a_fence_that_never_closes_does_not_hide_the_rest_of_the_report(repo: Path) -> None:
    log(repo, read("src/a.py", 1, 110, 309))
    text = "Ran this:\n\n```bash\npython3 other.py\n\nThe invariant in src/a.py holds.\n"
    assert run(repo, "--report", handback(repo, text)).returncode == 1


def test_a_bare_filename_counts_when_only_one_logged_file_answers_to_it(repo: Path) -> None:
    log(repo, read("src/a.py", 1, 110, 309))
    assert run(repo, "--report", handback(repo, "Checked a.py: nothing to change.\n")).returncode == 1


def test_a_bare_filename_shared_by_two_logged_files_names_neither(repo: Path) -> None:
    log(repo, read("src/a.py", 1, 110, 309), read("lib/a.py", 1, 20, 20))
    assert run(repo, "--report", handback(repo, "Checked a.py: nothing to change.\n")).returncode == 0


# --- CHECK 3: claim-on-unread (opt-in) ---------------------------------------

def test_strict_claims_catches_a_named_file_nobody_opened(repo: Path) -> None:
    log(repo, read("src/a.py", 1, 1, 1))
    report = handback(repo, "src/a.py is fine and README.md has no install section.\n")
    (repo / "README.md").write_text("# x\n\n## Install\n", encoding="utf-8")
    assert run(repo, "--report", report).returncode == 0
    r = run(repo, "--report", report, "--strict-claims")
    assert r.returncode == 1
    assert "claim-on-unread" in r.stdout and "README.md" in r.stdout


def test_strict_claims_without_a_report_is_a_usage_failure(repo: Path) -> None:
    log(repo, read("src/a.py", 1, 1, 1))
    assert run(repo, "--strict-claims").returncode == 2


# --- SARIF -------------------------------------------------------------------

def test_sarif_is_written_as_warnings(repo: Path) -> None:
    log(repo, edit("src/a.py"))
    out = repo / "out.sarif"
    assert run(repo, "--sarif", str(out)).returncode == 1
    sarif = json.loads(out.read_text(encoding="utf-8"))
    result = sarif["runs"][0]["results"][0]
    assert sarif["version"] == "2.1.0"
    assert result["ruleId"] == "edit-before-full-read"
    assert result["level"] == "warning"           # report-only until its FP rate is measured
    assert result["locations"][0]["physicalLocation"]["artifactLocation"]["uri"] == "src/a.py"


# --- the Claude Code adapter -------------------------------------------------
#
# Fixtures mimic the shape measured on Claude Code 2.1.272 (2026-09-17): the
# call in an assistant row, the result in a user row carrying `toolUseResult`.

def call(uid: str, tool: str, **given: Any) -> dict[str, Any]:
    return {"type": "assistant", "message": {"content": [
        {"type": "tool_use", "id": uid, "name": tool, "input": given}]}}


def result(uid: str, payload: Any, failed: bool = False) -> dict[str, Any]:
    return {"type": "user", "toolUseResult": payload, "message": {"content": [
        {"type": "tool_result", "tool_use_id": uid, "is_error": failed}]}}


def text_read(path: str, start: int, lines: int, total: int) -> dict[str, Any]:
    return {"type": "text", "file": {"filePath": path, "startLine": start,
                                     "numLines": lines, "totalLines": total}}


def transcript(root: Path, *rows: dict[str, Any]) -> str:
    path = root / "session.jsonl"
    path.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
    return str(path)


def test_transcript_with_a_capped_read_then_an_edit_is_caught(repo: Path) -> None:
    target = str(repo / "src" / "a.py")
    session = transcript(
        repo,
        call("t1", "Read", file_path=target), result("t1", text_read(target, 1, 752, 1111)),
        call("t2", "Edit", file_path=target, old_string="a", new_string="b"),
        result("t2", {"filePath": target, "oldString": "a", "newString": "b"}))
    r = run(repo, "--claude-transcript", session)
    assert r.returncode == 1
    assert "752 of 1111 lines" in r.stdout


def test_transcript_with_a_full_read_then_an_edit_passes(repo: Path) -> None:
    target = str(repo / "src" / "a.py")
    session = transcript(
        repo,
        call("t1", "Read", file_path=target), result("t1", text_read(target, 1, 40, 40)),
        call("t2", "Edit", file_path=target), result("t2", {"filePath": target}))
    assert run(repo, "--claude-transcript", session).returncode == 0


def blocks_then_edit(repo: Path, original: str) -> str:
    """Consecutive blocks over a 912-line file this runtime reports as 913 lines."""
    target = str(repo / "src" / "a.py")
    return transcript(
        repo,
        call("t1", "Read", file_path=target, limit=500), result("t1", text_read(target, 1, 500, 913)),
        call("t2", "Read", file_path=target, offset=501, limit=412),
        result("t2", text_read(target, 501, 412, 913)),
        call("t3", "Edit", file_path=target), result("t3", {"filePath": target,
                                                             "originalFile": original}))


def test_transcript_the_empty_segment_after_the_last_newline_is_not_an_unread_line(
        repo: Path) -> None:
    """The false positive measured on real sessions: careful block reading of a
    newline-terminated file came out as `912 of 913 lines`."""
    r = run(repo, "--claude-transcript", blocks_then_edit(repo, "x = 1\n" * 912))
    assert r.returncode == 0, r.stdout


def test_transcript_without_that_proof_the_last_line_stays_unread(repo: Path) -> None:
    """No trailing newline in the file as it was: line 913 is a real line."""
    r = run(repo, "--claude-transcript", blocks_then_edit(repo, "x = 1\n" * 912 + "tail"))
    assert r.returncode == 1
    assert "912 of 913 lines" in r.stdout


def test_transcript_a_proof_about_another_version_of_the_file_proves_nothing(repo: Path) -> None:
    """The edit's record ends in a newline, but it is a 1-line file and the read
    reported 3 lines: that record is not about the file the read saw."""
    target = str(repo / "src" / "a.py")
    session = transcript(
        repo,
        call("t1", "Read", file_path=target, limit=2), result("t1", text_read(target, 1, 2, 3)),
        call("t2", "Edit", file_path=target),
        result("t2", {"filePath": target, "originalFile": "one line only\n"}))
    r = run(repo, "--claude-transcript", session)
    assert r.returncode == 1
    assert "2 of 3 lines" in r.stdout


def test_emit_toollog_without_a_transcript_is_a_usage_failure(repo: Path) -> None:
    log(repo, read("src/a.py", 1, 9, 9))
    assert run(repo, "--emit-toollog", str(repo / "out.jsonl")).returncode == 2


def test_transcript_failed_calls_changed_nothing_and_read_nothing(repo: Path) -> None:
    target = str(repo / "src" / "a.py")
    session = transcript(
        repo,
        call("t1", "Edit", file_path=target), result("t1", "Error: file not read yet", failed=True),
        call("t2", "Read", file_path=target), result("t2", text_read(target, 1, 40, 40)),
        call("t3", "Edit", file_path=target), result("t3", {"filePath": target}))
    r = run(repo, "--claude-transcript", session)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "1 × failed call" in r.stdout


def test_transcript_write_is_a_create_or_an_overwrite_by_its_result(repo: Path) -> None:
    fresh, old = str(repo / "src" / "new.py"), str(repo / "src" / "a.py")
    created = transcript(repo, call("t1", "Write", file_path=fresh, content="x"),
                         result("t1", {"type": "create", "filePath": fresh}))
    assert run(repo, "--claude-transcript", created).returncode == 0
    clobbered = transcript(repo, call("t1", "Write", file_path=old, content="x"),
                           result("t1", {"type": "update", "filePath": old}))
    assert run(repo, "--claude-transcript", clobbered).returncode == 1


def test_transcript_emits_a_toollog_that_gives_the_same_verdict(repo: Path) -> None:
    target = str(repo / "src" / "a.py")
    session = transcript(
        repo,
        call("t1", "Read", file_path=target, limit=110), result("t1", text_read(target, 1, 110, 309)),
        call("t2", "Edit", file_path=target), result("t2", {"filePath": target}))
    emitted = repo / "emitted.jsonl"
    first = run(repo, "--claude-transcript", session, "--emit-toollog", str(emitted))
    second = run(repo, "--toollog", str(emitted))
    assert first.returncode == second.returncode == 1
    assert [json.loads(l)["op"] for l in emitted.read_text(encoding="utf-8").splitlines()] \
        == ["read", "edit"]
