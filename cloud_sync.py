"""Cloud daily sync: BTC + ETH Signal -> Alpaca PAPER account (fake money). Educational only.

Runs on GitHub Actions right after each UTC daily close (6 pm Chicago in winter, 7 pm in summer).
Stateless: every run recomputes the signal from Coinbase daily candles (public data) and makes the Alpaca paper
account match it. Running it twice is harmless — if the account already matches, it does nothing.

Signal (same rules as the TradingView "BTC + ETH Signal"):
  OWN a coin when it is higher than 28 days ago AND above its 100-day average, else CASH.
  Alpaca lends nothing for crypto, so each coin is held at most at its share: Bitcoin 70%, Ethereum 30%
  (smaller when the coin is very volatile: share x min(1, 100% / yearly volatility)).

Keys: environment variables ALPACA_KEY_ID and ALPACA_SECRET_KEY (GitHub repository secrets). Never printed.
Usage: python -I cloud_sync.py [--dry]
"""
import json, math, os, sys, time, urllib.error, urllib.parse, urllib.request
from datetime import datetime, timedelta, timezone

COINS = {"BTC-USD": 0.70, "ETH-USD": 0.30}
LOOK, MA, VOL_TARGET = 28, 100, 1.00
ALPACA = "https://paper-api.alpaca.markets"          # PAPER ONLY — orders can never reach a real-money account
assert ALPACA.startswith("https://paper-api.")
TOKEN_URL = "https://authx.alpaca.markets/v1/oauth2/token"
SYMBOL = {"BTC-USD": "BTC/USD", "ETH-USD": "ETH/USD"}
MIN_CHANGE = 0.05                                     # ignore changes smaller than 5% of the account
UTC = timezone.utc
_AUTH = {"mode": None, "token": None, "exp": 0.0}


# ---------------- prices (Coinbase public data) ----------------
def daily_closes(product, days=220):
    now = datetime.now(UTC)
    rows = {}
    for chunk in range(0, days, 290):
        end = now - timedelta(days=chunk)
        start = end - timedelta(days=min(290, days - chunk))
        url = (f"https://api.exchange.coinbase.com/products/{product}/candles?granularity=86400"
               f"&start={start:%Y-%m-%dT%H:%M:%SZ}&end={end:%Y-%m-%dT%H:%M:%SZ}")
        for attempt in range(6):
            try:
                with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "paper-bot"}), timeout=30) as r:
                    data = json.load(r)
                if isinstance(data, list): break
            except Exception:
                pass
            time.sleep(3 + 3 * attempt)
        else:
            raise RuntimeError("Coinbase did not respond")
        for ts, lo, hi, op, cl, vol in data:
            if datetime.fromtimestamp(ts, UTC) + timedelta(days=1) <= now:      # completed days only
                rows[ts] = float(cl)
    keys = sorted(rows)
    return [datetime.fromtimestamp(k, UTC).date() for k in keys], [rows[k] for k in keys]


def signal(closes):
    i = len(closes) - 1
    m = closes[i] / closes[i - LOOK] - 1
    avg = sum(closes[i - MA + 1:i + 1]) / MA
    rets = [closes[j] / closes[j - 1] - 1 for j in range(i - 29, i + 1)]
    mu = sum(rets) / 30
    vol = (sum((r - mu) ** 2 for r in rets) / 30) ** 0.5 * 365 ** 0.5
    on = m > 0 and closes[i] > avg
    return on, min(1.0, VOL_TARGET / vol) if on else 0.0, m, closes[i] / avg - 1


# ---------------- Alpaca paper ----------------
def _keys():
    return os.environ.get("ALPACA_KEY_ID", "").strip(), os.environ.get("ALPACA_SECRET_KEY", "").strip()


def _headers(mode):
    k, s = _keys()
    h = {"Content-Type": "application/json", "Accept": "application/json"}
    if mode == "oauth":
        if not (_AUTH["token"] and time.time() < _AUTH["exp"] - 60):
            form = urllib.parse.urlencode({"grant_type": "client_credentials", "client_id": k, "client_secret": s}).encode()
            req = urllib.request.Request(TOKEN_URL, data=form, method="POST",
                                         headers={"Content-Type": "application/x-www-form-urlencoded"})
            with urllib.request.urlopen(req, timeout=30) as r:
                d = json.load(r)
            _AUTH.update(token=d["access_token"], exp=time.time() + int(d.get("expires_in", 900)))
        h["Authorization"] = "Bearer " + _AUTH["token"]
    else:
        h.update({"APCA-API-KEY-ID": k, "APCA-API-SECRET-KEY": s})
    return h


def alpaca(method, path, body=None):
    data = json.dumps(body).encode() if body is not None else None
    for mode in ([_AUTH["mode"]] if _AUTH["mode"] else ["keys", "oauth"]):
        for attempt in range(4):
            try:
                req = urllib.request.Request(ALPACA + path, data=data, method=method, headers=_headers(mode))
                with urllib.request.urlopen(req, timeout=30) as r:
                    txt = r.read().decode(); _AUTH["mode"] = mode
                    return json.loads(txt) if txt else {}
            except urllib.error.HTTPError as e:
                if e.code in (429, 500, 502, 503, 504):
                    time.sleep(3 + 3 * attempt); continue
                if e.code in (400, 401, 403) and mode == "keys" and not _AUTH["mode"]:
                    break                                   # try the other kind of credentials
                raise RuntimeError(f"Alpaca error {e.code}: {e.read()[:200].decode(errors='replace')}")
            except urllib.error.URLError:
                time.sleep(3 + 3 * attempt)
    raise RuntimeError("Alpaca rejected the keys or did not respond (use PAPER keys)")


def main():
    dry = "--dry" in sys.argv
    if not all(_keys()):
        sys.exit("Missing ALPACA_KEY_ID / ALPACA_SECRET_KEY")
    acct = alpaca("GET", "/v2/account")
    if acct.get("crypto_status") not in (None, "ACTIVE"):
        sys.exit(f"Crypto trading is not active on this paper account ({acct.get('crypto_status')})")
    eq = float(acct["equity"])
    pos = {p["symbol"].replace("/", ""): p for p in alpaca("GET", "/v2/positions")}
    lines = [f"## Paper sync {datetime.now(UTC):%Y-%m-%d %H:%M} UTC {'(DRY RUN)' if dry else ''}",
             f"Alpaca paper account: ${eq:,.2f}", "",
             "| Coin | Last close | 28-day change | vs 100-day avg | Signal | Target | Holding now |",
             "|---|---|---|---|---|---|---|"]
    plan = []
    for c, share in COINS.items():
        days, closes = daily_closes(c)
        on, size, m, dma = signal(closes)
        frac = share * size
        key = SYMBOL[c].replace("/", "")
        val = float(pos[key]["market_value"]) if key in pos else 0.0
        qty = float(pos[key]["qty"]) if key in pos else 0.0
        plan.append((c, frac, val, qty, frac * eq - val, closes[-1]))
        lines.append(f"| {c[:3]} | {days[-1]} ${closes[-1]:,.0f} | {m * 100:+.1f}% | {dma * 100:+.1f}% | "
                     f"{'OWN' if on else 'CASH'} | {frac * 100:.0f}% | ${val:,.2f} |")
    actions = []
    for c, frac, val, qty, diff, px in plan:                               # sells first
        if qty > 0 and frac == 0:
            actions.append(f"SELL all {SYMBOL[c]} (${val:,.2f})")
            if not dry: alpaca("DELETE", f"/v2/positions/{SYMBOL[c].replace('/', '')}")
        elif qty > 0 and diff < -max(MIN_CHANGE * eq, 10):
            q = math.floor(min(qty, -diff / px) * 1e8) / 1e8          # round down: never sell more than we hold
            actions.append(f"SELL {q:.6f} {SYMBOL[c]} (${-diff:,.2f})")
            if not dry: alpaca("POST", "/v2/orders", {"symbol": SYMBOL[c], "qty": f"{q:.8f}", "side": "sell",
                                                      "type": "market", "time_in_force": "gtc"})
    if actions and not dry: time.sleep(5)
    for c, frac, val, qty, diff, px in plan:                               # then buys
        if frac > 0 and diff > max(MIN_CHANGE * eq, 10):
            cash = float(alpaca("GET", "/v2/account")["cash"])
            amt = min(diff, cash * 0.99)
            if amt < 10: continue
            actions.append(f"BUY {SYMBOL[c]} for ${amt:,.2f}")
            if not dry: alpaca("POST", "/v2/orders", {"symbol": SYMBOL[c], "notional": f"{amt:.2f}", "side": "buy",
                                                      "type": "market", "time_in_force": "gtc"})
    lines += ["", "Orders: " + ("; ".join(actions) if actions else "none (account already matches the signal)")]
    text = "\n".join(lines)
    print(text)
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a") as f:
            f.write(text + "\n")


if __name__ == "__main__":
    main()
