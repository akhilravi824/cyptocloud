"""Cloud daily sync: BTC + ETH Signal -> Alpaca PAPER account (fake money). Educational only.

Runs on GitHub Actions right after each UTC daily close (6 pm Chicago in winter, 7 pm in summer).
Stateless: every run recomputes the signal from Coinbase daily candles (public data) and makes the Alpaca paper
account match it. Running it twice is harmless — if the account already matches, it does nothing.

Signal (same rules as the TradingView "BTC + ETH Signal"):
  OWN a coin when it is higher than 28 days ago AND above its 100-day average, else CASH.
  Bitcoin only (since 2026-11-01). Alpaca lends nothing for crypto, so it holds at most 100% of the account
  (smaller when the coin is very volatile: share x min(1, 100% / yearly volatility)).

Keys: environment variables ALPACA_KEY_ID and ALPACA_SECRET_KEY (GitHub repository secrets). Never printed.
Usage: python -I cloud_sync.py [--dry]
"""
import json, math, os, sys, time, urllib.error, urllib.parse, urllib.request
from datetime import datetime, timedelta, timezone

COINS = {"BTC-USD": 1.00}                            # Bitcoin only since 2026-11-01 (was 70% BTC / 30% ETH)
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


PRICE_CAP = 0.003                                     # pay at most 0.3% above the ask (or get 0.3% below the bid)
DATA_API = "https://data.alpaca.markets/v1beta3/crypto/us/latest/quotes?symbols="


def quote(symbol):
    """Latest bid/ask from Alpaca's crypto data (falls back to Coinbase's ticker)."""
    try:
        req = urllib.request.Request(DATA_API + urllib.parse.quote(symbol), headers=_headers(_AUTH["mode"] or "keys"))
        with urllib.request.urlopen(req, timeout=20) as r:
            q = json.load(r)["quotes"][symbol]
        if q.get("bp") and q.get("ap"): return float(q["bp"]), float(q["ap"])
    except Exception:
        pass
    with urllib.request.urlopen(urllib.request.Request(
            f"https://api.exchange.coinbase.com/products/{symbol.replace('/', '-')}/ticker",
            headers={"User-Agent": "paper-bot"}), timeout=20) as r:
        t = json.load(r)
    return float(t["bid"]), float(t["ask"])


def place_ioc(symbol, side, qty_str=None, dollars=None, client_id=None):
    """Price-capped order that fills right away or is cancelled (limit + IOC), then confirmed.
    Returns a short text: what filled, or why nothing did (the next hourly run tries again)."""
    bid, ask = quote(symbol)
    if side == "buy":
        limit = round(ask * (1 + PRICE_CAP), 2)
        qty_str = f"{math.floor(dollars / limit * 1e8) / 1e8:.8f}"
    else:
        limit = round(bid * (1 - PRICE_CAP), 2)
    payload = {"symbol": symbol, "qty": qty_str, "side": side, "type": "limit",
               "limit_price": f"{limit:.2f}", "time_in_force": "ioc"}
    if client_id:
        payload["client_order_id"] = client_id
    o = alpaca("POST", "/v2/orders", payload)
    for _ in range(15):                                   # confirm: wait up to ~30 s for a final status
        if o.get("status") in ("filled", "canceled", "expired", "rejected"): break
        time.sleep(2); o = alpaca("GET", f"/v2/orders/{o['id']}")
    filled = float(o.get("filled_qty") or 0)
    if o.get("status") not in ("filled", "canceled", "expired", "rejected"):
        alpaca("DELETE", f"/v2/orders/{o['id']}")         # never leave an order hanging
    if filled > 0:
        part = "" if o.get("status") == "filled" else " (partly)"
        return f"filled{part} {filled:.6f} at ${float(o['filled_avg_price']):,.2f}"
    return f"NOT filled ({o.get('status')}; price moved more than {PRICE_CAP * 100:.1f}%), will retry next hour"


# Resting limit orders are reconciled on hourly runs using Alpaca's own order records.
WAIT_SEC = 10800
IMPROVE = .001
TERMINAL = {"filled", "canceled", "expired", "rejected"}
LIMIT_PREFIX = "btcl_"
FALLBACK_PREFIX = "btcf_"
EXIT_PREFIX = "btce_"


def age(order):
    ts = order.get("created_at") or order.get("submitted_at")
    if not ts:
        raise RuntimeError("Unknown order creation time")
    return (datetime.now(UTC) - datetime.fromisoformat(ts.replace("Z", "+00:00"))).total_seconds()


def history():
    return alpaca("GET", "/v2/orders?status=all&limit=500&direction=desc")


def by_id(orders, cid):
    return next((o for o in orders if o.get("client_order_id") == cid), None)


def cancel_owned(order):
    if not str(order.get("client_order_id", "")).startswith(LIMIT_PREFIX):
        raise RuntimeError("Refusing to cancel unrelated order")
    alpaca("DELETE", "/v2/orders/" + order["id"])
    for _ in range(12):
        time.sleep(2)
        result = alpaca("GET", "/v2/orders/" + order["id"])
        if result.get("status") in TERMINAL:
            return result
    raise RuntimeError("Unconfirmed cancellation; no replacement submitted")


def preflight(desired, dry=False):
    for order in alpaca("GET", "/v2/orders?status=open"):
        cid = str(order.get("client_order_id") or "")
        if order.get("symbol", "").replace("/", "") != "BTCUSD" or not cid.startswith(LIMIT_PREFIX):
            raise RuntimeError("Unknown open order on paper account; leaving orders untouched")
        if age(order) >= WAIT_SEC or desired == 0:
            if dry:
                print("DRY RUN: would cancel BTC order " + order["id"])
            else:
                cancel_owned(order)
                print("Canceled old or invalidated BTC order " + order["id"])
        else:
            print("BTC resting order still waiting " + order["id"])


def place(symbol, side, day, fraction, qty_str=None, dollars=None):
    if symbol != "BTC/USD":
        raise RuntimeError("BTC-only bot")
    orders = history()
    if side == "sell" and fraction == 0:
        # A trend exit is urgent; never wait 3h for maker pricing on a CASH signal.
        cid = EXIT_PREFIX + datetime.now(UTC).strftime("%Y%m%d%H")
        old = by_id(orders, cid)
        if old:
            return "CASH IOC already attempted this hour: " + str(old.get("status"))
        return "CASH IOC: " + place_ioc(symbol, side, qty_str=qty_str, client_id=cid)
    cid = LIMIT_PREFIX + day.strftime("%Y%m%d") + "_" + side
    active = [o for o in orders if o.get("symbol", "").replace("/", "") == "BTCUSD"
              and o.get("status") not in TERMINAL]
    if active:
        if len(active) == 1 and active[0].get("client_order_id") == cid:
            return "waiting on BTC limit " + active[0]["id"]
        raise RuntimeError("Another BTC order is pending; no duplicate order")
    old = by_id(orders, cid)
    if old is None:
        bid, ask = quote(symbol)
        if not 0 < bid <= ask:
            raise RuntimeError("Invalid BTC quote")
        mid = (bid + ask) / 2
        limit = (math.floor(min(bid, mid * (1 - IMPROVE)) * 100) / 100 if side == "buy"
                 else math.ceil(max(ask, mid * (1 + IMPROVE)) * 100) / 100)
        if limit <= 0:
            raise RuntimeError("Invalid limit price")
        qty = math.floor((dollars / limit * .995 if side == "buy" else float(qty_str)) * 1e8) / 1e8
        if qty * limit < 10:
            return "Order not placed: below 10 USD"
        result = alpaca("POST", "/v2/orders", {
            "symbol": symbol, "side": side, "type": "limit", "qty": f"{qty:.8f}",
            "limit_price": f"{limit:.2f}", "time_in_force": "gtc", "client_order_id": cid})
        return f'GTC resting {side} {qty:.8f} BTC at USD {limit:.2f} ({result.get("status")})'
    if old.get("status") not in TERMINAL:
        raise RuntimeError("Limit cancellation/fill status unconfirmed")
    if old.get("status") == "filled":
        return "Limit filled: " + str(old.get("filled_qty"))
    if age(old) < WAIT_SEC:
        return "Limit stopped early; wait until 3h before fallback"
    # Partial fills must be reconciled to the actual portfolio before fallback.
    fallback_id = (FALLBACK_PREFIX + day.strftime("%Y%m%d") + "_" + side + "_" +
                   datetime.now(UTC).strftime("%Y%m%d%H"))
    if by_id(orders, fallback_id):
        return "Fallback already attempted this hour"
    account = alpaca("GET", "/v2/account")
    positions = alpaca("GET", "/v2/positions")
    p = next((z for z in positions if z.get("symbol", "").replace("/", "") == "BTCUSD"), None)
    val = float(p["market_value"]) if p else 0.
    equity = float(account["equity"])
    remaining = fraction * equity - val
    if side == "buy":
        amt = min(max(0., remaining), float(account["cash"]) * .98)
        if amt < max(10., MIN_CHANGE * equity):
            return "No remaining buy needed"
        return "3h fallback buy IOC: " + place_ioc(symbol, side, dollars=amt, client_id=fallback_id)
    if not p or -remaining < max(10., MIN_CHANGE * equity):
        return "No remaining sell needed"
    bid, _ = quote(symbol)
    qty = math.floor(min(float(p["qty"]), -remaining / bid) * 1e8) / 1e8
    if qty * bid < 10:
        return "Residual sell below 10 USD"
    return "3h fallback sell IOC: " + place_ioc(symbol, side, qty_str=f"{qty:.8f}", client_id=fallback_id)



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
    plan, sig_rows = [], []
    for c, share in COINS.items():
        days, closes = daily_closes(c)
        on, size, m, dma = signal(closes)
        frac = share * size
        key = SYMBOL[c].replace("/", "")
        val = float(pos[key]["market_value"]) if key in pos else 0.0
        qty = float(pos[key]["qty"]) if key in pos else 0.0
        plan.append((c, frac, val, qty, frac * eq - val, closes[-1]))
        sig_rows.append((c, days[-1], closes[-1], m, dma, on, frac))
        lines.append(f"| {c[:3]} | {days[-1]} ${closes[-1]:,.0f} | {m * 100:+.1f}% | {dma * 100:+.1f}% | "
                     f"{'OWN' if on else 'CASH'} | {frac * 100:.0f}% | ${val:,.2f} |")
    expected = {SYMBOL[c].replace("/", "") for c in COINS}
    unexpected = [p["symbol"] for p in pos.values() if p["symbol"].replace("/", "") not in expected]
    if unexpected:
        raise RuntimeError("Dedicated BTC paper account required; unexpected holdings: " + str(unexpected))
    preflight(plan[0][1], dry)
    # An aged limit might have partly filled. Refresh balances before sizing.
    acct = alpaca("GET", "/v2/account")
    eq = float(acct["equity"])
    pos = {p["symbol"].replace("/", ""): p for p in alpaca("GET", "/v2/positions")}
    plan = [(c, frac, float(pos[key]["market_value"]) if key in pos else 0.0,
             float(pos[key]["qty"]) if key in pos else 0.0,
             frac * eq - (float(pos[key]["market_value"]) if key in pos else 0.0), px)
            for c, frac, val, qty, diff, px in plan
            for key in [SYMBOL[c].replace("/", "")]]
    actions = []
    for c, frac, val, qty, diff, px in plan:                               # sells first
        if qty > 0 and frac == 0:
            actions.append(f"SELL all {SYMBOL[c]} (${val:,.2f})" + ("" if dry else ": " +
                           place(SYMBOL[c], "sell", sig_rows[0][1], frac, qty_str=pos[SYMBOL[c].replace('/', '')]["qty"])))
        elif qty > 0 and diff < -max(MIN_CHANGE * eq, 10):
            q = math.floor(min(qty, -diff / px) * 1e8) / 1e8          # round down: never sell more than we hold
            actions.append(f"SELL {q:.6f} {SYMBOL[c]} (${-diff:,.2f})" + ("" if dry else ": " +
                           place(SYMBOL[c], "sell", sig_rows[0][1], frac, qty_str=f"{q:.8f}")))
    for c, frac, val, qty, diff, px in plan:                               # then buys
        if frac > 0 and diff > max(MIN_CHANGE * eq, 10):
            cash = float(alpaca("GET", "/v2/account")["cash"])
            amt = min(diff, cash * 0.98)                                  # leave room for the price cap and fee
            if amt < 10: continue
            actions.append(f"BUY {SYMBOL[c]} for ${amt:,.2f}" + ("" if dry else ": " +
                           place(SYMBOL[c], "buy", sig_rows[0][1], frac, dollars=amt)))
    lines += ["", "Orders: " + ("; ".join(actions) if actions else "none (account already matches the signal)")]
    text = "\n".join(lines)
    print(text)
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a") as f:
            f.write(text + "\n")
    if actions and not dry: time.sleep(3)                                  # let the account update before reporting
    report(sig_rows, actions, dry)


# ---------------- daily / weekly portfolio report (emailed by GitHub) ----------------
START = 1000.00                                        # the paper account started with $1,000
OWNER = "akhilravi824"                                 # @mentioned so GitHub emails every report
DAILY_TITLE = "Daily portfolio report"
NAMES = {"BTC-USD": "Bitcoin", "ETH-USD": "Ethereum"}


def money(x, sign=False):
    return (f"{'+' if x >= 0 else '-'}" if sign else ("-" if x < 0 else "")) + f"${abs(x):,.2f}"


def value_days_ago(days):
    """Account value at the end of the day `days` ago, from Alpaca's own daily history (None if unknown)."""
    h = alpaca("GET", "/v2/account/portfolio/history?period=1M&timeframe=1D")
    cutoff = time.time() - days * 86400
    pts = [(t, e) for t, e in zip(h.get("timestamp") or [], h.get("equity") or []) if e and t <= cutoff]
    return pts[-1][1] if pts else None


def build_report(sig_rows, actions):
    acct = alpaca("GET", "/v2/account")
    eq, cash = float(acct["equity"]), float(acct["cash"])
    pos = {p["symbol"].replace("/", ""): p for p in alpaca("GET", "/v2/positions")}
    day = sig_rows[0][1]
    d1, d7 = value_days_ago(1), value_days_ago(7)
    total = eq - START
    lines = [f"**Portfolio value: {money(eq)}**", "",
             "| | Change |", "|---|---|",
             f"| Today | {money(eq - d1, True) if d1 else 'n/a'} |",
             f"| Last 7 days | {money(eq - d7, True) + f' ({(eq / d7 - 1) * 100:+.1f}%)' if d7 else 'n/a'} |",
             f"| Since start ($1,000) | {money(total, True)} ({total / START * 100:+.1f}%) |", "",
             "| Holding | Amount | Value | Profit on it |", "|---|---|---|---|"]
    for c in COINS:
        p = pos.get(SYMBOL[c].replace("/", ""))
        if p:
            lines.append(f"| {NAMES[c]} | {float(p['qty']):.6f} | {money(float(p['market_value']))} | "
                         f"{money(float(p['unrealized_pl']), True)} |")
    lines += [f"| Cash | | {money(cash)} | |", "",
              f"Signal at the {day} close (UTC):", "",
              "| Coin | Close | vs 28 days ago | vs 100-day avg | Signal |", "|---|---|---|---|---|"]
    for c, d, px, m, dma, on, frac in sig_rows:
        lines.append(f"| {NAMES[c]} | ${px:,.0f} | {m * 100:+.1f}% | {dma * 100:+.1f}% | "
                     f"{'BUY / OWN ' + format(frac * 100, '.0f') + '%' if on else 'CASH'} |")
    lines += ["", "Orders this run: " + ("; ".join(actions) if actions else "none")]
    return day, eq, "\n".join(lines)


def weekly_report(day, eq):
    d7 = value_days_ago(7)
    since = (datetime.now(UTC) - timedelta(days=7)).strftime("%Y-%m-%dT%H:%M:%SZ")
    orders = alpaca("GET", f"/v2/orders?status=closed&after={since}&limit=100&direction=asc")
    fills = [o for o in orders if o.get("filled_avg_price")]
    total = eq - START
    lines = [f"**Week ending {day}: portfolio {money(eq)}**", "",
             "| | |", "|---|---|",
             f"| Value 7 days ago | {money(d7) if d7 else 'n/a'} |",
             f"| Profit this week | {money(eq - d7, True) + f' ({(eq / d7 - 1) * 100:+.1f}%)' if d7 else 'n/a'} |",
             f"| Profit since start ($1,000) | {money(total, True)} ({total / START * 100:+.1f}%) |", "",
             f"Trades this week: {len(fills)}"]
    for o in fills:
        lines.append(f"- {o['filled_at'][:10]} {o['side'].upper()} {o['symbol']}: "
                     f"{float(o['filled_qty']):.6f} at ${float(o['filled_avg_price']):,.2f}")
    return "\n".join(lines)


def gh(method, path, body=None):
    req = urllib.request.Request("https://api.github.com" + path, method=method,
                                 data=json.dumps(body).encode() if body is not None else None,
                                 headers={"Authorization": "Bearer " + os.environ["GITHUB_TOKEN"],
                                          "Accept": "application/vnd.github+json", "User-Agent": "paper-bot"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)


def important_order_events():
    """Email new resting limits, confirmed fills, and rejections, never hourly wait states."""
    cutoff = datetime.now(UTC) - timedelta(days=2)
    events = []
    for order in history():
        cid = str(order.get("client_order_id") or "")
        if not cid.startswith((LIMIT_PREFIX, FALLBACK_PREFIX, EXIT_PREFIX)):
            continue
        if order.get("symbol", "").replace("/", "") != "BTCUSD":
            continue
        stamp = order.get("created_at") or order.get("submitted_at")
        if not stamp or datetime.fromisoformat(stamp.replace("Z", "+00:00")) < cutoff:
            continue
        status = str(order.get("status") or "")
        filled = float(order.get("filled_qty") or 0)
        order_id = order["id"]
        if status in TERMINAL and filled > 0:
            kind = "filled"
            desc = (f"BTC {order['side'].upper()} filled: {filled:.8f} BTC at "
                    f"USD {float(order['filled_avg_price']):,.2f} ({status})")
        elif status == "rejected":
            kind = "rejected"
            desc = f"BTC {order['side'].upper()} rejected; check Alpaca order {order_id}"
        elif cid.startswith(LIMIT_PREFIX) and status not in TERMINAL:
            kind = "placed"
            desc = f"BTC {order['side'].upper()} resting limit submitted; awaiting fill for up to 3 hours"
        else:
            continue
        events.append((f"<!-- btc-order:{order_id}:{kind} -->", desc))
    return events


def report(sig_rows, actions, dry):
    day, eq, body = build_report(sig_rows, actions)
    weekly = datetime.fromisoformat(str(day)).weekday() == 6
    wbody = weekly_report(day, eq) if weekly else None
    repo = os.environ.get("GITHUB_REPOSITORY")
    if dry or not (repo and os.environ.get("GITHUB_TOKEN")):
        print("\n--- daily report (not posted) ---\n" + body + ("\n\n--- weekly report ---\n" + wbody if wbody else ""))
        return
    mark = f"<!-- close:{day} -->"
    issue = next((i for i in gh("GET", f"/repos/{repo}/issues?state=open&per_page=100")
                  if i["title"] == DAILY_TITLE and "pull_request" not in i), None)
    if not issue:
        issue = gh("POST", f"/repos/{repo}/issues", {"title": DAILY_TITLE, "body":
                   "One report after each daily close, plus important order changes. Hourly checks stay quiet."})
    since = (datetime.now(UTC) - timedelta(days=3)).strftime("%Y-%m-%dT%H:%M:%SZ")
    comments = gh("GET", f"/repos/{repo}/issues/{issue['number']}/comments?since={since}&per_page=100")
    bodies = [c.get("body") or "" for c in comments]
    daily_done = any(mark in content for content in bodies)
    new_events = [(marker, desc) for marker, desc in important_order_events()
                  if not any(marker in content for content in bodies)]
    # Waiting messages stay in the Actions log. Order events are idempotent across runs.
    if not daily_done or new_events:
        event_text = ("\n\n**Order changes**\n" +
                      "\n".join(f"{marker}\n- {desc}" for marker, desc in new_events)) if new_events else ""
        if not daily_done:
            message = f"{mark}\n@{OWNER} Daily paper portfolio, {day} close\n\n{body}{event_text}"
        else:
            message = f"{mark}\n@{OWNER} BTC paper order update\n{event_text}"
        gh("POST", f"/repos/{repo}/issues/{issue['number']}/comments", {"body": message})
        print("Posted daily report or new order event.")
    else:
        print("Hourly check complete: no email (daily report and events already sent).")
    if wbody and not daily_done:
        gh("POST", f"/repos/{repo}/issues", {"title": f"Weekly portfolio report: week ending {day}",
                                             "body": f"@{OWNER}\n\n{wbody}"})
        print("Weekly report posted.")


if __name__ == "__main__":
    main()
