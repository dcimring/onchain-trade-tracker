import json
import time

from . import db
from .zerion import ZerionClient


# Stablecoins valued at $1/unit when Zerion returns no price for a transfer.
STABLECOINS = {"USDC", "USDT", "DAI", "USDbC", "USDC.e", "USDT.e", "FDUSD", "PYUSD", "USDe"}


ZERO_ADDRESS = "0x0000000000000000000000000000000000000000"


def _qty(tr: dict) -> float:
    return float((tr.get("quantity") or {}).get("float") or 0)


def _tx_chain(tx: dict) -> str | None:
    return ((tx.get("relationships") or {}).get("chain") or {}).get("data", {}).get("id")


def _mirrored_transfer(tr: dict, transfers: list[dict]) -> dict | None:
    """The real transfer that ``tr`` mirrors, if ``tr`` is a bookkeeping token.

    Dividend-paying tokens (e.g. COUPONS) mint a non-tradable DIVIDEND_TRACKER
    share token 1:1 from the zero address on every buy, and burn it on every
    sell. It is never priced, so unless it is dropped it would outrank the real
    token as the trade's primary asset.
    """
    if tr.get("value") is not None:
        return None
    counterparty = tr.get("sender") if tr.get("direction") == "in" else tr.get("recipient")
    if (counterparty or "").lower() != ZERO_ADDRESS:
        return None
    qty = _qty(tr)
    symbol = (tr.get("fungible_info") or {}).get("symbol")
    for other in transfers:
        if other is tr or other.get("direction") != tr.get("direction"):
            continue
        if (other.get("fungible_info") or {}).get("symbol") == symbol:
            continue
        if qty > 0 and abs(_qty(other) - qty) <= 1e-9 * qty:
            return other
    return None


def dividend_sources_in(tx: dict) -> list[dict]:
    """Dividend-paying contracts revealed by this transaction.

    The mirror token's own contract is what later pays the dividends out, so
    its address identifies payouts, and the token it mirrors is the one paying.
    """
    chain = _tx_chain(tx)
    transfers = tx.get("attributes", {}).get("transfers", [])
    sources = []
    for tr in transfers:
        real = _mirrored_transfer(tr, transfers)
        if not real:
            continue
        real_info = real.get("fungible_info") or {}
        for impl in (tr.get("fungible_info") or {}).get("implementations") or []:
            if impl.get("chain_id") == chain and impl.get("address"):
                sources.append(
                    {
                        "chain": chain,
                        "address": impl["address"].lower(),
                        "symbol": real_info.get("symbol"),
                        "fungible_id": real.get("fungible_id") or real_info.get("id"),
                    }
                )
    return sources


def _dividend_source(tr: dict, chain: str | None, sources: dict[tuple, dict]) -> dict | None:
    if tr.get("direction") != "in":
        return None
    return sources.get((chain, (tr.get("sender") or "").lower()))


def transaction_to_dividends(tx: dict, wallet: str, sources: dict[tuple, dict]) -> list[dict]:
    """Payouts received from known dividend contracts, one row per token paid."""
    attrs = tx.get("attributes", {})
    chain = _tx_chain(tx)
    rows: dict[str, dict] = {}
    for tr in attrs.get("transfers", []):
        source = _dividend_source(tr, chain, sources)
        info = tr.get("fungible_info") or {}
        symbol = info.get("symbol")
        if not source or not symbol or not attrs.get("hash"):
            continue
        row = rows.setdefault(
            symbol,
            {
                "tx_hash": attrs.get("hash"),
                "wallet": wallet,
                "chain": chain,
                "timestamp": attrs.get("mined_at"),
                "source_symbol": source["symbol"],
                "source_fungible_id": source["fungible_id"],
                "symbol": symbol,
                "fungible_id": tr.get("fungible_id") or info.get("id"),
                "quantity": 0.0,
                "usd_value": None,
            },
        )
        qty = _qty(tr)
        row["quantity"] += qty
        value = tr.get("value")
        if value is None and symbol in STABLECOINS:
            value = qty
        if value is not None:
            row["usd_value"] = (row["usd_value"] or 0.0) + float(value)
    return list(rows.values())


def _group_transfers(transfers: list[dict], direction: str) -> list[dict]:
    """Group a trade's transfers in one direction by token, summing qty/USD.

    Zerion sometimes returns no ``value`` for a transfer (fresh or obscure
    tokens). Stablecoins are then valued at $1/unit; anything else is marked
    ``value_known=False`` with usd_value 0. Groups are returned primary-first:
    an unpriced token ranks first (Zerion prices majors and native tokens
    reliably, so an unpriced asset is almost certainly the token being traded),
    then by USD value descending.
    """
    groups: dict[str, dict] = {}
    for tr in transfers:
        if tr.get("direction") != direction:
            continue
        info = tr.get("fungible_info") or {}
        symbol = info.get("symbol")
        if not symbol:
            continue
        fungible_id = tr.get("fungible_id") or info.get("id")
        g = groups.setdefault(
            symbol,
            {"symbol": symbol, "fungible_id": fungible_id, "quantity": 0.0, "usd_value": 0.0, "value_known": False},
        )
        g["fungible_id"] = g["fungible_id"] or fungible_id
        qty = _qty(tr)
        g["quantity"] += qty
        value = tr.get("value")
        if value is None and symbol in STABLECOINS:
            value = qty
        if value is not None:
            g["usd_value"] += float(value)
            g["value_known"] = True
    return sorted(groups.values(), key=lambda g: (not g["value_known"], g["usd_value"]), reverse=True)


def _sum_transfers(transfers: list[dict], direction: str) -> dict:
    """Primary asset moved in one direction, with the USD value netted across all assets.

    Aggregator swaps (e.g. Solana routers) often move several tokens per direction:
    fees paid in a second token on the way out, or leftover/refunded SOL coming back
    in alongside the token bought. The quantity is just the primary token's. The
    USD value is the *net* amount for this direction: everything moved this way
    minus secondary (non-primary) assets moved the opposite way. So for a buy,
    sold usd = total paid - refunds received, i.e. the true cost of the bought
    token; for a sell, bought usd = total received - extra fees paid in other
    tokens, i.e. the true proceeds. usd_value is None when the primary token
    in this direction is unpriced, so a later re-sync can fill it in.
    """
    same = _group_transfers(transfers, direction)
    other = _group_transfers(transfers, "in" if direction == "out" else "out")
    if not same:
        return {"symbol": None, "fungible_id": None, "quantity": 0.0, "usd_value": None}
    primary = same[0]
    usd_value: float | None = None
    # If the primary token itself is unpriced, this side's value is unknown
    # (dust like leftover SOL must not stand in for it); pnl falls back to the
    # other side of the swap and a later re-sync can fill it in.
    if primary["value_known"]:
        total_usd = sum(g["usd_value"] for g in same)
        other_secondary_usd = sum(g["usd_value"] for g in other[1:])
        usd_value = max(total_usd - other_secondary_usd, 0.0)
    return {
        "symbol": primary["symbol"],
        "fungible_id": primary["fungible_id"],
        "quantity": primary["quantity"],
        "usd_value": usd_value,
    }


def transaction_to_trade(tx: dict, wallet: str, sources: dict[tuple, dict] | None = None) -> dict | None:
    attrs = tx.get("attributes", {})
    chain = _tx_chain(tx)
    all_transfers = attrs.get("transfers", [])
    # Mirror tokens are bookkeeping and dividend payouts are income (recorded
    # separately); neither is part of what was swapped.
    transfers = [
        tr
        for tr in all_transfers
        if not _mirrored_transfer(tr, all_transfers) and not _dividend_source(tr, chain, sources or {})
    ]
    sold = _sum_transfers(transfers, "out")
    bought = _sum_transfers(transfers, "in")
    if not sold["symbol"] and not bought["symbol"]:
        return None
    fee = attrs.get("fee") or {}
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


def _sync_dividends(client: ZerionClient, conn, wallet: str, sources: dict[tuple, dict], trade_txs: list[dict]) -> None:
    """Record payouts from known dividend contracts.

    Most arrive as standalone 'receive' transactions; some are claimed inside a
    trade. Receives are re-pulled from the last recorded payout (or, before any
    exist, from the first trade of a dividend-paying token), not from the trade
    sync cursor, so payouts older than the cursor aren't missed.
    """
    if not sources:
        return
    since_ms = db.dividend_sync_start_ms(conn, wallet, {s["symbol"] for s in sources.values()})
    if since_ms is None:
        return
    receives = client.get_transactions(wallet, "receive", min_mined_at_ms=max(0, since_ms - 3600 * 1000))
    for tx in [*trade_txs, *receives]:
        for dividend in transaction_to_dividends(tx, wallet, sources):
            db.upsert_dividend(conn, dividend)


def sync_wallet(client: ZerionClient, conn, wallet: str) -> int:
    # Re-fetch a 1h overlap window so late-indexed txs aren't missed.
    last = db.get_last_synced(conn, wallet)
    min_mined = max(0, last - 3600 * 1000) if last else None
    # Also re-pull from the earliest trade Zerion hasn't priced yet, so values
    # that were missing at first sync get filled in once they're available.
    unpriced_ms = db.earliest_unpriced_trade_ms(conn, wallet)
    if min_mined is not None and unpriced_ms is not None:
        min_mined = min(min_mined, max(0, unpriced_ms - 60 * 1000))
    fetched = client.get_transactions(wallet, "trade", min_mined_at_ms=min_mined)
    # Replay stored transactions alongside the fetched ones (fetched copies win),
    # so parser fixes and newly discovered dividend contracts reach old trades.
    by_id = {tx.get("id"): tx for tx in db.get_raw_transactions(conn, wallet)}
    by_id.update({tx.get("id"): tx for tx in fetched})
    txs = list(by_id.values())
    for tx in txs:
        for source in dividend_sources_in(tx):
            db.upsert_dividend_source(conn, source)
    sources = db.get_dividend_sources(conn)
    for tx in txs:
        trade = transaction_to_trade(tx, wallet, sources)
        if trade and trade["tx_hash"]:
            db.upsert_trade(conn, trade)
    _sync_dividends(client, conn, wallet, sources, txs)
    db.set_last_synced(conn, wallet, int(time.time() * 1000))
    conn.commit()
    return len(fetched)


def sync_all(wallets: list[str]) -> dict[str, int]:
    client = ZerionClient()
    conn = db.get_conn()
    try:
        return {w: sync_wallet(client, conn, w) for w in wallets}
    finally:
        conn.close()
