import asyncio
import json
import time

from fastapi import WebSocket
from lnbits.core.models import Payment
from lnbits.settings import settings
from lnbits.tasks import register_invoice_listener
from loguru import logger

from .crud import (
    get_charge,
    get_charge_by_onchain_address,
    get_pending_charges,
    update_charge,
)
from .helpers import call_webhook, check_charge_balance, sum_transactions, tx_after
from .models import Charge
from .websocket_handler import ws_receive_queue, ws_send_queue

tracked_addresses: list[str] = []
public_ws_listeners: dict[str, list[WebSocket]] = {}

# How often the on-chain balance is re-checked by REST as a fallback to the
# mempool websocket. The websocket only tracks successfully while the number of
# active addresses stays under mempool's per-connection limit, so this poll is
# the reliable detection path.
ONCHAIN_BALANCE_POLL_SECONDS = 120


async def restart_address_tracking():
    while settings.lnbits_running:
        try:
            charges = await get_pending_charges()
        except Exception as exc:
            logger.warning(f"Failed to load pending charges: {exc!s}")
            await asyncio.sleep(ONCHAIN_BALANCE_POLL_SECONDS)
            continue

        logger.debug(
            f"Onchain balance poll: {len(charges)} pending charges, "
            f"{len(tracked_addresses)} tracked."
        )

        for charge in charges:
            try:
                if not charge.onchainaddress:
                    continue
                if charge.timestamp.timestamp() + charge.time * 60 <= time.time():
                    # Expired: drop from ws tracking so stale addresses do not
                    # consume mempool's per-connection address limit.
                    stop_onchain_listener(charge.onchainaddress)
                    continue
                charge = await check_charge_balance(charge)
                assert charge.onchainaddress
                if charge.paid:
                    if not charge.settlement_method:
                        charge.settlement_method = "onchain"
                    charge.add_extra({"payment_method": "onchain"})
                    await update_charge(charge)
                    stop_onchain_listener(charge.onchainaddress)
                    logger.success(f"Charge {charge.id} marked as paid.")
                    continue
                start_onchain_listener(charge.onchainaddress)
            except Exception as exc:
                logger.warning(f"Error checking charge {charge.id}: {exc!s}")

        await asyncio.sleep(ONCHAIN_BALANCE_POLL_SECONDS)


async def wait_for_paid_invoices():
    invoice_queue = asyncio.Queue()
    register_invoice_listener(invoice_queue, "ext_satspay")

    while settings.lnbits_running:
        payment = await invoice_queue.get()
        await on_invoice_paid(payment)


async def send_success_websocket(charge: Charge):
    for charge_id, listeners in public_ws_listeners.items():
        if charge_id == charge.id:
            for listener in listeners:
                await listener.send_json(
                    {
                        "paid": charge.paid_fasttrack,
                        "balance": charge.balance,
                        "pending": charge.pending,
                        "settlement_method": charge.settlement_method,
                        "settlement_proof": charge.settlement_proof,
                        "completelink": (
                            charge.completelink if charge.paid_fasttrack else None
                        ),
                    }
                )


async def on_invoice_paid(payment: Payment) -> None:
    if not payment.extra or payment.extra.get("tag") != "charge":
        return

    charge_id = payment.extra.get("charge")
    if not charge_id:
        return

    charge = await get_charge(charge_id)
    assert charge, f"On invoice paid, charge `{charge_id}` not found."

    if payment.fiat_provider:
        # fiat payment created and confirmed via core's fiat provider integration
        if charge.paid or payment.fiat_provider != charge.fiat_provider:
            return
        charge.balance = charge.amount
        charge.paid = True
        charge.settlement_method = payment.fiat_provider
        charge.settlement_proof = payment.extra.get("fiat_checking_id")
        charge.add_extra({"payment_method": payment.fiat_provider})
        logger.success(f"Charge {charge.id} fiat invoice paid ({payment.fiat_provider}).")
        charge = await update_charge(charge)
        await send_success_websocket(charge)
        if charge.webhook:
            resp = await call_webhook(charge)
            charge.add_extra(resp)
            await update_charge(charge)
        return

    if charge.lnbitswallet and charge.payment_hash == payment.payment_hash:
        charge.balance = int(payment.amount / 1000)
        charge.paid = True
        logger.success(f"Charge {charge.id} invoice paid.")
        charge.settlement_method = "lightning"
        charge.settlement_proof = payment.preimage
        charge.add_extra({"payment_method": "lightning"})
        charge = await update_charge(charge)
        await send_success_websocket(charge)
        if charge.webhook:
            resp = await call_webhook(charge)
            charge.add_extra(resp)
            await update_charge(charge)


def start_onchain_listener(address: str):
    if address in tracked_addresses:
        return
    tracked_addresses.append(address)
    logger.debug(f"start_onchain_listener ({len(tracked_addresses)}")
    ws_send_queue.put_nowait({"track-addresses": tracked_addresses})


def stop_onchain_listener(address: str):
    if address in tracked_addresses:
        tracked_addresses.remove(address)
        logger.debug(f"stop_onchain_listener ({len(tracked_addresses)}")
        ws_send_queue.put_nowait({"track-addresses": tracked_addresses})


async def wait_for_onchain():
    while settings.lnbits_running:
        ws_message = await ws_receive_queue.get()
        txs = ws_message.get("multi-address-transactions")
        if not txs:
            continue
        for address, data in txs.items():
            await _handle_ws_message(address, data)


async def _handle_ws_message(address: str, data: dict):
    charge = await get_charge_by_onchain_address(address)
    assert charge, f"Charge with address `{address}` does not exist."
    # Only count money that arrived after this charge was created — the same
    # rule as the REST poll in check_charge_balance, or a pre-existing balance
    # settles the charge for free.
    since_ts = charge.timestamp.timestamp()
    mempool_txs = data.get("mempool", [])
    confirmed_txs = [tx for tx in data.get("confirmed", []) if tx_after(tx, since_ts)]
    unconfirmed_balance = sum_transactions(address, mempool_txs)
    confirmed_balance = sum_transactions(address, confirmed_txs, since_ts)
    unconfirmed_txids = [tx["txid"] for tx in mempool_txs]
    confirmed_txids = [tx["txid"] for tx in confirmed_txs]
    charge.add_extra({"txids": confirmed_txids + unconfirmed_txids})
    if charge.zeroconf:
        confirmed_balance += unconfirmed_balance
    charge.balance = confirmed_balance
    charge.pending = unconfirmed_balance
    charge.paid = charge.balance >= charge.amount
    if charge.paid:
        charge.settlement_method = "onchain"
        charge.settlement_proof = json.dumps(confirmed_txids + unconfirmed_txids)
        charge.add_extra({"payment_method": "onchain"})
        logger.success(f"Charge {charge.id} onchain paid.")
        stop_onchain_listener(address)
    charge = await update_charge(charge)
    await send_success_websocket(charge)
    if charge.webhook:
        resp = await call_webhook(charge)
        charge.add_extra(resp)
        await update_charge(charge)
