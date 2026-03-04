#!/usr/bin/env python3
"""
Polymarket Copy-Trade Monitor
Watches top leaderboard traders (all markets) and alerts on new trades via Telegram.
"""

import requests
import time
import json
import os
import csv
import argparse
from datetime import datetime, timezone
from pathlib import Path

DATA_DIR = Path(__file__).parent / "data"
DATA_DIR.mkdir(exist_ok=True)
TRADE_LOG = DATA_DIR / "copy_trades.csv"
WALLET_CACHE = DATA_DIR / "tracked_wallets.json"
STATE_FILE = DATA_DIR / "last_seen_trades.json"

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

LEADERBOARD_URL = "https://data-api.polymarket.com/v1/leaderboard"
ACTIVITY_URL = "https://data-api.polymarket.com/v1/activity"

def send_telegram(message):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print(f"[ALERT] {message}")
        return
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        resp = requests.post(url, json={"chat_id": TELEGRAM_CHAT_ID, "text": message, "parse_mode": "HTML", "disable_web_page_preview": True}, timeout=10)
        if resp.status_code != 200:
            print(f"[TG ERROR] {resp.status_code}: {resp.text[:200]}")
    except Exception as e:
        print(f"[TG ERROR] {e}")

def fetch_json(url, params=None, retries=3):
    for attempt in range(retries):
        try:
            resp = requests.get(url, params=params, timeout=15)
            if resp.status_code == 200:
                return resp.json()
            elif resp.status_code == 429:
                wait = 2 ** (attempt + 2)
                print(f"[RATE LIMIT] Waiting {wait}s...")
                time.sleep(wait)
            else:
                print(f"[API {resp.status_code}] {url} — {resp.text[:200]}")
                return None
        except Exception as e:
            print(f"[API ERROR] {url} — {e}")
            if attempt < retries - 1:
                time.sleep(2)
    return None

def build_watchlist(top_n=20, min_profit=0):
    print(f"\n📊 Fetching leaderboard (top {top_n}, min profit ${min_profit:,.0f})...")
    lb = fetch_json(LEADERBOARD_URL, params={"limit": max(top_n * 2, 100)})
    if not lb:
        print("[ERROR] Could not fetch leaderboard")
        return []
    watchlist = []
    for entry in lb:
        address = entry.get("proxyWallet", "")
        username = entry.get("userName", "") or address[:10]
        pnl = float(entry.get("pnl", 0))
        vol = float(entry.get("vol", 0))
        rank = int(entry.get("rank", 0))
        if not address or pnl < min_profit:
            continue
        watchlist.append({"address": address, "username": username, "pnl": pnl, "volume": vol, "rank": rank})
        if len(watchlist) >= top_n:
            break
    with open(WALLET_CACHE, "w") as f:
        json.dump(watchlist, f, indent=2)
    print(f"✅ Tracking {len(watchlist)} traders:")
    for w in watchlist[:10]:
        print(f"   #{w['rank']:<3} {w['username'][:20]:<20} PnL: ${w['pnl']:>12,.2f}  Vol: ${w['volume']:>12,.0f}")
    if len(watchlist) > 10:
        print(f"   ... and {len(watchlist) - 10} more")
    return watchlist

def load_state():
    if STATE_FILE.exists():
        with open(STATE_FILE) as f:
            return json.load(f)
    return {}

def save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f)

def detect_new_trades(wallet, state):
    address = wallet["address"]
    activity = fetch_json(ACTIVITY_URL, params={"user": address, "limit": 20})
    if not activity or not isinstance(activity, list):
        return []
    seen = set(state.get(address, []))
    new_trades = []
    current_ids = []
    for trade in activity:
        if trade.get("type") != "TRADE":
            continue
        trade_id = f"{trade.get('transactionHash', '')}-{trade.get('conditionId', '')}"
        current_ids.append(trade_id)
        if trade_id not in seen:
            new_trades.append(trade)
    state[address] = current_ids[:100]
    return new_trades

def format_trade_alert(trade, wallet):
    username = wallet.get("username", "Unknown")
    rank = wallet.get("rank", "?")
    pnl = wallet.get("pnl", 0)
    title = trade.get("title", "Unknown Market")
    outcome = trade.get("outcome", "?")
    side = trade.get("side", "?")
    price = trade.get("price", 0)
    size = trade.get("size", 0)
    usdc_size = trade.get("usdcSize", 0)
    ts = trade.get("timestamp", 0)
    event_slug = trade.get("eventSlug", "")
    try:
        time_str = datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%H:%M:%S UTC")
    except:
        time_str = str(ts)
    side_emoji = "🟢" if side == "BUY" else "🔴"
    return (
        f"🔔 <b>COPY TRADE ALERT</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━━\n"
        f"👤 <b>#{rank} {username}</b> (PnL: ${pnl:,.0f})\n"
        f"{side_emoji} {side} <b>{outcome}</b> @ {price:.2f}\n"
        f"💰 {size:,.1f} shares (${usdc_size:,.2f})\n"
        f"🏷️ {title}\n"
        f"⏰ {time_str}\n"
        f"━━━━━━━━━━━━━━━━━━━━━\n"
        f"🔗 https://polymarket.com/event/{event_slug}"
    )

def log_trade_csv(trade, wallet):
    is_new = not TRADE_LOG.exists()
    with open(TRADE_LOG, "a", newline="") as f:
        writer = csv.writer(f)
        if is_new:
            writer.writerow(["detected_at","rank","username","wallet","trader_pnl","title","outcome","side","price","size","usdc_size","timestamp","tx_hash","event_slug","condition_id"])
        writer.writerow([
            datetime.now(timezone.utc).isoformat(), wallet.get("rank",""), wallet.get("username",""),
            wallet.get("address",""), wallet.get("pnl",""), trade.get("title",""),
            trade.get("outcome",""), trade.get("side",""), trade.get("price",""),
            trade.get("size",""), trade.get("usdcSize",""), trade.get("timestamp",""),
            trade.get("transactionHash",""), trade.get("eventSlug",""), trade.get("conditionId",""),
        ])

def run_monitor(top_n=20, interval=45, min_profit=0, refresh_lb_mins=60):
    print("=" * 60)
    print("  POLYMARKET COPY-TRADE MONITOR")
    print("=" * 60)
    print(f"  Tracking: Top {top_n} traders (min profit: ${min_profit:,.0f})")
    print(f"  Poll interval: {interval}s")
    print(f"  Leaderboard refresh: every {refresh_lb_mins} min")
    print(f"  Telegram: {'✅ configured' if TELEGRAM_BOT_TOKEN else '❌ not set'}")
    print(f"  Trade log: {TRADE_LOG}")
    print("=" * 60)
    watchlist = build_watchlist(top_n=top_n, min_profit=min_profit)
    if not watchlist:
        print("[FATAL] No traders to watch. Check API connectivity.")
        return
    state = load_state()
    last_lb_refresh = time.time()
    print("\n🔄 Seeding initial trade state (no alerts)...")
    for wallet in watchlist:
        detect_new_trades(wallet, state)
        time.sleep(0.3)
    save_state(state)
    print(f"✅ State seeded for {len(watchlist)} wallets. Monitoring for new trades...\n")
    if TELEGRAM_BOT_TOKEN:
        names = ", ".join(w["username"][:15] for w in watchlist[:5])
        send_telegram(f"🟢 <b>Copy-Trade Monitor Started</b>\nTracking top {len(watchlist)} traders\nTop 5: {names}{'...' if len(watchlist) > 5 else ''}\nPolling every {interval}s")
    cycle = 0
    total_alerts = 0
    while True:
        try:
            cycle += 1
            cycle_start = time.time()
            new_count = 0
            if time.time() - last_lb_refresh > refresh_lb_mins * 60:
                print(f"\n🔄 Refreshing leaderboard...")
                new_watchlist = build_watchlist(top_n=top_n, min_profit=min_profit)
                if new_watchlist:
                    old_addrs = {w["address"] for w in watchlist}
                    new_addrs = {w["address"] for w in new_watchlist}
                    for w in new_watchlist:
                        if w["address"] in new_addrs - old_addrs:
                            print(f"   🆕 New: #{w['rank']} {w['username']} (${w['pnl']:,.0f})")
                            detect_new_trades(w, state)
                            time.sleep(0.3)
                    left = old_addrs - new_addrs
                    if left:
                        print(f"   📤 Dropped {len(left)} traders")
                    watchlist = new_watchlist
                    save_state(state)
                last_lb_refresh = time.time()
            for wallet in watchlist:
                new_trades = detect_new_trades(wallet, state)
                for trade in new_trades:
                    new_count += 1
                    total_alerts += 1
                    send_telegram(format_trade_alert(trade, wallet))
                    log_trade_csv(trade, wallet)
                    print(f"  🔔 #{wallet['rank']} {wallet['username']}: {trade.get('side','?')} {trade.get('outcome','?')} @{trade.get('price',0):.2f} (${trade.get('usdcSize',0):,.0f}) — {trade.get('title','')[:40]}")
                time.sleep(0.3)
            save_state(state)
            elapsed = time.time() - cycle_start
            ts = datetime.now().strftime("%H:%M:%S")
            if new_count > 0:
                print(f"[{ts}] Cycle {cycle}: {new_count} new trades ({elapsed:.1f}s)")
            elif cycle % 20 == 0:
                print(f"[{ts}] Cycle {cycle}: watching {len(watchlist)} traders, {total_alerts} total alerts ({elapsed:.1f}s)")
            time.sleep(max(1, interval - elapsed))
        except KeyboardInterrupt:
            print(f"\n\n⛔ Stopped. {total_alerts} total alerts across {cycle} cycles.")
            save_state(state)
            break
        except Exception as e:
            print(f"[ERROR] Cycle {cycle}: {e}")
            time.sleep(10)

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Polymarket Copy-Trade Monitor")
    parser.add_argument("--top", type=int, default=20)
    parser.add_argument("--interval", type=int, default=45)
    parser.add_argument("--min-profit", type=float, default=0)
    parser.add_argument("--refresh", type=int, default=60)
    args = parser.parse_args()
    run_monitor(top_n=args.top, interval=args.interval, min_profit=args.min_profit, refresh_lb_mins=args.refresh)
