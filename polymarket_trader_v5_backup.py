#!/usr/bin/env python3
"""
Polymarket Live Sports Trading Bot (v5 Strategy)
Uses tag_id=100639 on Gamma /events API (same as monitor).
"""

import os, sys, json, time, signal, logging, traceback, requests
from datetime import datetime, timedelta, timezone
from collections import defaultdict
from dotenv import load_dotenv

load_dotenv(os.path.expanduser("~/polymarket/.env"))

INITIAL_BANKROLL = 360.0
POLL_INTERVAL = 45
MIN_BET = 1.0
MIN_LIQUIDITY = 50
GAMMA_API = "https://gamma-api.polymarket.com"
CLOB_API = "https://clob.polymarket.com"
TAG_ID = 100639

STRATEGIES = {
    'TableTennis': {
        'slug_prefixes': {'wttmen-', 'wttwmn-'},
        'entry_threshold': 0.75, 'max_alloc_pct': 0.25,
        'max_per_bet_pct': 0.10, 'min_elapsed_min': 0,
        'est_remaining_min': 60, 'hist_winrate': 1.00,
    },
    'Hockey': {
        'slug_prefixes': {'nhl-'},
        'entry_threshold': 0.95, 'max_alloc_pct': 0.20,
        'max_per_bet_pct': 0.05, 'min_elapsed_min': 0,
        'est_remaining_min': 150, 'hist_winrate': 1.00,
    },
    'Tennis': {
        'slug_prefixes': {'atp-', 'wta-'},
        'entry_threshold': 0.85, 'max_alloc_pct': 0.30,
        'max_per_bet_pct': 0.10, 'min_elapsed_min': 45,
        'est_remaining_min': 60, 'hist_winrate': 0.96,
    },
}

SKIP_OUTCOMES = {'Over','Under','Draw','Tie'}
FINAL_PERIODS = {'VFT','FT','Final','CAN'}

LOG_DIR = os.path.expanduser("~/polymarket/trader_logs")
os.makedirs(LOG_DIR, exist_ok=True)
log_file = os.path.join(LOG_DIR, f"trader_{datetime.now().strftime('%Y%m%d_%H%M%S')}.log")
logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s',
    handlers=[logging.FileHandler(log_file), logging.StreamHandler(sys.stdout)])
logger = logging.getLogger(__name__)

def setup_clob_client():
    from py_clob_client.client import ClobClient
    pk = os.getenv("POLYMARKET_PK")
    funder = os.getenv("POLYMARKET_FUNDER")
    if not pk or not funder:
        logger.error("Missing POLYMARKET_PK or POLYMARKET_FUNDER"); sys.exit(1)
    client = ClobClient(CLOB_API, key=pk, chain_id=137, signature_type=1, funder=funder)
    creds = client.create_or_derive_api_creds()
    client.set_api_creds(creds)
    logger.info(f"CLOB client ready. Funder: {funder[:10]}...")
    return client

def get_live_balance(client):
    """Fetch actual USDC balance from Polymarket."""
    try:
        from py_clob_client.clob_types import BalanceAllowanceParams, AssetType
        bal = client.get_balance_allowance(BalanceAllowanceParams(
            asset_type=AssetType.COLLATERAL, signature_type=1))
        raw = int(bal.get('balance', 0))
        return raw / 1_000_000  # USDC has 6 decimals
    except Exception as e:
        logger.error(f"Balance fetch failed: {e}")
        return 0

def get_strategy(slug):
    for name, cfg in STRATEGIES.items():
        for prefix in cfg['slug_prefixes']:
            if slug.startswith(prefix):
                return name
    return None

def fetch_live_markets():
    """Fetch sports events using tag_id=100639, same as monitor."""
    all_markets = []
    # Fetch today + yesterday (like monitor) to catch live games
    from datetime import date
    et_now = datetime.now(timezone(timedelta(hours=-5)))  # US Eastern
    today = et_now.strftime('%Y-%m-%d')
    yesterday = (et_now - timedelta(days=1)).strftime('%Y-%m-%d')
    date_filters = [f'&event_date={today}', f'&event_date={yesterday}']
    for date_filter in date_filters:
      offset = 0
      while True:
        try:
            url = (f"{GAMMA_API}/events?tag_id={TAG_ID}"
                   f"&active=true&closed=false{date_filter}&limit=200&offset={offset}")
            resp = requests.get(url, timeout=15)
            resp.raise_for_status()
            events = resp.json()
        except Exception as e:
            logger.error(f"Fetch failed at offset={offset}: {e}")
            break
        if not events:
            break
        for event in events:
            for mkt in event.get("markets", []):
                slug = mkt.get("slug", "") or ""
                strat = get_strategy(slug)
                if not strat:
                    continue
                if mkt.get("sportsMarketType", "") != "moneyline":
                    continue
                outcomes_raw = mkt.get("outcomes", "[]")
                prices_raw = mkt.get("outcomePrices", "[]")
                tokens_raw = mkt.get("clobTokenIds", "[]")
                try: outcomes = json.loads(outcomes_raw) if isinstance(outcomes_raw, str) else outcomes_raw
                except: outcomes = []
                try: prices = json.loads(prices_raw) if isinstance(prices_raw, str) else prices_raw
                except: prices = []
                try: token_ids = json.loads(tokens_raw) if isinstance(tokens_raw, str) else tokens_raw
                except: token_ids = []
                if len(outcomes) < 2 or len(prices) < 2 or len(token_ids) < 2:
                    continue
                # Skip if any outcome is in SKIP list
                skip = False
                for o in outcomes:
                    if o in SKIP_OUTCOMES:
                        skip = True
                if skip:
                    continue
                liquidity = float(mkt.get("liquidity", 0) or 0)
                best_ask = float(mkt.get("bestAsk", 0) or 0)
                best_bid = float(mkt.get("bestBid", 0) or 0)
                game_start = mkt.get("gameStartTime", "")
                # Calculate elapsed minutes from gameStartTime
                elapsed_min = 0
                if game_start:
                    try:
                        gst = datetime.fromisoformat(game_start.replace("Z","+00:00").replace(" ", "T").split("+")[0] + "+00:00")
                        now = datetime.now(timezone.utc)
                        mins_since = (now - gst).total_seconds() / 60
                        # ONLY count as live if started 3min to 6hrs ago
                        if 3 <= mins_since <= 360:
                            elapsed_min = mins_since
                    except:
                        pass
                # Each outcome is a separate tradeable position
                for i in range(len(outcomes)):
                    try: prob = float(prices[i])
                    except: continue
                    all_markets.append({
                        'event_name': event.get("title", ""),
                        'slug': slug,
                        'strategy': strat,
                        'outcome': outcomes[i],
                        'implied_prob': prob,
                        'best_ask': best_ask if i == 0 else (1 - best_bid),
                        'liquidity': liquidity,
                        'game_elapsed': elapsed_min,
                        'game_start': game_start,
                        'token_id': token_ids[i] if i < len(token_ids) else "",
                        'neg_risk': mkt.get("negRisk", False),
                        'tick_size': str(mkt.get("orderPriceMinTickSize", "0.01")),
                        'market_id': str(mkt.get("id", "")),
                        'condition_id': mkt.get("conditionId", ""),
                    })
        offset += 200
        if len(events) < 200:
            break
    return all_markets

def calc_ev_per_hour(prob, strat_name, elapsed_min):
    cfg = STRATEGIES[strat_name]
    edge = cfg['hist_winrate'] - prob
    if edge <= 0: return -1
    rem = max(cfg['est_remaining_min'] - max(elapsed_min - cfg['min_elapsed_min'],0)*0.5, 5)
    return edge / (rem/60)

class TradingBot:
    def __init__(self, clob_client, dry_run=False):
        self.client = clob_client
        self.dry_run = dry_run
        real_bal = get_live_balance(clob_client)
        self.bankroll = real_bal if real_bal > 0 else 290.0  # fallback
        self.open_positions = {}
        self.closed_positions = []
        self.exposure = defaultdict(float)
        self.entered_tokens = set()
        self.scan_count = 0
        self.total_wagered = 0
        self.total_pnl = 0
        self.trade_log_file = os.path.join(LOG_DIR, f"trades_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json")
        mode = "DRY RUN" if dry_run else "LIVE"
        logger.info(f"{'='*60}")
        logger.info(f"  Polymarket Bot — {mode} — ${self.bankroll:.2f}")
        for n, c in STRATEGIES.items():
            logger.info(f"  {n}: >={c['entry_threshold']*100:.0f}%, "
                f"min {c['min_elapsed_min']}m, {c['max_per_bet_pct']*100:.0f}% bet, "
                f"{c['max_alloc_pct']*100:.0f}% alloc, slugs: {c['slug_prefixes']}")
        logger.info(f"{'='*60}")

    def check_exits(self, markets):
        mkt_by_token = {m['token_id']: m for m in markets if m['token_id']}
        for tid in list(self.open_positions):
            pos = self.open_positions[tid]
            m = mkt_by_token.get(tid)
            if not m:
                continue
            prob = m.get('implied_prob', 0)
            action = None
            # SELL at 99c+ to lock profit (don't wait for slow resolution)
            if prob >= 0.99:
                action = 'SELL_WIN'
            # Cut losses if price collapses
            elif prob <= 0.10:
                action = 'SELL_LOSS'
            if not action:
                continue
            won = action == 'SELL_WIN'
            sell_price = min(prob, 0.99)  # cap at 99c
            if not won:
                sell_price = prob  # sell scraps at whatever price
            success = self._sell_position(tid, sell_price, pos['shares'], pos)
            if not success and not self.dry_run:
                # retry 1c lower for wins, skip for losses
                if won:
                    success = self._sell_position(tid, sell_price - 0.01, pos['shares'], pos)
                    if success: sell_price -= 0.01
                if not success:
                    logger.warning(f"  ⏳ Sell failed {pos['outcome'][:25]} @{prob:.3f} - retry next cycle")
                    continue
            sell_revenue = pos['shares'] * sell_price
            cost = pos['cost']; pnl = sell_revenue - cost
            self.bankroll += sell_revenue
            self.exposure[pos['strategy']] = max(0, self.exposure[pos['strategy']]-cost)
            self.total_pnl += pnl
            self.closed_positions.append({**pos,'pnl':pnl,'won':won,'exit_price':sell_price,
                'exit_ts':datetime.now(timezone.utc).isoformat()})
            emoji = '✅' if won else '❌'
            tag = 'WIN' if won else 'LOSS'
            logger.info(f"  {emoji} {tag} | {pos['outcome'][:25]} | "
                f"in@{pos['entry_price']:.3f} out@{sell_price:.3f} | "
                f"${cost:.2f} -> ${sell_revenue:.2f} (PnL: ${pnl:+.2f}) | Bank: ${self.bankroll:.2f}")
            del self.open_positions[tid]
            self._save()

    def _sell_position(self, token_id, price, size, pos):
        if self.dry_run:
            logger.info(f"  [DRY] Sell {size:.1f} shr {pos['outcome'][:25]} @ {price:.3f}")
            return True
        try:
            from py_clob_client.clob_types import OrderArgs, OrderType
            args = OrderArgs(price=price, size=size, side='SELL', token_id=token_id)
            signed = self.client.create_order(args)
            resp = self.client.post_order(signed, OrderType.FOK)
            if resp and resp.get('success'):
                logger.info(f"  ✅ Sold: {resp.get('orderID','?')}")
                return True
            resp2 = self.client.post_order(signed, OrderType.GTC)
            if resp2 and resp2.get('success'):
                logger.info(f"  ✅ Sell posted: {resp2.get('orderID','?')}")
                return True
            logger.warning(f"  ⚠️ Sell failed: {resp}")
            return False
        except Exception as e:
            logger.error(f"  ❌ Sell error: {e}")
            return False

    def find_opportunities(self, markets):
        cands = []
        for m in markets:
            tid = m['token_id']
            if not tid or tid in self.entered_tokens or tid in self.open_positions: continue
            cfg = STRATEGIES[m['strategy']]
            if m['implied_prob'] < cfg['entry_threshold']: continue
            if m['implied_prob'] >= 0.99: continue  # no upside at 99c+
            if m['game_elapsed'] < cfg['min_elapsed_min']: continue
            if m['liquidity'] < MIN_LIQUIDITY: continue
            evh = calc_ev_per_hour(m['implied_prob'], m['strategy'], m['game_elapsed'])
            if evh <= 0: continue
            cands.append({**m, 'ev_hour': evh})
        cands.sort(key=lambda x: -x['ev_hour'])
        return cands

    def allocate_and_execute(self, candidates):
        for c in candidates:
            tid = c['token_id']
            if tid in self.entered_tokens or tid in self.open_positions: continue
            cfg = STRATEGIES[c['strategy']]
            avail = min(self.bankroll * cfg['max_per_bet_pct'],
                       self.bankroll * cfg['max_alloc_pct'] - self.exposure[c['strategy']])
            if avail < MIN_BET: continue
            exec_price = c['implied_prob']  # Use implied prob as price
            shares = avail / exec_price
            success = self._place_order(tid, exec_price, shares, c)
            if success:
                self.open_positions[tid] = {
                    'token_id': tid, 'event': c['event_name'], 'outcome': c['outcome'],
                    'strategy': c['strategy'], 'entry_price': exec_price,
                    'shares': shares, 'cost': avail,
                    'entry_ts': datetime.now(timezone.utc).isoformat(),
                }
                self.entered_tokens.add(tid)
                self.exposure[c['strategy']] += avail
                self.bankroll -= avail
                self.total_wagered += avail
                logger.info(f"  🎯 ENTRY | {c['outcome'][:25]} @ {exec_price:.3f} | "
                    f"${avail:.2f} ({shares:.1f} shr) | EVH:{c['ev_hour']:.3f} | "
                    f"Bank: ${self.bankroll:.2f} | {c['strategy']} | {c['slug']}")
                self._save()

    def _place_order(self, token_id, price, size, info):
        if self.dry_run:
            logger.info(f"  [DRY] Buy {size:.1f} shr {info['outcome'][:25]} @ {price:.3f}")
            return True
        try:
            from py_clob_client.clob_types import OrderArgs, OrderType
            order_args = OrderArgs(price=round(price, 2), size=float(int(size)), side="BUY", token_id=token_id)
            signed = self.client.create_order(order_args)
            resp = self.client.post_order(signed, OrderType.FOK)
            logger.info(f"  📋 FOK response: {resp}")
            if resp and resp.get("success"):
                order_id = resp.get('orderID','?')
                status = resp.get('status','').lower()
                matched = float(resp.get('matchedAmount', 0) or 0)
                if status in ('matched', 'delayed', 'live') or matched > 0 or order_id:
                    logger.info(f"  ✅ Order FILLED: {order_id} | matched: {matched}")
                    return True
                else:
                    logger.warning(f"  ⚠️ FOK NOT filled: {order_id} | status={status} matched={matched}")
                    return False
            logger.error(f"  ❌ Failed: {resp}")
            return False
        except Exception as e:
            logger.error(f"  ❌ Error: {e}")
            return False

    def _save(self):
        try:
            with open(self.trade_log_file, 'w') as f:
                json.dump({'bankroll': self.bankroll, 'wagered': self.total_wagered,
                    'pnl': self.total_pnl, 'open': list(self.open_positions.values()),
                    'closed': self.closed_positions}, f, indent=2, default=str)
        except: pass

    def print_status(self):
        w = sum(1 for p in self.closed_positions if p.get('won'))
        l = len(self.closed_positions) - w
        oc = sum(p['cost'] for p in self.open_positions.values())
        logger.info(f"\n{'='*60}")
        logger.info(f"  STATUS #{self.scan_count} | Cash: ${self.bankroll:.2f} | "
            f"Open: {len(self.open_positions)} (${oc:.2f}) | {w}W/{l}L | "
            f"Wagered: ${self.total_wagered:.2f} | PnL: ${self.total_pnl:+.2f}")
        for t,p in self.open_positions.items():
            logger.info(f"    {p['outcome'][:30]} @ {p['entry_price']:.3f} ${p['cost']:.2f} {p['strategy']}")
        logger.info(f"{'='*60}\n")

    def run_once(self):
        self.scan_count += 1
        markets = fetch_live_markets()
        by_strat = defaultdict(int)
        for m in markets: by_strat[m['strategy']] += 1
        logger.info(f"Scan #{self.scan_count}: {len(markets)} outcomes | "
            + " | ".join(f"{k}:{v}" for k,v in sorted(by_strat.items())))
        # Show games that are actually in-progress (elapsed > 0)
        live = [m for m in markets if m['game_elapsed'] > 0]
        if live:
            logger.info(f"  {len(live)} in-progress outcomes:")
            seen = set()
            for m in sorted(live, key=lambda x: -x['implied_prob'])[:10]:
                key = m['event_name']
                if key in seen: continue
                seen.add(key)
                logger.info(f"    {m['outcome'][:25]} @ {m['implied_prob']:.3f} | "
                    f"{m['game_elapsed']:.0f}min | Liq:${m['liquidity']:.0f} | {m['strategy']}")
        self.check_exits(markets)
        cands = self.find_opportunities(markets)
        if cands:
            logger.info(f"  {len(cands)} qualifying opportunities:")
            for c in cands[:5]:
                logger.info(f"    {c['outcome'][:25]} @ {c['implied_prob']:.3f} "
                    f"EVH:{c['ev_hour']:.3f} Liq:${c['liquidity']:.0f} {c['strategy']}")
        self.allocate_and_execute(cands)
        if self.scan_count % 5 == 0:
            self.print_status()
        # Re-sync balance from chain every 10 scans
        if self.scan_count % 10 == 0:
            live_bal = get_live_balance(self.client)
            if live_bal > 0:
                open_cost = sum(p["cost"] for p in self.open_positions.values())
                expected = self.bankroll + open_cost
                if abs(live_bal - expected) > 5:  # > discrepancy
                    logger.info(f"  💰 Balance sync: chain=${live_bal:.2f} vs tracked=${expected:.2f}")
                    # Update bankroll to reflect reality (e.g. manual sells on website)
                    self.bankroll = live_bal - open_cost
                    if self.bankroll < 0: self.bankroll = live_bal

    def run(self):
        logger.info("Starting bot loop...")
        running = True
        def stop(s,f): nonlocal running; running=False
        signal.signal(signal.SIGINT, stop)
        signal.signal(signal.SIGTERM, stop)
        self.print_status()
        while running:
            try: self.run_once()
            except Exception as e:
                logger.error(f"Error: {e}"); logger.debug(traceback.format_exc())
            try: time.sleep(POLL_INTERVAL)
            except KeyboardInterrupt: break
        self.print_status(); self._save()

def main():
    dry_run = "--dry-run" in sys.argv
    once = "--once" in sys.argv
    if not dry_run:
        print(f"\n⚠️  LIVE — Bankroll: ${INITIAL_BANKROLL:.2f}")
        if input("Type 'GO': ").strip() != "GO":
            print("Aborted."); sys.exit(0)
    else:
        print(f"\n🧪 DRY RUN\n")
    client = setup_clob_client()
    bot = TradingBot(client, dry_run=dry_run)
    if once: bot.run_once(); bot.print_status()
    else: bot.run()

if __name__ == "__main__":
    main()
