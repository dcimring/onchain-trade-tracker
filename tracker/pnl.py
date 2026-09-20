"""Average-cost position math over the local trade ledger."""

from collections import defaultdict

from .sync import STABLECOINS

# Cash-like assets: when exactly one side of a swap is one of these, its USD
# value is the money actually paid or received, so it prices the whole swap.
QUOTE_ASSETS = STABLECOINS | {"ETH", "WETH", "SOL", "WSOL", "BNB", "WBNB", "POL", "MATIC", "AVAX"}


def swap_value(t: dict) -> float:
    """The single USD value a swap is booked at, used for both of its legs.

    Zerion values each side at its own market price, and the two differ by
    slippage and token taxes. Booking the legs at different values would count
    that gap twice when both tokens are tracked positions. With cash on one
    side, the cash amount is the value (cost = what you paid, proceeds = what
    you got). For token→token swaps it is the value received: the position you
    left absorbs the exit friction and the new one starts at its market value.
    """
    sold_usd = t["sold_usd_value"] or 0.0
    bought_usd = t["bought_usd_value"] or 0.0
    sold_is_quote = t["sold_symbol"] in QUOTE_ASSETS
    bought_is_quote = t["bought_symbol"] in QUOTE_ASSETS
    if sold_is_quote and not bought_is_quote:
        return sold_usd or bought_usd
    return bought_usd or sold_usd


def compute_positions(trades: list[dict], dividends: list[dict] | None = None) -> dict[str, dict]:
    """Replay trades chronologically, tracking each token with average-cost basis.

    Every swap is both a buy of the bought token (cost = the swap's value, plus
    the network fee) and a sell of the sold token (proceeds = that same value;
    see ``swap_value``). Units sold that were never bought through a
    tracked trade (e.g. ETH bridged in from elsewhere) carry no cost basis, so
    they contribute zero realized PnL and are counted as untracked.

    A dividend is income for the token that paid it (realized PnL at the USD
    value when received) and an acquisition of the token it was paid in, with
    that same value as its cost basis.
    """
    positions: dict[str, dict] = defaultdict(
        lambda: {
            "quantity": 0.0,
            "cost_basis": 0.0,
            "realized_pnl": 0.0,
            "total_bought_qty": 0.0,
            "total_bought_usd": 0.0,
            "untracked_sold_qty": 0.0,
            "dividend_income": 0.0,
            "fungible_id": None,
            "trade_count": 0,
        }
    )

    events = [(t["timestamp"] or "", 0, t) for t in trades]
    events += [(d["timestamp"] or "", 1, d) for d in dividends or []]
    for _, is_dividend, t in sorted(events, key=lambda e: e[:2]):
        if is_dividend:
            qty = t["quantity"] or 0.0
            usd = t["usd_value"] or 0.0
            p = positions[t["symbol"]]
            p["quantity"] += qty
            p["cost_basis"] += usd
            p["total_bought_qty"] += qty
            p["total_bought_usd"] += usd
            p["fungible_id"] = t["fungible_id"] or p["fungible_id"]
            source = positions[t["source_symbol"]]
            source["realized_pnl"] += usd
            source["dividend_income"] += usd
            continue

        sold_sym = t["sold_symbol"]
        bought_sym = t["bought_symbol"]
        sold_qty = t["sold_quantity"] or 0.0
        bought_qty = t["bought_quantity"] or 0.0
        value = swap_value(t)
        fee = t["fee_usd"] or 0.0

        # Buy side: acquire bought token at the swap's value.
        if bought_sym and bought_qty > 0:
            cost = value + fee
            p = positions[bought_sym]
            p["quantity"] += bought_qty
            p["cost_basis"] += cost
            p["total_bought_qty"] += bought_qty
            p["total_bought_usd"] += cost
            p["fungible_id"] = t["bought_fungible_id"] or p["fungible_id"]
            p["trade_count"] += 1

        # Sell side: dispose of sold token at that same value.
        if sold_sym and sold_qty > 0:
            proceeds = value
            p = positions[sold_sym]
            p["fungible_id"] = t["sold_fungible_id"] or p["fungible_id"]
            p["trade_count"] += 1
            tracked_qty = min(sold_qty, max(p["quantity"], 0.0))
            untracked_qty = sold_qty - tracked_qty
            if tracked_qty > 0 and p["quantity"] > 0:
                avg_cost = p["cost_basis"] / p["quantity"]
                cost_removed = avg_cost * tracked_qty
                p["realized_pnl"] += proceeds * (tracked_qty / sold_qty) - cost_removed
                p["cost_basis"] -= cost_removed
                p["quantity"] -= tracked_qty
            if untracked_qty > 0:
                p["untracked_sold_qty"] += untracked_qty

    result = {}
    for sym, p in positions.items():
        avg_entry = p["total_bought_usd"] / p["total_bought_qty"] if p["total_bought_qty"] > 0 else None
        result[sym] = {**p, "avg_entry_price": avg_entry}
    return result
