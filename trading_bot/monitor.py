import os
import requests
from typing import Optional, Tuple
from dotenv import load_dotenv

# Load .env if present (keeps local workflow simple)
env_path = os.path.join(os.path.dirname(__file__), '.env')
if os.path.exists(env_path):
    load_dotenv(env_path)

TELEGRAM_TOKEN_ENV = "TELEGRAM_BOT_TOKEN"
TELEGRAM_CHAT_ENV = "TELEGRAM_CHAT_ID"


def send_telegram(message: str, token: Optional[str] = None, chat_id: Optional[str] = None) -> Tuple[bool, Optional[str]]:
    """Send a Telegram message via bot API (one-way notifications).

    Compatible with telegram_bot.py listener: both use the same Bot API; alerts and chat commands work independently.
    Expects environment variables TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID if token/chat_id not provided.
    Returns (True, None) on success, (False, error_message) on failure. Retries without proxy on proxy/connection errors if TELEGRAM_USE_PROXY set.
    """
    token = token or os.getenv(TELEGRAM_TOKEN_ENV)
    chat_id = chat_id or os.getenv(TELEGRAM_CHAT_ENV)
    if not token or not chat_id:
        raise ValueError(
            f"Telegram token or chat id not provided. Set env vars {TELEGRAM_TOKEN_ENV} and {TELEGRAM_CHAT_ENV}"
        )
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {"chat_id": chat_id, "text": message}
    def _post(proxies=None):
        return requests.post(url, json=payload, timeout=10, proxies=proxies or {})

    try:
        r = _post()
        r.raise_for_status()
        return True, None
    except Exception as e:
        err_msg = str(e)
        use_proxy = os.getenv("TELEGRAM_USE_PROXY", "0").strip().lower() in ("1", "true", "yes")
        retry_without_proxy = use_proxy and (
            "ProxyError" in type(e).__name__
            or "ConnectionError" in type(e).__name__
            or "proxy" in err_msg.lower()
            or "ECONNREFUSED" in err_msg
        )
        if retry_without_proxy:
            try:
                r = _post(proxies={"http": None, "https": None})
                r.raise_for_status()
                return True, None
            except Exception as e2:
                return False, str(e2)
        return False, err_msg


if __name__ == '__main__':
    # quick local test runner. Only runs if env vars are set.
    test_msg = "Trading bot: Telegram integration test from your local trading_bot/monitor.py"
    try:
        ok, _ = send_telegram(test_msg)
        if ok:
            print("Test message sent (check Telegram).")
        else:
            print("Failed to send test message. See printed error.")
    except Exception as ex:
        print("Telegram not configured:", ex)

