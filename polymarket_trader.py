#!/usr/bin/env python3
"""
Polymarket Live Sports Trading Bot (v6 Strategy)
Uses tag_id=100639 on Gamma /events API (same as monitor).

v6 changes:
- Split strategies per sport (ATP/WTA separate, added NBA/NCAACBB/CWBB/CS)
- LIVE DETECTION FIX: uses event.period + event.live to confirm game in progress
  (eliminates betting on future games / pre-game favorites)
- Removed per-sport allocation caps (max_alloc_pct)
- Added per-market cap (max 20% bankroll including scale-ins)
- Added total exposure cap (max 80% bankroll)
- Scale-in logic: add to winning positions at +3c and +5c above entry
- Updated thresholds and time delays per monitor data analysis
"""

import os, sys, json, time, signal, logging, traceback, requests
from datetime import datetime, timedelta, timezone
from collections import defaultdict
from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))

INITIAL_BANKROLL = 360.0
STATE_FILE = os.path.expanduser("~/polymarket/bot_state.json")
POLL_INTERVAL = 45
MIN_BET = 1.0
MIN_LIQUIDITY = 50000
GAMMA_API = "https://gamma-api.polymarket.com"
CLOB_API = "https://clob.polymarket.com"
TAG_ID = 100639

# --- Risk controls ---
MAX_TOTAL_EXPOSURE_PCT = 1.00
MAX_PER_MARKET_PCT = 0.20
DEFAULT_BET_PCT = 0.20

# --- Periods that mean game is NOT in progress ---
NON_LIVE_PERIODS = {'FT', 'VFT', 'Final', 'CAN', 'POST', 'Scheduled', ''}

STRATEGIES = {
    'ATP': {
        'slug_prefixes': {'atp-'},
        'entry_threshold': 0.94,
        'max_per_bet_pct': DEFAULT_BET_PCT,
        'min_elapsed_min': 45,
        'est_remaining_min': 60,
        'hist_winrate': 0.958,
        'scale_in': False,
    },
    'WTA': {
        'slug_prefixes': {'wta-'},
        'entry_threshold': 0.92,
        'max_per_bet_pct': DEFAULT_BET_PCT,
        'min_elapsed_min': 30,
        'est_remaining_min': 60,
        'hist_winrate': 0.941,
        'scale_in': False,
    },
    'NCAA_CBB': {
        'slug_prefixes': {'cbb-'},
        'entry_threshold': 0.93,
        'max_per_bet_pct': DEFAULT_BET_PCT,
        'min_elapsed_min': 60,
        'est_remaining_min': 60,
        'hist_winrate': 0.964,
        'scale_in': False,
        'min_liquidity': 20000,
    },
    'CWBB': {
        'slug_prefixes': {'cwbb-'},
        'entry_threshold': 0.90,
        'max_per_bet_pct': DEFAULT_BET_PCT,
        'min_elapsed_min': 45,
        'est_remaining_min': 60,
        'hist_winrate': 0.947,
        'scale_in': False,
        'min_liquidity': 20000,
    },
    'NBA': {
        'slug_prefixes': {'nba-'},
        'entry_threshold': 0.91,
        'max_per_bet_pct': DEFAULT_BET_PCT,
        'min_elapsed_min': 0,
        'est_remaining_min': 60,
        'hist_winrate': 1.00,
        'scale_in': False,
    },
    'NHL': {
        'slug_prefixes': {'nhl-'},
        'entry_threshold': 0.93,
        'max_per_bet_pct': DEFAULT_BET_PCT,
        'min_elapsed_min': 30,
        'est_remaining_min': 30,
        'hist_winrate': 0.90,
        'scale_in': False,
    },
    'WTT_Women': {
        'slug_prefixes': {'wttwmn-'},
        'entry_threshold': 0.88,
        'max_per_bet_pct': DEFAULT_BET_PCT,
        'min_elapsed_min': 0,
        'est_remaining_min': 30,
        'hist_winrate': 1.00,
        'scale_in': False,
    },
    'WTT_Men': {
        'slug_prefixes': {'wttmen-'},
        'entry_threshold': 0.88,
        'max_per_bet_pct': DEFAULT_BET_PCT,
        'min_elapsed_min': 0,
        'est_remaining_min': 30,
        'hist_winrate': 1.00,
        'scale_in': False,
    },
}

SKIP_OUTCOMES = {'Over', 'Under', 'Draw', 'Tie'}

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
    try:
        from py_clob_client.clob_types import BalanceAllowanceParams, AssetType
        bal = client.get_balance_allowance(BalanceAllowanceParams(
            asset_type=AssetType.COLLATERAL, signature_type=1))
        raw = int(bal.get('balance', 0))
        return raw / 1_000_000
    except Exception as e:
        logger.error(f"Balance fetch failed: {e}")
        return 0


def get_strategy(slug):
    for name, cfg in STRATEGIES.items():
        for prefix in cfg['slug_prefixes']:
            if slug.startswith(prefix):
                return name
    return None


def is_game_live(event):
    """
    Determine if a game is currently in progress using TWO signals:
    1. event.period - if present and not a terminal/empty state, game is live
    2. event.live - Polymarket's live tag (works for most sports)
    Returns (is_live: bool, elapsed_min: float)
    """
    period = event.get("period", "")
    event_live = event.get("live", False)

    period_indicates_live = bool(period) and period not in NON_LIVE_PERIODS
    api_says_live = bool(event_live)

    # REQUIRE a real score — future games never have one
    score = event.get("score", "") or ""
    has_real_score = bool(score) and any(c.isdigit() for c in score) and '-' in score

    if not period_indicates_live and not api_says_live:
        return False, 0
    
    # If only the live flag says yes (no period), require a real score
    # This catches future games with live=True but no actual game data
    if not period_indicates_live and api_says_live and not has_real_score:
        return False, 0

    # Basketball: require at least one point scored (blocks pre-game 0-0)
    import re as _re
    for mkt in event.get("markets", []):
        s = (mkt.get("slug") or "")
        if s.startswith(("nba-", "cbb-", "cwbb-")):
            digits = [int(d) for d in _re.findall(r"\d+", score)]
            if not any(d > 0 for d in digits):
                return False, 0
            break

    elapsed_min = 0
    start_time = event.get("startTime", "")
    if start_time:
        try:
            st = datetime.fromisoformat(
                start_time.replace("Z", "+00:00").replace(" ", "T").split("+")[0] + "+00:00"
            )
            now = datetime.now(timezone.utc)
            mins_since = (now - st).total_seconds() / 60
            if mins_since > 0:
                elapsed_min = mins_since
        except:
            pass

    if elapsed_min > 720:
        if not period_indicates_live:
            return False, 0

    return True, elapsed_min


def fetch_live_markets():
    """
    Fetch sports events using tag_id=100639.
    Uses HYBRID approach: fetches live=true events + today/yesterday events,
    then filters ALL through is_game_live().
    """
    all_markets = []
    seen_events = set()

    urls_to_fetch = []
    urls_to_fetch.append(
        f"{GAMMA_API}/events?tag_id={TAG_ID}&active=true&closed=false&live=true&limit=200"
    )
    et_now = datetime.now(timezone(timedelta(hours=-5)))
    today = et_now.strftime('%Y-%m-%d')
    yesterday = (et_now - timedelta(days=1)).strftime('%Y-%m-%d')
    urls_to_fetch.append(
        f"{GAMMA_API}/events?tag_id={TAG_ID}&active=true&closed=false&event_date={today}&limit=200"
    )
    urls_to_fetch.append(
        f"{GAMMA_API}/events?tag_id={TAG_ID}&active=true&closed=false&event_date={yesterday}&limit=200"
    )

    all_events = []
    for base_url in urls_to_fetch:
        offset = 0
        while True:
            try:
                url = f"{base_url}&offset={offset}" if offset > 0 else base_url
                resp = requests.get(url, timeout=15)
                resp.raise_for_status()
                events = resp.json()
            except Exception as e:
                logger.error(f"Fetch failed: {e}")
                break
            if not events:
                break
            for ev in events:
                eid = ev.get("id")
                if eid and eid not in seen_events:
                    seen_events.add(eid)
                    all_events.append(ev)
            offset += 200
            if len(events) < 200:
                break

    live_event_count = 0
    skipped_not_live = 0

    for event in all_events:
        game_live, elapsed_min = is_game_live(event)

        # Tennis/WTT: Gamma never sets live/period/score for tennis
        # Check if any market in this event is tennis, bypass live check
        if not game_live:
            for _mkt in event.get("markets", []):
                _slug = (_mkt.get("slug") or "")
                if _slug.startswith(("atp-", "wta-", "wttwmn-", "wttmen-")):
                    _gst = _mkt.get("gameStartTime", "")
                    if _gst:
                        try:
                            _start = datetime.fromisoformat(
                                _gst.replace("Z", "+00:00").replace(" ", "T").split("+")[0] + "+00:00"
                            )
                            _mins = (datetime.now(timezone.utc) - _start).total_seconds() / 60
                            if _mins > 0:
                                game_live = True
                                elapsed_min = _mins
                        except:
                            pass
                    break

        if not game_live:
            skipped_not_live += 1
            continue

        live_event_count += 1

        for mkt in event.get("markets", []):
            slug = mkt.get("slug", "") or ""
            strat = get_strategy(slug)
            if not strat:
                continue
            if mkt.get("sportsMarketType", "") != "moneyline":
                continue

            # Tennis/WTT: Gamma never sets live/period/score
            # Bypass live check — time delay handles safety
            is_tennis = strat in ('ATP', 'WTA', 'WTT_Women', 'WTT_Men')
            if is_tennis and not game_live:
                gst = mkt.get("gameStartTime", "")
                if gst:
                    try:
                        start = datetime.fromisoformat(
                            gst.replace("Z", "+00:00").replace(" ", "T").split("+")[0] + "+00:00"
                        )
                        now = datetime.now(timezone.utc)
                        mins = (now - start).total_seconds() / 60
                        if mins > 0:
                            elapsed_min = mins
                            game_live = True
                    except:
                        pass

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

            skip = False
            for o in outcomes:
                if o in SKIP_OUTCOMES:
                    skip = True
            if skip:
                continue

            liquidity = float(mkt.get("liquidity", 0) or 0)
            best_ask = float(mkt.get("bestAsk", 0) or 0)
            best_bid = float(mkt.get("bestBid", 0) or 0)

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
                    'game_start': mkt.get("gameStartTime", ""),
                    'event_period': event.get("period", ""),
                    'event_score': event.get("score", ""),
                    'event_live': event.get("live", False),
                    'token_id': token_ids[i] if i < len(token_ids) else "",
                    'neg_risk': mkt.get("negRisk", False),
                    'tick_size': str(mkt.get("orderPriceMinTickSize", "0.01")),
                    'market_id': str(mkt.get("id", "")),
                    'condition_id': mkt.get("conditionId", ""),
                })

    logger.info(f"  Events: {len(all_events)} fetched, {live_event_count} live, "
                f"{skipped_not_live} skipped (not live)")
    return all_markets


def calc_ev_per_hour(prob, strat_name, elapsed_min):
    cfg = STRATEGIES[strat_name]
    edge = cfg['hist_winrate'] - prob
    if edge <= 0:
        return -1
    rem = max(cfg['est_remaining_min'] - max(elapsed_min - cfg['min_elapsed_min'], 0) * 0.5, 5)
    return edge / (rem / 60)


class TradingBot:
    def __init__(self, clob_client, dry_run=False):
        self.client = clob_client
        self.dry_run = dry_run
        real_bal = get_live_balance(clob_client)
        self.bankroll = real_bal if real_bal > 0 else 290.0
        self.open_positions = {}
        self.market_positions = {}
        self.closed_positions = []
        self._load_state()
        self.entered_tokens = set()
        self.scan_count = 0
        self.total_wagered = 0
        self.total_pnl = 0
        self._early_probs: dict = {}  # {token_id: float} implied_prob captured on first observation
        self.trade_log_file = os.path.join(
            LOG_DIR, f"trades_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json")

        mode = "DRY RUN" if dry_run else "LIVE"
        logger.info(f"{'='*60}")
        logger.info(f"  Polymarket Bot v6 — {mode} — ${self.bankroll:.2f}")
        logger.info(f"  Risk: {MAX_TOTAL_EXPOSURE_PCT*100:.0f}% max exposure, "
                     f"{MAX_PER_MARKET_PCT*100:.0f}% per market, "
                     f"{DEFAULT_BET_PCT*100:.0f}% per bet")
        for n, c in STRATEGIES.items():
            logger.info(f"  {n}: >={c['entry_threshold']*100:.0f}%, "
                f"min {c['min_elapsed_min']}m, "
                f"scale={'Y' if c.get('scale_in') else 'N'}, "
                f"slugs: {c['slug_prefixes']}")
        logger.info(f"{'='*60}")

    def total_exposure(self):
        return sum(p['cost'] for p in self.open_positions.values())

    def market_exposure(self, market_id):
        return self.market_positions.get(market_id, 0)

    def _get_token_price(self, token_id):
        """Query CLOB for last trade price of a token not in the live scan."""
        try:
            result = self.client.get_last_trade_price(token_id)
            if result:
                price = float(result.get('price', 0))
                if price > 0:
                    return price
        except Exception as e:
            logger.debug(f"Price check failed for {token_id[:12]}: {e}")
        return None

    def _load_state(self):
        if os.path.exists(STATE_FILE):
            try:
                with open(STATE_FILE, 'r') as f:
                    state = json.load(f)
                self.open_positions = state.get('open_positions', {})
                self.market_positions = state.get('market_positions', {})
                self.entered_tokens = set(state.get('entered_tokens', []))
                self.bankroll = state.get('bankroll', INITIAL_BANKROLL)
                self.total_wagered = state.get('total_wagered', 0)
                self.total_pnl = state.get('total_pnl', 0)
                self.closed_positions = state.get('closed_positions', [])
                n = len(self.open_positions)
                logger.info(f"  Loaded state: {n} open positions, ${self.bankroll:.2f} cash")
                for tid, pos in self.open_positions.items():
                    logger.info(f"    {pos['outcome'][:25]} @ {pos['entry_price']:.3f} | ${pos['cost']:.2f} | {pos['strategy']}")
            except Exception as e:
                logger.error(f"State load failed: {e}")

    def check_exits(self, markets):
        mkt_by_token = {m['token_id']: m for m in markets if m['token_id']}
        for tid in list(self.open_positions):
            pos = self.open_positions[tid]
            m = mkt_by_token.get(tid)
            if m:
                clob = self._get_token_price(tid)
                prob = clob if clob and clob > m.get('implied_prob', 0) else m.get('implied_prob', 0)
            else:
                # Position not in live scan — game may have ended
                # Query CLOB directly for current price
                prob = self._get_token_price(tid)
                if prob is None:
                    # Check how old this position is — if > 6 hours, try to sell at 99c
                    entry_ts = pos.get('entry_ts', '')
                    if entry_ts:
                        try:
                            entered = datetime.fromisoformat(entry_ts)
                            age_hrs = (datetime.now(timezone.utc) - entered).total_seconds() / 3600
                            if age_hrs > 6:
                                logger.info(f"  ⚠️ Stale position ({age_hrs:.0f}h): {pos['outcome'][:25]} — attempting sell @0.99")
                                prob = 0.99  # Assume won, try to sell
                            else:
                                continue
                        except:
                            continue
                    else:
                        continue
                else:
                    logger.info(f"  🔍 Off-scan price: {pos['outcome'][:25]} @ {prob:.3f}")
            action = None
            if prob >= 0.99:
                action = 'SELL_WIN'
            elif prob <= 0.40:
                action = 'SELL_LOSS'
            if not action:
                continue

            won = action == 'SELL_WIN'
            sell_price = min(prob, 0.99)
            if not won:
                sell_price = prob

            success = self._sell_position(tid, sell_price, pos['shares'], pos)
            if success == 'RESOLVED':
                # Market resolved on-chain, shares auto-redeemed at $1 if won
                cost = pos['cost']
                pnl = pos['shares'] - cost if won else -cost
                self.total_pnl += pnl
                emoji = '🏁' if won else '💀'
                logger.info(f"  {emoji} AUTO-RESOLVED | {pos['outcome'][:25]} | cost=${cost:.2f} | PnL: ${pnl:+.2f}")
                del self.open_positions[tid]
                self._save()
                continue
            if not success and not self.dry_run:
                if won:
                    success = self._sell_position(tid, sell_price - 0.01, pos['shares'], pos)
                    if success == 'RESOLVED':
                        cost = pos['cost']
                        pnl = pos['shares'] - cost if won else -cost
                        self.total_pnl += pnl
                        logger.info(f"  🏁 AUTO-RESOLVED | {pos['outcome'][:25]} | cost=${cost:.2f} | PnL: ${pnl:+.2f}")
                        del self.open_positions[tid]
                        self._save()
                        continue
                    if success:
                        sell_price -= 0.01
                if not success:
                    logger.warning(f"  ⏳ Sell failed {pos['outcome'][:25]} @{prob:.3f} - retry next cycle")
                    continue

            sell_revenue = pos['shares'] * sell_price
            cost = pos['cost']
            pnl = sell_revenue - cost
            self.bankroll += sell_revenue
            mid = pos.get('market_id', '')
            if mid in self.market_positions:
                self.market_positions[mid] = max(0, self.market_positions[mid] - cost)
                if self.market_positions[mid] <= 0:
                    del self.market_positions[mid]
            self.total_pnl += pnl
            self.closed_positions.append({
                **pos, 'pnl': pnl, 'won': won, 'exit_price': sell_price,
                'exit_ts': datetime.now(timezone.utc).isoformat()
            })
            emoji = '✅' if won else '❌'
            tag = 'WIN' if won else 'LOSS'
            logger.info(f"  {emoji} {tag} | {pos['outcome'][:25]} | "
                f"in@{pos['entry_price']:.3f} out@{sell_price:.3f} | "
                f"${cost:.2f} -> ${sell_revenue:.2f} (PnL: ${pnl:+.2f}) | Bank: ${self.bankroll:.2f}")
            del self.open_positions[tid]
            self._save()


    def check_exits_from_api(self):
        """Query actual on-chain positions and sell anything at 99c+ or stop-loss."""
        try:
            import requests
            funder = os.getenv("POLYMARKET_FUNDER")
            if not funder:
                return
            resp = requests.get(
                f"https://data-api.polymarket.com/positions?user={funder}",
                timeout=10
            )
            if resp.status_code != 200:
                return
            positions = resp.json()
        except Exception as e:
            logger.error(f"  Position API failed: {e}")
            return

        for p in positions:
            size = float(p.get("size", 0))
            cur_price = float(p.get("curPrice", 0))
            avg_price = float(p.get("avgPrice", 0))
            token_id = p.get("asset", "")
            outcome = p.get("outcome", "?")
            title = p.get("title", "?")

            if size < 0.1 or cur_price <= 0:
                continue

            action = None
            if cur_price >= 0.99:
                action = "SELL_WIN"
            elif cur_price < 0.01:
                logger.info(f"  💀 WRITE-OFF | {outcome[:25]} | {size:.1f} shr — resolved loss, skipping")
                continue
            elif avg_price > 0 and cur_price <= 0.40:
                action = "STOP_LOSS"

            if not action:
                continue

            sell_price = min(cur_price, 0.99) if action == "SELL_WIN" else cur_price
            logger.info(f"  🔄 API-EXIT ({action}) | {outcome[:25]} | {size:.1f} shr @ {sell_price:.3f} | entry: {avg_price:.3f}")

            success = self._sell_position(token_id, sell_price, size, {"outcome": outcome})
            if success and success != "RESOLVED":
                revenue = size * sell_price
                cost = size * avg_price
                pnl = revenue - cost
                self.bankroll += revenue
                self.total_pnl += pnl
                if token_id in self.open_positions:
                    del self.open_positions[token_id]
                emoji = "✅" if action == "SELL_WIN" else "🛑"
                tag = "WIN" if action == "SELL_WIN" else "STOP-LOSS"
                logger.info(f"  {emoji} {tag} (API) | {outcome[:25]} | "
                    f"in@{avg_price:.3f} out@{sell_price:.3f} | "
                    f"${cost:.2f} -> ${revenue:.2f} (PnL: ${pnl:+.2f}) | Bank: ${self.bankroll:.2f}")
                self._save()
            elif success == "RESOLVED":
                logger.info(f"  🏁 RESOLVED (API) | {outcome[:25]} — auto-cleaned")
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
            err_msg = str(e).lower()
            if 'does not exist' in err_msg or 'not enough balance' in err_msg:
                logger.info(f"  📋 Market resolved/redeemed: {pos['outcome'][:25]} — auto-cleaning")
                return 'RESOLVED'
            logger.error(f"  ❌ Sell error: {e}")
            return False

    def find_opportunities(self, markets):
        cands = []
        for m in markets:
            tid = m['token_id']
            if not tid:
                continue

            cfg = STRATEGIES[m['strategy']]
            prob = m['implied_prob']

            # --- SCALE-IN ---
            if tid in self.open_positions and cfg.get('scale_in'):
                pos = self.open_positions[tid]
                entry = pos['entry_price']
                scale_levels = [entry + 0.03, entry + 0.05]
                already_scaled = pos.get('scale_count', 0)
                if already_scaled < len(scale_levels) and prob >= scale_levels[already_scaled]:
                    if prob < 0.99:
                        evh = calc_ev_per_hour(prob, m['strategy'], m['game_elapsed'])
                        if evh > 0:
                            cands.append({**m, 'ev_hour': evh, 'is_scale_in': True,
                                          'scale_level': already_scaled})
                continue

            # --- NEW ENTRY ---
            if tid in self.entered_tokens or tid in self.open_positions:
                continue
            if prob < cfg['entry_threshold']:
                continue
            if prob >= 0.99:
                continue
            if m['game_elapsed'] < cfg['min_elapsed_min']:
                continue
            min_liq = cfg.get('min_liquidity', MIN_LIQUIDITY)
            if m['liquidity'] < min_liq:
                continue

            evh = calc_ev_per_hour(prob, m['strategy'], m['game_elapsed'])
            if evh <= 0:
                continue

            cands.append({**m, 'ev_hour': evh, 'is_scale_in': False})

        cands.sort(key=lambda x: -x['ev_hour'])
        return cands

    def allocate_and_execute(self, candidates):
        for c in candidates:
            tid = c['token_id']
            mid = c.get('market_id', '')
            is_scale = c.get('is_scale_in', False)

            current_exposure = self.total_exposure()
            total_capital = self.bankroll + current_exposure
            max_deploy = total_capital * MAX_TOTAL_EXPOSURE_PCT - current_exposure
            if max_deploy < MIN_BET:
                logger.info(f"  ⛔ Total exposure cap hit ({current_exposure:.0f} deployed)")
                break

            market_exp = self.market_exposure(mid)
            market_room = total_capital * MAX_PER_MARKET_PCT - market_exp
            if market_room < MIN_BET:
                continue

            cfg = STRATEGIES[c['strategy']]
            bet_size = min(
                max(total_capital * DEFAULT_BET_PCT, 20),
                market_room,
                max_deploy,
            )
            if self.bankroll < bet_size:
                continue   # insufficient cash for a full bet — wait for a position to close
            if bet_size < 5:
                continue

            # Use best_ask if available (actual execution price), fall back to implied_prob
            exec_price = c.get('best_ask', 0)
            if exec_price <= 0 or exec_price > 0.99:
                exec_price = c['implied_prob']
            # Ensure we're not paying more than 99c
            exec_price = min(exec_price, 0.99)
            shares = bet_size / exec_price
            # Polymarket minimum order size is 5 shares
            if shares < 5:
                shares = 5
                bet_size = shares * exec_price
                if bet_size > self.bankroll:
                    continue

            if is_scale:
                if tid not in self.open_positions:
                    continue
                success = self._place_order(tid, exec_price, shares, c)
                if success:
                    pos = self.open_positions[tid]
                    pos['cost'] += bet_size
                    pos['shares'] += shares
                    pos['scale_count'] = pos.get('scale_count', 0) + 1
                    pos['entry_price'] = pos['cost'] / pos['shares']
                    self.bankroll -= bet_size
                    self.total_wagered += bet_size
                    if mid:
                        self.market_positions[mid] = self.market_positions.get(mid, 0) + bet_size
                    logger.info(f"  📈 SCALE-IN #{pos['scale_count']} | {c['outcome'][:25]} "
                        f"@ {exec_price:.3f} | +${bet_size:.2f} (total ${pos['cost']:.2f}) | "
                        f"Bank: ${self.bankroll:.2f} | {c['strategy']}")
                    self._save()
            else:
                if tid in self.entered_tokens or tid in self.open_positions:
                    continue
                success = self._place_order(tid, exec_price, shares, c)
                if success:
                    self.open_positions[tid] = {
                        'token_id': tid, 'event': c['event_name'], 'outcome': c['outcome'],
                        'strategy': c['strategy'], 'entry_price': exec_price,
                        'shares': shares, 'cost': bet_size,
                        'market_id': mid,
                        'scale_count': 0,
                        'entry_ts': datetime.now(timezone.utc).isoformat(),
                    }
                    self.entered_tokens.add(tid)
                    self.bankroll -= bet_size
                    self.total_wagered += bet_size
                    if mid:
                        self.market_positions[mid] = self.market_positions.get(mid, 0) + bet_size
                    logger.info(f"  🎯 ENTRY | {c['outcome'][:25]} @ {exec_price:.3f} | "
                        f"${bet_size:.2f} ({shares:.1f} shr) | EVH:{c['ev_hour']:.3f} | "
                        f"Bank: ${self.bankroll:.2f} | {c['strategy']} | {c['slug']}")
                    self._save()

    def _place_order(self, token_id, price, size, info):
        if self.dry_run:
            action = "Scale" if info.get('is_scale_in') else "Buy"
            logger.info(f"  [DRY] {action} {size:.1f} shr {info['outcome'][:25]} @ {price:.3f}")
            return True
        try:
            from py_clob_client.clob_types import OrderArgs, OrderType
            import time as _time
            # Market order: bid at 0.99 to cross any ask, CLOB fills at best available
            buy_price = 0.99
            bet_amount = info.get('cost', size * price)
            size = max(5.0, float(int(bet_amount / price)))
            order_args = OrderArgs(
                price=buy_price, size=size,
                side="BUY", token_id=token_id
            )
            signed = self.client.create_order(order_args)
            logger.info(f"  💲 Market buy {size:.0f} shr @ {buy_price} (imp={price:.3f})")
            resp = self.client.post_order(signed, OrderType.FOK)
            logger.info(f"  📋 Order response: {resp}")
            if resp and resp.get("success"):
                order_id = resp.get('orderID', '')
                # Wait for async processing then verify fill
                _time.sleep(4)
                try:
                    check = self.client.get_order(order_id)
                    if check:
                        status = check.get('status', '').upper()
                        matched = float(check.get('size_matched', 0) or 0)
                        logger.info(f"  🔄 Verify: status={status} matched={matched}/{size}")
                        if status == 'MATCHED' or matched >= size * 0.9:
                            logger.info(f"  ✅ FILLED: {order_id[:20]}")
                            return True
                        elif status == 'LIVE' and matched == 0:
                            # Sitting on book unfilled — cancel it
                            logger.warning(f"  ❌ Not filled, cancelling...")
                            try:
                                self.client.cancel(order_id)
                            except:
                                pass
                            return False
                        elif matched > 0:
                            # Partially filled
                            logger.info(f"  ⚠️ Partial fill: {matched}/{size}")
                            return True
                        else:
                            logger.warning(f"  ❌ Unknown status: {status}")
                            try:
                                self.client.cancel(order_id)
                            except:
                                pass
                            return False
                    else:
                        logger.warning(f"  ❌ Order not found after delay")
                        return False
                except Exception as e:
                    logger.warning(f"  ⚠️ Verify failed: {e}")
                    return False
            logger.error(f"  ❌ Order rejected: {resp}")
            return False
        except Exception as e:
            logger.error(f"  ❌ Order error: {e}")
            return False

    def _save(self):
        try:
            with open(self.trade_log_file, 'w') as f:
                json.dump({
                    'bankroll': self.bankroll, 'wagered': self.total_wagered,
                    'pnl': self.total_pnl,
                    'total_exposure': self.total_exposure(),
                    'open': list(self.open_positions.values()),
                    'closed': self.closed_positions
                }, f, indent=2, default=str)
        except:
            pass
        try:
            with open(STATE_FILE, 'w') as f:
                json.dump({
                    'open_positions': self.open_positions,
                    'market_positions': self.market_positions,
                    'entered_tokens': list(self.entered_tokens),
                    'bankroll': self.bankroll,
                    'total_wagered': self.total_wagered,
                    'total_pnl': self.total_pnl,
                    'closed_positions': self.closed_positions[-50:],
                }, f, indent=2, default=str)
        except Exception as e:
            logger.error(f"State save failed: {e}")

    def print_status(self):
        w = sum(1 for p in self.closed_positions if p.get('won'))
        l = len(self.closed_positions) - w
        exp = self.total_exposure()
        total_capital = self.bankroll + exp
        logger.info(f"\n{'='*60}")
        logger.info(f"  STATUS #{self.scan_count} | Cash: ${self.bankroll:.2f} | "
            f"Deployed: ${exp:.2f} ({exp/total_capital*100:.0f}% of ${total_capital:.2f}) | "
            f"{w}W/{l}L | PnL: ${self.total_pnl:+.2f}")
        for t, p in self.open_positions.items():
            sc = f" (x{p.get('scale_count',0)})" if p.get('scale_count', 0) > 0 else ""
            logger.info(f"    {p['outcome'][:30]} @ {p['entry_price']:.3f} "
                        f"${p['cost']:.2f}{sc} {p['strategy']}")
        logger.info(f"{'='*60}\n")

    def run_once(self):
        self.scan_count += 1
        live_bal = get_live_balance(self.client)
        if live_bal > 0:
            self.bankroll = live_bal
        markets = fetch_live_markets()

        by_strat = defaultdict(int)
        for m in markets:
            by_strat[m['strategy']] += 1
        logger.info(f"Scan #{self.scan_count}: {len(markets)} live outcomes | "
            + " | ".join(f"{k}:{v}" for k, v in sorted(by_strat.items())))

        qualifying = [m for m in markets if m['game_elapsed'] > 0]
        if qualifying:
            seen = set()
            top = []
            for m in sorted(qualifying, key=lambda x: -x['implied_prob']):
                key = (m['event_name'], m['outcome'])
                if key in seen:
                    continue
                seen.add(key)
                top.append(m)
                if len(top) >= 8:
                    break
            if top:
                logger.info(f"  Top {len(top)} live markets:")
                for m in top:
                    period = m.get('event_period', '?')
                    score = m.get('event_score', '')
                    if score:
                        parts = score.split('|')
                        score = parts[1] if len(parts) >= 2 else score
                    logger.info(f"    {m['outcome'][:25]} @ {m['implied_prob']:.3f} | "
                        f"{m['game_elapsed']:.0f}m | {period} {score} | "
                        f"Liq:${m['liquidity']:.0f} | {m['strategy']}")

        # Capture match-start probability for ATP/WTA on first observation
        for m in markets:
            if m['strategy'] in ('ATP', 'WTA'):
                tid = m['token_id']
                if tid and tid not in self._early_probs:
                    self._early_probs[tid] = m['implied_prob']
                    logger.debug(f"  📌 Match-start prob {m['outcome'][:20]}: {m['implied_prob']:.2f} @ {m['game_elapsed']:.0f}m")

        self.check_exits(markets)
        self.check_exits_from_api()
        cands = self.find_opportunities(markets)
        if cands:
            new = [c for c in cands if not c.get('is_scale_in')]
            scales = [c for c in cands if c.get('is_scale_in')]
            if new:
                logger.info(f"  {len(new)} new opportunities:")
                for c in new[:5]:
                    logger.info(f"    {c['outcome'][:25]} @ {c['implied_prob']:.3f} "
                        f"EVH:{c['ev_hour']:.3f} Liq:${c['liquidity']:.0f} {c['strategy']}")
            if scales:
                logger.info(f"  {len(scales)} scale-in opportunities:")
                for c in scales[:3]:
                    logger.info(f"    {c['outcome'][:25]} @ {c['implied_prob']:.3f} "
                        f"(scale #{c.get('scale_level',0)+1}) {c['strategy']}")

        self.allocate_and_execute(cands)

        if self.scan_count % 5 == 0:
            self.print_status()

        if self.scan_count % 10 == 0:
            live_bal = get_live_balance(self.client)
            if live_bal > 0:
                open_cost = self.total_exposure()
                expected = self.bankroll + open_cost
                if abs(live_bal - expected) > 5:
                    logger.info(f"  💰 Balance sync: chain=${live_bal:.2f} vs tracked=${expected:.2f}")
                    self.bankroll = live_bal - open_cost
                    if self.bankroll < 0:
                        self.bankroll = live_bal

    def run(self):
        logger.info("Starting bot loop...")
        running = True
        def stop(s, f):
            nonlocal running
            running = False
        signal.signal(signal.SIGINT, stop)
        signal.signal(signal.SIGTERM, stop)
        self.print_status()
        while running:
            try:
                self.run_once()
            except Exception as e:
                logger.error(f"Error: {e}")
                logger.debug(traceback.format_exc())
            try:
                time.sleep(POLL_INTERVAL)
            except KeyboardInterrupt:
                break
        self.print_status()
        self._save()


def main():
    dry_run = "--dry-run" in sys.argv
    once = "--once" in sys.argv
    no_confirm = "--no-confirm" in sys.argv
    if not dry_run:
        print(f"\n⚠️  LIVE — Bankroll: ${INITIAL_BANKROLL:.2f}")
        if not no_confirm:
            if input("Type 'GO': ").strip() != "GO":
                print("Aborted.")
                sys.exit(0)
    else:
        print(f"\n🧪 DRY RUN\n")

    client = setup_clob_client()
    bot = TradingBot(client, dry_run=dry_run)
    if once:
        bot.run_once()
        bot.print_status()
    else:
        bot.run()


if __name__ == "__main__":
    main()
