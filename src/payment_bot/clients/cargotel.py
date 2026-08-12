"""CargoTel client (6-digit loads).

The :class:`CargoTelClient` protocol is the seam between our tools and the CargoTel
back-office, exactly as :class:`~payment_bot.clients.transport_pro.TransportProClient` is
for Transport Pro. :class:`MockCargoTelClient` is the fixture-backed implementation tests
and the demo run against; :mod:`payment_bot.clients.cargotel_http` is the live one.

The method set is deliberately small. CargoTel exposes one page per load and everything the
reply needs is on it, so there is a single read — no dispatch history, no settlement table,
no separate file-history call. Methods that cannot be answered from that page are absent
rather than present-and-empty, so a tool cannot mistake "not available here" for "nothing
on file".

``get_authorization_context`` returns the same
:class:`~payment_bot.models.AuthorizationContext` the Transport Pro path returns, so
``check_authorization`` can apply one matching policy to both systems. It is assembled from
the carrier's client record, which is a second fetch — the load page names the carrier but
carries no address.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from payment_bot.errors import ClientError
from payment_bot.models import AuthorizationContext
from payment_bot.models.cargotel import CargoTelCarrier, CargoTelLoad


@runtime_checkable
class CargoTelClient(Protocol):
    """Read-only access to CargoTel load data."""

    def get_load(self, load_id: str) -> CargoTelLoad:
        """Return the parsed load page.

        Raise :class:`~payment_bot.errors.ClientError` when the load cannot be read — which
        includes the session cookie having expired, because CargoTel serves the login page
        with HTTP 200 and that must never be parsed as an empty load.
        """

    def get_carrier(self, client_id: str) -> CargoTelCarrier:
        """Return the carrier's client record — contacts, factoring name, default terms."""

    def get_authorization_context(self, load_id: str) -> AuthorizationContext:
        """Return who may receive disclosure about this load."""


@dataclass(frozen=True, slots=True)
class CargoTelLoadFixture:
    """One mock load, and the carrier record it points at."""

    load: CargoTelLoad
    carrier: CargoTelCarrier | None = None


class MockCargoTelClient:
    """In-memory, fixture-backed :class:`CargoTelClient` for tests and the demo."""

    def __init__(self, fixtures: dict[str, CargoTelLoadFixture] | None = None) -> None:
        self._fixtures: dict[str, CargoTelLoadFixture] = dict(fixtures or {})

    def add(self, fixture: CargoTelLoadFixture) -> None:
        """Register (or replace) a fixture keyed by its load id."""

        self._fixtures[fixture.load.load_id] = fixture

    def _fixture(self, load_id: str) -> CargoTelLoadFixture:
        try:
            return self._fixtures[load_id.strip()]
        except KeyError:
            raise ClientError(f"CargoTel: load {load_id!r} not found") from None

    def get_load(self, load_id: str) -> CargoTelLoad:
        return self._fixture(load_id).load

    def get_carrier(self, client_id: str) -> CargoTelCarrier:
        for fixture in self._fixtures.values():
            if fixture.carrier is not None and fixture.carrier.client_id == client_id.strip():
                return fixture.carrier
        raise ClientError(f"CargoTel: carrier {client_id!r} not found")

    def get_authorization_context(self, load_id: str) -> AuthorizationContext:
        fixture = self._fixture(load_id)
        return build_authorization_context(fixture.load, fixture.carrier)


def build_authorization_context(
    load: CargoTelLoad, carrier: CargoTelCarrier | None
) -> AuthorizationContext:
    """Assemble the disclosure context from a load and its carrier record.

    Shared by the mock and the live client so both answer identically.

    ``factoring_emails`` is always empty: the carrier record names the factoring company but
    holds no address for it, so a factor writing in cannot be matched here. That is the same
    gap the Transport Pro path has, and it has the same answer — a curated domain roster —
    rather than being papered over by matching on the factor's name.
    """

    if carrier is None:
        return AuthorizationContext(carrier_company=load.carrier_name)
    return AuthorizationContext(
        carrier_company=carrier.name or load.carrier_name,
        authorized_emails=carrier.emails,
        # The trimmed factor, NOT the raw "<factor> C/O <carrier>" string — see
        # `CargoTelCarrier.factoring_company`. Passing the raw value lets the carrier's own
        # words match an unrelated factor in the roster, which is a live hole on this data.
        factoring_company=carrier.factoring_company,
        factoring_emails=(),
    )
