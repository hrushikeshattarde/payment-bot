"""The Lambda entrypoint's cold-start contract.

The handler is thin by design — ``process_inbox`` does the work — so what is worth testing
is precisely the part that is *not* shared with the local runner: turning AWS-shaped inputs
(a secret ARN, an S3 object) into the ``PAYBOT_*`` environment the settings object expects,
and doing it in an order that cannot half-succeed.

The failure this guards against is specific and quiet. A worker that starts without
credentials does not crash: it builds a Gmail client, fails to authenticate, and reports
"no mail matched" — which is indistinguishable from a genuinely empty inbox, on a schedule,
for as long as nobody looks. So every step here fails closed instead.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from payment_bot import lambda_handler
from payment_bot.config import get_settings


class _FakeSecrets:
    def __init__(self, values: dict[str, str]) -> None:
        self._values = values
        self.requested: list[str] = []

    def get_secret_value(self, SecretId: str) -> dict[str, str]:  # noqa: N803 - boto3's name
        self.requested.append(SecretId)
        if SecretId not in self._values:
            raise RuntimeError(f"ResourceNotFoundException: {SecretId}")
        return {"SecretString": self._values[SecretId]}


class _FakeS3:
    def __init__(self, payload: str | None) -> None:
        self._payload = payload
        self.requested: list[tuple[str, str]] = []

    def download_file(self, bucket: str, key: str, path: str) -> None:
        self.requested.append((bucket, key))
        if self._payload is None:
            raise RuntimeError("NoSuchKey")
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(self._payload)


@pytest.fixture
def fake_boto3(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Stand in for boto3 without installing it, and record what was asked for."""

    built: dict[str, Any] = {"secrets": _FakeSecrets({}), "s3": _FakeS3(None)}

    def client(name: str, **_: Any) -> Any:
        return built["secrets"] if name == "secretsmanager" else built["s3"]

    monkeypatch.setattr(lambda_handler, "_boto3", lambda: SimpleNamespace(client=client))
    return built


# --- secrets ----------------------------------------------------------------
def test_a_secret_becomes_the_paybot_variable_it_maps_to(fake_boto3: dict[str, Any]) -> None:
    """The whole indirection: the bot reads config, never a secret store."""

    fake_boto3["secrets"] = _FakeSecrets({"arn:google": '{"client_email": "bot@x.iam"}'})
    env = {"PAYBOT_SECRET_GOOGLE_SA": "arn:google"}

    loaded = lambda_handler.load_secrets(env)

    assert loaded == ["PAYBOT_GOOGLE_SA_JSON"]
    assert env["PAYBOT_GOOGLE_SA_JSON"] == '{"client_email": "bot@x.iam"}'


def test_an_unreferenced_secret_is_skipped_not_defaulted(fake_boto3: dict[str, Any]) -> None:
    """An unset CargoTel/TP secret is a configuration state, not a failure.

    Blank-filling it would make "this path is off" and "this password is wrong" look alike
    at the point of use, which is where they need to look different.
    """

    env = {"PAYBOT_SECRET_GOOGLE_SA": "", "PAYBOT_SECRET_TP_PASSWORD": "   "}

    assert lambda_handler.load_secrets(env) == []
    assert "PAYBOT_TP_PASSWORD" not in env
    assert fake_boto3["secrets"].requested == []


def test_an_unreadable_secret_raises_rather_than_starting_without_it(
    fake_boto3: dict[str, Any],
) -> None:
    """Fail closed. The alternative is a scheduled run that quietly answers nothing."""

    fake_boto3["secrets"] = _FakeSecrets({})  # the ARN resolves to nothing
    env = {"PAYBOT_SECRET_GOOGLE_SA": "arn:missing"}

    with pytest.raises(RuntimeError, match="ResourceNotFound"):
        lambda_handler.load_secrets(env)


def test_secrets_manager_is_reached_once_for_many_secrets(fake_boto3: dict[str, Any]) -> None:
    """Both secrets, one client. Cold-start cost is paid once per container, not per read."""

    fake_boto3["secrets"] = _FakeSecrets({"arn:google": "{}", "arn:tp": "hunter2"})
    env = {"PAYBOT_SECRET_GOOGLE_SA": "arn:google", "PAYBOT_SECRET_TP_PASSWORD": "arn:tp"}

    assert sorted(lambda_handler.load_secrets(env)) == [
        "PAYBOT_GOOGLE_SA_JSON",
        "PAYBOT_TP_PASSWORD",
    ]
    assert env["PAYBOT_TP_PASSWORD"] == "hunter2"


# --- roster -----------------------------------------------------------------
def test_the_roster_is_fetched_and_pointed_at(
    fake_boto3: dict[str, Any], tmp_path: Any
) -> None:
    roster = json.dumps({"rtsfinancial.com": "RTS Financial"})
    fake_boto3["s3"] = _FakeS3(roster)
    env = {"PAYBOT_ROSTER_BUCKET": "paybot-config", "PAYBOT_ROSTER_KEY": "roster.json"}
    target = str(tmp_path / "factoring_domains.json")

    written = lambda_handler.load_roster(env, path=target)

    assert written == target
    assert env["PAYBOT_FACTORING_DOMAINS_FILE"] == target
    assert json.loads(Path(target).read_text(encoding="utf-8")) == {
        "rtsfinancial.com": "RTS Financial"
    }
    assert fake_boto3["s3"].requested == [("paybot-config", "roster.json")]


def test_no_roster_configured_is_a_valid_deployment(fake_boto3: dict[str, Any]) -> None:
    """The inline PAYBOT_FACTORING_DOMAINS patches can stand alone."""

    env: dict[str, str] = {}

    assert lambda_handler.load_roster(env) is None
    assert "PAYBOT_FACTORING_DOMAINS_FILE" not in env
    assert fake_boto3["s3"].requested == []


def test_an_unreadable_roster_raises_rather_than_authorising_nobody(
    fake_boto3: dict[str, Any], tmp_path: Any
) -> None:
    """Matches Settings._merge_factoring_domains_file.

    An empty roster does not fail visibly — it turns every factoring enquiry into an
    escalation, and it would take a day of quiet escalations to notice.
    """

    fake_boto3["s3"] = _FakeS3(None)
    env = {"PAYBOT_ROSTER_BUCKET": "paybot-config", "PAYBOT_ROSTER_KEY": "gone.json"}

    with pytest.raises(RuntimeError, match="NoSuchKey"):
        lambda_handler.load_roster(env, path=str(tmp_path / "roster.json"))


# --- carrier contacts -------------------------------------------------------
def test_the_carrier_contacts_file_is_fetched_and_pointed_at(
    fake_boto3: dict[str, Any], tmp_path: Any
) -> None:
    """Same shape as the roster, and it shares the bucket: one config bucket, two objects."""

    contacts = json.dumps({"STRATAN INC": ["ar.strataninc@example.com"]})
    fake_boto3["s3"] = _FakeS3(contacts)
    env = {
        "PAYBOT_ROSTER_BUCKET": "paybot-config",
        "PAYBOT_CARRIER_CONTACTS_KEY": "carrier_contacts.json",
    }
    target = str(tmp_path / "carrier_contacts.json")

    written = lambda_handler.load_carrier_contacts(env, path=target)

    assert written == target
    assert env["PAYBOT_CARRIER_CONTACTS_FILE"] == target
    assert json.loads(Path(target).read_text(encoding="utf-8")) == {
        "STRATAN INC": ["ar.strataninc@example.com"]
    }
    assert fake_boto3["s3"].requested == [("paybot-config", "carrier_contacts.json")]


def test_no_carrier_contacts_configured_is_a_valid_deployment(
    fake_boto3: dict[str, Any],
) -> None:
    """How this shipped before the file existed — the inline entries stand alone."""

    env: dict[str, str] = {}

    assert lambda_handler.load_carrier_contacts(env) is None
    assert "PAYBOT_CARRIER_CONTACTS_FILE" not in env
    assert fake_boto3["s3"].requested == []


def test_a_roster_bucket_without_a_contacts_key_fetches_nothing(
    fake_boto3: dict[str, Any],
) -> None:
    """The common case: a stack with a roster and no contact list yet must not 404.

    Mirrors the template's HasCarrierContacts condition, which requires both the bucket and
    the key — so the IAM grant is absent rather than pointing at a key that is not there.
    """

    env = {"PAYBOT_ROSTER_BUCKET": "paybot-config", "PAYBOT_ROSTER_KEY": "roster.json"}

    assert lambda_handler.load_carrier_contacts(env) is None
    assert fake_boto3["s3"].requested == []


def test_an_unreadable_contacts_file_raises(
    fake_boto3: dict[str, Any], tmp_path: Any
) -> None:
    """Same reasoning as the roster: a list that loaded as empty authorises nobody."""

    fake_boto3["s3"] = _FakeS3(None)
    env = {
        "PAYBOT_ROSTER_BUCKET": "paybot-config",
        "PAYBOT_CARRIER_CONTACTS_KEY": "gone.json",
    }

    with pytest.raises(RuntimeError, match="NoSuchKey"):
        lambda_handler.load_carrier_contacts(env, path=str(tmp_path / "contacts.json"))


def test_the_two_objects_are_fetched_independently(
    fake_boto3: dict[str, Any], tmp_path: Any
) -> None:
    """Each points at its own settings variable, and neither writes the other's."""

    fake_boto3["s3"] = _FakeS3(json.dumps({"x": ["y"]}))
    env = {
        "PAYBOT_ROSTER_BUCKET": "paybot-config",
        "PAYBOT_ROSTER_KEY": "factoring_domains.json",
        "PAYBOT_CARRIER_CONTACTS_KEY": "carrier_contacts.json",
    }

    lambda_handler.load_roster(env, path=str(tmp_path / "r.json"))
    lambda_handler.load_carrier_contacts(env, path=str(tmp_path / "c.json"))

    assert env["PAYBOT_FACTORING_DOMAINS_FILE"] == str(tmp_path / "r.json")
    assert env["PAYBOT_CARRIER_CONTACTS_FILE"] == str(tmp_path / "c.json")
    assert fake_boto3["s3"].requested == [
        ("paybot-config", "factoring_domains.json"),
        ("paybot-config", "carrier_contacts.json"),
    ]


# --- ordering ---------------------------------------------------------------
def test_settings_are_built_after_the_environment_is_whole(
    fake_boto3: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """``get_settings`` is lru_cached, so the cache must be cleared after the env is set.

    Getting this backwards is silent: settings would be built from a partial environment,
    every credential would read as blank, and the run would report an empty inbox.
    """

    fake_boto3["secrets"] = _FakeSecrets({"arn:tp": "from-secrets-manager"})
    monkeypatch.setenv("PAYBOT_SECRET_TP_PASSWORD", "arn:tp")
    monkeypatch.delenv("PAYBOT_SECRET_GOOGLE_SA", raising=False)
    monkeypatch.setenv("PAYBOT_ROSTER_BUCKET", "")

    # Prime the cache with the pre-secret environment, exactly as an accidental early
    # get_settings() call would.
    get_settings.cache_clear()
    get_settings()

    settings = lambda_handler.bootstrap()

    assert settings.tp_password.get_secret_value() == "from-secrets-manager"
    get_settings.cache_clear()


def test_the_handler_summarises_outcomes_and_honours_a_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The return value is what CloudWatch shows beside the invocation."""

    from payment_bot.pipeline import Outcome, PipelineResult

    calls: dict[str, Any] = {}

    def fake_process_inbox(settings: Any, *, limit: int | None = None, **kwargs: Any):
        calls["limit"] = limit
        return [
            PipelineResult(Outcome.AWAITING_REVIEW, "drafted", "c1"),
            PipelineResult(Outcome.AWAITING_REVIEW, "drafted", "c2"),
            PipelineResult(Outcome.ESCALATED, "not authorized", "c3"),
        ]

    monkeypatch.setattr(lambda_handler, "process_inbox", fake_process_inbox)
    monkeypatch.setattr(lambda_handler, "build_clients", lambda settings, **kwargs: None)
    monkeypatch.setattr(lambda_handler, "_SETTINGS", get_settings())

    summary = lambda_handler.handler({"limit": 1}, None)

    assert calls["limit"] == 1
    assert summary == {"processed": 3, "outcomes": {"awaiting_review": 2, "escalated": 1}}


def test_the_handler_falls_back_to_the_configured_fetch_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A scheduled invocation carries no payload, and must not process an unbounded inbox.

    The bound matters more here than it looks: the turn budget scales with load count
    (§3.7), so the fetch limit is what keeps a run inside Lambda's 15-minute cap.
    """

    calls: dict[str, Any] = {}

    def fake_process_inbox(settings: Any, *, limit: int | None = None, **kwargs: Any):
        calls["limit"] = limit
        return []

    monkeypatch.setattr(lambda_handler, "process_inbox", fake_process_inbox)
    monkeypatch.setattr(lambda_handler, "build_clients", lambda settings, **kwargs: None)
    settings = get_settings()
    monkeypatch.setattr(lambda_handler, "_SETTINGS", settings)

    lambda_handler.handler({}, None)

    assert calls["limit"] == settings.gmail_fetch_limit


def test_the_module_imports_under_the_lambda_handler_path() -> None:
    """``payment_bot.lambda_handler.handler`` is what template.yaml names.

    A rename here breaks the deployment with a Runtime.ImportModuleError and nothing else,
    so the string in the template is worth pinning from this side too.
    """

    assert "payment_bot.lambda_handler" in sys.modules
    assert callable(lambda_handler.handler)
