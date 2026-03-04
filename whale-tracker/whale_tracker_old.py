#!/usr/bin/env python3
import os, sys, json, time, csv, urllib.request, urllib.parse, urllib.error
from datetime import datetime, timezone, timedelta
from pathlib import Path

MIN_BET_USD = 75_000
POLL_INTERVAL_SECONDS = 60
ROOKIE_MAX_TRADES = 15
ROOKIE_MAX_AGE_DAYS = 30
WHALE_TIER_USD = 150_000

DATA_DIR = Path(__file__).parent / "data"
TRADES_CSV = DATA_DIR / "whale_trades.csv"
STATE_FILE = DATA_DIR / "whale_state.json"

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

import ssl
ssl._create_default_https_context = ssl._create_unverified_context

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
            req = urllib.request.Request(url, headers={"User-Agent": "PolymarketWhaleTracker/1.0", "Accept": "application/json"})
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

def get_wallet_profile(wallet_address):
    data = api_get(f"{GAMMA_API}/public-profile", {"address": wallet_address})
    if data:
        return {"created_at": data.get("createdAt"), "name": data.get("name", ""), "pseudonym": data.get("pseudonym", ""), "bio": data.get("bio", "")}
    return None

def get_wallet_trade_count(wallet_address):
    data = api_get(f"{DATA_API}/activity", {"user": wallet_address, "type": "TRADE", "limit": 200})
    if data and isinstance(data, list):
        return len(data)
    return -1

def analyze_wallet(wallet_address, wallet_cache):
    if wallet_address in wallet_cache:
        return wallet_cache[wallet_address]
    profile = get_wallet_profile(wallet_address)
    trade_count = get_wallet_trade_count(wallet_address)
    account_age_days = None
    created_at_str = None
    if profile and profile.get("created_at"):
        try:
            created_at_str = profile["created_at"]
            ca = created_at_str.replace("Z", "+00:00")
            if "." in ca:
                parts = ca.split(".")
                ca = parts[0] + "+00:00" if "+" not in parts[1] else parts[0] + "+" + parts[1].split("+")[1]
            created_dt = datetime.fromisoformat(ca)
            account_age_days = (datetime.now(timezone.utc) - created_dt).days
        except Exception:
            pass
    is_rookie = trade_count >= 0 and trade_count < ROOKIE_MAX_TRADES
    is_new = account_age_days is not None and account_age_days < ROOKIE_MAX_AGE_DAYS
    display_name = ""
    if profile:
        display_name = profile.get("name") or profile.get("pseudonym") or ""
    result = {"display_name": display_name, "trade_count": trade_count, "account_age_days": account_age_days, "created_at": created_at_str, "is_rookie": is_rookie, "is_new": is_new, "is_insider_signal": is_rookie or is_new}
    wallet_cache[wallet_address] = result
    return result

def fetch_large_trades():
    data = api_get(f"{DATA_API}/trades", {"filterType": "CASH", "filterAmount": MIN_BET_USD, "side": "BUY", "limit": 100})
    if data and isinstance(data, list):
        return data
    return []

def init_csv():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    if not TRADES_CSV.exists():
        with open(TRADES_CSV, "w", newline="") as f:
            writer = csv.writer(f)
            writer.writerow(["detected_at", "trade_timestamp", "wallet", "display_name", "side", "outcome", "title", "event_slug", "size_tokens", "price", "usdc_value", "trade_count", "account_age_days", "is_rookie", "is_new", "is_insider_signal", "polymarket_url", "tx_hash"])

def log_trade(trade, wallet_info):
    usdc_value = trade.get("size", 0) * trade.get("price", 0)
    slug = trade.get("eventSlug", "")
    pm_url = "https://polymarket.com/event/" + slug
    with open(TRADES_CSV, "a", newline="") as f:
        writer = csv.writer(f)
        writer.writerow([datetime.now(timezone.utc).isoformat(), trade.get("timestamp", ""), trade.get("proxyWallet", ""), wallet_info.get("display_name", ""), trade.get("side", ""), trade.get("outcome", ""), trade.get("title", ""), trade.get("eventSlug", ""), trade.get("size", ""), trade.get("price", ""), f"{usdc_value:.2f}", wallet_info.get("trade_count", ""), wallet_info.get("account_age_days", ""), wallet_info.get("is_rookie", ""), wallet_info.get("is_new", ""), wallet_info.get("is_insider_signal", ""), pm_url, trade.get("transactionHash", "")])

def format_alert(trade, wallet_info):
    usdc_value = trade.get("size", 0) * trade.get("price", 0)
    price = trade.get("price", 0)
    implied_pct = str(round(price * 100, 1)) + "%" if price else "?"
    if wallet_info["is_insider_signal"] and usdc_value >= WHALE_TIER_USD:
        header = "INSIDER ALERT - MEGA"
    elif wallet_info["is_insider_signal"]:
        header = "INSIDER ALERT"
    elif usdc_value >= WHALE_TIER_USD:
        header = "WHALE TRADE"
    else:
        header = "LARGE TRADE"
    wallet = trade.get("proxyWallet", "")
    name = wallet_info.get("display_name", "")
    wallet_short = wallet[:6] + "..." + wallet[-4:] if len(wallet) > 10 else wallet
    wallet_label = name + " (" + wallet_short + ")" if name else wallet_short
    age = wallet_info.get("account_age_days")
    tc = wallet_info.get("trade_count", -1)
    age_str = str(age) + "d old" if age is not None else "unknown age"
    trades_str = str(tc) + " trades" if tc >= 0 else "unknown trades"
    rookie_flags = []
    if wallet_info.get("is_new"):
        rookie_flags.append("NEW ACCOUNT")
    if wallet_info.get("is_rookie"):
        rookie_flags.append("ROOKIE (" + trades_str + ")")
    flags_str = " | ".join(rookie_flags) if rookie_flags else "Established trader"
    title = trade.get("title", "Unknown market")
    outcome = trade.get("outcome", "?")
    event_slug = trade.get("eventSlug", "")
    pm_url = "https://polymarket.com/event/" + event_slug if event_slug else ""
    profile_url = "https://polymarket.com/profile/" + wallet if wallet else ""
    size_val = trade.get("size", 0)
    lines = []
    lines.append("<b>" + header + "</b>")
    lines.append("")
    lines.append("<b>" + outcome + "</b> on: " + title)
    lines.append("Amount: $" + f"{usdc_value:,.0f}" + " @ " + implied_pct)
    lines.append("Shares: " + f"{size_val:,.0f}")
    lines.append("")
    lines.append("Wallet: " + wallet_label)
    lines.append("Status: " + flags_str)
    lines.append("Account: " + age_str)
    lines.append("")
    link_parts = []
    if pm_url:
        link_parts.append('<a href="' + pm_url + '">Market</a>')
    if profile_url:
        link_parts.append('<a href="' + profile_url + '">Profile</a>')
    if link_parts:
        lines.append(" | ".join(link_parts))
    return "\n".join(lines)

def load_state():
    if STATE_FILE.exists():
        try:
            with open(STATE_FILE) as f:
                return json.load(f)
        except Exception:
            pass
    return {"seen_txs": [], "wallet_cache": {}, "last_timestamp": 0}

def save_state(state):
    state["seen_txs"] = state["seen_txs"][-5000:]
    if len(state["wallet_cache"]) > 2000:
        keys = list(state["wallet_cache"].keys())
        for k in keys[:len(keys) - 2000]:
            del state["wallet_cache"][k]
    with open(STATE_FILE, "w") as f:
        json.dump(state, f)

def main():
    log("=" * 60)
    log("Polymarket Whale & Insider Tracker")
    log("  Min bet: $" + f"{MIN_BET_USD:,}")
    log("  Rookie threshold: <" + str(ROOKIE_MAX_TRADES) + " trades or under " + str(ROOKIE_MAX_AGE_DAYS) + "d old")
    log("  Poll interval: " + str(POLL_INTERVAL_SECONDS) + "s")
    if TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID:
        log("  Telegram: ENABLED")
        send_telegram("<b>Whale Tracker started</b>\nMonitoring trades ≥ $" + f"{MIN_BET_USD:,}" + "\nRookie = under " + str(ROOKIE_MAX_TRADES) + " trades or under " + str(ROOKIE_MAX_AGE_DAYS) + "d old")
    else:
        log("  Telegram: DISABLED (set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID)")
    log("=" * 60)
    init_csv()
    state = load_state()
    seen_txs = set(state.get("seen_txs", []))
    wallet_cache = state.get("wallet_cache", {})
    cycle = 0
    first_run = len(seen_txs) == 0
    while True:
        cycle += 1
        log("")
        log("=== Cycle " + str(cycle) + " ===")
        try:
            trades = fetch_large_trades()
            if trades is None:
                log("  Failed to fetch trades, retrying next cycle")
                time.sleep(POLL_INTERVAL_SECONDS)
                continue
            new_trades = []
            for t in trades:
                tx = t.get("transactionHash", "")
                if tx and tx not in seen_txs:
                    new_trades.append(t)
                    seen_txs.add(tx)
            if first_run:
                log("  First run: marked " + str(len(new_trades)) + " existing trades as seen (no alerts)")
                first_run = False
                state["seen_txs"] = list(seen_txs)
                state["wallet_cache"] = wallet_cache
                save_state(state)
                log("  Sleeping " + str(POLL_INTERVAL_SECONDS) + "s...")
                time.sleep(POLL_INTERVAL_SECONDS)
                continue
            log("  " + str(len(trades)) + " trades fetched, " + str(len(new_trades)) + " new")
            insider_count = 0
            whale_count = 0
            for trade in new_trades:
                wallet = trade.get("proxyWallet", "")
                if not wallet:
                    continue
                usdc_value = trade.get("size", 0) * trade.get("price", 0)
                wallet_info = analyze_wallet(wallet, wallet_cache)
                log_trade(trade, wallet_info)
                is_insider = wallet_info.get("is_insider_signal", False)
                is_mega = usdc_value >= WHALE_TIER_USD
                if is_insider:
                    insider_count += 1
                if is_mega:
                    whale_count += 1
                name = wallet_info.get("display_name", "")
                wallet_short = wallet[:6] + "..." + wallet[-4:] if len(wallet) > 10 else wallet
                label = name + " (" + wallet_short + ")" if name else wallet_short
                raw_title = trade.get("title", "")
                title_short = raw_title[:50] + "..." if len(raw_title) > 50 else raw_title
                prefix = "!! INSIDER" if is_insider else "WHALE" if is_mega else "$$$"
                log("  " + prefix + " $" + f"{usdc_value:,.0f}" + " " + trade.get("outcome", "") + ' on "' + title_short + '" by ' + label)
                if wallet_info.get("is_new"):
                    log("       NEW Account " + str(wallet_info.get("account_age_days", "?")) + "d old, " + str(wallet_info.get("trade_count", "?")) + " trades")
                elif wallet_info.get("is_rookie"):
                    log("       ROOKIE: only " + str(wallet_info.get("trade_count", "?")) + " trades")
                if is_insider or is_mega:
                    msg = format_alert(trade, wallet_info)
                    sent = send_telegram(msg)
                    if sent:
                        log("       Telegram sent")
                time.sleep(0.5)
            if new_trades:
                log("  Summary: " + str(len(new_trades)) + " new trades, " + str(insider_count) + " insider signals, " + str(whale_count) + " whales")
            state["seen_txs"] = list(seen_txs)
            state["wallet_cache"] = wallet_cache
            save_state(state)
        except KeyboardInterrupt:
            log("Shutting down...")
            state["seen_txs"] = list(seen_txs)
            state["wallet_cache"] = wallet_cache
            save_state(state)
            break
        except Exception as e:
            log("  ERROR: " + str(e))
            import traceback
            traceback.print_exc()
        log("  Sleeping " + str(POLL_INTERVAL_SECONDS) + "s...")
        try:
            time.sleep(POLL_INTERVAL_SECONDS)
        except KeyboardInterrupt:
            log("Shutting down...")
            state["seen_txs"] = list(seen_txs)
            state["wallet_cache"] = wallet_cache
            save_state(state)
            break

if __name__ == "__main__":
    main()
