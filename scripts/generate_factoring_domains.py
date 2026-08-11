"""Generate a factoring-domains JSON file from the settlement system's factor export.

Input: a CSV with columns ``payName`` (factoring company, exactly as recorded on loads)
and ``email`` (remit contact, possibly several, possibly NULL). This is the same table
payments are remitted against, so a domain here carries the organisation's own operational
trust — stronger evidence than anything scraped off the web.

Output: ``{"<payname lowercased>": ["domain", ...], ...}`` for ``PAYBOT_FACTORING_DOMAINS_FILE``.

Deliberately skipped, with a count reported per reason:

* rows with no email (nothing to derive a domain from);
* rows whose name is marked dead — DNU / "do not use" / duplicate / "wrong company";
* free-mail and ISP domains (gmail.com, comcast.net, …) — a factor that remits to a Gmail
  address must NOT make every Gmail sender that factor. Uses the same exclusion list as
  ``check_authorization``'s domain matching.

Hand-verified corrections live in ``factoring_domains_manual.json`` beside the output and are
merged over the generated entries — union per company, so a manual send-from domain adds to
the remit domain the export gave rather than replacing it. Regenerating therefore never drops
them.

Usage::

    python scripts/generate_factoring_domains.py <export.csv> <factoring_domains.json>
    python scripts/generate_factoring_domains.py <export.csv> <out.json> <manual.json>
"""

from __future__ import annotations

import csv
import json
import re
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from payment_bot.tools.shared import _FREE_MAIL_DOMAINS  # one exclusion list, not two

#: Names that mean the row is dead data, not a factor.
_DEAD_NAME_RE = re.compile(r"\bdnu\b|do\s*not\s*use|duplicate|wrong\s+company|\*\*", re.IGNORECASE)

_DOMAIN_RE = re.compile(r"^[a-z0-9][a-z0-9.-]*\.[a-z]{2,}$")


def _domains_of(email_field: str) -> set[str]:
    """Every plausible domain in a free-form email field.

    The export is hand-typed: addresses are separated by ``,`` ``;`` or spaces, and one
    live row reads ``"payments @flexent.com"`` — so anything containing ``@`` is treated
    as an address and the part after the last ``@`` kept if it looks like a domain.
    """

    domains: set[str] = set()
    for token in re.split(r"[,;\s]+", email_field.strip()):
        if "@" not in token:
            continue
        domain = token.rsplit("@", 1)[-1].strip().strip(".").lower()
        if _DOMAIN_RE.match(domain):
            domains.add(domain)
    return domains


def generate(csv_path: Path) -> tuple[dict[str, list[str]], Counter[str]]:
    """Build the name → domains map plus per-reason skip counts."""

    result: dict[str, set[str]] = {}
    skipped: Counter[str] = Counter()

    with csv_path.open(encoding="utf-8-sig", newline="") as handle:
        for row in csv.DictReader(handle):
            name = (row.get("payName") or "").strip()
            email_field = (row.get("email") or "").strip()
            if not name:
                skipped["no name"] += 1
                continue
            if _DEAD_NAME_RE.search(name):
                skipped["marked DNU/duplicate"] += 1
                continue
            if not email_field or email_field.upper() == "NULL":
                skipped["no email on record"] += 1
                continue

            domains = _domains_of(email_field)
            free_mail = {d for d in domains if d in _FREE_MAIL_DOMAINS}
            if free_mail:
                skipped["free-mail domain dropped"] += len(free_mail)
            usable = domains - free_mail
            if not usable:
                skipped["no usable domain"] += 1
                continue
            result.setdefault(name.lower(), set()).update(usable)

    return {name: sorted(domains) for name, domains in sorted(result.items())}, skipped


#: Hand-verified entries, merged over the generated ones. Sits beside the output by default.
#:
#: The export records where we REMIT; factors SEND from somewhere else, and rows with a NULL
#: email are skipped entirely — so some gaps are structural and no regeneration will close
#: them. Those corrections used to live in ``PAYBOT_FACTORING_DOMAINS`` in ``.env``, which put
#: business data in a credentials file and made it invisible to anyone reading the roster.
#: Keeping them in their own file, merged here, means regenerating never silently drops them.
_MANUAL_FILENAME = "factoring_domains_manual.json"


def _load_manual(path: Path) -> dict[str, list[str]]:
    """Read the hand-verified patch file. Absent is fine; malformed is not.

    Keys beginning with ``_`` are documentation, not companies — the file carries its own
    README and a per-entry evidence note, because a domain that authorises a disclosure
    should not be a bare string with nobody able to say where it came from. They are skipped
    here rather than in the caller so no consumer of this function ever sees them.
    """

    if not path.is_file():
        return {}
    loaded = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(loaded, dict):
        raise ValueError(f"{path} must hold a JSON object of name -> [domains]")

    out: dict[str, list[str]] = {}
    for key, value in loaded.items():
        name = str(key).strip().lower()
        if not name or name.startswith("_"):
            continue
        if not isinstance(value, list):
            raise ValueError(f"{path}: {key!r} must map to a list of domains, got {type(value).__name__}")
        out[name] = [str(d).strip().lower() for d in value if str(d).strip()]
    return out


def main(argv: list[str]) -> int:
    if len(argv) not in (3, 4):
        print(__doc__)
        return 2
    csv_path, out_path = Path(argv[1]), Path(argv[2])
    manual_path = Path(argv[3]) if len(argv) == 4 else out_path.parent / _MANUAL_FILENAME

    mapping, skipped = generate(csv_path)
    generated_count = len(mapping)

    manual = _load_manual(manual_path)
    for name, domains in manual.items():
        # Union, not replace: a manual entry adds a send-from domain without discarding the
        # remit domain the export supplied for the same company.
        mapping[name] = sorted(set(mapping.get(name, [])) | set(domains))

    out_path.write_text(json.dumps(mapping, indent=1) + "\n", encoding="utf-8")

    total_domains = sum(len(d) for d in mapping.values())
    print(f"wrote {out_path}: {len(mapping)} factoring companies, {total_domains} domains")
    if manual:
        added = len(mapping) - generated_count
        print(f"  merged {len(manual)} hand-verified entr{'y' if len(manual) == 1 else 'ies'} "
              f"from {manual_path.name} ({added} new compan{'y' if added == 1 else 'ies'})")
    else:
        print(f"  no {manual_path.name} found — generated entries only")
    for reason, count in skipped.most_common():
        print(f"  skipped {count:>4}  {reason}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
