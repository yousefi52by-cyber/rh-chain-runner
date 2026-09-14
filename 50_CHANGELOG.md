# V5.10.0 — Final Scanner Hardening + Telegram Structure

Rebased from the last fully regression-tested V5.9.7 baseline.

## Scanner hardening
- Blockscout token-universe discovery is now one page per scan. Persisted/stale pagination cursors are ignored; this removes the recurring 422 cursor loop while preserving activity discovery.
- Gecko technical analysis uses the DexScreener-selected pool first. A single 404 fallback to the best Gecko pool is allowed; 429 responses are never immediately retried.
- Gecko request pacing is reduced to a conservative 6/minute and the scan no longer fans out across multiple pools.
- Missing/insufficient Gecko OHLCV remains a safe `DATA_INCOMPLETE` result and never contributes confirmations.
- Global multi-chain discovery, Gold/XAUUSD radar, fair rotation, early-listing reserve, capped consecutive confirmations, risk-based Demo sizing, and LIVE lock are preserved.
- Fixed Telegram `/discover` tuple handling.
- Telegram now has dedicated Opportunities and Risk views.
- Latest scanned prices remain persisted for Demo position display.

## Telegram structure
Main panel:
- Market Scan
- Opportunities
- Token Check
- Discovery
- Demo / Positions
- Watchlist
- Reports
- Risk
- Settings
- Live status
- Panic Stop

Opportunity alert hierarchy:
1. BUY CANDIDATE / CONFIRMED
2. WATCH
3. DATA INCOMPLETE
4. REJECT (normally kept out of the user-facing alert stream)

Telegram alerts are designed to show: asset/chain, score, setup, entry readiness, price, entry zone, stop, risk flags, confirmations, and Demo action without dumping raw JSON.

LIVE remains disabled.

## Package 74 hardening
- Consolidated early new-pool discovery directly into the runtime.
- Consolidated early discovery ranking fallback directly into the runtime.
- Consolidated Telegram opportunity anti-spam logic directly into the runtime.
- Removed runtime patching from the GitHub Actions workflow; CI now tests the actual source that it executes.
- Normalized runtime/UI version labels to V5.10.0.
- Made Telegram opportunity push alerts explicitly opt-in (`telegram_opportunity_alerts=false`).
