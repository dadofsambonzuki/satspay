"""Webhook delivery must not become an SSRF response oracle.

The target comes from the charge owner, so the request has to go through
lnbits' SafeHttpClient and the reply must never be handed back.
"""

from datetime import datetime
from unittest.mock import AsyncMock

import pytest

from .. import helpers
from ..models import Charge

SECRET = "INTERNAL-SERVICE-SECRET"


class FakeResponse:
    reason_phrase = "OK"
    text = SECRET

    def raise_for_status(self):
        return None


class FakeClient:
    """Stands in for SafeHttpClient, recording what it was asked to do."""

    instances: list["FakeClient"] = []

    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.calls = []
        FakeClient.instances.append(self)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def request(self, **kwargs):
        self.calls.append(("request", kwargs))
        return FakeResponse()

    async def post(self, **kwargs):
        self.calls.append(("post", kwargs))
        return FakeResponse()


class BlockedClient(FakeClient):
    async def post(self, **kwargs):
        raise ValueError("UnsafeHttpDestinationError: private destination")


def charge(**overrides) -> Charge:
    fields = {
        "id": "charge",
        "user": "owner",
        "name": "t",
        "description": "d",
        "amount": 1000,
        "time": 30,
        "timestamp": datetime.now(),
        "webhook": "http://127.0.0.1:5000/api/v1/health",
    }
    fields.update(overrides)
    return Charge(**fields)  # type: ignore[arg-type]


@pytest.fixture(autouse=True)
def settings_ok(monkeypatch):
    monkeypatch.setattr(
        helpers,
        "get_or_create_satspay_settings",
        AsyncMock(return_value=type("S", (), {"webhook_method": "POST"})()),
    )


@pytest.mark.anyio
async def test_webhook_response_body_is_never_returned(monkeypatch):
    FakeClient.instances.clear()
    monkeypatch.setattr(helpers, "SafeHttpClient", FakeClient)

    result = await helpers.call_webhook(charge())

    assert result["webhook_success"] is True
    assert result["webhook_response"] == ""
    assert SECRET not in str(result)
    # ...and the body was never even requested
    assert FakeClient.instances[0].kwargs.get("read_body") is False


@pytest.mark.anyio
async def test_blocked_private_target_gives_a_sanitised_failure(monkeypatch):
    monkeypatch.setattr(helpers, "SafeHttpClient", BlockedClient)

    result = await helpers.call_webhook(charge())

    assert result["webhook_success"] is False
    assert result["webhook_message"] == "Webhook request failed."
    assert result["webhook_response"] == ""
    # the target must not leak back to the caller
    assert "127.0.0.1" not in str(result)


@pytest.mark.anyio
async def test_webhook_payload_is_unchanged(monkeypatch):
    FakeClient.instances.clear()
    monkeypatch.setattr(helpers, "SafeHttpClient", FakeClient)

    await helpers.call_webhook(charge())

    method, kwargs = FakeClient.instances[0].calls[0]
    assert method == "post"
    assert kwargs["json"]["id"] == "charge"
    assert kwargs["json"]["webhook"] == "http://127.0.0.1:5000/api/v1/health"
