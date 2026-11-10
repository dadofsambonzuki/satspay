"""The mempool endpoint and address must not be usable as an SSRF primitive.

`mempool_url` is an administrator setting, so a private destination is allowed
only when the administrator opts in; the address is validated before it is
interpolated into the request path.
"""

from unittest.mock import AsyncMock

import pytest
from pydantic import ValidationError

from .. import helpers
from ..models import CreateCharge, SatspaySettings

ADDRESS = "bc1qexampleaddress000000000000000000000000"


class FakeClient:
    instances: list["FakeClient"] = []

    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.urls = []
        FakeClient.instances.append(self)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def get(self, url, **kwargs):
        self.urls.append(url)
        raise RuntimeError("stop here: transport identity already recorded")


def settings(mempool_url="https://mempool.space", allow_private=False):
    return SatspaySettings(
        mempool_url=mempool_url, mempool_allow_private_ips=allow_private
    )


@pytest.mark.anyio
async def test_private_mempool_is_opted_out_by_default(monkeypatch):
    FakeClient.instances.clear()
    monkeypatch.setattr(helpers, "SafeHttpClient", FakeClient)
    monkeypatch.setattr(
        helpers, "get_or_create_satspay_settings", AsyncMock(return_value=settings())
    )

    with pytest.raises(RuntimeError):
        await helpers.fetch_onchain_balance(ADDRESS)

    assert FakeClient.instances[0].kwargs["private_endpoint"] is None


@pytest.mark.anyio
async def test_admin_opt_in_passes_the_private_endpoint(monkeypatch):
    FakeClient.instances.clear()
    monkeypatch.setattr(helpers, "SafeHttpClient", FakeClient)
    monkeypatch.setattr(
        helpers,
        "get_or_create_satspay_settings",
        AsyncMock(
            return_value=settings(
                mempool_url="http://192.168.1.43:3006", allow_private=True
            )
        ),
    )

    with pytest.raises(RuntimeError):
        await helpers.fetch_onchain_balance(ADDRESS)

    assert FakeClient.instances[0].kwargs["private_endpoint"] == "http://192.168.1.43:3006"


@pytest.mark.anyio
@pytest.mark.parametrize(
    "address",
    ["../etc/passwd", "abc", "bc1q example", "x" * 200, "addr?x=1", "addr#frag"],
)
async def test_invalid_address_never_reaches_the_transport(monkeypatch, address):
    FakeClient.instances.clear()
    monkeypatch.setattr(helpers, "SafeHttpClient", FakeClient)

    with pytest.raises(ValueError):
        await helpers.fetch_onchain_balance(address)

    assert FakeClient.instances == []


@pytest.mark.parametrize(
    "url",
    [
        "ftp://mempool.space",
        "https://user:pass@mempool.space",
        "https://mempool.space/?x=1",
        "https://mempool.space/#frag",
        "mempool.space",
        "",
    ],
)
def test_mempool_url_validator_rejects_unsafe_urls(url):
    with pytest.raises(ValidationError):
        SatspaySettings(mempool_url=url)


@pytest.mark.parametrize(
    "url",
    ["https://mempool.space", "http://192.168.1.43:3006", "https://mempool.space/testnet"],
)
def test_mempool_url_validator_accepts_plain_urls(url):
    assert SatspaySettings(mempool_url=url).mempool_url == url


@pytest.mark.parametrize("wallet", ["../other", "core:../x", "a b", "x?y=1"])
def test_onchainwallet_reference_is_pattern_checked(wallet):
    with pytest.raises(ValidationError):
        CreateCharge(description="d", time=30, onchainwallet=wallet)


@pytest.mark.parametrize("wallet", [None, "core:receiving-wallet", "abc_123-XYZ"])
def test_onchainwallet_reference_accepts_wallet_ids(wallet):
    data = CreateCharge(description="d", time=30, onchainwallet=wallet)
    assert data.onchainwallet == wallet
