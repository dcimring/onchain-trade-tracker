import json
import sqlite3
from datetime import datetime

from .config import DB_PATH

SCHEMA = """
CREATE TABLE IF NOT EXISTS trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    tx_hash TEXT NOT NULL,
    wallet TEXT NOT NULL,
    chain TEXT,
    timestamp TEXT NOT NULL,
    sold_symbol TEXT,
    sold_fungible_id TEXT,
    sold_quantity REAL,
    sold_usd_value REAL,
    bought_symbol TEXT,
    bought_fungible_id TEXT,
    bought_quantity REAL,
    bought_usd_value REAL,
    fee_usd REAL DEFAULT 0,
    raw_json TEXT,
    UNIQUE (tx_hash, wallet)
);

CREATE TABLE IF NOT EXISTS dividend_sources (
    chain TEXT NOT NULL,
    address TEXT NOT NULL,
    symbol TEXT NOT NULL,
    fungible_id TEXT,
    PRIMARY KEY (chain, address)
);

CREATE TABLE IF NOT EXISTS dividends (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    tx_hash TEXT NOT NULL,
    wallet TEXT NOT NULL,
    chain TEXT,
    timestamp TEXT NOT NULL,
    source_symbol TEXT NOT NULL,
    source_fungible_id TEXT,
    symbol TEXT NOT NULL,
    fungible_id TEXT,
    quantity REAL,
    usd_value REAL,
    UNIQUE (tx_hash, wallet, symbol)
);

CREATE TABLE IF NOT EXISTS sync_state (
    wallet TEXT PRIMARY KEY,
    last_synced_at INTEGER
);
"""


def get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    return conn


def upsert_trade(conn: sqlite3.Connection, t: dict) -> None:
    conn.execute(
        """
        INSERT INTO trades (tx_hash, wallet, chain, timestamp,
            sold_symbol, sold_fungible_id, sold_quantity, sold_usd_value,
            bought_symbol, bought_fungible_id, bought_quantity, bought_usd_value,
            fee_usd, raw_json)
        VALUES (:tx_hash, :wallet, :chain, :timestamp,
            :sold_symbol, :sold_fungible_id, :sold_quantity, :sold_usd_value,
            :bought_symbol, :bought_fungible_id, :bought_quantity, :bought_usd_value,
            :fee_usd, :raw_json)
        ON CONFLICT (tx_hash, wallet) DO UPDATE SET
            chain = excluded.chain,
            timestamp = excluded.timestamp,
            sold_symbol = excluded.sold_symbol,
            sold_fungible_id = excluded.sold_fungible_id,
            sold_quantity = excluded.sold_quantity,
            sold_usd_value = COALESCE(excluded.sold_usd_value, trades.sold_usd_value),
            bought_symbol = excluded.bought_symbol,
            bought_fungible_id = excluded.bought_fungible_id,
            bought_quantity = excluded.bought_quantity,
            bought_usd_value = COALESCE(excluded.bought_usd_value, trades.bought_usd_value),
            fee_usd = CASE WHEN excluded.fee_usd > 0 THEN excluded.fee_usd ELSE trades.fee_usd END,
            raw_json = excluded.raw_json
        """,
        t,
    )


def get_trades(conn: sqlite3.Connection, wallet: str | None = None, token: str | None = None) -> list[dict]:
    query = "SELECT * FROM trades WHERE 1=1"
    params: list = []
    if wallet:
        query += " AND wallet = ?"
        params.append(wallet)
    if token:
        query += " AND (sold_symbol = ? OR bought_symbol = ?)"
        params.extend([token, token])
    query += " ORDER BY timestamp ASC"
    return [dict(r) for r in conn.execute(query, params).fetchall()]


def get_raw_transactions(conn: sqlite3.Connection, wallet: str) -> list[dict]:
    """The Zerion transactions behind this wallet's stored trades, as originally fetched."""
    rows = conn.execute("SELECT raw_json FROM trades WHERE wallet = ? AND raw_json IS NOT NULL", (wallet,)).fetchall()
    return [json.loads(r["raw_json"]) for r in rows]


def earliest_unpriced_trade_ms(conn: sqlite3.Connection, wallet: str) -> int | None:
    """Epoch ms of the oldest trade for this wallet missing a USD value on either side."""
    row = conn.execute(
        "SELECT MIN(timestamp) AS ts FROM trades WHERE wallet = ? "
        "AND (sold_usd_value IS NULL OR bought_usd_value IS NULL)",
        (wallet,),
    ).fetchone()
    if not row or not row["ts"]:
        return None
    return _iso_to_ms(row["ts"])


def _iso_to_ms(ts: str) -> int:
    return int(datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp() * 1000)


def upsert_dividend_source(conn: sqlite3.Connection, s: dict) -> None:
    conn.execute(
        "INSERT INTO dividend_sources (chain, address, symbol, fungible_id) "
        "VALUES (:chain, :address, :symbol, :fungible_id) "
        "ON CONFLICT (chain, address) DO UPDATE SET symbol = excluded.symbol, fungible_id = excluded.fungible_id",
        s,
    )


def get_dividend_sources(conn: sqlite3.Connection) -> dict[tuple, dict]:
    """Known dividend-paying contracts, keyed by (chain, lowercase address)."""
    rows = conn.execute("SELECT * FROM dividend_sources").fetchall()
    return {(r["chain"], r["address"]): dict(r) for r in rows}


def upsert_dividend(conn: sqlite3.Connection, d: dict) -> None:
    conn.execute(
        """
        INSERT INTO dividends (tx_hash, wallet, chain, timestamp, source_symbol,
            source_fungible_id, symbol, fungible_id, quantity, usd_value)
        VALUES (:tx_hash, :wallet, :chain, :timestamp, :source_symbol,
            :source_fungible_id, :symbol, :fungible_id, :quantity, :usd_value)
        ON CONFLICT (tx_hash, wallet, symbol) DO UPDATE SET
            chain = excluded.chain,
            timestamp = excluded.timestamp,
            source_symbol = excluded.source_symbol,
            source_fungible_id = excluded.source_fungible_id,
            fungible_id = excluded.fungible_id,
            quantity = excluded.quantity,
            usd_value = COALESCE(excluded.usd_value, dividends.usd_value)
        """,
        d,
    )


def get_dividends(conn: sqlite3.Connection, wallet: str | None = None, token: str | None = None) -> list[dict]:
    query = "SELECT * FROM dividends WHERE 1=1"
    params: list = []
    if wallet:
        query += " AND wallet = ?"
        params.append(wallet)
    if token:
        query += " AND (source_symbol = ? OR symbol = ?)"
        params.extend([token, token])
    query += " ORDER BY timestamp ASC"
    return [dict(r) for r in conn.execute(query, params).fetchall()]


def dividend_sync_start_ms(conn: sqlite3.Connection, wallet: str, source_symbols: set[str]) -> int | None:
    """Epoch ms to pull dividend payouts from: the wallet's latest recorded payout,
    else its first trade of a dividend-paying token. None if it never traded one."""
    row = conn.execute("SELECT MAX(timestamp) AS ts FROM dividends WHERE wallet = ?", (wallet,)).fetchone()
    if row and row["ts"]:
        return _iso_to_ms(row["ts"])
    marks = ",".join("?" * len(source_symbols))
    row = conn.execute(
        f"SELECT MIN(timestamp) AS ts FROM trades WHERE wallet = ? AND bought_symbol IN ({marks})",
        (wallet, *source_symbols),
    ).fetchone()
    return _iso_to_ms(row["ts"]) if row and row["ts"] else None


def get_last_synced(conn: sqlite3.Connection, wallet: str) -> int | None:
    row = conn.execute("SELECT last_synced_at FROM sync_state WHERE wallet = ?", (wallet,)).fetchone()
    return row["last_synced_at"] if row else None


def set_last_synced(conn: sqlite3.Connection, wallet: str, ts_ms: int) -> None:
    conn.execute(
        "INSERT INTO sync_state (wallet, last_synced_at) VALUES (?, ?) "
        "ON CONFLICT (wallet) DO UPDATE SET last_synced_at = excluded.last_synced_at",
        (wallet, ts_ms),
    )
