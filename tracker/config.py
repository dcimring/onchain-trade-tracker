import os
from pathlib import Path

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DB_PATH = PROJECT_ROOT / "tracker.db"

def get_api_key() -> str:
    load_dotenv(PROJECT_ROOT / ".env", override=True)
    key = os.environ.get("ZERION_API_KEY", "").strip()
    if not key or key == "your_zerion_api_key_here":
        raise RuntimeError("ZERION_API_KEY is not set — add it to .env")
    return key


def get_wallets() -> list[str]:
    load_dotenv(PROJECT_ROOT / ".env", override=True)
    raw = os.environ.get("WALLET_ADDRESSES", "")
    wallets = [w.strip() for w in raw.split(",") if w.strip()]
    wallets = [w for w in wallets if w != "0xYourWalletAddressHere"]
    if not wallets:
        raise RuntimeError("WALLET_ADDRESSES is not set — add at least one address to .env")
    return wallets
