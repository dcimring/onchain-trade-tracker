"""Average-cost position math over the local trade ledger."""

from collections import defaultdict


def compute_positions(trades: list[dict]) -> dict[str, dict]:
    """Replay trades chronologically, tracking each token with average-cost basis.

    Every swap is both a buy of the bought token (cost = USD value of what was
    sold, plus the network fee) and a sell of the sold token (proceeds = USD
    value of what was bought). Units sold that were never bought through a
    tracked trade (e.g. ETH bridged in from elsewhere) carry no cost basis, so
    they contribute zero realized PnL and are counted as untracked.
    """
    positions: dict[str, dict] = defaultdict(
        lambda: {
            "quantity": 0.0,
            "cost_basis": 0.0,
            "realized_pnl": 0.0,
            "total_bought_qty": 0.0,
            "total_bought_usd": 0.0,
            "untracked_sold_qty": 0.0,
            "fungible_id": None,
            "trade_count": 0,
        }
    )

    for t in sorted(trades, key=lambda t: t["timestamp"] or ""):
        sold_sym = t["sold_symbol"]
        bought_sym = t["bought_symbol"]
        sold_qty = t["sold_quantity"] or 0.0
        bought_qty = t["bought_quantity"] or 0.0
        sold_usd = t["sold_usd_value"] or 0.0
        bought_usd = t["bought_usd_value"] or 0.0
        fee = t["fee_usd"] or 0.0

        # Buy side: acquire bought token at the USD value paid.
        if bought_sym and bought_qty > 0:
            cost = (sold_usd or bought_usd) + fee
            p = positions[bought_sym]
            p["quantity"] += bought_qty
            p["cost_basis"] += cost
            p["total_bought_qty"] += bought_qty
            p["total_bought_usd"] += cost
            p["fungible_id"] = t["bought_fungible_id"] or p["fungible_id"]
            p["trade_count"] += 1

        # Sell side: dispose of sold token at the USD value received.
        if sold_sym and sold_qty > 0:
            proceeds = bought_usd or sold_usd
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
