"""Enumerations shared across models, tools, and the gate.

String-valued so they serialise cleanly into tool JSON and match the literal values
used in the PRD tool contracts (§4.2).
"""

from __future__ import annotations

from enum import StrEnum


class System(StrEnum):
    """Which back-office system owns a load, decided purely by ID length (§4.1)."""

    TRANSPORT_PRO = "transport_pro"  # 7-digit
    QUICKBOOKS = "quickbooks"  # 6-digit
    INVALID = "invalid"  # any other length -> do not look up


class AuthDecision(StrEnum):
    """Result of ``check_authorization`` (§4.2)."""

    ALLOW = "ALLOW"
    DENY = "DENY"
    FACTORING = "FACTORING"


class SensitiveFlag(StrEnum):
    """Sensitive-change signals from ``detect_sensitive_change`` (§4.2)."""

    BANK_CHANGE = "bank_change"
    NOA_SETUP_CHANGE = "noa_setup_change"
    EMAIL_CONTACT_CHANGE = "email_contact_change"
    NONE = "none"


class SensitiveAction(StrEnum):
    """What to do given the sensitive-change flags."""

    ESCALATE = "escalate"
    CONTINUE = "continue"


class Intent(StrEnum):
    """Email intent from ``classify_intent`` (§4.2)."""

    PAYMENT_STATUS = "payment_status"
    RATE_VERIFICATION = "rate_verification"
    NEITHER = "neither"
    UNCERTAIN = "uncertain"


class PayBasis(StrEnum):
    """Basis for a reported pay date (§4.1.1)."""

    ACTUAL = "actual"  # actual_payment_date passed through, no computation
    ESTIMATED = "estimated"  # derived from estimated_payment_date via Mon/Thu rule
