"""Assemble the evidence behind an "unknown factoring sender" escalation.

The roster decision itself is not automatable and must not be. It answers "may this sender
see this carrier's payment data?", and if "absent from the roster" resolved to "add it and
proceed" the roster would stop being a control and become a log of everyone who has ever
written in. Registering a lookalike domain is cheap, and the first reply — amount, schedule,
carrier — is the reconnaissance a payment-redirect attempt is built on. Name resemblance
cannot separate the two: an attacker's lookalike scores exactly as well as the real company.

So this module automates everything *around* the decision instead. When a sender is denied on
a load that does have a factor on file, it gathers what a reviewer would otherwise spend
fifteen minutes gathering — who the factor is, how many of our carriers a grant would cover,
whether our own records already hold a DIFFERENT domain for that factor, and a paste-ready
entry with its evidence skeleton — and hands it over as one block. The human still answers
yes or no; they just no longer have to do the digging first.

The conflict line is the one that earns this module. On 2026-08-13 a Partners Funding
enquiry was authorised from ``getpartnersfunding.com`` while our own factor record carried
``partnersfundinginc.com``. Both are weak sources that disagree, which is exactly the
question to put to the company — and nobody saw it, because nothing put the two facts side
by side.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from payment_bot.config import Settings
from payment_bot.logging import get_logger
from payment_bot.tools.shared import (
    _FREE_MAIL_DOMAINS,
    _factor_names_match,
    _sender_domain,
    company_acronym,
    company_tokens,
)

_log = get_logger("roster_candidate")


@dataclass(frozen=True, slots=True)
class RosterCandidate:
    """One proposed roster entry, with the evidence for and against it."""

    sender_email: str
    sender_domain: str
    factor_on_file: str
    roster_key: str
    load_ids: tuple[str, ...]
    carrier_companies: tuple[str, ...] = ()
    #: Domains the roster ALREADY authorises for this factor. Non-empty means the factor is
    #: known and the sender is using some other domain — the strongest reason to pause.
    configured_domains: tuple[str, ...] = ()
    #: Domains our own records hold for this factor, from the review file when one is
    #: configured. Same signal as above, one step earlier: it fires before any entry exists.
    recorded_domains: tuple[str, ...] = ()
    #: How many of our carriers factor to this company — the breadth of the grant, since an
    #: entry authorises the sender on EVERY load factored to them, not just the one asked about.
    carrier_count: int | None = None
    #: How the domain resembles the factor name, or "" when it does not resemble it at all.
    #: A hint for the reviewer, never evidence: resemblance is what an attacker manufactures.
    resemblance: str = ""
    #: True when the sender is on a free-mail domain. The proposed entry is then their whole
    #: ADDRESS rather than the domain — see :meth:`entry_json`.
    free_mail: bool = False
    #: The addresses already authorised on the carrier's own record. Printed so a near-miss
    #: is visible at a glance, which is the whole diagnosis in the free-mail case.
    carrier_on_file_emails: tuple[str, ...] = ()

    @property
    def conflicting_domains(self) -> tuple[str, ...]:
        """Every domain we already associate with this factor that is NOT the sender's."""

        known = {*self.configured_domains, *self.recorded_domains}
        return tuple(sorted(d for d in known if d != self.sender_domain))

    def entry_json(self) -> str:
        """The line to paste into ``factoring_domains_manual.json``.

        A free-mail sender gets their whole ADDRESS rather than their domain. The roster
        accepts either, and which one it is decides the grant: ``gmail.com`` would authorise
        every Gmail address on earth as this factor, while ``billing@gmail.com`` authorises
        exactly the mailbox that wrote in.
        """

        value = self.sender_email if self.free_mail else self.sender_domain
        return f"  {json.dumps(self.roster_key)}: {json.dumps([value])},"

    def render(self) -> str:
        """The reviewer-facing block."""

        lines = [
            "ROSTER CANDIDATE — a human decides this; nothing has been added.",
            f"  sender        : {self.sender_email}",
            f"  domain        : {self.sender_domain}",
            f"  factor on file: {self.factor_on_file}",
            f"  load(s)       : {', '.join(self.load_ids)}",
        ]
        if self.carrier_companies:
            lines.append(f"  carrier(s)    : {', '.join(self.carrier_companies)}")
        if self.carrier_count is not None:
            lines.append(
                f"  grant breadth : {self.carrier_count:,} carriers factor to this company — "
                "an entry authorises the sender on EVERY one of their loads, not just this."
            )
        lines.append(f"  resemblance   : {self.resemblance or 'none — the domain does not echo the name'}")

        # Suppressed on free-mail: "we hold apexcapitalcorp.com, they wrote from gmail.com"
        # invites comparing two things that were never comparable, and the block below says
        # something more useful about the same sender.
        if self.conflicting_domains and not self.free_mail:
            lines += [
                "",
                "  ** WE ALREADY HOLD A DIFFERENT DOMAIN FOR THIS FACTOR **",
                f"     ours   : {', '.join(self.conflicting_domains)}",
                f"     sender : {self.sender_domain}",
                "     Innocent readings exist — a separate collections or marketing domain is",
                "     ordinary. So is a lookalike. Ask the company which domains are theirs,",
                "     using a contact from OUR records, never one from the mail in question.",
            ]
        elif not self.free_mail:
            lines += [
                "",
                "  We hold no other domain for this factor, so nothing corroborates this one",
                "  except the mail that is asking to be trusted. Verify out of band.",
            ]

        if self.free_mail:
            # A roster value is either a domain or a whole address, and which one it is
            # decides the grant. This packet used to render the DOMAIN for a free-mail
            # sender — "gmail.com" for Apex Capital Corp — as a line to copy, which would
            # have authorised every Gmail address on earth as that factor. The dangerous
            # thing about a paste-ready instruction is precisely that it gets pasted, so it
            # now renders the address and says why.
            lines += [
                "",
                f"  FREE-MAIL SENDER — {self.sender_domain}. The entry below is the WHOLE",
                "  ADDRESS, not the domain, and that distinction is the whole safety of it:",
                f"  \"{self.sender_domain}\" would authorise every address at that provider",
                f"  as {self.factor_on_file}; the address authorises exactly this mailbox.",
                "",
                "  Consider first whether this is the CARRIER writing rather than the factor.",
                "  If it is, the carrier's own record is the better home — the bot answers",
                "  them from then on and everyone reading that record can see why.",
            ]
            if self.carrier_on_file_emails:
                lines += [
                    "",
                    "  Authorised on the carrier's record today:",
                    *(f"     {addr}" for addr in self.carrier_on_file_emails),
                    f"     sender: {self.sender_email}",
                    "",
                    "  COMPARE THOSE CAREFULLY. A free-mail address one word away from a",
                    "  genuine one is both what a carrier's new billing alias looks like and",
                    "  what someone who read the genuine one would register. Nothing in the",
                    "  mail can tell them apart — confirm with the carrier on a number from",
                    "  OUR records before adding it.",
                ]
            else:
                lines += [
                    "",
                    "  The carrier's record carries no addresses at all, so there is nothing",
                    "  to compare this against. Confirm with the carrier before adding it.",
                ]

        lines += [
            "",
            "  If genuine, add to factoring_domains_manual.json:",
            f"{self.entry_json()}",
            "  …with an _evidence note citing the load, then regenerate:",
            "    python scripts/generate_factoring_domains.py <export.csv> "
            "factoring_domains.json factoring_domains_manual.json",
        ]
        return "\n".join(lines)


@dataclass(slots=True)
class _Hints:
    """Factor-name → domains our own records hold, loaded from the review file."""

    by_name: dict[str, tuple[tuple[str, ...], int | None]] = field(default_factory=dict)

    @classmethod
    def load(cls, path: str) -> _Hints:
        """Read the candidates/review file. A missing or malformed file is not fatal.

        This is a review artifact, not runtime configuration — the bot must keep working
        without it. Its absence costs the conflict line and nothing else, so it degrades to
        a slightly thinner packet rather than an escalation that fails to escalate.
        """

        hints = cls()
        try:
            raw = json.loads(Path(path).read_text(encoding="utf-8"))
        except Exception as exc:
            _log.warning("factor_domain_hints_unreadable", extra={"path": path, "error": str(exc)})
            return hints
        for bucket in raw.values() if isinstance(raw, dict) else []:
            if not isinstance(bucket, list):
                continue
            for row in bucket:
                if not isinstance(row, dict):
                    continue
                name = str(row.get("factorName") or "").strip()
                if not name:
                    continue
                domains = tuple(
                    str(d).strip().lower()
                    for d in (row.get("tproDomains") or []) + (row.get("rosterDomains") or [])
                    if str(d).strip()
                )
                count = row.get("carrierCount")
                hints.by_name[name] = (domains, count if isinstance(count, int) else None)
        return hints

    def lookup(self, factor: str) -> tuple[tuple[str, ...], int | None]:
        for name, (domains, count) in self.by_name.items():
            if _factor_names_match(name, factor):
                return domains, count
        return (), None


def _describe_resemblance(domain: str, factor: str) -> str:
    """Why the domain looks like the factor's, in the reviewer's terms.

    Deliberately descriptive rather than scored. A number invites treating resemblance as
    evidence, and it is the one signal an attacker controls completely.
    """

    stem = domain.rsplit(".", 1)[0].replace("-", "").replace(".", "").lower()
    shared = sorted(t for t in company_tokens(factor) if len(t) > 3 and t in stem)
    acronym = company_acronym(factor)
    parts = []
    if shared:
        parts.append(f"contains name word(s) {', '.join(shared)}")
    if acronym and acronym in stem:
        parts.append(f"contains the initials {acronym!r}")
    return "; ".join(parts)


def build_candidate(
    *,
    sender_email: str,
    factor_on_file: str,
    load_ids: tuple[str, ...],
    settings: Settings,
    carrier_companies: tuple[str, ...] = (),
    carrier_on_file_emails: tuple[str, ...] = (),
) -> RosterCandidate | None:
    """Assemble the packet, or ``None`` when a roster entry is not the missing piece.

    Returns ``None`` when the load carries no factor: the sender is then simply not a party
    to it, and proposing a roster entry would invite authorising a stranger to fix an
    escalation that is working correctly.
    """

    domain = _sender_domain(sender_email)
    if not (domain and factor_on_file.strip()):
        return None

    factor = factor_on_file.strip()
    configured = tuple(
        sorted(
            {
                str(d).strip().lower().lstrip("@")
                for name, domains in settings.factoring_domains.items()
                if _factor_names_match(name, factor)
                for d in domains
            }
        )
    )
    recorded: tuple[str, ...] = ()
    count: int | None = None
    if settings.factor_domain_hints_file:
        recorded, count = _Hints.load(settings.factor_domain_hints_file).lookup(factor)

    return RosterCandidate(
        sender_email=sender_email,
        sender_domain=domain,
        factor_on_file=factor,
        # Lowercased bare name: the roster is keyed that way and _factor_names_match
        # normalises punctuation, so this links to the load without widening anything.
        roster_key=factor.lower(),
        load_ids=load_ids,
        carrier_companies=carrier_companies,
        configured_domains=configured,
        recorded_domains=recorded,
        carrier_count=count,
        resemblance=_describe_resemblance(domain, factor),
        free_mail=domain in _FREE_MAIL_DOMAINS,
        carrier_on_file_emails=carrier_on_file_emails,
    )


def log_candidate(candidate: RosterCandidate, correlation_id: str) -> dict[str, Any]:
    """Emit the packet as a structured event and return the payload.

    Structured rather than only rendered, so the deployed worker's log carries it to
    CloudWatch and a metric filter can count how often this shape arrives — the number that
    says whether the roster is keeping up with real mail.
    """

    payload = {
        "sender_domain": candidate.sender_domain,
        "factor_on_file": candidate.factor_on_file,
        "load_ids": list(candidate.load_ids),
        "conflicting_domains": list(candidate.conflicting_domains),
        "carrier_count": candidate.carrier_count,
        "resemblance": candidate.resemblance,
    }
    _log.info("roster_candidate", extra={"correlation_id": correlation_id, **payload})
    return payload
