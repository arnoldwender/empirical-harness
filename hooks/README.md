# Hooks — keeping the pillars present

The Codex only works if it's *in context* when the agent acts. A one-time paste into
`AGENTS.md` works; a hook makes it automatic, every session, and opens each run with the
thesis.

## `session-start.sh`

Emits, to stdout:

1. The **opening thesis** + a rotating **maxim of the day** (`bin/precept`, drawn from
   `precepts.txt`).
2. The **conduct block** — the four pillars, precedence, and the gate limit (`codex-block.md`).

It's harness-agnostic: any harness that can run a command at session start can use it, and
its stdout is plain readable text.

## Wiring it into Claude Code

Claude Code injects a `SessionStart` hook's stdout into the session context. Add to your
`settings.json` (use the **absolute** path, and check your Claude Code version's hook docs —
the schema evolves):

```json
{
  "hooks": {
    "SessionStart": [
      {
        "hooks": [
          { "type": "command", "command": "/abs/path/to/empirical-harness/hooks/session-start.sh" }
        ]
      }
    ]
  }
}
```

## Wiring it into any other harness

Run `hooks/session-start.sh` as the first step of your session bootstrap and prepend its
output to the system prompt. The thesis goes first, the pillars stay present.

## `read-before-edit.py` — the order gate, live

A Claude Code `PreToolUse` hook for `Edit`, `Write` and `MultiEdit`. Before the tool runs, it
asks [`gate/read_order.py`](../gate/read_order.py)'s question about that one file in that one
session — *was it read, all of it, before it is changed?* — and, if not, tells the agent so
in the tool result. It **warns**; it does not block:

> read-order: you are about to edit `x.py` having read 20 of 200 lines in this session
> (windows: 1-20). … Read what is missing (consecutive blocks until every line was seen)
> and edit again. … Warning mode: this edit is NOT blocked.

Why a hook when the gate exists: the gate judges a session after the fact, when the edit on
the half-read file has already been made. The rule was measured broken in nine sessions out
of ten (README, *Measured before it was believed*); a rule that is written down and broken
that often needs a mechanism at the moment of the act.

```json
{
  "hooks": {
    "PreToolUse": [
      {
        "matcher": "Edit|Write|MultiEdit",
        "hooks": [
          { "type": "command",
            "command": "python3 /abs/path/to/empirical-harness/hooks/read-before-edit.py",
            "timeout": 10 }
        ]
      }
    ]
  }
}
```

What it reads: the session transcript Claude Code hands every hook (`transcript_path`), only
the rows that mention the file and only after the last context compaction. What it writes:
one receipt line per run to `~/.local/state/empirical-harness/read-order-receipts.jsonl`
(`READ_ORDER_RECEIPTS=…` to move it, `off` to disable) — verdict, lines seen, lines total,
milliseconds. The receipts are how you find out whether the warning changes anything: count
`partial` and `never-read` over a week.

Exemptions: `.conduct/read-order-allow.txt` in the working directory, one glob per line with
its reason after a `#` (the gate's own file; a glob with no reason is not honoured).
`READ_ORDER_HOOK_MODE=block` makes it deny the edit instead (exit 2); shipped so the switch
exists, not the default. Any error of its own is a receipt and exit 0 — the hook is never
the reason a session cannot proceed.

Verified on Claude Code 2.1.274 (2026-09-17), in a clean-room session with only this hook
wired: the agent received the warning attached to the edit's result. Three things it learned
from the runtime the hard way are written in the file's header — the transcript is written
with a lag, so an edit's result may not be there when the next edit's hook fires; the
runtime counts the empty segment after a final newline as a line; and windows have to be
remapped through the session's own edits exactly, with the `structuredPatch` each Edit
result carries, or a deletion can launder lines never seen.

Tests: [`tests/test_read_before_edit_hook.py`](../tests/test_read_before_edit_hook.py) ·
mutants: [`tests/mutation_check_read_before_edit.py`](../tests/mutation_check_read_before_edit.py).

## Just want to see it?

```sh
./hooks/session-start.sh
```
