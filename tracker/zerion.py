import time

import httpx

from .config import get_api_key

BASE_URL = "https://api.zerion.io"
RETRYABLE = {429, 503}
MAX_RETRIES = 5


class ZerionClient:
    def __init__(self, api_key: str | None = None):
        self._client = httpx.Client(
            base_url=BASE_URL,
            auth=(api_key or get_api_key(), ""),
            headers={"accept": "application/json"},
            timeout=30,
            follow_redirects=True,
        )

    def _get(self, url: str, params: dict | None = None) -> dict:
        for attempt in range(MAX_RETRIES):
            resp = self._client.get(url, params=params)
            if resp.status_code in RETRYABLE:
                # 503 = Zerion warming up the wallet; 429 = rate limit
                time.sleep(2 * (attempt + 1))
                continue
            resp.raise_for_status()
            return resp.json()
        resp.raise_for_status()
        return resp.json()

    def get_trade_transactions(self, address: str, min_mined_at_ms: int | None = None) -> list[dict]:
        """All 'trade' (swap) transactions for a wallet, following pagination."""
        params: dict = {
            "filter[operation_types]": "trade",
            "filter[asset_types]": "fungible",
            "filter[trash]": "only_non_trash",
            "page[size]": 100,
            "currency": "usd",
        }
        if min_mined_at_ms:
            params["filter[min_mined_at]"] = min_mined_at_ms

        transactions: list[dict] = []
        url: str | None = f"/v1/wallets/{address}/transactions/"
        while url:
            data = self._get(url, params=params)
            transactions.extend(data.get("data", []))
            url = data.get("links", {}).get("next")
            params = None  # next link already carries the query string
        return transactions

    def get_positions(self, address: str) -> list[dict]:
        data = self._get(
            f"/v1/wallets/{address}/positions/",
            params={
                "filter[positions]": "only_simple",
                "filter[trash]": "only_non_trash",
                "currency": "usd",
                "sort": "value",
            },
        )
        return data.get("data", [])

    def get_pnl(self, address: str, fungible_ids: list[str] | None = None) -> dict:
        params: dict = {"currency": "usd"}
        if fungible_ids:
            params["filter[fungible_ids]"] = ",".join(fungible_ids)
        return self._get(f"/v1/wallets/{address}/pnl", params=params)
