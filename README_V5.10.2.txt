Robinhood Chain Global Multi-Asset Trader — V5.10.2

Mode: DEMO / scanner. LIVE is disabled and locked.

Core:
- Global discovery across supported chains plus Robinhood Chain on-chain discovery.
- DexScreener market/liquidity/flow data.
- Blockscout PRO API holder/concentration data for Robinhood Chain.
- Technical analysis from GeckoTerminal hourly OHLCV.
- 1H technical indicators plus 4H trend/RSI confirmation derived from the same hourly data.
- Configurable technical-history minimum (default 60 hourly candles).
- Setup classification: BREAKOUT, RETEST, PULLBACK, TREND_CONTINUATION, EMA_REVERSAL.
- Consecutive confirmations, risk-based Demo sizing, SL/TP, break-even, partial TP, trailing stop and trend-failure exits.
- Telegram control panel and scheduled one-shot Telegram polling for the GitHub Actions runner.

Important:
- Do not upload config.json or any secret values.
- Repository secrets required by the public runner: PRIVATE_REPO_TOKEN, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, BLOCKSCOUT_API_KEY.
- BLOCKSCOUT_API_KEY should be a current Blockscout PRO key.
