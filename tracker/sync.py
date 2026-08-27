import json
import time

from . import db
from .zerion import ZerionClient


def _sum_transfers(transfers: list[dict], direction: str) -> dict:
    """Aggregate a trade's transfers in one direction into symbol/qty/USD."""
    symbol = None
    fungible_id = None
    quantity = 0.0
    usd_value = 0.0
    for tr in transfers:
        if tr.get("direction") != direction:
            continue
        info = tr.get("fungible_info") or {}
        if info:
            symbol = info.get("symbol") or symbol
            fungible_id = tr.get("fungible_id") or info.get("id") or fungible_id
        quantity += float((tr.get("quantity") or {}).get("float") or 0)
        usd_value += float(tr.get("value") or 0)
    return {
        "symbol": symbol,
        "fungible_id": fungible_id,
        "quantity": quantity,
        "usd_value": usd_value,
    }


def transaction_to_trade(tx: dict, wallet: str) -> dict | None:
    attrs = tx.get("attributes", {})
    transfers = attrs.get("transfers", [])
    sold = _sum_transfers(transfers, "out")
    bought = _sum_transfers(transfers, "in")
    if not sold["symbol"] and not bought["symbol"]:
        return None
    fee = attrs.get("fee") or {}
    chain = ((tx.get("relationships") or {}).get("chain") or {}).get("data", {}).get("id")
    return {
        "tx_hash": attrs.get("hash"),
        "wallet": wallet,
        "chain": chain,
        "timestamp": attrs.get("mined_at"),
        "sold_symbol": sold["symbol"],
        "sold_fungible_id": sold["fungible_id"],
        "sold_quantity": sold["quantity"],
        "sold_usd_value": sold["usd_value"],
        "bought_symbol": bought["symbol"],
        "bought_fungible_id": bought["fungible_id"],
        "bought_quantity": bought["quantity"],
        "bought_usd_value": bought["usd_value"],
        "fee_usd": float(fee.get("value") or 0),
        "raw_json": json.dumps(tx),
    }


def sync_wallet(client: ZerionClient, conn, wallet: str) -> int:
    # Re-fetch a 1h overlap window so late-indexed txs aren't missed.
    last = db.get_last_synced(conn, wallet)
    min_mined = max(0, last - 3600 * 1000) if last else None
    txs = client.get_trade_transactions(wallet, min_mined_at_ms=min_mined)
    count = 0
    for tx in txs:
        trade = transaction_to_trade(tx, wallet)
        if trade and trade["tx_hash"]:
            db.upsert_trade(conn, trade)
            count += 1
    db.set_last_synced(conn, wallet, int(time.time() * 1000))
    conn.commit()
    return count


def sync_all(wallets: list[str]) -> dict[str, int]:
    client = ZerionClient()
    conn = db.get_conn()
    try:
        return {w: sync_wallet(client, conn, w) for w in wallets}
    finally:
        conn.close()
