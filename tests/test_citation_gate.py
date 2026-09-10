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


# --- CHECK 4: public-domain arithmetic ---------------------------------------

def test_eu_pd_claim_for_a_recent_translator_is_caught(repo: Path) -> None:
    """A 1928 translation is PD in the US and may not be in the EU.

    This is the trap the family already documented: the US rule is publication
    based, the EU rule is life-of-the-author plus 70. Arnold publishes from
    Germany, so an unqualified EU claim is the one that bites.
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


# --- false positives: the reason a gate survives contact with users ----------

def test_prose_containing_an_em_dash_is_not_a_citation(repo: Path) -> None:
    """The regression that shipped: the gate fired on the repo's own prose.

    Both of these are blockquoted prose that happens to contain an em dash.
    Neither is delimited as a quotation, so neither is one.
    """
    (repo / "README.md").write_text(CLEAN_README + textwrap.dedent("""\

        > The first utterance of the harness: a fixed thesis, then a rotating
        > maxim from the empiricist canon — Bacon, Newton, Huxley, Aristotle.

        > Four pillars of empirical practice — and every rule earns its place by
        > carrying a way to prove it broken.
        """), encoding="utf-8")
    r = run(repo)
    assert r.returncode == 0, f"gate fired on its own prose:\n{r.stdout}"


def test_short_delimited_fragment_is_not_treated_as_a_claim(repo: Path) -> None:
    (repo / "README.md").write_text(CLEAN_README + textwrap.dedent("""\

        > *Done* — meaning what the gates return.
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
