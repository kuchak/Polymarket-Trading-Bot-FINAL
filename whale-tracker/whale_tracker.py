#!/usr/bin/env python3
"""Polymarket Whale & Insider Tracker v2"""
import os, sys, json, time, csv, ssl, urllib.request, urllib.parse, urllib.error
from datetime import datetime, timezone, timedelta
from pathlib import Path

ssl._create_default_https_context = ssl._create_unverified_context

POLL_MIN_USD = 20_000
ALERT_TRADE_USD = 75_000
ALERT_POSITION_USD = 100_000
POLL_INTERVAL_SECONDS = 60
ROOKIE_MAX_TRADES = 15
ROOKIE_MAX_AGE_DAYS = 30
DATA_DIR = Path(__file__).parent / "data"
TRADES_CSV = DATA_DIR / "whale_trades.csv"
STATE_FILE = DATA_DIR / "whale_state.json"
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")
DATA_API = "https://data-api.polymarket.com"
GAMMA_API = "https://gamma-api.polymarket.com"

def log(msg):
    ts = datetime.now().strftime("%H:%M:%S")
    print(f"[{ts}] {msg}", flush=True)

def api_get(url, params=None, retries=2):
    if params:
        qs = urllib.parse.urlencode(params, doseq=True)
        url = f"{url}?{qs}"
    for attempt in range(retries + 1):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "PolymarketWhaleTracker/2.0", "Accept": "application/json"})
            with urllib.request.urlopen(req, timeout=30) as resp:
                return json.loads(resp.read().decode())
        except Exception as e:
            if attempt < retries:
                time.sleep(2 ** attempt)
            else:
                log(f"  API error {url}: {e}")
                return None

def send_telegram(message):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return False
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        data = json.dumps({"chat_id": TELEGRAM_CHAT_ID, "text": message, "parse_mode": "HTML", "disable_web_page_preview": True}).encode()
        req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            result = json.loads(resp.read().decode())
            return result.get("ok", False)
    except Exception as e:
        log(f"  Telegram error: {e}")
        return False

def get_wallet_profile(addr):
    data = api_get(f"{GAMMA_API}/public-profile", {"address": addr})
    if data:
        return {"created_at": data.get("createdAt"), "name": data.get("name", ""), "pseudonym": data.get("pseudonym", ""), "bio": data.get("bio", "")}
    return None

def get_wallet_trade_count(addr):
    data = api_get(f"{DATA_API}/activity", {"user": addr, "type": "TRADE", "limit": 200})
    if data and isinstance(data, list):
        return len(data)
    return -1

def get_wallet_position(addr, condition_id):
    data = api_get(f"{DATA_API}/positions", {"user": addr, "market": condition_id, "sizeThreshold": 0})
    if data and isinstance(data, list):
        total = 0.0
        for pos in data:
            cv = float(pos.get("currentValue", 0) or 0)
            iv = float(pos.get("initialValue", 0) or 0)
            total += max(cv, iv)
        return total
    return 0.0

def analyze_wallet(addr, cache):
    if addr in cache:
        return cache[addr]
    profile = get_wallet_profile(addr)
    tc = get_wallet_trade_count(addr)
    age_days = None
    ca_str = None
    if profile and profile.get("created_at"):
        try:
            ca_str = profile["created_at"]
            ca = ca_str.replace("Z", "+00:00")
            if "." in ca:
                parts = ca.split(".")
                ca = parts[0] + "+00:00" if "+" not in parts[1] else parts[0] + "+" + parts[1].split("+")[1]
            age_days = (datetime.now(timezone.utc) - datetime.fromisoformat(ca)).days
        except Exception:
            pass
    is_rookie = tc >= 0 and tc < ROOKIE_MAX_TRADES
    is_new = age_days is not None and age_days < ROOKIE_MAX_AGE_DAYS
    dn = ""
    if profile:
        dn = profile.get("name") or profile.get("pseudonym") or ""
    result = {"display_name": dn, "trade_count": tc, "account_age_days": age_days, "created_at": ca_str, "is_rookie": is_rookie, "is_new": is_new, "is_insider_signal": is_rookie or is_new}
    cache[addr] = result
    return result

def fetch_large_trades():
    data = api_get(f"{DATA_API}/trades", {"filterType": "CASH", "filterAmount": POLL_MIN_USD, "side": "BUY", "limit": 100})
    if data and isinstance(data, list):
        return data
    return []

def init_csv():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    if not TRADES_CSV.exists():
        with open(TRADES_CSV, "w", newline="") as f:
            csv.writer(f).writerow(["detected_at","trade_timestamp","wallet","display_name","side","outcome","title","event_slug","size_tokens","price","usdc_value","position_value","trade_count","account_age_days","is_rookie","is_new","is_insider_signal","polymarket_url","tx_hash"])

def log_trade(trade, wi, pv):
    uv = trade.get("size", 0) * trade.get("price", 0)
    with open(TRADES_CSV, "a", newline="") as f:
        csv.writer(f).writerow([datetime.now(timezone.utc).isoformat(), trade.get("timestamp",""), trade.get("proxyWallet",""), wi.get("display_name",""), trade.get("side",""), trade.get("outcome",""), trade.get("title",""), trade.get("eventSlug",""), trade.get("size",""), trade.get("price",""), f"{uv:.2f}", f"{pv:.2f}", wi.get("trade_count",""), wi.get("account_age_days",""), wi.get("is_rookie",""), wi.get("is_new",""), wi.get("is_insider_signal",""), "https://polymarket.com/event/" + trade.get("eventSlug",""), trade.get("transactionHash","")])

def format_alert(trade, wi, pv, reasons):
    uv = trade.get("size", 0) * trade.get("price", 0)
    price = trade.get("price", 0)
    pct = str(round(price * 100, 1)) + "%" if price else "?"
    bt = "big_trade" in reasons
    bp = "big_position" in reasons
    ins = wi.get("is_insider_signal", False)
    if bt and bp and ins: h = "\U0001f6a8\U0001f6a8 INSIDER WHALE"
    elif bt and bp: h = "\U0001f40b WHALE + BIG POSITION"
    elif bp and ins: h = "\U0001f6a8 INSIDER BIG POSITION"
    elif bt and ins: h = "\U0001f6a8 INSIDER WHALE"
    elif bt: h = "\U0001f40b WHALE TRADE"
    elif bp: h = "\U0001f4b0 BIG POSITION"
    else: h = "\U0001f4b0 LARGE TRADE"
    w = trade.get("proxyWallet", "")
    nm = wi.get("display_name", "")
    ws = w[:6] + "..." + w[-4:] if len(w) > 10 else w
    wl = nm + " (" + ws + ")" if nm else ws
    age = wi.get("account_age_days")
    tc = wi.get("trade_count", -1)
    age_s = str(age) + "d old" if age is not None else "unknown age"
    tc_s = str(tc) + " trades" if tc >= 0 else "unknown trades"
    flags = []
    if wi.get("is_new"): flags.append("\U0001f195 NEW ACCOUNT")
    if wi.get("is_rookie"): flags.append("\U0001f476 ROOKIE (" + tc_s + ")")
    fs = " | ".join(flags) if flags else "Established trader"
    title = trade.get("title", "Unknown market")
    outcome = trade.get("outcome", "?")
    es = trade.get("eventSlug", "")
    pm = "https://polymarket.com/event/" + es if es else ""
    pu = "https://polymarket.com/profile/" + w if w else ""
    sz = trade.get("size", 0)
    lines = ["<b>" + h + "</b>", "", "<b>" + outcome + "</b> on: " + title, "\U0001f4b5 Trade: $" + f"{uv:,.0f}" + " @ " + pct, "\U0001f4ca " + f"{sz:,.0f}" + " shares"]
    if bp or pv >= ALERT_POSITION_USD:
        lines.append("\U0001f4bc Position: $" + f"{pv:,.0f}" + " in this market")
    lines += ["", "\U0001f464 " + wl, "\U0001f4cb " + fs, "\U0001f4c5 Account: " + age_s, ""]
    lp = []
    if pm: lp.append('<a href="' + pm + '">Market</a>')
    if pu: lp.append('<a href="' + pu + '">Profile</a>')
    if lp: lines.append(" | ".join(lp))
    return "\n".join(lines)

def load_state():
    if STATE_FILE.exists():
        try:
            with open(STATE_FILE) as f: return json.load(f)
        except: pass
    return {"seen_txs": [], "wallet_cache": {}, "last_timestamp": 0}

def save_state(state):
    state["seen_txs"] = state["seen_txs"][-5000:]
    wc = state["wallet_cache"]
    if len(wc) > 2000:
        for k in list(wc.keys())[:len(wc)-2000]: del wc[k]
    with open(STATE_FILE, "w") as f: json.dump(state, f)

def main():
    log("=" * 60)
    log("Polymarket Whale & Insider Tracker v2")
    log(f"  Poll >= ${POLL_MIN_USD:,} | Trade alert >= ${ALERT_TRADE_USD:,} | Position alert >= ${ALERT_POSITION_USD:,}")
    log(f"  Rookie: <{ROOKIE_MAX_TRADES} trades or <{ROOKIE_MAX_AGE_DAYS}d | Interval: {POLL_INTERVAL_SECONDS}s")
    if TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID:
        log("  Telegram: ENABLED")
        send_telegram("<b>Whale Tracker v2 started</b>\nTrade alert: >= $" + f"{ALERT_TRADE_USD:,}" + "\nPosition alert: >= $" + f"{ALERT_POSITION_USD:,}" + " in one market\nPolling trades >= $" + f"{POLL_MIN_USD:,}")
    else:
        log("  Telegram: DISABLED")
    log("=" * 60)
    init_csv()
    state = load_state()
    seen = set(state.get("seen_txs", []))
    wc = state.get("wallet_cache", {})
    cycle = 0
    first = len(seen) == 0
    while True:
        cycle += 1
        log(f"\n=== Cycle {cycle} ===")
        try:
            trades = fetch_large_trades()
            if trades is None:
                log("  Failed to fetch, retry next cycle")
                time.sleep(POLL_INTERVAL_SECONDS)
                continue
            new = []
            for t in trades:
                tx = t.get("transactionHash", "")
                if tx and tx not in seen:
                    new.append(t)
                    seen.add(tx)
            if first:
                log(f"  First run: marked {len(new)} existing trades as seen (no alerts)")
                first = False
                state["seen_txs"] = list(seen)
                state["wallet_cache"] = wc
                save_state(state)
                time.sleep(POLL_INTERVAL_SECONDS)
                continue
            log(f"  {len(trades)} fetched, {len(new)} new")
            ac = 0
            for trade in new:
                wallet = trade.get("proxyWallet", "")
                if not wallet: continue
                uv = trade.get("size", 0) * trade.get("price", 0)
                cid = trade.get("conditionId", "")
                wi = analyze_wallet(wallet, wc)
                pv = get_wallet_position(wallet, cid) if cid else 0.0
                log_trade(trade, wi, pv)
                reasons = set()
                if uv >= ALERT_TRADE_USD: reasons.add("big_trade")
                if pv >= ALERT_POSITION_USD: reasons.add("big_position")
                nm = wi.get("display_name", "")
                ws = wallet[:6] + "..." + wallet[-4:] if len(wallet) > 10 else wallet
                label = nm + " (" + ws + ")" if nm else ws
                rt = trade.get("title", "")
                ts = rt[:50] + "..." if len(rt) > 50 else rt
                ps = " | Pos:$" + f"{pv:,.0f}" if pv > 0 else ""
                if reasons:
                    pfx = "\U0001f6a8" if wi.get("is_insider_signal") else "\U0001f40b"
                    log(f'  {pfx} ${uv:,.0f}{ps} {trade.get("outcome","")} on "{ts}" by {label}')
                    msg = format_alert(trade, wi, pv, reasons)
                    if send_telegram(msg): log("       \U0001f4f1 Telegram sent")
                    ac += 1
                    time.sleep(0.5)
                else:
                    log(f'  $$$ ${uv:,.0f}{ps} {trade.get("outcome","")} on "{ts}" by {label} (below threshold)')
            if new: log(f"  Summary: {len(new)} new, {ac} alerts")
            state["seen_txs"] = list(seen)
            state["wallet_cache"] = wc
            save_state(state)
        except KeyboardInterrupt:
            log("Shutting down...")
            state["seen_txs"] = list(seen)
            state["wallet_cache"] = wc
            save_state(state)
            break
        except Exception as e:
            log(f"  ERROR: {e}")
            import traceback; traceback.print_exc()
        log(f"  Sleeping {POLL_INTERVAL_SECONDS}s...")
        try: time.sleep(POLL_INTERVAL_SECONDS)
        except KeyboardInterrupt:
            state["seen_txs"] = list(seen)
            state["wallet_cache"] = wc
            save_state(state)
            break

if __name__ == "__main__":
    main()
