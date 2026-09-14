# V5.10.2 — Reporting + Early Listing Demo Lane

- Added all-time, Early Listings and opportunity entries to Telegram reports.
- Persisted recent early-listing detections for reporting.
- Added a Demo-only early-listing automatic entry lane, bounded by existing Demo risk controls.
- Added configurable early-listing stop loss and score.
- Live trading remains disabled and locked.

# V5.10.1 — Technical continuity + Telegram runner integration

- Technical analysis now requires a configurable 60 hourly candles by default instead of 200, allowing established-but-younger pools to receive EMA20/EMA50/RSI analysis while still rejecting truly insufficient history.
- Added 4H trend/RSI confirmation derived from the same hourly OHLCV set; no extra Gecko API call is required.
- Fixed repeated Telegram signal alerts: after the cooldown, an active signal is repeated only after a material score change, setup change, or a genuinely new signal.
- Fixed scanner state migration to the current schema version.
- Telegram polling can be performed once per scheduled runner pass, so the public GitHub Actions runner can process commands without a permanent server.

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
