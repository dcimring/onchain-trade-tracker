from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from . import db, pnl
from .config import PROJECT_ROOT, get_wallets
from .sync import sync_all
from .zerion import ZerionClient

app = FastAPI(title="Onchain Trade Tracker")

STATIC_DIR = PROJECT_ROOT / "static"


@app.get("/")
def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/api/wallets")
def wallets() -> dict:
    return {"wallets": get_wallets()}


@app.post("/api/sync")
def sync() -> dict:
    try:
        counts = sync_all(get_wallets())
    except RuntimeError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"synced": counts}


@app.get("/api/trades")
def trades(wallet: str | None = None, token: str | None = None) -> dict:
    conn = db.get_conn()
    try:
        rows = db.get_trades(conn, wallet=wallet, token=token)
        dividends = db.get_dividends(conn, wallet=wallet, token=token)
    finally:
        conn.close()
    for r in rows:
        r.pop("raw_json", None)
    return {"trades": rows, "dividends": dividends}


def _current_holdings(client: ZerionClient, wallet_list: list[str]) -> dict[str, dict]:
    """Aggregate current balances/prices per token symbol across wallets."""
    holdings: dict[str, dict] = {}
    for w in wallet_list:
        for pos in client.get_positions(w):
            attrs = pos.get("attributes", {})
            info = attrs.get("fungible_info") or {}
            symbol = info.get("symbol")
            if not symbol:
                continue
            fungible_id = ((pos.get("relationships") or {}).get("fungible") or {}).get("data", {}).get("id")
            h = holdings.setdefault(
                symbol,
                {"quantity": 0.0, "value": 0.0, "price": None, "fungible_id": fungible_id, "name": info.get("name")},
            )
            h["quantity"] += float((attrs.get("quantity") or {}).get("float") or 0)
            h["value"] += float(attrs.get("value") or 0)
            if attrs.get("price") is not None:
                h["price"] = float(attrs["price"])
            h["fungible_id"] = h["fungible_id"] or fungible_id
    return holdings


def _zerion_pnl_check(client: ZerionClient, wallet_list: list[str], fungible_ids: list[str]) -> dict[str, dict]:
    """Best-effort per-fungible PnL from Zerion, summed across wallets."""
    checks: dict[str, dict] = {}
    if not fungible_ids:
        return checks
    for w in wallet_list:
        try:
            data = client.get_pnl(w, fungible_ids=fungible_ids)
        except Exception:
            continue
        attrs = ((data.get("data") or {}).get("attributes")) or {}
        by_id = ((attrs.get("breakdown") or {}).get("by_id")) or {}
        for fid, item in by_id.items():
            c = checks.setdefault(fid, {"realized_gain": 0.0, "unrealized_gain": 0.0, "avg_buy_price": None})
            c["realized_gain"] += float(item.get("realized_gain") or 0)
            c["unrealized_gain"] += float(item.get("unrealized_gain") or 0)
            if item.get("average_buy_price") is not None:
                c["avg_buy_price"] = float(item["average_buy_price"])
    return checks


@app.get("/api/positions")
def positions() -> dict:
    try:
        wallet_list = get_wallets()
        client = ZerionClient()
    except RuntimeError as e:
        raise HTTPException(status_code=400, detail=str(e))

    conn = db.get_conn()
    try:
        trades = db.get_trades(conn)
        dividends = db.get_dividends(conn)
    finally:
        conn.close()

    computed = pnl.compute_positions(trades, dividends)
    holdings = _current_holdings(client, wallet_list)

    fungible_ids = [p["fungible_id"] for p in computed.values() if p["fungible_id"]]
    zerion_checks = _zerion_pnl_check(client, wallet_list, fungible_ids)

    rows = []
    totals = {"value": 0.0, "unrealized_pnl": 0.0, "realized_pnl": 0.0, "cost_basis": 0.0}
    for symbol, p in computed.items():
        if p["total_bought_qty"] <= 0 and p["realized_pnl"] == 0:
            continue
        held = holdings.get(symbol, {})
        current_price = held.get("price")
        # Value the *tracked* position (units bought via synced trades), not the
        # raw wallet balance, which can include untracked holdings such as a
        # stablecoin deposit that was never part of a swap.
        current_value = None
        if p["quantity"] > 1e-12:
            if current_price is not None:
                current_value = p["quantity"] * current_price
            elif held.get("value") is not None:
                current_value = held["value"]
        unrealized = (current_value - p["cost_basis"]) if current_value is not None else None
        row = {
            "symbol": symbol,
            "name": held.get("name"),
            "fungible_id": p["fungible_id"],
            "quantity": p["quantity"],
            "wallet_quantity": held.get("quantity"),
            "avg_entry_price": p["avg_entry_price"],
            "current_price": current_price,
            "current_value": current_value,
            "cost_basis": p["cost_basis"],
            "unrealized_pnl": unrealized,
            "unrealized_pnl_pct": (unrealized / p["cost_basis"] * 100) if unrealized is not None and p["cost_basis"] > 0 else None,
            "realized_pnl": p["realized_pnl"],
            "dividend_income": p["dividend_income"],
            "trade_count": p["trade_count"],
            "untracked_sold_qty": p["untracked_sold_qty"],
            "zerion": zerion_checks.get(p["fungible_id"] or ""),
        }
        rows.append(row)
        totals["cost_basis"] += p["cost_basis"]
        totals["realized_pnl"] += p["realized_pnl"]
        if current_value is not None and p["quantity"] > 1e-12:
            totals["value"] += current_value
        if unrealized is not None:
            totals["unrealized_pnl"] += unrealized

    rows.sort(key=lambda r: (r["current_value"] or 0), reverse=True)
    return {"positions": rows, "totals": totals}


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
