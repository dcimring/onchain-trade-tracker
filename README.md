# Onchain Trade Tracker

![Onchain Trade Tracker](.github/social-preview.png)

A self-hosted **crypto portfolio tracker** and **DeFi PnL dashboard** for onchain
trades across multiple wallets, powered by the [Zerion API](https://developers.zerion.io).
It syncs your DEX swap transactions into a local SQLite ledger, computes **cost
basis** and **average entry price** (using the USD value of what you sold at swap
time), and shows current value plus **realized and unrealized profit and loss** —
cross-checked against Zerion's own PnL numbers.

Works on any chain Zerion indexes — Ethereum, Base, Arbitrum, Optimism, Polygon,
Solana, Robinhood Chain, and 30+ more — with no account, no subscription, and no
data leaving your machine. A free, open-source alternative to paid trade-PnL
features in portfolio apps.

## Setup

1. Get an API key at [dashboard.zerion.io](https://dashboard.zerion.io).
2. Edit `.env`:

   ```
   ZERION_API_KEY=zk_prod_...
   WALLET_ADDRESSES=0xabc...,0xdef...
   ```

3. Install dependencies:

   ```
   uv sync
   ```

## Run

```
uv run uvicorn tracker.app:app --port 8000
```

Open http://localhost:8000 and hit **Sync**. Trades land in `tracker.db`
(gitignored); syncs are incremental after the first pull.

## How PnL is computed

- Every Zerion `trade` transaction is stored as one swap (sold → bought).
- Each swap is booked at **one USD value for both legs** (Zerion prices the two
  sides separately, and they differ by slippage and token taxes — using both
  would count that gap twice). With cash on one side (ETH, SOL, stablecoins),
  it's the cash amount: **cost basis** of a buy = what you paid + network fee,
  proceeds of a sell = what you got. For a **token→token swap** it's the value
  *received*: the old position realizes its PnL (absorbing the exit friction)
  and the new one starts at its market value.
- **Average entry price** = total USD spent ÷ total units bought.
- **Realized PnL** uses the average-cost method when you sell.
- **Unrealized PnL** = current value (live from Zerion positions) − remaining cost basis.
- Aggregator swaps that move several tokens per direction (e.g. Solana routers
  via FOMO/Jupiter that hand back leftover SOL alongside the token bought, or
  take a fee in a second token) are netted: the trade is recorded as the
  primary token in/out, and the cost is total paid minus refunds received.
- **Dividend tokens**: tokens that mint a 1:1 `DIVIDEND_TRACKER`-style receipt
  from the zero address on every buy are recognised; the receipt is ignored, and
  its contract is remembered as a dividend source. Payouts from it (standalone
  `receive` transactions or ones claimed inside a trade) count as realized PnL
  for the paying token, and as a buy of the token paid at its value when received.
- If Zerion returns no USD value for a transfer, stablecoins are valued at $1
  and the trade is re-fetched on later syncs until it's priced.
- Tokens sold without a tracked purchase (e.g. ETH you bridged in) are flagged
  with `*` — those units carry no cost basis and contribute zero realized PnL.
- The **Zerion check** badge compares our total PnL per token against Zerion's
  `/pnl` endpoint; `✓` means within 1%.
