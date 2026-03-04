#!/usr/bin/env python3
"""
Backtest v4: Smart resolution detection.
Winner = outcome with max_prob >= 0.95 where opponent dropped to <= 0.05
Uses implied_prob (Gamma) since CLOB prices are often missing.
"""
import csv
from collections import defaultdict

SNAPSHOT_FILE = "/Users/yy/polymarket/anthropic/polymarket-monitor/data/market_snapshots.csv"

LEAGUE_TO_STRATEGY = {
    'atp': 'ATP', 'wta': 'WTA', 'ncaa-cbb': 'NCAA_CBB', 'cwbb': 'CWBB',
    'nba-2026': 'NBA', 'nhl-2026': 'NHL', 'ahl-2026': 'NHL', 'khl-2026': 'NHL',
    'wtt-mens-singles': 'WTT_Men', 'wtt-womens-singles': 'WTT_Women',
}

STRATEGIES = {
    'ATP':       {'thresh': 0.88, 'min_elapsed': 45},
    'WTA':       {'thresh': 0.83, 'min_elapsed': 30},
    'NCAA_CBB':  {'thresh': 0.83, 'min_elapsed': 60},
    'CWBB':      {'thresh': 0.85, 'min_elapsed': 45},
    'NBA':       {'thresh': 0.88, 'min_elapsed': 0},
    'NHL':       {'thresh': 0.93, 'min_elapsed': 30},
    'WTT_Men':   {'thresh': 0.83, 'min_elapsed': 0},
    'WTT_Women': {'thresh': 0.83, 'min_elapsed': 0},
}

MIN_LIQUIDITY = 20000
SKIP_OUTCOMES = {'Over', 'Under', 'Over 2.5', 'Under 2.5', 'Over 1.5', 'Under 1.5',
                 'Over 3.5', 'Under 3.5', 'Draw', 'Tie', 'Yes', 'No'}

print("Pass 1: Scanning snapshots...")
# Track per (event, outcome): max_prob, min_prob, all qualifying entries
outcome_data = {}
row_count = 0

with open(SNAPSHOT_FILE) as f:
    reader = csv.DictReader(f)
    for row in reader:
        row_count += 1
        if row_count % 500000 == 0:
            print(f"  ...{row_count/1000000:.1f}M rows")

        if row.get('market_type') != 'moneyline': continue
        outcome = row.get('outcome_name', '')
        if outcome in SKIP_OUTCOMES: continue
        league = row.get('league', '')
        strat = LEAGUE_TO_STRATEGY.get(league)
        if not strat: continue

        event = row.get('event_name', '')
        key = (event, outcome)

        try:
            prob = float(row.get('implied_prob', 0) or 0)
            liq = float(row.get('liquidity', 0) or 0)
            elapsed_str = (row.get('game_elapsed', '') or '').replace('m', '')
            elapsed = float(elapsed_str) if elapsed_str else 0
        except (ValueError, TypeError):
            continue

        if key not in outcome_data:
            outcome_data[key] = {
                'max_prob': prob, 'min_prob': prob,
                'league': league, 'strategy': strat,
                'event': event, 'outcome': outcome,
                'entries': {},  # thresh -> first qualifying entry at that thresh
            }
        od = outcome_data[key]
        od['max_prob'] = max(od['max_prob'], prob)
        od['min_prob'] = min(od['min_prob'], prob)

        # Record first qualifying entry at various thresholds
        cfg = STRATEGIES[strat]
        if (prob < 0.98 and liq >= MIN_LIQUIDITY and elapsed >= cfg['min_elapsed']):
            for t in [0.80, 0.83, 0.85, 0.88, 0.90, 0.92, 0.93, 0.95, 0.97]:
                if prob >= t and t not in od['entries']:
                    od['entries'][t] = {
                        'entry_price': prob, 'liquidity': liq, 'elapsed': elapsed,
                    }

print(f"  Scanned {row_count} rows, {len(outcome_data)} outcomes tracked")

# Determine winners: group by event
events = defaultdict(list)
for key, data in outcome_data.items():
    events[data['event']].append(data)

# Resolution: event is resolved if highest outcome >= 0.95 AND lowest outcome <= 0.10
resolved = {}  # (event, outcome) -> won True/False
resolved_events = 0
for event_name, outs in events.items():
    if len(outs) < 2: continue
    best = max(outs, key=lambda x: x['max_prob'])
    worst = min(outs, key=lambda x: x['min_prob'])
    # Need clear winner: one side hit 95%+ and at least one side dropped to 10% or below
    if best['max_prob'] >= 0.95 and worst['min_prob'] <= 0.10:
        resolved_events += 1
        for o in outs:
            k = (o['event'], o['outcome'])
            resolved[k] = (o == best)

print(f"  {len(events)} events, {resolved_events} resolved")
won_count = sum(1 for v in resolved.values() if v)
lost_count = sum(1 for v in resolved.values() if not v)
print(f"  Resolved outcomes: {won_count} winners, {lost_count} losers")

# Build results at the CURRENT strategy thresholds
results = []
for key, data in outcome_data.items():
    if key not in resolved: continue
    won = resolved[key]
    strat = data['strategy']
    cfg = STRATEGIES[strat]
    entry = data['entries'].get(cfg['thresh'])
    if entry is None: continue
    results.append({
        'event': data['event'], 'outcome': data['outcome'],
        'strategy': strat, 'league': data['league'],
        'entry_price': entry['entry_price'],
        'liquidity': entry['liquidity'], 'elapsed': entry['elapsed'],
        'won': won, 'max_prob': data['max_prob'],
        'all_entries': data['entries'],
    })

wins = [r for r in results if r['won']]
losses = [r for r in results if not r['won']]

print(f"\n{'='*70}")
print(f"BACKTEST (current thresholds): {len(results)} trades ({len(wins)}W / {len(losses)}L = {len(wins)/len(results)*100:.1f}%)")
print(f"{'='*70}")

print(f"\n{'Sport':12} {'#':>4} {'W':>3} {'L':>3} {'WR':>6} {'AvgEntry':>9} {'$10 PnL':>8}")
print(f"{'-'*52}")
by_strat = defaultdict(list)
for r in results:
    by_strat[r['strategy']].append(r)
for strat in sorted(by_strat):
    rs = by_strat[strat]
    w = len([r for r in rs if r['won']])
    l = len(rs) - w
    ae = sum(r['entry_price'] for r in rs) / len(rs)
    pnl = sum(10*(0.99-r['entry_price'])/r['entry_price'] if r['won'] else -10 for r in rs)
    print(f"{strat:12} {len(rs):>4} {w:>3} {l:>3} {w/len(rs)*100:>5.1f}% {ae:>8.3f} ${pnl:>+7.2f}")
pnl_all = sum(10*(0.99-r['entry_price'])/r['entry_price'] if r['won'] else -10 for r in results)
print(f"{'TOTAL':12} {len(results):>4} {len(wins):>3} {len(losses):>3} {len(wins)/len(results)*100:>5.1f}% {'':>8} ${pnl_all:>+7.2f}")

print(f"\n{'='*70}")
print("ALL LOSSES (at current thresholds)")
print(f"{'='*70}")
for r in sorted(losses, key=lambda x: -x['entry_price']):
    print(f"  {r['event'][:45]:45} | {r['outcome'][:20]:20} | {r['strategy']:10} | Entry: {r['entry_price']:.3f} | Liq: ${r['liquidity']:>8,.0f}")

# GLOBAL threshold sweep (apply same threshold to all sports)
print(f"\n{'='*70}")
print("GLOBAL THRESHOLD SWEEP (per $10 bet)")
print(f"{'='*70}")
for thresh in [0.80, 0.83, 0.85, 0.88, 0.90, 0.92, 0.93, 0.95, 0.97]:
    trades = []
    for key, data in outcome_data.items():
        if key not in resolved: continue
        entry = data['entries'].get(thresh)
        if entry is None: continue
        trades.append({'won': resolved[key], 'ep': entry['entry_price'], 'strat': data['strategy']})
    if not trades: continue
    tw = len([t for t in trades if t['won']])
    tl = len(trades) - tw
    tp = sum(10*(0.99-t['ep'])/t['ep'] if t['won'] else -10 for t in trades)
    ln = [t['strat'] for t in trades if not t['won']]
    loss_summary = defaultdict(int)
    for l in ln: loss_summary[l] += 1
    ls = ', '.join(f"{k}:{v}" for k,v in sorted(loss_summary.items()))
    print(f"  >= {thresh:.0%}: {len(trades):>3} ({tw}W/{tl}L) WR:{tw/len(trades)*100:>5.1f}% PnL: ${tp:>+8.2f} | Loss sports: {ls if ls else 'NONE'}")

# PER-SPORT threshold sweep
print(f"\n{'='*70}")
print("PER-SPORT THRESHOLD SWEEP")
print(f"{'='*70}")
sport_outcomes = defaultdict(list)
for key, data in outcome_data.items():
    if key not in resolved: continue
    sport_outcomes[data['strategy']].append((key, data))

for strat in sorted(sport_outcomes):
    items = sport_outcomes[strat]
    print(f"\n  {strat} ({len(items)} resolved outcomes):")
    for thresh in [0.80, 0.83, 0.85, 0.88, 0.90, 0.92, 0.93, 0.95, 0.97]:
        trades = []
        for key, data in items:
            entry = data['entries'].get(thresh)
            if entry is None: continue
            trades.append({'won': resolved[key], 'ep': entry['entry_price'],
                          'outcome': data['outcome']})
        if not trades: continue
        tw = len([t for t in trades if t['won']])
        tl = len(trades) - tw
        tp = sum(10*(0.99-t['ep'])/t['ep'] if t['won'] else -10 for t in trades)
        ln = [t['outcome'][:18] for t in trades if not t['won']]
        print(f"    >= {thresh:.0%}: {len(trades):>3} ({tw}W/{tl}L) WR:{tw/len(trades)*100:>5.1f}% PnL: ${tp:>+8.2f} | {', '.join(ln[:3]) if ln else 'NONE'}")

# LIQUIDITY sweep
print(f"\n{'='*70}")
print("LIQUIDITY SENSITIVITY (at current thresholds)")
print(f"{'='*70}")
for ml in [5000, 10000, 20000, 50000, 100000, 200000]:
    # Rebuild with different liq threshold
    trades = []
    for key, data in outcome_data.items():
        if key not in resolved: continue
        strat = data['strategy']
        cfg = STRATEGIES[strat]
        # Check if any entry at this threshold had enough liquidity
        entry = data['entries'].get(cfg['thresh'])
        if entry and entry['liquidity'] >= ml:
            trades.append({'won': resolved[key], 'ep': entry['entry_price']})
    if not trades: continue
    tw = len([t for t in trades if t['won']])
    tl = len(trades) - tw
    tp = sum(10*(0.99-t['ep'])/t['ep'] if t['won'] else -10 for t in trades)
    print(f"  >= ${ml/1000:.0f}k: {len(trades):>3} ({tw}W/{tl}L) WR:{tw/len(trades)*100:>5.1f}% PnL: ${tp:>+8.2f}")

# OPTIMAL combo finder
print(f"\n{'='*70}")
print("OPTIMAL CONFIGURATION SEARCH")
print(f"{'='*70}")
best_roi = -999
best_config = None
for sport in sorted(sport_outcomes):
    items = sport_outcomes[sport]
    print(f"\n  {sport}:")
    sport_best_roi = -999
    sport_best = None
    for thresh in [0.80, 0.83, 0.85, 0.88, 0.90, 0.92, 0.93, 0.95, 0.97]:
        trades = []
        for key, data in items:
            entry = data['entries'].get(thresh)
            if entry is None: continue
            trades.append({'won': resolved[key], 'ep': entry['entry_price']})
        if len(trades) < 3: continue  # Need minimum sample
        tw = len([t for t in trades if t['won']])
        tl = len(trades) - tw
        tp = sum(10*(0.99-t['ep'])/t['ep'] if t['won'] else -10 for t in trades)
        roi = tp / (len(trades) * 10) * 100
        if roi > sport_best_roi:
            sport_best_roi = roi
            sport_best = (thresh, len(trades), tw, tl, tp, roi)
    if sport_best:
        t, n, w, l, p, r = sport_best
        print(f"    BEST: >= {t:.0%} | {n} trades ({w}W/{l}L) | PnL: ${p:>+.2f} | ROI: {r:+.1f}%")

print("\nDone.")
