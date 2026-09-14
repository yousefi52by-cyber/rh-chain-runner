# Robinhood Chain Bot V5.10.0 — Final Upload Package

## Private bot repository
Upload these files to the root of the private `-rh-chain-alert-bot` repository.
Do NOT upload `config.json`, `state.json`, `__pycache__`, or any API/token values.

The old private GitHub Actions scanner workflow is intentionally NOT included.
The scanner is executed by the public `rh-chain-runner` repository.

## GitHub Secrets used by the runner
- PRIVATE_REPO_TOKEN
- TELEGRAM_BOT_TOKEN
- TELEGRAM_CHAT_ID
- BLOCKSCOUT_API_KEY

Never put secret values in source files.

## Telegram
The bot code includes Telegram alerts, inline menu, Demo status, positions,
watchlist, reports, settings, and panic-stop controls. Telegram access is
restricted to configured admin chat IDs.

The scheduled scanner sends Telegram alerts during each scan. The interactive
polling controller is available with:

    python 03_rh_chain_bot_v4.py --config config.json --state state.json --telegram

For continuous interactive Telegram polling, use a persistent host/service;
GitHub scheduled Actions are not intended to keep a polling process alive.

## Safety
LIVE remains disabled in this package. No private key is accepted or stored.
