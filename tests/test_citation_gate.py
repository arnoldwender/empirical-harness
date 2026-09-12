"""Mutation tests for the citation gate.

Every check gets the same treatment: build a repo that PASSES, then plant the
one defect that check exists to catch, and require the gate to go red. A test
that only ever sees a clean repo proves nothing — it would still pass if the
check were deleted.

    python3 -m pytest tests/ -q

The gate is invoked as a subprocess rather than imported, because the exit code
is part of the contract the whole conduct-harness family shares (0 clean,
1 findings, 2 the gate itself broke). Importing would test the functions and
leave the contract untested.

This file is shared byte-for-byte with the other conduct-harness repos that
carry `gate/citations.py`, for the same reason the gate itself is: a fabricated
citation is the same defect in every tradition. Its fixtures deliberately name
no scripture — the gate is being tested, not the repo's canon.
"""

from __future__ import annotations

import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

GATE = Path(__file__).resolve().parent.parent / "gate" / "citations.py"

CLEAN_SOURCE = """\
work: "The Advancement of Learning"
author: "Francis Bacon"
author_born: 1561
author_died: 1626
year: 1605
pd_status_us: "Published 1605 - public domain."
pd_status_eu: "Author died 1626 - public domain."
source_url: "https://www.gutenberg.org/ebooks/5500"
provenance: verified
quotes:
  - "If a man will begin with certainties, he shall end in doubts."
"""

CLEAN_README = """\
# Test harness

## The first word

> *If a man will begin with certainties, he shall end in doubts.* — Francis Bacon
"""


def run(root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    env = {**os.environ, "HARNESS_ROOT": str(root)}
    return subprocess.run([sys.executable, str(GATE), *args],
                          capture_output=True, text=True, env=env, check=False)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A minimal repo the gate passes cleanly."""
    (tmp_path / "sources").mkdir()
    (tmp_path / "sources" / "bacon.yml").write_text(CLEAN_SOURCE, encoding="utf-8")
    (tmp_path / "README.md").write_text(CLEAN_README, encoding="utf-8")
    return tmp_path


# --- the control -------------------------------------------------------------

def test_clean_repo_passes(repo: Path) -> None:
    """Without this, every test below could pass because the gate always fails."""
    r = run(repo)
    assert r.returncode == 0, r.stdout
    assert "traces to a source" in r.stdout


# --- CHECK 1: unsourced quotation --------------------------------------------

def test_quotation_with_no_source_is_caught(repo: Path) -> None:
    (repo / "README.md").write_text(CLEAN_README + textwrap.dedent("""\

        > *A quotation nobody ever wrote, invented for this test.* — Francis Bacon
        """), encoding="utf-8")
    r = run(repo)
    assert r.returncode == 1
    assert "unsourced-quote" in r.stdout


def test_deleting_the_source_file_turns_a_passing_repo_red(repo: Path) -> None:
    """The falsifier of CHECK 1: remove the evidence, the claim must fail."""
    assert run(repo).returncode == 0
    (repo / "sources" / "bacon.yml").unlink()
    assert run(repo).returncode == 1


# --- the three quotation shapes ----------------------------------------------
#
# Each shape gets a red test AND a false-positive test. A shape the extractor
# cannot see is the most dangerous kind of bug this gate can have: it reports
# "0 attributed quotations, clean, exit 0" and issues a green badge over a pool
# nobody checked. That is exactly what happened to the Zen edition before these
# tests existed — the gate found nothing in it, and nothing was wrong with the
# gate's exit code.

def test_shape_a_flowing_blockquote_is_checked(repo: Path) -> None:
    (repo / "README.md").write_text(CLEAN_README + textwrap.dedent("""\

        > *Invented flowing quotation with no source at all.* — Some Author
        """), encoding="utf-8")
    assert run(repo).returncode == 1


def test_shape_b_undelimited_bullet_is_checked(repo: Path) -> None:
    """A pool listed as bare bullets must not be invisible to the gate."""
    (repo / "README.md").write_text(CLEAN_README + textwrap.dedent("""\

        > - Invented bullet quotation with no delimiters and no source — Some Author
        """), encoding="utf-8")
    r = run(repo)
    assert r.returncode == 1, f"an undelimited bullet was never checked:\n{r.stdout}"
    assert "unsourced-quote" in r.stdout


def test_shape_c_attribution_on_its_own_line_is_checked(repo: Path) -> None:
    """The README pull-quote form: quotation on one row, attribution on the next."""
    (repo / "README.md").write_text(CLEAN_README + textwrap.dedent("""\

        > Invented pull quotation that appears in no source file whatsoever.
        > — Some Author (1644-1694)
        """), encoding="utf-8")
    r = run(repo)
    assert r.returncode == 1, f"a two-line pull quote was never checked:\n{r.stdout}"
    assert "unsourced-quote" in r.stdout


def test_shape_c_binds_to_the_line_directly_above(repo: Path) -> None:
    """An attribution must not reach back past an intervening quotation."""
    (repo / "README.md").write_text(CLEAN_README + textwrap.dedent("""\

        > Some earlier line of the blockquote that is not being attributed here.
        > Invented pull quotation that appears in no source file whatsoever.
        > — Some Author (1644-1694)
        """), encoding="utf-8")
    r = run(repo)
    assert r.returncode == 1
    assert "Invented pull quotation" in r.stdout, r.stdout
    assert "Some earlier line" not in r.stdout


def test_composite_epigraph_quotes_the_translation_not_the_romanisation(repo: Path) -> None:
    """`原文 — *romaji* — "translation" — Author` must yield the TRANSLATION.

    The regression this defends against is subtle and silent. A single regex
    whose attribution tail runs to end of line matches greedily from the
    LEFTMOST delimited span, so it hands back the romanisation as the quotation
    and everything after it as the "attribution". The gate then demands a source
    for a string nobody ever quoted, and the real sentence goes unchecked.
    """
    (repo / "README.md").write_text(CLEAN_README + textwrap.dedent("""\

        > 一日不作、一日不食 — *ichijitsu nasazareba* — "An invented translation with no source." — Some Author (720-814)
        """), encoding="utf-8")
    r = run(repo)
    assert r.returncode == 1
    assert "An invented translation" in r.stdout, (
        f"the gate quoted something other than the translation:\n{r.stdout}")
    assert "ichijitsu" not in r.stdout, (
        f"the gate treated the romanisation as the quotation:\n{r.stdout}")


def test_quotation_containing_an_em_dash_is_not_cut_in_half(repo: Path) -> None:
    """A quotation may contain an em dash of its own.

    Splitting on the FIRST dash truncates the sentence, and the truncated
    fragment is no longer delimited end to end — so the line is dropped in
    silence rather than reported.
    """
    (repo / "README.md").write_text(CLEAN_README + textwrap.dedent("""\

        > "An invented sentence — with a dash inside it — and no source." — Some Author (1233)
        """), encoding="utf-8")
    r = run(repo)
    assert r.returncode == 1, f"a quotation with an internal em dash was dropped:\n{r.stdout}"
    assert "and no source" in r.stdout, r.stdout


def test_bold_delimited_quotation_is_checked(repo: Path) -> None:
    (repo / "README.md").write_text(CLEAN_README + textwrap.dedent("""\

        > **An invented bold maxim that appears in no source file.** — Some Author
        """), encoding="utf-8")
    assert run(repo).returncode == 1


def test_a_sourced_quotation_in_every_shape_passes(repo: Path) -> None:
    """All three shapes must also be able to come out GREEN.

    Without this the three tests above would still pass if the gate simply
    failed everything it saw.
    """
    (repo / "README.md").write_text(CLEAN_README + textwrap.dedent("""\

        > - If a man will begin with certainties, he shall end in doubts. — Francis Bacon

        > If a man will begin with certainties, he shall end in doubts.
        > — Francis Bacon
        """), encoding="utf-8")
    r = run(repo)
    assert r.returncode == 0, r.stdout


# --- CHECK 2: incomplete provenance ------------------------------------------

@pytest.mark.parametrize("field", ["work", "author", "author_born", "author_died",
                                   "year", "pd_status_us", "pd_status_eu", "source_url"])
def test_each_required_provenance_field_is_enforced(repo: Path, field: str) -> None:
    """Dropping any single field must fail. Otherwise the field is decoration."""
    src = repo / "sources" / "bacon.yml"
    kept = [ln for ln in src.read_text(encoding="utf-8").splitlines()
            if not ln.startswith(f"{field}:")]
    src.write_text("\n".join(kept) + "\n", encoding="utf-8")
    r = run(repo)
    assert r.returncode == 1, f"dropping `{field}` did not fail the gate"
    assert "incomplete-provenance" in r.stdout


def test_unverified_without_a_note_is_caught(repo: Path) -> None:
    """`provenance: unverified` is honest only if it says what is unverified."""
    src = repo / "sources" / "bacon.yml"
    src.write_text(src.read_text(encoding="utf-8").replace(
        "provenance: verified", "provenance: unverified"), encoding="utf-8")
    r = run(repo)
    assert r.returncode == 1
    assert "unverified-without-note" in r.stdout


def test_unverified_with_a_note_passes(repo: Path) -> None:
    """The point is to make honesty cheap, not to ban uncertainty."""
    src = repo / "sources" / "bacon.yml"
    src.write_text(src.read_text(encoding="utf-8").replace(
        "provenance: verified",
        'provenance: unverified\nprovenance_note: "Attribution not located in a '
        'specific edition."'), encoding="utf-8")
    assert run(repo).returncode == 0


def test_unverified_sources_are_counted_on_a_clean_run(repo: Path) -> None:
    """A pile of unverified sources must not be able to grow unwatched."""
    src = repo / "sources" / "bacon.yml"
    src.write_text(src.read_text(encoding="utf-8").replace(
        "provenance: verified",
        'provenance: unverified\nprovenance_note: "Edition not located."'),
        encoding="utf-8")
    r = run(repo)
    assert r.returncode == 0
    assert "unverified" in r.stdout, "a clean run hid its unverified count"


# --- CHECK 3: anachronism ----------------------------------------------------

def test_work_dated_before_the_author_was_born(repo: Path) -> None:
    src = repo / "sources" / "bacon.yml"
    src.write_text(src.read_text(encoding="utf-8").replace(
        "year: 1605", "year: 1540"), encoding="utf-8")
    r = run(repo)
    assert r.returncode == 1
    assert "anachronism" in r.stdout


def test_work_dated_after_the_author_died(repo: Path) -> None:
    """The Newton case: Regula I cited as 1687 when it dates from 1713."""
    src = repo / "sources" / "bacon.yml"
    src.write_text(src.read_text(encoding="utf-8").replace(
        "year: 1605", "year: 1700"), encoding="utf-8")
    r = run(repo)
    assert r.returncode == 1
    assert "anachronism" in r.stdout


def test_declared_posthumous_edition_is_allowed(repo: Path) -> None:
    """Posthumous publication is real. It just has to be declared, not assumed."""
    src = repo / "sources" / "bacon.yml"
    src.write_text(src.read_text(encoding="utf-8").replace(
        "year: 1605", "year: 1700\nposthumous: true"), encoding="utf-8")
    assert run(repo).returncode == 0


def test_edition_dated_after_the_translator_died(repo: Path) -> None:
    """The compensating control for anonymous scripture.

    An anonymous text has no author dates, so the author arithmetic above is
    inert for most of what these repos quote. The translator always has dates,
    and an edition cannot postdate the hand that made it.
    """
    src = repo / "sources" / "bacon.yml"
    src.write_text(src.read_text(encoding="utf-8").replace(
        "year: 1605",
        'year: 1605\ntranslator: "Someone Victorian"\ntranslator_died: 1400'),
        encoding="utf-8")
    r = run(repo)
    assert r.returncode == 1
    assert "anachronism" in r.stdout


def test_ancient_author_with_negative_years_passes(repo: Path) -> None:
    """Aristotle: author_born -384. The arithmetic must work below zero."""
    src = repo / "sources" / "bacon.yml"
    src.write_text(src.read_text(encoding="utf-8")
                   .replace("author_born: 1561", "author_born: -384")
                   .replace("author_died: 1626", "author_died: -322")
                   .replace("year: 1605", "year: -340"), encoding="utf-8")
    assert run(repo).returncode == 0


# --- CHECK 4: public-domain arithmetic ---------------------------------------

def test_eu_pd_claim_for_a_recent_translator_is_caught(repo: Path) -> None:
    """A 1928 translation is PD in the US and may not be in the EU.

    This is the trap the family already documented: the US rule is publication
    based, the EU rule is life-of-the-author plus 70. These repos are published
    from Germany, so an unqualified EU claim is the one that bites.
    """
    src = repo / "sources" / "bacon.yml"
    src.write_text(src.read_text(encoding="utf-8").replace(
        'pd_status_eu: "Author died 1626 - public domain."',
        'translator: "Someone Modern"\ntranslator_died: 1990\n'
        'pd_status_eu: "public domain"'), encoding="utf-8")
    r = run(repo)
    assert r.returncode == 1
    assert "pd-claim" in r.stdout


def test_eu_pd_claim_that_shows_its_arithmetic_passes(repo: Path) -> None:
    src = repo / "sources" / "bacon.yml"
    src.write_text(src.read_text(encoding="utf-8").replace(
        'pd_status_eu: "Author died 1626 - public domain."',
        'translator: "Someone Modern"\ntranslator_died: 1990\n'
        'pd_status_eu: "Translator died 1990; 1990 + 70 = 2060, still in copyright '
        'in the EU - the original wording is used instead."'), encoding="utf-8")
    assert run(repo).returncode == 0


def test_translator_with_no_death_year_is_caught(repo: Path) -> None:
    """A translation whose term nobody computed is a PD claim nobody checked."""
    src = repo / "sources" / "bacon.yml"
    src.write_text(src.read_text(encoding="utf-8").replace(
        "year: 1605", 'year: 1605\ntranslator: "Someone Unnamed"'), encoding="utf-8")
    r = run(repo)
    assert r.returncode == 1
    assert "pd-claim" in r.stdout


# --- false positives: the reason a gate survives contact with users ----------

def test_prose_containing_an_em_dash_is_not_a_citation(repo: Path) -> None:
    """The regression that shipped: the gate fired on the repo's own prose.

    Both of these are blockquoted prose that happens to contain an em dash.
    Neither is delimited as a quotation, so neither is one.
    """
    (repo / "README.md").write_text(CLEAN_README + textwrap.dedent("""\

        > The first utterance of the harness: a fixed thesis, then a rotating
        > maxim from the canon — Bacon, Newton, Huxley, Aristotle.

        > Four pillars of practice — and every rule earns its place by
        > carrying a way to prove it broken.
        """), encoding="utf-8")
    r = run(repo)
    assert r.returncode == 0, f"gate fired on its own prose:\n{r.stdout}"


def test_short_delimited_fragment_is_not_treated_as_a_claim(repo: Path) -> None:
    (repo / "README.md").write_text(CLEAN_README + textwrap.dedent("""\

        > *Done* — meaning what the gates return.
        """), encoding="utf-8")
    assert run(repo).returncode == 0


def test_blockquote_with_no_attribution_is_prose(repo: Path) -> None:
    (repo / "README.md").write_text(CLEAN_README + textwrap.dedent("""\

        > A unit test started failing after your change. Make the suite pass.
        """), encoding="utf-8")
    assert run(repo).returncode == 0


# --- the contract: exit 2 is the gate's own failure --------------------------

def test_missing_sources_directory_is_a_finding_not_a_pass(repo: Path) -> None:
    """When the data is absent the answer is never 'clean'.

    The rule the whole family shares: when the input is missing, the default is
    never the value that means 'all good'.
    """
    for f in (repo / "sources").iterdir():
        f.unlink()
    (repo / "sources").rmdir()
    r = run(repo)
    assert r.returncode == 1
    assert r.returncode != 0


def test_unparseable_source_is_reported_not_skipped(repo: Path) -> None:
    (repo / "sources" / "broken.yml").write_text(
        "work: fine\n   badly: indented\n", encoding="utf-8")
    r = run(repo)
    assert r.returncode == 1
    assert "sources" in r.stdout


# --- SARIF output ------------------------------------------------------------

def test_sarif_is_written_and_well_formed(repo: Path, tmp_path: Path) -> None:
    import json
    (repo / "README.md").write_text(CLEAN_README + textwrap.dedent("""\

        > *A quotation nobody ever wrote, invented for this test.* — Francis Bacon
        """), encoding="utf-8")
    out = tmp_path / "out.sarif"
    r = run(repo, "--sarif", str(out))
    assert r.returncode == 1
    doc = json.loads(out.read_text(encoding="utf-8"))
    assert doc["version"] == "2.1.0"
    assert doc["runs"][0]["results"], "SARIF carries no results for a failing run"
    assert doc["runs"][0]["results"][0]["ruleId"] == "unsourced-quote"

def test_a_table_row_inside_a_blockquote_is_not_a_quotation(repo: Path) -> None:
    """The regression that shipped: the copyright table fired the gate.

    The Bushido edition documents its own provenance in a markdown table nested
    in a blockquote. Every row arrives at the extractor looking like blockquote
    prose, and a cell reading `**not in the EU until 2029** — Giles died 1958`
    has exactly the shape Shape A hunts for: a delimited span closing right
    before an em dash and an attribution.

    Two findings, both false, both inside the table that exists precisely to be
    honest about provenance. A gate that fires on the honesty section teaches
    people to delete the honesty section.
    """
    (repo / "README.md").write_text(CLEAN_README + textwrap.dedent("""\

        > | Lines | English | Status |
        > | --- | --- | --- |
        > | 2 x Sun Tzu | Lionel Giles, 1910 | **not in the EU until 2029** — Giles died 1958 |
        > | Lao Tzu | James Legge, 1891 | **public domain everywhere** — Legge died 1897 |
        """), encoding="utf-8")
    r = run(repo)
    assert r.returncode == 0, f"the gate fired on a provenance table:\n{r.stdout}"


def test_the_gate_still_bites_next_to_a_table(repo: Path) -> None:
    """The other half: skipping tables must not skip real citations near them."""
    (repo / "README.md").write_text(CLEAN_README + textwrap.dedent("""\

        > | Lines | Status |
        > | --- | --- |
        > | 1 | **fine** — nobody |

        > *A line nobody ever wrote, planted to test the gate.* — Francis Bacon
        """), encoding="utf-8")
    r = run(repo)
    assert r.returncode == 1
    assert "unsourced-quote" in r.stdout

def test_a_new_markdown_document_is_covered_without_touching_code(repo: Path) -> None:
    """Discovery, not a list. The list is what made the gate blind.

    Measured across the family: one hardcoded list or another skipped MAXIMS.md,
    BLESSING.md, REFERENCE.md and LAWS.md — 26 attributed lines in total,
    reported as a clean pass by a gate that never opened the file they were in.
    """
    (repo / "NEWDOC.md").write_text(
        '> *A line nobody ever wrote, planted in a brand new file.* \u2014 Francis Bacon\n',
        encoding="utf-8")
    r = run(repo)
    assert r.returncode == 1, "a new document was not discovered"
    assert "NEWDOC.md" in r.stdout


def test_boilerplate_documents_stay_out_of_scope(repo: Path) -> None:
    """Discovery must not mean scanning the licence text for quotations."""
    (repo / "CODE_OF_CONDUCT.md").write_text(
        '> *A line nobody ever wrote, in boilerplate.* \u2014 Francis Bacon\n',
        encoding="utf-8")
    assert run(repo).returncode == 0


def test_the_run_declares_which_documents_it_read(repo: Path) -> None:
    """The line this gate spent a day earning.

    Zero findings over zero coverage prints identically to zero findings over
    full coverage, and the reader cannot tell them apart. So the run says which
    documents it opened and how many attributed lines it found in each.
    """
    r = run(repo)
    assert r.returncode == 0
    assert "read " in r.stdout and "document(s)" in r.stdout
    assert "README.md=" in r.stdout, f"coverage not reported per file:\n{r.stdout}"
