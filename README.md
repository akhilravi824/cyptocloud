# Crypto paper bot (cloud)

Every hour GitHub Actions runs `cloud_sync.py` (only the first run after each UTC daily close can trade), which:

1. downloads Bitcoin daily prices from Coinbase (public data),
2. applies the "BTC + ETH Signal" rules (own a coin when it is above its price 28 days ago **and** above its 100-day average),
3. makes the **Alpaca paper** (fake money) account match: Bitcoin (up to 100%) when the signal is on, otherwise cash. (Bitcoin only since 2026-11-01; it was 70% BTC / 30% ETH.)

It can only reach Alpaca's paper-trading server. Keys live in the repository secrets `ALPACA_KEY_ID` and
`ALPACA_SECRET_KEY` and are never printed. Each run's result appears in the **Actions** tab (run summary).

This repo is public: anyone can read the code and the run reports, but the keys stay secret. GitHub switches off
schedules in public repos after 60 days without commits, so `keepalive.yml` adds one empty commit a month.

Educational only, not financial advice.
