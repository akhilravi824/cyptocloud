# Crypto paper bot (cloud)

Every day right after the UTC daily close, GitHub Actions runs `cloud_sync.py`, which:

1. downloads Bitcoin and Ethereum daily prices from Coinbase (public data),
2. applies the "BTC + ETH Signal" rules (own a coin when it is above its price 28 days ago **and** above its 100-day average),
3. makes the **Alpaca paper** (fake money) account match: up to 70% Bitcoin, up to 30% Ethereum, otherwise cash.

It can only reach Alpaca's paper-trading server. Keys live in the repository secrets `ALPACA_KEY_ID` and
`ALPACA_SECRET_KEY` and are never printed. Each run's result appears in the **Actions** tab (run summary).

Educational only, not financial advice.
