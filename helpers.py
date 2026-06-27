import hashlib
import hmac
import traceback
import uuid

import httpx
from lnbits.core.crud import get_standalone_payment
from lnbits.helpers import normalize_endpoint
from lnbits.settings import settings
from loguru import logger

from .crud import get_or_create_satspay_settings
from .models import Charge, CreateCharge, FiatConfig, OnchainBalance


def _satspay_internal_host() -> str:
    return "127.0.0.1" if settings.host in ("0.0.0.0", "::") else settings.host


async def call_webhook(charge: Charge):
    try:
        assert charge.webhook, "charge has no webhook"
        settings = await get_or_create_satspay_settings()
        async with httpx.AsyncClient() as client:
            # wordpress expects a GET request with json-encoded binary content
            if settings.webhook_method == "GET":
                r = await client.request(
                    method="GET",
                    url=charge.webhook,
                    content=charge.json(),
                    timeout=10,
                )
            else:
                r = await client.post(
                    url=charge.webhook,
                    json=charge.json(),
                    timeout=10,
                )
            r.raise_for_status()
            logger.success(f"Webhook sent for charge {charge.id}")
            return {
                "webhook_success": True,
                "webhook_message": r.reason_phrase,
                "webhook_response": r.text,
            }
    except Exception as e:
        logger.warning(f"Failed to call webhook for charge {charge.id}")
        logger.warning(charge.webhook)
        logger.warning(traceback.format_exc())
        return {"webhook_success": False, "webhook_message": str(e)}


async def fetch_onchain_balance(onchain_address: str) -> OnchainBalance:
    settings = await get_or_create_satspay_settings()
    async with httpx.AsyncClient() as client:
        res = await client.get(
            f"{settings.mempool_url}/api/address/{onchain_address}/txs"
        )
        res.raise_for_status()
        data = res.json()
        confirmed_txs = [tx for tx in data if tx["status"]["confirmed"]]
        unconfirmed_txs = [tx for tx in data if not tx["status"]["confirmed"]]
        txids = [tx["txid"] for tx in data]
        confirmed = sum_transactions(onchain_address, confirmed_txs)
        unconfirmed = sum_transactions(onchain_address, unconfirmed_txs)
        return OnchainBalance(confirmed=confirmed, unconfirmed=unconfirmed, txids=txids)


async def fetch_onchain_config_network(api_key: str) -> str:
    async with httpx.AsyncClient() as client:
        r = await client.get(
            url=f"http://{_satspay_internal_host()}:{settings.port}/watchonly/api/v1/config",
            headers={"X-API-KEY": api_key},
        )
        r.raise_for_status()
        config = r.json()
        return config["network"]


async def fetch_onchain_address(wallet_id: str, api_key: str) -> str:
    async with httpx.AsyncClient() as client:
        r = await client.get(
            url=f"http://{_satspay_internal_host()}:{settings.port}/watchonly/api/v1/address/{wallet_id}",
            headers={"X-API-KEY": api_key},
        )
        r.raise_for_status()
        address_data = r.json()
        if not address_data and "address" not in address_data:
            raise ValueError("Cannot fetch new address!")
        return address_data["address"]


async def check_charge_balance(charge: Charge) -> Charge:
    if charge.paid:
        return charge

    if charge.lnbitswallet and charge.payment_hash:
        payment = await get_standalone_payment(charge.payment_hash)
        if payment:
            status = await payment.check_status()
            if status.success:
                charge.add_extra({"payment_method": "lightning"})
                charge.balance = charge.amount

    if charge.onchainaddress:
        try:
            balance = await fetch_onchain_balance(charge.onchainaddress)
            charge.add_extra({"txids": balance.txids})
            if (
                balance.confirmed != charge.balance
                or balance.unconfirmed != charge.pending
            ):
                charge.balance = (
                    balance.confirmed + balance.unconfirmed
                    if charge.zeroconf
                    else balance.confirmed
                )
                charge.pending = balance.unconfirmed
                charge.add_extra({"payment_method": "onchain"})
        except Exception as exc:
            logger.warning(f"Charge check onchain address failed with: {exc!s}")

    charge.paid = charge.balance >= charge.amount

    if charge.webhook:
        resp = await call_webhook(charge)
        charge.add_extra(resp)

    return charge


def sum_outputs(address: str, vouts) -> int:
    return sum(
        [vout["value"] for vout in vouts if vout.get("scriptpubkey_address") == address]
    )


def sum_transactions(address: str, txs) -> int:
    return sum([sum_outputs(address, tx["vout"]) for tx in txs])


def get_txids(address: str, data) -> list[str]:
    confirmed_txs = data.get("confirmed", [])
    confirmed_txids = [
        vout["txid"]
        for vout in confirmed_txs
        if vout.get("scriptpubkey_address") == address
    ]
    mempool_txs = data.get("mempool", [])
    mempool_txids = [
        vout["txid"]
        for vout in mempool_txs
        if vout.get("scriptpubkey_address") == address
    ]
    return confirmed_txids + mempool_txids


async def create_fiat_invoice_for_charge(
    charge: Charge, data: CreateCharge, fiat_config: FiatConfig
) -> dict | None:
    if not fiat_config or not fiat_config.enabled:
        return None
    if not fiat_config.api_key:
        logger.warning(f"Fiat provider '{fiat_config.provider}' missing api_key")
        return None

    payment_hash = uuid.uuid4().hex
    fiat_currency = data.fiat_currency or data.currency or "usd"
    fiat_amount = data.currency_amount or 0.0

    try:
        if fiat_config.provider == "stripe":
            result = await _create_stripe_checkout(fiat_amount, fiat_currency, payment_hash, fiat_config)
        elif fiat_config.provider == "paypal":
            result = await _create_paypal_checkout(fiat_amount, fiat_currency, payment_hash, fiat_config)
        elif fiat_config.provider == "revolut":
            result = await _create_revolut_checkout(fiat_amount, fiat_currency, payment_hash, fiat_config)
        elif fiat_config.provider == "square":
            result = await _create_square_checkout(fiat_amount, fiat_currency, payment_hash, fiat_config)
        else:
            return None
        return result
    except Exception as exc:
        logger.warning(f"Error creating fiat invoice: {exc}")
        return None


async def verify_fiat_webhook(
    provider: str, payload: bytes, signature: str | None, fiat_config: FiatConfig
) -> str | None:
    if not signature:
        raise ValueError(f"Missing webhook signature for {provider}")
    if provider == "stripe" and fiat_config.webhook_secret:
        return _verify_stripe_webhook(payload, signature, fiat_config.webhook_secret)
    if provider == "revolut" and fiat_config.webhook_secret:
        return _verify_revolut_webhook(payload, signature, fiat_config.webhook_secret)
    raise ValueError(f"Webhook verification not supported for {provider}")


def _verify_stripe_webhook(payload: bytes, sig_header: str, secret: str) -> str | None:
    try:
        sig_parts = sig_header.split(",")
        t_val = v1_val = None
        for part in sig_parts:
            if "=" in part:
                key, val = part.split("=", 1)
                if key.strip() == "t": t_val = val.strip()
                elif key.strip() == "v1": v1_val = val.strip()
        if not t_val or not v1_val:
            raise ValueError("Invalid signature format")
        signed_payload = f"{t_val}.{payload.decode()}"
        expected = hmac.new(secret.encode(), signed_payload.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(expected, v1_val):
            raise ValueError("Invalid signature")
        return t_val
    except Exception as e:
        raise ValueError(f"Stripe webhook verification failed: {e}") from e


def _verify_revolut_webhook(payload: bytes, signature: str, secret: str) -> str | None:
    try:
        computed = hmac.new(secret.encode(), payload, hashlib.sha256).hexdigest()
        if not hmac.compare_digest(computed, signature.replace("v1=", "")):
            raise ValueError("Invalid signature")
        return "revolut_order"
    except Exception as e:
        raise ValueError(f"Revolut webhook verification failed: {e}") from e


async def _create_stripe_checkout(amount: float, currency: str, payment_hash: str, config: FiatConfig) -> dict:
    from urllib.parse import urlencode
    endpoint = "https://api.stripe.com"
    headers = {"Authorization": f"Bearer {config.api_key}", "Content-Type": "application/x-www-form-urlencoded", "User-Agent": settings.user_agent}
    amount_cents = int(amount * 100)
    success_url = settings.stripe_payment_success_url or "https://lnbits.com"
    form_data = {"mode": "payment", "success_url": success_url, "metadata[payment_hash]": payment_hash, "metadata[source]": "satspay", "line_items[0][price_data][currency]": currency.lower(), "line_items[0][price_data][product_data][name]": "SatsPay Payment", "line_items[0][price_data][unit_amount]": str(amount_cents), "line_items[0][quantity]": "1"}
    async with httpx.AsyncClient(base_url=endpoint, headers=headers) as client:
        r = await client.post("/v1/checkout/sessions", content=urlencode(form_data))
        r.raise_for_status()
        data = r.json()
        return {"payment_request": data.get("url"), "checking_id": data.get("id")}


async def _create_paypal_checkout(amount: float, currency: str, payment_hash: str, config: FiatConfig) -> dict:
    import json
    endpoint = normalize_endpoint((json.loads(config.extra or "{}").get("endpoint") or "https://api-m.paypal.com"))
    async with httpx.AsyncClient(base_url=endpoint) as client:
        r = await client.post("/v1/oauth2/token", data={"grant_type": "client_credentials"}, auth=(config.api_key or "", config.api_secret or ""), headers={"Accept": "application/json"})
        r.raise_for_status()
        token = r.json()["access_token"]
        headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
        checkout_data = {"intent": "CAPTURE", "purchase_units": [{"amount": {"currency_code": currency.upper(), "value": f"{amount:.2f}"}, "custom_id": payment_hash}], "payment_source": {"paypal": {"experience_context": {"return_url": settings.paypal_payment_success_url or "https://lnbits.com", "cancel_url": settings.paypal_payment_success_url or "https://lnbits.com"}}}}
        r = await client.post("/v2/checkout/orders", json=checkout_data, headers=headers)
        r.raise_for_status()
        data = r.json()
        payment_url = next((link["href"] for link in data.get("links", []) if link.get("rel") == "payer-action"), None)
        return {"payment_request": payment_url, "checking_id": data.get("id")}


async def _create_revolut_checkout(amount: float, currency: str, payment_hash: str, config: FiatConfig) -> dict:
    import json
    endpoint = normalize_endpoint((json.loads(config.extra or "{}").get("endpoint") or "https://merchant.revolut.com"))
    headers = {"Authorization": f"Bearer {config.api_key}", "Revolut-Api-Version": "2024-09-01", "Content-Type": "application/json", "User-Agent": settings.user_agent}
    checkout_data = {"amount": int(amount * 100), "currency": currency.upper(), "description": "SatsPay Payment", "merchant_order_ext_ref": payment_hash, "redirect_url": settings.revolut_payment_success_url or "https://lnbits.com"}
    async with httpx.AsyncClient(base_url=endpoint, headers=headers) as client:
        r = await client.post("/api/orders", json=checkout_data)
        r.raise_for_status()
        data = r.json()
        return {"payment_request": data.get("checkout_url"), "checking_id": data.get("id")}


async def _create_square_checkout(amount: float, currency: str, payment_hash: str, config: FiatConfig) -> dict:
    import json
    extra = json.loads(config.extra or "{}")
    endpoint = normalize_endpoint(extra.get("endpoint") or "https://connect.squareup.com")
    version = extra.get("api_version") or "2024-09-19"
    location_id = extra.get("location_id") or ""
    headers = {"Authorization": f"Bearer {config.api_key}", "Square-Version": version, "Content-Type": "application/json", "User-Agent": settings.user_agent}
    amount_cents = int(amount * 100)
    checkout_data = {"idempotency_key": uuid.uuid4().hex, "order": {"order": {"location_id": location_id, "line_items": [{"quantity": "1", "name": "SatsPay Payment", "base_price_money": {"amount": amount_cents, "currency": currency.upper()}}]}}, "redirect_url": settings.square_payment_success_url or "https://lnbits.com", "ask_for_shipping_address": False}
    async with httpx.AsyncClient(base_url=endpoint, headers=headers) as client:
        r = await client.post("/v2/online-checkout/payment-links", json=checkout_data)
        r.raise_for_status()
        data = r.json()
        payment_link = data.get("payment_link", {})
        return {"payment_request": payment_link.get("url"), "checking_id": payment_link.get("id")}
