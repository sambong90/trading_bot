import os
import requests
from typing import Optional
from dotenv import load_dotenv

# Load .env if present (keeps local workflow simple)
env_path = os.path.join(os.path.dirname(__file__), '.env')
if os.path.exists(env_path):
    load_dotenv(env_path)

TELEGRAM_TOKEN_ENV = "TELEGRAM_BOT_TOKEN"
TELEGRAM_CHAT_ENV = "TELEGRAM_CHAT_ID"


def send_telegram(message: str, token: Optional[str] = None, chat_id: Optional[str] = None) -> bool:
    """Send a Telegram message via bot API.

    Expects environment variables TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID if token/chat_id not provided.
    Returns True on HTTP 200, else False.
    """
    token = token or os.getenv(TELEGRAM_TOKEN_ENV)
    chat_id = chat_id or os.getenv(TELEGRAM_CHAT_ENV)
    if not token or not chat_id:
        raise ValueError(
            f"Telegram token or chat id not provided. Set env vars {TELEGRAM_TOKEN_ENV} and {TELEGRAM_CHAT_ENV}"
        )
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {"chat_id": chat_id, "text": message}
    try:
        r = requests.post(url, json=payload, timeout=10)
        r.raise_for_status()
        return True
    except Exception as e:
        # keep exception message modest; caller can log more
        print("[monitor.send_telegram] error:", e)
        return False


if __name__ == '__main__':
    # quick local test runner. Only runs if env vars are set.
    test_msg = "Trading bot: Telegram integration test from your local trading_bot/monitor.py"
    try:
        ok = send_telegram(test_msg)
        if ok:
            print("Test message sent (check Telegram).")
        else:
            print("Failed to send test message. See printed error.")
    except Exception as ex:
        print("Telegram not configured:", ex)
