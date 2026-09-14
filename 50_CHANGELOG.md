# V5.11.1 — Persistent Settings + Momentum Frontier

- Telegram `/set KEY VALUE` settings persist in `state.json` and survive the public GitHub Actions runner recreation of `config.json`.
- Added a reserved fast-momentum scan frontier for MOO-like moves.
- Added configurable 30–120 minute momentum lane using 1H/5m acceleration, volume and flow.
- Blockscout/other 4xx provider errors are not retried.
- LIVE remains locked; settings interface cannot enable LIVE.
