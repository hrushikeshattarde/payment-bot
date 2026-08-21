"""Auto-rostering an unknown factoring sender, and the four cases it still refuses.

``PAYBOT_AUTO_ADD_FACTORING_DOMAINS`` answers instead of escalating when a sender's domain is
missing from the roster: it writes the domain under the factor the LOAD already names, then
re-runs authorization. Off by default.

It is the most consequential switch in the system, and what it trades away is stated plainly in
``Settings.auto_add_factoring_domains`` and argued against at length in
``payment_bot.roster_candidate``. Enabled, the roster stops being a list of domains somebody
verified and becomes a list of domains that wrote in and named a factor already on the load.

So the tests that matter most here are the refusals. Each one is a case where auto-adding would
make the roster meaningless rather than merely permissive.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from payment_bot.config import Settings
from payment_bot.errors import ClientError, ConfigError
from payment_bot.grounding import GroundingLedger
from payment_bot.models import AuthorizationContext, InboundEmail
from payment_bot.pipeline import PaymentBotPipeline
from payment_bot.roster_candidate import append_manual_entries
from payment_bot.tools.base import ToolContext

pytestmark = pytest.mark.unit

LOAD = "2534786"
FACTOR = "TFC"


class _Tp:
    def __init__(self, factoring_company: str | None = FACTOR, raises: bool = False) -> None:
        self._factor = factoring_company
        self._raises = raises

    def get_authorization_context(self, load_id: str) -> AuthorizationContext:
        if self._raises:
            raise ClientError(f"load {load_id} unreadable")
        return AuthorizationContext(
            carrier_companies=("American Logistics Prime Line Inc",),
            authorized_emails=("dispatch@alpl.example",),
            payable_parties=(("American Logistics Prime Line Inc", self._factor),),
        )


def _pipeline(settings: Settings, tp: _Tp) -> tuple[PaymentBotPipeline, ToolContext]:
    pipeline = PaymentBotPipeline.__new__(PaymentBotPipeline)
    pipeline._settings = settings  # type: ignore[attr-defined]
    pipeline._today = __import__("datetime").date(2026, 8, 17)  # type: ignore[attr-defined]
    ctx = ToolContext(tp=tp, ledger=GroundingLedger(), correlation_id="auto", settings=settings)
    return pipeline, ctx


def _settings(tmp_path: Path, sender_domains: dict[str, tuple[str, ...]] | None = None) -> Settings:
    roster = tmp_path / "factoring_domains.json"
    roster.write_text("{}", encoding="utf-8")
    return Settings(
        _env_file=None,
        allow_factoring=True,
        auto_add_factoring_domains=True,
        factoring_domains_file=str(roster),
        factoring_domains=sender_domains or {},  # type: ignore[arg-type]
    )


def _email(sender: str = "team-a@tfcfactoring.com") -> InboundEmail:
    return InboundEmail(
        message_id="<m>",
        thread_id="t",
        from_email=sender,
        from_name="T|F|C",
        subject=f"Verification - {LOAD}",
        body=f"Please verify the rate on load {LOAD}.",
    )


# --- the happy path ---------------------------------------------------------
def test_the_domain_is_added_under_the_factor_the_load_names(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    pipeline, ctx = _pipeline(settings, _Tp())

    added = pipeline._auto_add_factoring_domains(_email(), (LOAD,), ctx, "auto")

    assert added is True
    # Widened in memory for the rest of the run...
    assert ctx.settings.factoring_domains["tfc"] == ("tfcfactoring.com",)
    # ...and persisted where a regeneration will keep it.
    manual = tmp_path / "factoring_domains_manual.json"
    import json

    written = json.loads(manual.read_text(encoding="utf-8"))
    assert written["tfc"] == ["tfcfactoring.com"]
    assert "AUTO-ADDED" in written["_evidence"]["tfc"]
    assert "Nobody verified it" in written["_evidence"]["tfc"]


def test_the_evidence_note_records_what_was_and_was_not_established(tmp_path: Path) -> None:
    """An auto-added entry must be findable and revocable, not anonymous."""

    settings = _settings(tmp_path)
    pipeline, ctx = _pipeline(settings, _Tp())
    pipeline._auto_add_factoring_domains(_email(), (LOAD,), ctx, "auto")

    import json

    note = json.loads((tmp_path / "factoring_domains_manual.json").read_text(encoding="utf-8"))[
        "_evidence"
    ]["tfc"]

    assert "PAYBOT_AUTO_ADD_FACTORING_DOMAINS" in note
    assert LOAD in note, "the load whose factor record supplied the company"
    assert "team-a@tfcfactoring.com" in note
    assert "2026-08-17" in note


# --- the four refusals ------------------------------------------------------
def test_a_free_mail_sender_is_never_auto_added(tmp_path: Path) -> None:
    """The domain form would authorise every mailbox at that provider, and be inert anyway."""

    settings = _settings(tmp_path)
    pipeline, ctx = _pipeline(settings, _Tp())

    added = pipeline._auto_add_factoring_domains(
        _email("tfcfactoring@gmail.com"), (LOAD,), ctx, "auto"
    )

    assert added is False
    assert not (tmp_path / "factoring_domains_manual.json").exists()


def test_a_domain_rostered_to_another_company_is_never_auto_added(tmp_path: Path) -> None:
    """The Faro/BasicBlock shape: one factor's real domain on another factor's load.

    Auto-adding it would let one company answer for another's loads, which is the single thing
    the per-load factor match exists to prevent.
    """

    settings = _settings(tmp_path, {"basicblock inc.": ("tfcfactoring.com",)})
    pipeline, ctx = _pipeline(settings, _Tp())

    added = pipeline._auto_add_factoring_domains(_email(), (LOAD,), ctx, "auto")

    assert added is False
    assert not (tmp_path / "factoring_domains_manual.json").exists()


def test_a_load_with_no_factor_on_file_is_never_used(tmp_path: Path) -> None:
    """With no factor named on the load there is no company to attach the domain to.

    This is the guard that keeps the COMPANY half coming from our data rather than the mail:
    without it, any sender could be rostered against a load carrying no factor at all.
    """

    settings = _settings(tmp_path)
    pipeline, ctx = _pipeline(settings, _Tp(factoring_company=None))

    added = pipeline._auto_add_factoring_domains(_email(), (LOAD,), ctx, "auto")

    assert added is False


def test_an_unreadable_load_is_skipped_not_fatal(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    pipeline, ctx = _pipeline(settings, _Tp(raises=True))

    assert pipeline._auto_add_factoring_domains(_email(), (LOAD,), ctx, "auto") is False


def test_nothing_happens_when_the_switch_is_off(tmp_path: Path) -> None:
    """The default. The escalation path is untouched unless a deployment opts in."""

    assert Settings(_env_file=None).auto_add_factoring_domains is False


# --- the writer -------------------------------------------------------------
def test_append_unions_and_never_replaces(tmp_path: Path) -> None:
    roster = tmp_path / "factoring_domains.json"
    roster.write_text("{}", encoding="utf-8")
    manual = tmp_path / "factoring_domains_manual.json"
    import json

    manual.write_text(
        json.dumps({"tfc": ["existing.example"], "_evidence": {"tfc": "prior"}}),
        encoding="utf-8",
    )
    settings = Settings(
        _env_file=None,
        factoring_domains_file=str(roster),
        factoring_domains={"tfc": ("existing.example",)},  # type: ignore[arg-type]
    )

    widened = append_manual_entries({"tfc": "tfcfactoring.com"}, note="n", settings=settings)

    written = json.loads(manual.read_text(encoding="utf-8"))
    assert written["tfc"] == ["existing.example", "tfcfactoring.com"]
    assert written["_evidence"]["tfc"].startswith("prior || ")
    assert list(written)[-1] == "_evidence"
    assert widened.factoring_domains["tfc"] == ("existing.example", "tfcfactoring.com")


def test_append_refuses_when_no_roster_file_is_configured() -> None:
    """Silently doing nothing would leave the caller believing an entry exists."""

    with pytest.raises(ConfigError, match="PAYBOT_FACTORING_DOMAINS_FILE is unset"):
        append_manual_entries(
            {"tfc": "tfcfactoring.com"}, note="n", settings=Settings(_env_file=None)
        )
