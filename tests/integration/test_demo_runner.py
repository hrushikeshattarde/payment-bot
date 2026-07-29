"""Smoke test for the local demo entrypoint."""

from __future__ import annotations

import pytest

from payment_bot.pipeline import Outcome
from payment_bot.runner import run_demo


@pytest.mark.integration
def test_run_demo_sends(capsys: pytest.CaptureFixture[str]) -> None:
    result = run_demo()
    assert result.outcome is Outcome.SENT
    assert result.sent_message is not None
    # The report was printed.
    out = capsys.readouterr().out
    assert "OUTCOME" in out
    assert "SENT" in out
