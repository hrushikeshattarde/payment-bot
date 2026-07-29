"""Transport Pro client (7-digit loads).

The :class:`TransportProClient` protocol is the seam between our tools and the live TP
API. The real implementation (an HTTP client) is blocked on PRD §9 open dependencies, so
this slice ships :class:`MockTransportProClient`, a fixture-backed implementation with
identical typing. Swapping in the real client later changes nothing above this layer.

Each endpoint maps to a Load Summary screen (§4.3). The load payload (§4.3.0) is the
grounding source of truth; the auxiliary endpoints (dispatch/settlement/files) are
separate screens.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

from payment_bot.errors import ClientError
from payment_bot.models import (
    AuthorizationContext,
    DispatchRow,
    FileDocument,
    NoaFactoring,
    SettlementEntry,
    TransportProLoad,
)


@runtime_checkable
class TransportProClient(Protocol):
    """Read-only access to Transport Pro load data."""

    def get_load(self, load_id: str) -> TransportProLoad:
        """Return the full load payload (§4.3.0). Raise :class:`ClientError` if absent."""

    def get_dispatch_history(self, load_id: str) -> list[DispatchRow]:
        """Return dispatch rows (§4.3 ``tp_get_dispatch_history``)."""

    def get_settlement_entries(self, load_id: str) -> list[SettlementEntry]:
        """Return settlement entries; empty list means "no settlement entries found"."""

    def get_file_history(self, load_id: str) -> list[FileDocument]:
        """Return indexed documents (§4.3 ``tp_get_file_history``)."""

    def get_noa_factoring(self, load_id: str) -> NoaFactoring:
        """Return read-only NOA / factoring status (§4.3 ``tp_get_noa_factoring``)."""

    def get_authorization_context(self, load_id: str) -> AuthorizationContext:
        """Return who may receive disclosure about this load (source for auth)."""


@dataclass(frozen=True, slots=True)
class LoadFixture:
    """The full multi-endpoint dataset for one mock load."""

    load: TransportProLoad
    dispatch: list[DispatchRow] = field(default_factory=list)
    settlement: list[SettlementEntry] = field(default_factory=list)
    files: list[FileDocument] = field(default_factory=list)
    noa_factoring: NoaFactoring = field(default_factory=NoaFactoring)
    authorization: AuthorizationContext = field(default_factory=AuthorizationContext)


class MockTransportProClient:
    """In-memory, fixture-backed :class:`TransportProClient` for tests and the demo."""

    def __init__(self, fixtures: dict[str, LoadFixture] | None = None) -> None:
        self._fixtures: dict[str, LoadFixture] = dict(fixtures or {})

    def add(self, fixture: LoadFixture) -> None:
        """Register (or replace) a fixture keyed by its load id."""

        self._fixtures[fixture.load.load_id_str] = fixture

    def _get(self, load_id: str) -> LoadFixture:
        try:
            return self._fixtures[load_id.strip()]
        except KeyError:
            raise ClientError(f"Transport Pro: load {load_id!r} not found") from None

    def get_load(self, load_id: str) -> TransportProLoad:
        return self._get(load_id).load

    def get_dispatch_history(self, load_id: str) -> list[DispatchRow]:
        return list(self._get(load_id).dispatch)

    def get_settlement_entries(self, load_id: str) -> list[SettlementEntry]:
        return list(self._get(load_id).settlement)

    def get_file_history(self, load_id: str) -> list[FileDocument]:
        return list(self._get(load_id).files)

    def get_noa_factoring(self, load_id: str) -> NoaFactoring:
        return self._get(load_id).noa_factoring

    def get_authorization_context(self, load_id: str) -> AuthorizationContext:
        return self._get(load_id).authorization
