Setup & Quickstart

This trading bot workspace is configured to run locally in a Python virtual environment.

1) Create & activate venv

   python3 -m venv .venv
   source .venv/bin/activate

2) Install requirements

   pip install -r trading_bot/requirements.txt

3) Set environment variables (example)

   export TELEGRAM_BOT_TOKEN="<your_bot_token>"
   export TELEGRAM_CHAT_ID="<your_chat_id>"
   # For live trading (when you enable):
   export UPBIT_ACCESS_KEY="<your_access_key>"
   export UPBIT_SECRET_KEY="<your_secret_key>"

4) Run Telegram test (paper mode by default)

   python trading_bot/monitor.py

5) Run a sample paper cycle (after installing requirements)

   python -c "from trading_bot.main import run_paper_cycle; run_paper_cycle()"

Security notes
- Keep API keys secret. Do NOT paste them into public chats.
- Live trading requires explicit --live flag and manual confirmation in the code.

If you want, I can proceed to run the tests after you install the venv and requirements, or I can attempt to install packages for you (need permission)."}