# Contributing

Contributions are welcome — bug reports, fixes, and improvements alike.

## The short version

1. **Fork** the repository and create a branch off `main`:
   `fix/short-description`, `feat/short-description`, `docs/…`, `refactor/…`.
2. Make your change. Keep it focused — one concern per pull request is easier to review
   and much easier to revert if it turns out to be wrong.
3. Open a **pull request against `main`**. Describe what changes and why; if it fixes an
   issue, link it.

`main` is protected: it takes no direct pushes, and every change arrives through a pull
request. That applies to the maintainer too.

## Before you open the PR

- Run whatever checks the repository ships. If there is a `scripts/` directory with a
  check in it, run that; if there is a CI workflow, it will run the same thing on your PR.
- Match the surrounding style rather than introducing a new one. A formatting-only rewrite
  of untouched code makes a change hard to read.
- Commit messages use `[Action] Brief description` — for example `[Fix] Handle empty pool`,
  `[Docs] Clarify install step`. Keep the subject line short and the body for the why.

## What makes a change easy to accept

- It does one thing, and the title says what that thing is.
- It explains the problem, not only the patch. A failing case beats a description of it.
- It does not widen scope silently. If a small fix grows into a refactor, say so in the PR
  or split it out.
- Nothing invented: no benchmark, citation, or capability claim that cannot be checked
  from the repository itself.

## What we won't merge — even when it is well built

This family is ten repositories that share one contract and deliberately do **not** share
detection logic. Most declined pull requests are good work aimed at the wrong place. These are
declined on sight, and this is why:

- **A rule without a falsifier.** Every rule in [`CODEX.md`](CODEX.md) names the observable
  condition under which a reviewer can say it was broken. A rule nobody can check is a belief,
  and the codex does not carry beliefs.
- **A quotation without a source.** Every attributed line must resolve to a file in
  [`sources/`](sources/) stating the work, the author, the translator, their dates, and the
  public-domain status in the US and in the EU separately. The citation gate decides this; a
  persuasive pull-request description does not. An ancient original says nothing about the
  copyright of its translation.
- **A clearer rewording of a ratified rule.** The rules are numbered because other files cite
  them by number. A rewording for style shifts meaning in ways no test catches. Propose a change
  of substance, together with the case that motivates it.
- **Detection logic copied from a sibling harness.** The gate in [`gate/`](gate/) checks
  something the other nine do not, and that difference is the reason there are ten repositories
  rather than one. The citation gate is the single piece of gate logic the family holds in
  common, and it moves through all ten together.
- **A check that audits the whole file where it could audit the diff.** A gate that turns every
  inherited repository red on its first day is uninstalled on its second.
- **A check that cannot fail.** A new check arrives with the mutation that proves it: remove the
  mechanism and the suite must go red. See [`tests/`](tests/).
- **A gate that returns 0 or 1 when it was the gate that crashed.** Exit 2 is reserved for the
  gate's own failure. A checker that cannot tell its verdict from its breakage fails open.
- **Install instructions for a runtime nobody ran them on.** An unverified install path is a
  claim this repository cannot back.

Before calling something a gap, check whether the absence is the design: the README states, on
purpose, what each gate does **not** cover. If you are unsure whether a change fits, open an
issue first. Asking is cheaper than a pull request that fights the design.

## Reporting a problem instead

If you have found a bug but not a fix, open an issue with what you did, what you expected,
and what happened. A reproduction is worth more than a description.

For anything with security implications, do **not** open a public issue — see
[SECURITY.md](SECURITY.md).
