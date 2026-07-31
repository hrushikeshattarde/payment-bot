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

Usage::

    python scripts/generate_factoring_domains.py <export.csv> <factoring_domains.json>
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


def main(argv: list[str]) -> int:
    if len(argv) != 3:
        print(__doc__)
        return 2
    csv_path, out_path = Path(argv[1]), Path(argv[2])

    mapping, skipped = generate(csv_path)
    out_path.write_text(json.dumps(mapping, indent=1) + "\n", encoding="utf-8")

    total_domains = sum(len(d) for d in mapping.values())
    print(f"wrote {out_path}: {len(mapping)} factoring companies, {total_domains} domains")
    for reason, count in skipped.most_common():
        print(f"  skipped {count:>4}  {reason}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
