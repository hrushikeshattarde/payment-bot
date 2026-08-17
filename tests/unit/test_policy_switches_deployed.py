"""Every policy switch reaches the deployed worker.

Found live on 2026-08-15. ``noa_attachment_replies`` and ``factoring_prenoa_replies`` were
both ``true`` in ``.env`` and absent from ``deploy/template.yaml``, so the Lambda never
received them and ``Settings`` fell back to ``False`` — the strict behaviour — for each:

* every rate verification with the factor's NOA attached escalated unanswered, which is the
  standard pre-funding packet and therefore most of them;
* every roster-verified factor asking about a load with no factor of record was DENIED
  outright, since pre-NOA is the only branch that authorises them.

Both defaults are deliberate and correct in ``Settings``: a deployment that says nothing
should get the strict behaviour. That is exactly what makes the omission invisible — nothing
fails, nothing logs, and the symptom is mail quietly escalating in production while the same
mail drafts on a workstation.

The template is read as TEXT rather than parsed, following
``test_the_module_imports_under_the_lambda_handler_path``: the point is that the string is
present and wired, and a YAML parser would add a dependency for no extra confidence.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from payment_bot.config import Settings

pytestmark = pytest.mark.unit

_REPO = Path(__file__).resolve().parents[2]
_TEMPLATE = (_REPO / "deploy" / "template.yaml").read_text(encoding="utf-8")
_PARAMS_EXAMPLE = (_REPO / "deploy" / "params.example.json").read_text(encoding="utf-8")

#: Settings field → CloudFormation parameter name, for every switch that decides whether a
#: class of mail gets answered at all.
#:
#: Mapped explicitly rather than derived: parameter names are chosen by people, and
#: ``cargotel_replies`` is ``CargoTelReplies`` because CargoTel is a product name. A
#: mechanical ``capitalize()`` produces ``CargotelReplies`` and the test passes for the wrong
#: reason — or fails for a naming convention that was never broken.
POLICY_SWITCHES: dict[str, str] = {
    "allow_factoring": "AllowFactoring",
    "cargotel_replies": "CargoTelReplies",
    "sensitive_bank_replies": "SensitiveBankReplies",
    "sensitive_noa_replies": "SensitiveNoaReplies",
    "noa_attachment_replies": "NoaAttachmentReplies",
    "factoring_prenoa_replies": "FactoringPrenoaReplies",
}


def _env_var(field: str) -> str:
    return f"PAYBOT_{field.upper()}"


@pytest.mark.parametrize("field", POLICY_SWITCHES)
def test_the_switch_is_passed_to_the_function(field: str) -> None:
    """Absent from the template, the switch silently reverts to its strict default."""

    assert f"{_env_var(field)}:" in _TEMPLATE, (
        f"{_env_var(field)} is not in the Lambda's Environment.Variables, so a deployed stack "
        f"runs with {field}={Settings.model_fields[field].default!r} whatever .env says"
    )


@pytest.mark.parametrize(("field", "parameter"), sorted(POLICY_SWITCHES.items()))
def test_the_switch_is_a_stack_parameter(field: str, parameter: str) -> None:
    """Operators change these per deployment, so each needs its own parameter."""

    assert f"{parameter}:" in _TEMPLATE
    assert f'"{parameter}"' in _PARAMS_EXAMPLE


def test_no_policy_switch_is_missing_from_this_list() -> None:
    """The guard that makes this file worth having.

    A new ``*_replies`` switch added to Settings and not wired into the template would
    reproduce the 2026-08-15 bug exactly. Adding it here is the cheap step that forces the
    template edit, so the failure is a red test rather than production mail escalating.
    """

    reply_switches = {
        name
        for name, field in Settings.model_fields.items()
        if name.endswith("_replies") and field.default is False
    }
    unlisted = reply_switches - set(POLICY_SWITCHES)

    assert not unlisted, (
        f"new policy switch(es) {sorted(unlisted)} are not in POLICY_SWITCHES — add them, then "
        "wire them into deploy/template.yaml as a parameter and an Environment variable"
    )


def test_the_two_that_were_missing_default_to_on_in_the_template() -> None:
    """Pinned because the strict Settings default is what made the omission invisible.

    A template parameter defaulting to "false" would leave a stack deployed from the example
    behaving like the bug even with the wiring in place.
    """

    for field in ("noa_attachment_replies", "factoring_prenoa_replies"):
        parameter = POLICY_SWITCHES[field]
        block = _TEMPLATE.split(f"{parameter}:", 1)[1][:200]
        assert 'Default: "true"' in block, f"{parameter} should default on"
        assert '["true", "false"]' in block, f"{parameter} should be constrained"
