# Telegram Control & Alert Structure — V5.11.0

## 1. Main menu
- 📊 Market Scan — run one bounded market scan and return a compact summary.
- 🎯 Opportunities — current highest-ranked WATCH / BUY CANDIDATE ideas.
- 🔎 Token Check — inspect one contract.
- 🔍 Discovery — show newly discovered candidates by chain.
- 💰 Demo — cash, equity, P/L, daily loss, trades.
- 📦 Positions — each open Demo position with entry/current/P&L/stop.
- 👀 Watchlist — configured, manual, and today's rotating watch.
- 📈 Reports — today / 7 days / 30 days.
- 🛡 Risk — allocation, risk/trade, daily loss, max positions.
- ⚙️ Settings — read/change supported thresholds.
- 🔐 Live — status only while LIVE is disabled.
- 🛑 Panic Stop — immediately blocks Demo entries and Live arming.

## 2. User-facing opportunity message

`🎯 OPPORTUNITY | SYMBOL`

- Score / Verdict / Technical score
- Asset / Chain
- Price / Market Cap / Liquidity
- 1H / 4H
- Setup
- Entry ready
- Entry zone
- Suggested stop
- Risk flags
- Confirmations

No raw JSON in normal alerts.

## 3. Confirmed entry message

`🚨 CONFIRMED ENTRY | SYMBOL`

- Score + confirmations
- Setup + entry zone
- Current price
- Suggested stop / target framework
- Risk flags
- Demo action (entered / blocked + reason)

## 4. Position message

`📦 DEMO POSITION | SYMBOL`

- Entry / current
- Unrealized P/L and %
- Stop
- High since entry
- Partial TP / BE / trailing state

## 5. Exit message

`📉 DEMO EXIT | SYMBOL`

- Reason
- Entry / exit
- P/L / P/L %
- Remaining daily-loss budget

## 6. Data-quality policy

- `DATA_INCOMPLETE` is never an entry signal.
- Missing technical OHLCV never increases confirmations.
- `REJECT` is not promoted to a user-facing opportunity alert.
- Repeated opportunities are deduplicated by cooldown plus meaningful score/setup/entry changes.

## 7. Safety

- LIVE stays disabled.
- No private key is stored or requested.
- Telegram commands are accepted only from configured admin chat IDs.
