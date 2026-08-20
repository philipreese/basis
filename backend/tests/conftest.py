"""Test-wide safety rails.

operator.py loads the developer's real .env at import, so any test that
reaches send_ntfy WITHOUT patching it would push to the REAL phone topic
(this happened — #278's receipt tests spammed the operator's phone with
'remote HALT applied'). Strip every ntfy variable before each test: a test
that wants a topic sets its own, and httpx is then the only thing left to
patch.
"""

import pytest


@pytest.fixture(autouse=True)
def _no_real_ntfy(monkeypatch):
    monkeypatch.delenv("NTFY_TOPIC", raising=False)
    monkeypatch.delenv("NTFY_COMMAND_TOPIC", raising=False)
    monkeypatch.setenv("NTFY_SERVER", "http://ntfy.invalid")  # unroutable — belt and braces
