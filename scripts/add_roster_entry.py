"""Add a hand-verified factoring domain to the roster, with the checks done for you.

Every roster addition in this repo has needed the same five things done by hand: check the
domain is not free mail, check the key does not also match an unrelated company, check the
domain is not already rostered to somebody else, write an evidence note, and regenerate. That
is a fifteen-minute job per entry, which is why it has been getting done in conversation
instead. This does all of it in one command and refuses the additions that are unsafe.

    python scripts/add_roster_entry.py "acme factoring, llc" collections.acmefactoring.com \
        --evidence "sender ar@collections.acmefactoring.com, factor of record on the load" \
        --load 2462934 --sender ar@collections.acmefactoring.com

The example is a placeholder on purpose. A real key-and-domain pair here would publish one
roster entry into a public repo, which is the thing ``factoring_domains_manual.json`` is
gitignored to prevent.

Dry run by default in the sense that nothing is written until the checks pass and you confirm;
pass --yes to skip the prompt in a script.

WHAT THIS DELIBERATELY DOES NOT DO: decide. It will not add an entry on its own, and the bot
will never call it. See payment_bot.roster_candidate's module docstring — if "absent from the
roster" resolved to "add it and proceed", the roster would stop being a control and become a
log of everyone who has ever written in. This removes the typing, not the judgement.
"""

from __future__ import annotations

import argparse
import csv
import json
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

from payment_bot.tools.shared import (  # noqa: E402
    _FREE_MAIL_DOMAINS,
    _factor_names_match,
)

MANUAL = REPO / "factoring_domains_manual.json"
ROSTER = REPO / "factoring_domains.json"
GENERATOR = REPO / "scripts" / "generate_factoring_domains.py"


def _export_names(csv_path: Path) -> set[str]:
    names: set[str] = set()
    if not csv_path.is_file():
        return names
    with csv_path.open(encoding="utf-8-sig", newline="") as handle:
        for row in csv.DictReader(handle):
            name = (row.get("payName") or "").strip()
            if name:
                names.add(name)
    return names


def _check_value(value: str) -> list[str]:
    """Refusals for one roster value. Empty list means it is acceptable."""

    problems: list[str] = []
    bare = value.lstrip("@").lower()
    if "@" in value.lstrip("@"):
        # A whole address. Free mail is fine here — that is the supported way to authorise a
        # factor whose send-from is free mail, and it grants exactly that mailbox.
        return problems
    if bare in _FREE_MAIL_DOMAINS:
        problems.append(
            f"{value!r} is a bare free-mail domain, which would authorise every mailbox at "
            f"{bare} as this factor. _roster_entry_matches refuses it at lookup anyway, so "
            f"the entry would be silently inert. Use the whole ADDRESS instead "
            f"(e.g. billing@{bare})."
        )
    if "." not in bare:
        problems.append(f"{value!r} does not look like a domain or an address")
    return problems


def _collateral(key: str, names: set[str]) -> list[str]:
    return sorted(n for n in names if _factor_names_match(key, n))


def _already_rostered_elsewhere(value: str, roster: dict[str, list[str]], key: str) -> list[str]:
    """Keys OTHER than this one that already carry this value.

    The check that would have caught the Faro/BasicBlock case: basicblock.io was already
    rostered to BasicBlock when it was proposed for a Faro load, which made it a known
    company's real domain arriving on another factor's load rather than an unknown lookalike.
    """

    wanted = value.lstrip("@").lower()
    return sorted(
        other
        for other, domains in roster.items()
        if other != key and any(str(d).lstrip("@").lower() == wanted for d in domains)
    )


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("key", help="factor name as the LOAD spells it, lowercased")
    parser.add_argument("values", nargs="+", help="domain(s) or whole address(es) to authorise")
    parser.add_argument("--evidence", required=True, help="why this is genuine; cite the load")
    parser.add_argument("--load", default="", help="load id the evidence rests on")
    parser.add_argument("--sender", default="", help="verify this sender authorises afterwards")
    parser.add_argument("--export", default="", help="settlements CSV (for the collateral check)")
    parser.add_argument("--yes", action="store_true", help="skip the confirmation prompt")
    args = parser.parse_args(argv[1:])

    key = args.key.strip().lower()
    if not key:
        print("ERROR: key is empty")
        return 2

    manual = json.loads(MANUAL.read_text(encoding="utf-8")) if MANUAL.is_file() else {}
    roster = json.loads(ROSTER.read_text(encoding="utf-8")) if ROSTER.is_file() else {}

    export = Path(args.export) if args.export else next(iter(REPO.glob("data-*.csv")), None)
    names = _export_names(export) if export else set()

    print(f"key    : {key!r}")
    print(f"values : {args.values}")
    print(f"export : {export.name if export else '(none found — collateral check SKIPPED)'}")
    print()

    problems: list[str] = []
    for value in args.values:
        problems.extend(_check_value(value))
        elsewhere = _already_rostered_elsewhere(value, roster, key)
        if elsewhere:
            print(f"** {value!r} IS ALREADY ROSTERED TO: {elsewhere}")
            print("   That makes this a known company's real domain arriving on another")
            print("   factor's load, not an unknown one. Adding it here lets one company")
            print("   answer for another's loads. Confirm that is what you mean.")
            print()

    if names:
        hits = _collateral(key, names)
        print(f"collateral: this key matches {len(hits)} of {len(names)} export names")
        for name in hits:
            print(f"    {name}")
        if not hits:
            print("    (none — the key matches no factor on any load, so it can never fire)")
            problems.append(
                f"{key!r} matches no factor name in the export; check the spelling against "
                "the load's factor field"
            )
        print()

    existing = manual.get(key, [])
    merged = sorted({*(str(v) for v in existing), *(v.strip() for v in args.values)})
    print(f"before : {existing or '(new key)'}")
    print(f"after  : {merged}")
    print()

    if problems:
        print("REFUSED:")
        for problem in problems:
            print(f"  - {problem}")
        return 1

    if not args.yes:
        reply = input("write this entry and regenerate? [y/N] ").strip().lower()
        if reply != "y":
            print("aborted; nothing written")
            return 1

    note = args.evidence.strip()
    if args.load and args.load not in note:
        note = f"load {args.load}: {note}"
    evidence = manual.setdefault("_evidence", {})
    if not isinstance(evidence, dict):
        print("ERROR: _evidence is not an object")
        return 2
    prior = evidence.get(key)
    evidence[key] = f"{prior} || {note}" if prior else note

    manual[key] = merged
    # `_evidence` last, so the file keeps reading as entries-then-notes.
    ordered = {k: v for k, v in manual.items() if k != "_evidence"}
    ordered["_evidence"] = evidence
    MANUAL.write_text(json.dumps(ordered, indent=1) + "\n", encoding="utf-8")
    print(f"wrote {MANUAL.name}")

    if not export:
        print("no settlements CSV found — regenerate by hand once you have one")
        return 0
    result = subprocess.run(
        [sys.executable, str(GENERATOR), str(export), str(ROSTER), str(MANUAL)],
        check=False,
    )
    if result.returncode != 0:
        return result.returncode

    if args.sender:
        import types

        from payment_bot.config import Settings
        from payment_bot.tools.shared import _configured_factor_domain

        settings = Settings()
        ctx = types.SimpleNamespace(settings=settings)
        for name in _collateral(key, names) or [key]:
            ok = _configured_factor_domain([name], args.sender, ctx) is not None
            print(f"  {'AUTHORISED' if ok else 'still DENIED'}: {args.sender} on {name!r}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
