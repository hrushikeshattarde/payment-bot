"""The roster-add helper does the checks that were being done by hand, and refuses the rest.

Every addition in this repo has needed the same five steps: refuse free mail, check the key
does not also match an unrelated company, check the domain is not already rostered to somebody
else, write an evidence note, regenerate. Fifteen minutes per entry, which is why it kept
happening in conversation instead of in a command.

What it must NOT do is decide. ``payment_bot.roster_candidate``'s module docstring is explicit:
if "absent from the roster" resolved to "add it and proceed", the roster would stop being a
control and become a log of everyone who has ever written in. The bot never calls this; a
person does, and the confirmation prompt is part of the point.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

_REPO = Path(__file__).resolve().parents[2]
_SCRIPT = _REPO / "scripts" / "add_roster_entry.py"


def _module():
    spec = importlib.util.spec_from_file_location("add_roster_entry", _SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


MOD = _module()


# --- the refusals -----------------------------------------------------------
def test_a_bare_free_mail_domain_is_refused() -> None:
    """It would authorise every Gmail user as that factor, and be inert anyway."""

    problems = MOD._check_value("gmail.com")

    assert problems
    assert "free-mail" in problems[0]
    assert "billing@gmail.com" in problems[0], "the message must name the supported form"


def test_a_free_mail_whole_address_is_accepted() -> None:
    """The one supported way to authorise a factor on free mail; it grants exactly itself."""

    assert MOD._check_value("billing@gmail.com") == []
    assert MOD._check_value("verify@basicblock.io") == []


def test_a_domain_written_with_an_at_is_accepted() -> None:
    assert MOD._check_value("@acmefactoring.com") == []


def test_something_that_is_not_a_domain_is_refused() -> None:
    assert MOD._check_value("acmefactoring") != []


# --- the warning that took an investigation last time -----------------------
def test_a_domain_rostered_to_another_company_is_reported() -> None:
    """The Faro/BasicBlock case: a known factor's real domain on another factor's load.

    Surfacing this is what separates "unknown domain, possibly a lookalike" from "real company,
    wrong load" — two situations that call for opposite decisions and read identically in an
    escalation packet.
    """

    roster = {
        "basicblock inc.": ["basicblock.io"],
        "basic block inc.": ["basicblock.io"],
        "faro factoring llc": ["farofactoring.com"],
    }

    elsewhere = MOD._already_rostered_elsewhere(
        "basicblock.io", roster, key="faro factoring llc dba faro capital"
    )

    assert elsewhere == ["basic block inc.", "basicblock inc."]


def test_the_key_itself_is_not_reported_as_elsewhere() -> None:
    """Adding a second domain to a company that already has one is the ordinary case."""

    roster = {"rts financial service, inc": ["rtsfinancial.com", "ryanrts.com"]}

    assert (
        MOD._already_rostered_elsewhere(
            "ryanrts.com", roster, key="rts financial service, inc"
        )
        == []
    )


def test_the_at_prefix_does_not_hide_a_collision() -> None:
    roster = {"basicblock inc.": ["basicblock.io"]}

    assert MOD._already_rostered_elsewhere("@basicblock.io", roster, key="other") == [
        "basicblock inc."
    ]


# --- the collateral check ---------------------------------------------------
def test_collateral_lists_every_export_name_the_key_would_answer_for() -> None:
    names = {
        "RTS Financial Service, Inc",
        "RTS FINANCIAL SERVICE, INC",
        "RTS International Inc",
        "Triumph Business Capital",
    }

    hits = MOD._collateral("rts financial service, inc", names)

    assert "RTS Financial Service, Inc" in hits
    assert "RTS FINANCIAL SERVICE, INC" in hits
    # The collision this key has always had to avoid.
    assert "RTS International Inc" not in hits
    assert "Triumph Business Capital" not in hits


# --- the happy path, end to end --------------------------------------------
def test_it_writes_the_entry_the_evidence_and_regenerates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    manual = tmp_path / "factoring_domains_manual.json"
    roster = tmp_path / "factoring_domains.json"
    export = tmp_path / "data-test.csv"

    manual.write_text(
        json.dumps(
            {
                "_README": ["notes, not a company"],
                "acme factoring, llc": ["acmefactoring.com"],
                "_evidence": {"acme factoring, llc": "prior note"},
            }
        ),
        encoding="utf-8",
    )
    roster.write_text(json.dumps({}), encoding="utf-8")
    export.write_text(
        '"payName","email"\n"Acme Factoring, LLC","remit@acmefactoring.com"\n', encoding="utf-8"
    )

    monkeypatch.setattr(MOD, "MANUAL", manual)
    monkeypatch.setattr(MOD, "ROSTER", roster)

    code = MOD.main(
        [
            "add_roster_entry.py",
            "acme factoring, llc",
            "collections.acmefactoring.com",
            "--evidence",
            "sender ar@collections.acmefactoring.com",
            "--load",
            "2462934",
            "--export",
            str(export),
            "--yes",
        ]
    )

    assert code == 0, capsys.readouterr().out

    written = json.loads(manual.read_text(encoding="utf-8"))
    # Union, not replace: the existing domain survives.
    assert written["acme factoring, llc"] == [
        "acmefactoring.com",
        "collections.acmefactoring.com",
    ]
    # The evidence is appended, not overwritten, and carries the load id.
    note = written["_evidence"]["acme factoring, llc"]
    assert note.startswith("prior note")
    assert "2462934" in note
    # Documentation keys survive, and _evidence stays last so the file still reads as
    # entries-then-notes.
    assert written["_README"] == ["notes, not a company"]
    assert list(written)[-1] == "_evidence"
    # And the generator ran, so the roster reflects the addition.
    assert "collections.acmefactoring.com" in json.loads(roster.read_text(encoding="utf-8"))[
        "acme factoring, llc"
    ]


def test_a_refused_value_writes_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The check has to run BEFORE the write, or --yes would paste a free-mail domain in."""

    manual = tmp_path / "factoring_domains_manual.json"
    manual.write_text(json.dumps({"_evidence": {}}), encoding="utf-8")
    export = tmp_path / "data-test.csv"
    export.write_text('"payName","email"\n"Acme Factoring, LLC",NULL\n', encoding="utf-8")

    monkeypatch.setattr(MOD, "MANUAL", manual)
    monkeypatch.setattr(MOD, "ROSTER", tmp_path / "factoring_domains.json")
    before = manual.read_text(encoding="utf-8")

    code = MOD.main(
        [
            "add_roster_entry.py",
            "acme factoring, llc",
            "gmail.com",
            "--evidence",
            "whatever",
            "--export",
            str(export),
            "--yes",
        ]
    )

    assert code == 1
    assert manual.read_text(encoding="utf-8") == before, "nothing may be written on a refusal"


def test_evidence_is_mandatory() -> None:
    """An entry without a reason is how the file fills up with domains nobody can vouch for."""

    with pytest.raises(SystemExit):
        MOD.main(["add_roster_entry.py", "acme factoring, llc", "acmefactoring.com"])
