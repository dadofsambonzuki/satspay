"""Only the owner may read or act on a charge.

Charge ids are handed to buyers in URLs, so they are not a secret: every
private route must scope by the caller's user id and answer 404 otherwise.
"""

from datetime import datetime
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException

from .. import views_api
from ..models import Charge


def charge(**overrides) -> Charge:
    fields = {
        "id": "charge",
        "user": "owner",
        "name": "t",
        "description": "d",
        "amount": 1000,
        "time": 30,
        "timestamp": datetime.now(),
    }
    fields.update(overrides)
    return Charge(**fields)  # type: ignore[arg-type]


@pytest.mark.anyio
async def test_owner_can_load_their_charge(monkeypatch):
    monkeypatch.setattr(views_api, "get_charge", AsyncMock(return_value=charge()))
    result = await views_api._get_owned_charge("charge", "owner")
    assert result.id == "charge"


@pytest.mark.anyio
async def test_other_user_gets_404_not_403(monkeypatch):
    monkeypatch.setattr(views_api, "get_charge", AsyncMock(return_value=charge()))
    with pytest.raises(HTTPException) as exc:
        await views_api._get_owned_charge("charge", "someone-else")
    assert exc.value.status_code == 404
    assert exc.value.detail == "Charge does not exist."


@pytest.mark.anyio
async def test_missing_charge_is_indistinguishable(monkeypatch):
    monkeypatch.setattr(views_api, "get_charge", AsyncMock(return_value=None))
    with pytest.raises(HTTPException) as exc:
        await views_api._get_owned_charge("charge", "owner")
    assert exc.value.status_code == 404
    assert exc.value.detail == "Charge does not exist."
