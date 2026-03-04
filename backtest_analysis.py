#!/usr/bin/env python3
"""
Backtest v3: Determine outcomes from snapshot data directly.
Winner = outcome that reached 99%+ implied_prob
Loser = the other side of the same event
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

print("Pass 1: Scanning all snapshots to find outcomes and max probs...")
# Track per (event, outcome): max_prob, first qualifying entry, league
outcome_data = {}  # (event, outcome) -> {max_prob, first_entry, league, strategy, ...}
row_count = 0

with open(SNAPSHOT_FILE) as f:
    reader = csv.DictReader(f)
    for row in reader:
        row_count += 1
        if row_count % 500000 == 0:
            print(f"  ...{row_count/1000000:.1f}M rows")

        if row.get('market_type', '') != 'moneyline':
            continue
        outcome = row.get('outcome_name', '')
        if outcome in SKIP_OUTCOMES:
            continue
        league = row.get('league', '')
        strat = LEAGUE_TO_STRATEGY.get(league)
        if not strat:
            continue

        event = row.get('event_name', '')
        key = (event, outcome)

        try:
            prob = float(row.get('implied_prob', 0) or 0)
            liq = float(row.get('liquidity', 0) or 0)
            elapsed_str = (row.get('game_elapsed', '') or '').replace('m', '')
            elapsed = float(elapsed_str) if elapsed_str else 0
        except (ValueError, TypeError):
            continue

        # Track max prob for determining winner
        if key not in outcome_data:
            outcome_data[key] = {
                'max_prob': prob, 'league': league, 'strategy': strat,
                'event': event, 'outcome': outcome,
                'first_entry': None,  # first snapshot crossing threshold
            }
        else:
            if prob > outcome_data[key]['max_prob']:
                outcome_data[key]['max_prob'] = prob

        # Track first time crossing entry threshold
        cfg = STRATEGIES[strat]
        if (prob >= cfg['thresh'] and prob < 0.99
                and elapsed >= cfg['min_elapsed']
                and liq >= MIN_LIQUIDITY
                and outcome_data[key]['first_entry'] is None):
            outcome_data[key]['first_entry'] = {
                'entry_price': prob, 'liquidity': liq, 'elapsed': elapsed,
                'ts': row.get('timestamp', ''),
            }

print(f"  Scanned {row_count} rows")
print(f"  {len(outcome_data)} unique moneyline outcomes tracked")

# Determine winners: group by event, winner = highest max_prob
events = defaultdict(list)
for key, data in outcome_data.items():
    events[data['event']].append(data)

resolved = {}  # (event, outcome) -> won True/False
for event_name, outcomes in events.items():
    # An event is "resolved" if at least one outcome reached 99%+
    max_outcome = max(outcomes, key=lambda x: x['max_prob'])
    if max_outcome['max_prob'] >= 0.99:
        for o in outcomes:
            k = (o['event'], o['outcome'])
            resolved[k] = (o == max_outcome)  # True if winner

print(f"  {len(events)} unique events, {sum(1 for e in events.values() if max(o['max_prob'] for o in e) >= 0.99)} resolved (had 99%+ outcome)")

# Now build results: only include outcomes that had a qualifying entry AND are resolved
results = []
for key, data in outcome_data.items():
    if data['first_entry'] is None:
        continue
    if key not in resolved:
        continue
    won = resolved[key]
    entry = data['first_entry']
    results.append({
        'event': data['event'],
        'outcome': data['outcome'],
        'strategy': data['strategy'],
        'league': data['league'],
        'entry_price': entry['entry_price'],
        'liquidity': entry['liquidity'],
        'elapsed': entry['elapsed'],
        'won': won,
    })

if not results:
    print("\nNo qualifying trades found. Sample data:")
    samples = [(k, d) for k, d in outcome_data.items() if d['max_prob'] >= 0.99][:10]
    for k, d in samples:
        fe = d['first_entry']
        print(f"  {d['outcome'][:30]} | {d['strategy']} | max={d['max_prob']:.3f} | entry={'YES @ '+str(fe['entry_price']) if fe else 'NO'}")
    import sys; sys.exit()

wins = [r for r in results if r['won']]
losses = [r for r in results if not r['won']]

print(f"\n{'='*70}")
print(f"BACKTEST: {len(results)} trades ({len(wins)}W / {len(losses)}L = {len(wins)/len(results)*100:.1f}%)")
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
print("ALL LOSSES")
print(f"{'='*70}")
for r in sorted(losses, key=lambda x: -x['entry_price']):
    print(f"  {r['event'][:45]:45} | {r['outcome'][:20]:20} | {r['strategy']:10} | Entry: {r['entry_price']:.3f} | Liq: ${r['liquidity']:>8,.0f}")

print(f"\n{'='*70}")
print("THRESHOLD SENSITIVITY (per $10 bet, all sports)")
print(f"{'='*70}")
for thresh in [0.80, 0.83, 0.85, 0.88, 0.90, 0.92, 0.93, 0.95, 0.97]:
    filt = [r for r in results if r['entry_price'] >= thresh]
    if not filt: continue
    fw = len([r for r in filt if r['won']])
    fl = len(filt) - fw
    fp = sum(10*(0.99-r['entry_price'])/r['entry_price'] if r['won'] else -10 for r in filt)
    ln = [r['outcome'][:20] for r in filt if not r['won']]
    print(f"  >= {thresh:.0%}: {len(filt):>3} ({fw}W/{fl}L) WR:{fw/len(filt)*100:>5.1f}% PnL: ${fp:>+7.2f} | Losses: {', '.join(ln) if ln else 'NONE'}")

# Per-sport threshold sweep
print(f"\n{'='*70}")
print("PER-SPORT THRESHOLD SWEEP")
print(f"{'='*70}")
for strat in sorted(by_strat):
    rs = by_strat[strat]
    print(f"\n  {strat} ({len(rs)} total trades):")
    for thresh in [0.80, 0.83, 0.85, 0.88, 0.90, 0.92, 0.93, 0.95, 0.97]:
        filt = [r for r in rs if r['entry_price'] >= thresh]
        if not filt: continue
        fw = len([r for r in filt if r['won']])
        fl = len(filt) - fw
        fp = sum(10*(0.99-r['entry_price'])/r['entry_price'] if r['won'] else -10 for r in filt)
        print(f"    >= {thresh:.0%}: {len(filt):>3} ({fw}W/{fl}L) WR:{fw/len(filt)*100:>5.1f}% PnL: ${fp:>+7.2f}")

print(f"\n{'='*70}")
print("LIQUIDITY SENSITIVITY")
print(f"{'='*70}")
for ml in [5000, 10000, 20000, 50000, 100000]:
    filt = [r for r in results if r['liquidity'] >= ml]
    if not filt: continue
    fw = len([r for r in filt if r['won']])
    fl = len(filt) - fw
    fp = sum(10*(0.99-r['entry_price'])/r['entry_price'] if r['won'] else -10 for r in filt)
    print(f"  >= ${ml/1000:.0f}k: {len(filt):>3} ({fw}W/{fl}L) WR:{fw/len(filt)*100:>5.1f}% PnL: ${fp:>+7.2f}")

# Time-delay sensitivity for sports that use it
print(f"\n{'='*70}")
print("TIME DELAY SENSITIVITY (minutes elapsed at entry)")
print(f"{'='*70}")
for strat in ['ATP', 'WTA', 'NCAA_CBB']:
    if strat not in by_strat: continue
    # Re-scan with different min_elapsed values
    all_outcomes = [(k, d) for k, d in outcome_data.items()
                    if d['strategy'] == strat and k in resolved]
    print(f"\n  {strat}:")
    for min_el in [0, 15, 30, 45, 60, 90, 120]:
        cfg = STRATEGIES[strat]
        trades = []
        for key, data in all_outcomes:
            # Re-check: would this outcome have qualified with this min_elapsed?
            # We need to re-scan snapshots... too expensive.
            # Instead approximate: if first_entry exists AND elapsed >= min_el
            fe = data['first_entry']
            if fe and fe['elapsed'] >= min_el and fe['entry_price'] >= cfg['thresh']:
                trades.append({'won': resolved[key], 'entry_price': fe['entry_price']})
        if not trades: continue
        tw = len([t for t in trades if t['won']])
        tl = len(trades) - tw
        tp = sum(10*(0.99-t['entry_price'])/t['entry_price'] if t['won'] else -10 for t in trades)
        print(f"    >= {min_el:>3}m: {len(trades):>3} ({tw}W/{tl}L) WR:{tw/len(trades)*100:>5.1f}% PnL: ${tp:>+7.2f}")

print("\nDone.")
