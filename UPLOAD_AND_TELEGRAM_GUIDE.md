# V5.11.1 Upload + Telegram

## Private bot repository
Upload/replace these files at repository root:
- 01_config.example.json
- 03_rh_chain_bot_v4.py
- 50_TESTS.py
- 50_EARLY_DISCOVERY_TESTS.py
- 51_TELEGRAM_NOISE_TESTS.py
- 50_CHANGELOG.md
- 07_TELEGRAM_STRUCTURE.md
- README_V5.11.1.txt
- UPLOAD_AND_TELEGRAM_GUIDE.md
- .github/workflows/SCANNER_WORKFLOW.yml

Do NOT upload:
- config.json
- __pycache__/
- old duplicate workflow copies outside .github/workflows/
- any API key/token/private key

## Public runner repository
No new secret names are required. Keep:
- PRIVATE_REPO_TOKEN
- TELEGRAM_BOT_TOKEN
- TELEGRAM_CHAT_ID
- BLOCKSCOUT_API_KEY

The existing public runner workflow calls the private bot with `--once`. In V5.11.1, `--once` also processes a bounded batch of Telegram updates after the scan. This makes Telegram usable without a permanent server; command response latency is normally up to one scheduled runner interval.

## Telegram commands
/start or /menu
/status
/help
/scan
/discover
/opportunities
/risk
/watchlist
/scan_token SYMBOL 0xCONTRACT_ADDRESS
/watch SYMBOL 0xCONTRACT_ADDRESS
/unwatch SYMBOL or ADDRESS
/demo
/positions
/demo_balance AMOUNT
/demo_reset
/today /daily /weekly /monthly
/settings
/set KEY VALUE
/panic /resume
/live

LIVE remains disabled in the example configuration.
