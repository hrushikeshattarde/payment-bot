"""The roster generator's hand-verified merge.

These entries authorise disclosures, so the merge behaviour is worth pinning: a manual entry
must ADD a send-from domain to whatever the export gave, never replace it. Live case that
proves the point — Nu-Ko Capital remits to nu-ko.com (from the export) but sends collections
mail from nukocapital.com; a replace would have dropped the remit domain.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from typing import Any

import pytest

_SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "generate_factoring_domains.py"


def _load_module() -> Any:
    spec = importlib.util.spec_from_file_location("_roster_gen", _SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


gen = _load_module()


@pytest.mark.unit
def test_manual_entries_union_with_generated_domains(tmp_path: Path) -> None:
    """The live Nu-Ko shape: same company, second domain, both must survive."""

    csv = tmp_path / "export.csv"
    csv.write_text('"payName","email"\n"Nu-Ko Capital","collection@nu-ko.com"\n', encoding="utf-8")
    manual = tmp_path / "factoring_domains_manual.json"
    manual.write_text(json.dumps({"nu-ko capital": ["nukocapital.com"]}), encoding="utf-8")
    out = tmp_path / "roster.json"

    assert gen.main(["gen", str(csv), str(out), str(manual)]) == 0
    roster = json.loads(out.read_text(encoding="utf-8"))
    assert sorted(roster["nu-ko capital"]) == ["nu-ko.com", "nukocapital.com"]


@pytest.mark.unit
def test_manual_only_company_is_added(tmp_path: Path) -> None:
    """A factor absent from the export entirely — its export row had a NULL email."""

    csv = tmp_path / "export.csv"
    csv.write_text('"payName","email"\n"Dorado Finance",NULL\n', encoding="utf-8")
    manual = tmp_path / "m.json"
    manual.write_text(json.dumps({"dorado finance": ["doradofinance.com"]}), encoding="utf-8")
    out = tmp_path / "roster.json"

    assert gen.main(["gen", str(csv), str(out), str(manual)]) == 0
    assert json.loads(out.read_text(encoding="utf-8")) == {"dorado finance": ["doradofinance.com"]}


@pytest.mark.unit
def test_underscore_keys_are_documentation_not_companies(tmp_path: Path) -> None:
    """The manual file carries its own README and per-entry evidence notes."""

    csv = tmp_path / "export.csv"
    csv.write_text('"payName","email"\n"Apex Capital","ar@apexcapitalcorp.com"\n', encoding="utf-8")
    manual = tmp_path / "m.json"
    manual.write_text(
        json.dumps({
            "_README": ["why this file exists"],
            "_evidence": {"bobtail capital": "load 2526561"},
            "bobtail capital": ["bobtail.com"],
        }),
        encoding="utf-8",
    )
    out = tmp_path / "roster.json"

    assert gen.main(["gen", str(csv), str(out), str(manual)]) == 0
    roster = json.loads(out.read_text(encoding="utf-8"))
    assert sorted(roster) == ["apex capital", "bobtail capital"]


@pytest.mark.unit
def test_an_absent_manual_file_is_fine(tmp_path: Path) -> None:
    """Generating without corrections must still work — the file is optional."""

    csv = tmp_path / "export.csv"
    csv.write_text('"payName","email"\n"Apex Capital","ar@apexcapitalcorp.com"\n', encoding="utf-8")
    out = tmp_path / "roster.json"

    assert gen.main(["gen", str(csv), str(out), str(tmp_path / "nope.json")]) == 0
    assert json.loads(out.read_text(encoding="utf-8")) == {"apex capital": ["apexcapitalcorp.com"]}


@pytest.mark.unit
def test_a_malformed_manual_entry_fails_loudly(tmp_path: Path) -> None:
    """Silently ignoring a broken entry would authorise nobody and say nothing."""

    manual = tmp_path / "m.json"
    manual.write_text(json.dumps({"bobtail capital": "bobtail.com"}), encoding="utf-8")
    with pytest.raises(ValueError, match="must map to a list"):
        gen._load_manual(manual)


@pytest.mark.unit
def test_regenerating_is_idempotent(tmp_path: Path) -> None:
    """Running twice must not drift — the whole point is surviving regeneration."""

    csv = tmp_path / "export.csv"
    csv.write_text('"payName","email"\n"Nu-Ko Capital","collection@nu-ko.com"\n', encoding="utf-8")
    manual = tmp_path / "m.json"
    manual.write_text(json.dumps({"nu-ko capital": ["nukocapital.com"]}), encoding="utf-8")
    out = tmp_path / "roster.json"

    gen.main(["gen", str(csv), str(out), str(manual)])
    first = out.read_text(encoding="utf-8")
    gen.main(["gen", str(csv), str(out), str(manual)])
    assert out.read_text(encoding="utf-8") == first
