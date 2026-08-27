import sqlite3

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
            sold_usd_value = excluded.sold_usd_value,
            bought_symbol = excluded.bought_symbol,
            bought_fungible_id = excluded.bought_fungible_id,
            bought_quantity = excluded.bought_quantity,
            bought_usd_value = excluded.bought_usd_value,
            fee_usd = excluded.fee_usd,
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


def get_last_synced(conn: sqlite3.Connection, wallet: str) -> int | None:
    row = conn.execute("SELECT last_synced_at FROM sync_state WHERE wallet = ?", (wallet,)).fetchone()
    return row["last_synced_at"] if row else None


def set_last_synced(conn: sqlite3.Connection, wallet: str, ts_ms: int) -> None:
    conn.execute(
        "INSERT INTO sync_state (wallet, last_synced_at) VALUES (?, ?) "
        "ON CONFLICT (wallet) DO UPDATE SET last_synced_at = excluded.last_synced_at",
        (wallet, ts_ms),
    )
