"""Find the carrier addresses our records are missing, from mail we have already answered.

Half the carriers on the CargoTel path are on free mail — measured across six live records,
three list *only* a Gmail address and each of those lists exactly one. A carrier running
dispatch, accounting and billing mailboxes therefore has one address that authorizes and the
rest that escalate, because on free mail the exact address is the only thing that can prove
anything. Two such escalations arrived on 2026-08-13 alone, and "no authorized party match"
is the largest single bucket in the escalation log.

Adding each address to config as it turns up treats the symptom one email at a time. The
records are simply incomplete, and the evidence needed to complete them is already sitting in
the mailbox: every address that has written in about a load, and whether a colleague replied.

So this reads that history, groups the addresses by the carrier whose loads they wrote about,
and reports the ones missing from that carrier's record.

**It decides nothing and changes nothing.** Output is a review file. Two addresses can write
about the same carrier's loads and only one of them be the carrier — that is precisely the
case a human must judge, and the strongest signal here (a colleague replied) is evidence
about what already happened, never authorization.

Usage::

    python scripts/discover_carrier_contacts.py --days 180
    python scripts/discover_carrier_contacts.py --days 365 --limit 800 --out contacts.json
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from payment_bot.clients import (
    build_cargotel_client,
    build_gmail_api_client,
    build_transport_pro_client,
)
from payment_bot.config import get_settings
from payment_bot.domain import route_load
from payment_bot.models import InboundEmail, System
from payment_bot.tools.shared import _FREE_MAIL_DOMAINS, _load_ids_in

#: Written next to the roster's own review file, and ignored by git for the same reason:
#: it maps real company names to real addresses.
DEFAULT_OUT = "carrier_contacts_CANDIDATES_REVIEW_ONLY.json"


@dataclass
class Sighting:
    """One external address seen writing about one carrier's loads."""

    address: str
    messages: int = 0
    loads: set[str] = field(default_factory=set)
    subjects: list[str] = field(default_factory=list)
    #: Threads this address appears in, so the next field can be worked out.
    threads: set[str] = field(default_factory=set)
    #: True when someone on our side has written in a thread this address is part of. The
    #: strongest signal available, because it means a human already engaged with them — and
    #: still only evidence: a colleague may have replied to say "who are you?".
    answered_by_us: bool = False


def _external(address: str, settings: Any) -> bool:
    """Ours and the group's own address are not carrier contacts."""

    address = address.strip().lower()
    if not address or "@" not in address:
        return False
    domain = address.split("@")[-1]
    ours = {
        (settings.mailbox or "").split("@")[-1].lower(),
        (settings.gmail_user or "").split("@")[-1].lower(),
    }
    return domain not in {d for d in ours if d}


def _carrier_for(load_id: str, tp: Any, cgt: Any) -> str | None:
    system = route_load(load_id).system
    try:
        if system is System.QUICKBOOKS:
            return None if cgt is None else cgt.get_authorization_context(load_id).carrier_label
        if system is System.TRANSPORT_PRO:
            return tp.get_authorization_context(load_id).carrier_label
    except Exception:
        # A load that will not resolve tells us nothing about who may write about it. It is
        # not an error to report: phantom ids reach this mailbox constantly.
        return None
    return None


def collect(emails: list[InboundEmail], settings: Any, tp: Any, cgt: Any) -> dict[str, dict[str, Sighting]]:
    """Group external senders by the carrier whose loads they wrote about."""

    by_carrier: dict[str, dict[str, Sighting]] = defaultdict(dict)
    resolved: dict[str, str | None] = {}
    for message in emails:
        sender = (message.from_email or "").strip().lower()
        if not _external(sender, settings):
            continue
        text = "\n".join(p for p in (message.subject, message.body, message.html_text) if p)
        for load_id in dict.fromkeys(_load_ids_in(text)):
            if load_id not in resolved:
                resolved[load_id] = _carrier_for(load_id, tp, cgt)
            carrier = resolved[load_id]
            if not carrier:
                continue
            seen = by_carrier[carrier].setdefault(sender, Sighting(address=sender))
            seen.messages += 1
            seen.loads.add(load_id)
            if message.thread_id:
                seen.threads.add(message.thread_id)
            if message.subject and message.subject not in seen.subjects:
                seen.subjects.append(message.subject[:80])
    return by_carrier


def _classify(address: str, on_record: set[str], roster_domains: set[str]) -> str:
    """What KIND of party this address is, which decides where it belongs — or whether it does.

    The first sweep made the mistake this exists to prevent: it listed
    ``collections@operfi.com`` and ``jtagulao@gsquaredfunding.com`` under the carriers whose
    loads they wrote about, as if they were carrier staff. They are factors. Putting a
    factor's address on a carrier's contact record would authorise that factor for every one
    of that carrier's loads through the carrier door, bypassing the roster entirely — which
    is the check built to decide exactly that question.

    * ``carrier-colleague`` — shares a real domain with an address already on the record.
      Someone at the same company; the record simply does not list them yet. The clearest
      case, and the one worth acting on first.
    * ``known-factor`` — the domain is in the factoring roster. Belongs there, not here, and
      already authorises on loads factored to them.
    * ``unknown`` — everything else. A free-mail address the carrier may or may not own, a
      third-party service, a stranger. Judgement, every time.
    """

    domain = address.split("@")[-1].lower()
    if domain in roster_domains:
        return "known-factor"
    if domain not in _FREE_MAIL_DOMAINS and domain in {a.split("@")[-1] for a in on_record}:
        return "carrier-colleague"
    return "unknown"


def review_rows(
    by_carrier: dict[str, dict[str, Sighting]],
    tp: Any,
    cgt: Any,
    roster_domains: set[str] | None = None,
) -> list[dict[str, Any]]:
    """One row per carrier, splitting what is already on record from what is not."""

    roster_domains = roster_domains or set()

    rows: list[dict[str, Any]] = []
    for carrier, sightings in sorted(by_carrier.items()):
        on_record: set[str] = set()
        for sighting in sightings.values():
            for load_id in sighting.loads:
                try:
                    system = route_load(load_id).system
                    client = cgt if system is System.QUICKBOOKS else tp
                    if client is None:
                        continue
                    ctx = client.get_authorization_context(load_id)
                    on_record.update(e.strip().lower() for e in ctx.authorized_emails)
                except Exception:
                    continue
            break  # one resolvable load is enough; the record is per carrier

        missing = [s for a, s in sorted(sightings.items()) if a not in on_record]
        if not missing:
            continue
        rows.append(
            {
                "carrier": carrier,
                "onRecord": sorted(on_record),
                "recordIsFreeMailOnly": bool(on_record)
                and all(a.split("@")[-1] in _FREE_MAIL_DOMAINS for a in on_record),
                "missing": [
                    {
                        "address": s.address,
                        "freeMail": s.address.split("@")[-1] in _FREE_MAIL_DOMAINS,
                        "messages": s.messages,
                        "loads": sorted(s.loads),
                        "subjects": s.subjects[:3],
                        # The strongest signal in this file, and still only evidence: a
                        # colleague may have replied to ask who they were.
                        "answeredByUs": s.answered_by_us,
                        "kind": _classify(s.address, on_record, roster_domains),
                    }
                    for s in sorted(missing, key=lambda s: -s.messages)
                ],
            }
        )
    return rows


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--days", type=int, default=180, help="how far back to read")
    parser.add_argument("--limit", type=int, default=500, help="max messages to fetch")
    parser.add_argument("--out", default=DEFAULT_OUT)
    parser.add_argument(
        "--query",
        default="",
        help="override the Gmail query entirely (default: mail to the monitored mailbox)",
    )
    args = parser.parse_args(argv)

    settings = get_settings()
    mailbox = settings.gmail_user or settings.mailbox
    query = args.query or f"to:{settings.mailbox} newer_than:{args.days}d"

    print(f"reading {mailbox} — query: {query!r}  (max {args.limit} messages)")
    gmail = build_gmail_api_client(settings)
    emails = gmail.search(query, limit=args.limit)
    print(f"  {len(emails)} messages")

    tp = build_transport_pro_client(settings)
    cgt = (
        build_cargotel_client(settings)
        if settings.cargotel_replies and settings.cargotel_configured
        else None
    )
    if cgt is None:
        print("  ! CargoTel is off or unconfigured — 6-digit loads will be skipped")

    by_carrier = collect(emails, settings, tp, cgt)

    # Threads someone on our side has written in. A human replying to an address is the
    # nearest thing to a vouch that this mailbox contains — worth surfacing, never enough
    # to authorise on, since the reply may have been "who are you?".
    answered = {
        m.thread_id
        for m in gmail.search(f"from:{mailbox} newer_than:{args.days}d", limit=args.limit)
        if m.thread_id
    }
    for sightings in by_carrier.values():
        for sighting in sightings.values():
            sighting.answered_by_us = bool(sighting.threads & answered)
    print(f"  {len(answered)} threads we have replied in")

    roster_domains = {
        str(d).strip().lower().lstrip("@")
        for domains in settings.factoring_domains.values()
        for d in domains
    }
    rows = review_rows(by_carrier, tp, cgt, roster_domains)

    out = Path(args.out)
    out.write_text(
        json.dumps({"carriersWithMissingContacts": rows}, indent=1), encoding="utf-8"
    )

    # Grouped by kind rather than by carrier. A flat per-carrier list buries the dozen
    # obvious wins among factors that must never be added, which is how the first run read.
    flat = [(r, m) for r in rows for m in r["missing"]]
    print(f"\n  {len(rows)} carriers with addresses not on their record ({len(flat)} addresses)")
    for kind, heading in (
        ("carrier-colleague", "SAME COMPANY as an address already on the record — add these"),
        ("unknown", "UNKNOWN — judgement needed on every one"),
        ("known-factor", "KNOWN FACTOR — belongs in the roster, NOT on a carrier record"),
    ):
        group = [(r, m) for r, m in flat if m["kind"] == kind]
        print(f"\n  {heading}  ({len(group)})")
        for row, miss in group[:20]:
            marks = ("  [we have replied]" if miss["answeredByUs"] else "") + (
                "  [free-mail]" if miss["freeMail"] else ""
            )
            print(f"     {row['carrier'][:32]:<32} {miss['address']}{marks}")
        if len(group) > 20:
            print(f"     … {len(group) - 20} more in the file")
    print(f"\nwrote {out}")
    print(
        "\nNOTHING WAS ADDED. Confirm each address with the carrier on a number from OUR\n"
        "records — not one from the mail — then put it on their record in the back office.\n"
        "PAYBOT_CARRIER_CONTACTS is the stopgap; the record is where it belongs."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
