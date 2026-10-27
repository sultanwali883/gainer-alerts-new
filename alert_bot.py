#!/usr/bin/env python3
"""
Early-gainer ALERT BOT (daily/intraday trading).

Every run it:
  1. Scans Binance USDT coins for an EARLY move: volume surging (3x+ normal) and price
     just starting to climb (+1.5% to +10% in the last hour), while the coin is still
     under +20% on the day (i.e. before it tops the Gainers list).
  2. Sends you a BUY alert with Stop %, Target %, and position Size %.
  3. Tracks every coin it alerted and sends SELL alerts when the stop or target is
     hit, or after 24h (daily trading: close the day flat).

Alerts go to your phone via ntfy.sh (free, no account) and/or Telegram.
ntfy setup: install the "ntfy" app, tap +, subscribe to a long random topic name
(e.g. gainers-k8x2m9qz41), then export NTFY_TOPIC=that-name
Telegram setup (optional, if it works in your country):
  - Telegram -> search @BotFather -> /newbot -> copy the TOKEN
  - Send any message to your new bot, then open
    https://api.telegram.org/bot<TOKEN>/getUpdates and copy "chat":{"id": ...}
  - export TELEGRAM_TOKEN=...   export TELEGRAM_CHAT_ID=...

Run once:        python alert_bot.py
Run forever:     python alert_bot.py --loop        (checks every 5 minutes)
No Binance key needed (public data only). Not financial advice: the backtests showed
no proven edge, so use small amounts and always honor the stop.
"""
import os, sys, json, time, requests
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

BASE = "https://data-api.binance.vision"
STATE = "state.json"
MIN_VOL, COOLDOWN_H, MAX_AGE_H = 3_000_000, 6, 24
SKIP = {"USDC", "FDUSD", "TUSD", "USDP", "DAI", "EUR", "AEUR", "USDE", "XUSD", "BFUSD",
        "USD1", "RLUSD", "PAXG", "XAUT", "UST"}


def get(path, **p):
    r = requests.get(BASE + path, params=p, timeout=20)
    r.raise_for_status()
    return r.json()


def tg(msg):
    print(msg, "\n")
    topic = os.getenv("NTFY_TOPIC")
    if topic:                                   # free push notifications via ntfy.sh
        try:
            requests.post(f"https://ntfy.sh/{topic}", data=msg.encode("utf-8"),
                          headers={"Title": "Crypto signal", "Priority": "high"}, timeout=15)
        except Exception as e:
            print("ntfy error:", e)
    tok, chat = os.getenv("TELEGRAM_TOKEN"), os.getenv("TELEGRAM_CHAT_ID")
    if tok and chat:
        try:
            requests.post(f"https://api.telegram.org/bot{tok}/sendMessage",
                          data={"chat_id": chat, "text": msg}, timeout=15)
        except Exception as e:
            print("Telegram error:", e)


def ema(v, n):
    k, e = 2 / (n + 1), v[0]
    for x in v[1:]:
        e = x * k + e * (1 - k)
    return e


def rsi(c, n=14):
    g = l = 0.0
    for i in range(-n, 0):
        d = c[i] - c[i - 1]
        g += max(d, 0); l += max(-d, 0)
    return 100.0 if l == 0 else 100 - 100 / (1 + g / l)


def analyze(t):
    sym = t["symbol"]
    k = get("/api/v3/klines", symbol=sym, interval="15m", limit=100)[:-1]  # closed candles
    c = [float(x[4]) for x in k]; h = [float(x[2]) for x in k]
    lo = [float(x[3]) for x in k]; v = [float(x[7]) for x in k]
    spike = (sum(v[-4:]) / 4) / ((sum(v[-84:-4]) / 80) or 1)
    chg1h = (c[-1] / c[-5] - 1) * 100
    if spike < 3 or not 1.5 <= chg1h <= 10: return None
    if not (c[-1] > ema(c, 20) and ema(c, 20) > ema(c, 50)) or rsi(c) > 80: return None
    atr = sum(max(h[i] - lo[i], abs(h[i] - c[i - 1]), abs(lo[i] - c[i - 1]))
              for i in range(-14, 0)) / 14
    stop_pct = max(2 * atr / c[-1] * 100, 1.0)      # never tighter than 1%
    if stop_pct > 6: return None                      # too volatile to size safely
    return dict(sym=sym[:-4], price=c[-1], chg24=float(t["priceChangePercent"]),
                chg1h=chg1h, spike=spike, stop_pct=stop_pct)


def scan(state):
    btc = [float(x[4]) for x in get("/api/v3/klines", symbol="BTCUSDT", interval="15m", limit=20)]
    if btc[-1] / btc[-17] - 1 < -0.02:
        print("BTC falling >2% in 4h: skipping new buys."); return
    tk = [t for t in get("/api/v3/ticker/24hr")
          if t["symbol"].endswith("USDT") and t["symbol"][:-4].isascii()
          and t["symbol"][:-4].isalnum() and t["symbol"][:-4] not in SKIP
          and not t["symbol"][:-4].endswith(("UP", "DOWN"))
          and float(t["quoteVolume"]) >= MIN_VOL
          and 0 <= float(t["priceChangePercent"]) <= 20]
    tk = sorted(tk, key=lambda t: -float(t["quoteVolume"]))[:120]

    def safe(t):
        try: return analyze(t)
        except Exception: return None
    with ThreadPoolExecutor(8) as ex:
        hits = sorted((r for r in ex.map(safe, tk) if r), key=lambda r: -r["spike"])
    now = time.time()
    for r in hits[:3]:
        old = state.get(r["sym"])
        if old and now - old["t"] < COOLDOWN_H * 3600: continue
        size = min(20, 100 / r["stop_pct"])
        stop = r["price"] * (1 - r["stop_pct"] / 100)
        target = r["price"] * (1 + 2 * r["stop_pct"] / 100)
        state[r["sym"]] = dict(entry=r["price"], stop=stop, target=target, t=now, open=True)
        tg(f"BUY SIGNAL: {r['sym']}/USDT\n"
           f"Price {r['price']:.6g} | 24h {r['chg24']:+.1f}% | last hour {r['chg1h']:+.1f}%\n"
           f"Volume {r['spike']:.1f}x normal\n"
           f"Stop: -{r['stop_pct']:.1f}% ({stop:.6g})\n"
           f"Target: +{2*r['stop_pct']:.1f}% ({target:.6g})\n"
           f"Size: {size:.0f}% of account (stop-out = ~1% loss)\n"
           f"Not hit within 24h -> I'll tell you to close it.")


def check_open(state):
    now = time.time()
    for sym, p in state.items():
        if not p.get("open"): continue
        try:
            price = float(get("/api/v3/ticker/price", symbol=sym + "USDT")["price"])
        except Exception:
            continue
        pnl = (price / p["entry"] - 1) * 100
        why = ("STOP hit" if price <= p["stop"] else "TARGET hit" if price >= p["target"]
               else "24h passed, close the trade" if now - p["t"] > MAX_AGE_H * 3600 else None)
        if why:
            p["open"] = False
            tg(f"SELL {sym}/USDT: {why}\nEntry {p['entry']:.6g} -> now {price:.6g} ({pnl:+.1f}%)")


def run():
    state = json.load(open(STATE)) if os.path.exists(STATE) else {}
    try:
        check_open(state)
        scan(state)
    except Exception as e:
        print("Run error:", e)
    json.dump(state, open(STATE, "w"))
    print(datetime.now(timezone.utc).strftime("Checked %Y-%m-%d %H:%M UTC"))


if __name__ == "__main__":
    if "--loop" in sys.argv:
        while True:
            run(); time.sleep(300)
    else:
        run()
