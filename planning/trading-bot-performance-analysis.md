# Trading Bot Performance Analysis — March 4, 2026

## Overall Stats (102 unique trades, deduplicated from 52 log files)

| Metric | Value |
|--------|-------|
| Total closed trades | 102 |
| Wins / Losses | 96W / 6L |
| Win rate | 94.1% |
| PnL from wins | +$198.32 |
| PnL from losses | -$188.24 |
| **Net PnL** | **+$10.09** |
| Avg win | +$2.07 |
| Avg loss | -$31.37 |
| Active span | Feb 26 - Mar 4 (5d 16h) |
| Starting bankroll | ~$360 |
| Lowest bankroll | $14.20 (Feb 28) — 96% drawdown |
| Current bankroll | $296.66 + $48.99 open = $345.65 |

## Per-Sport Breakdown

| Sport | W | L | Win% | Total PnL | Avg Win | Avg Loss | Biggest Loss |
|-------|---|---|------|-----------|---------|----------|--------------|
| ATP | 23 | 3 | 88.5% | -$14.55 | +$2.46 | -$23.68 | -$59.54 |
| WTA | 19 | 0 | 100% | +$34.81 | +$1.83 | — | — |
| NCAA_CBB | 28 | 2 | 93.3% | -$53.52 | +$1.52 | -$48.01 | -$59.31 |
| NBA | 24 | 1 | 96.0% | +$31.43 | +$2.19 | -$21.17 | -$21.17 |
| TableTennis | 1 | 0 | 100% | +$8.48 | +$8.48 | — | — |
| Tennis (v5) | 1 | 0 | 100% | +$3.44 | +$3.44 | — | — |

## All 6 Losses

| # | Event | Sport | Entry | Exit | Cost | PnL | Scale-in | Date |
|---|-------|-------|-------|------|------|-----|----------|------|
| 1 | Kecmanovic vs Cobolli (Mexican Open) | ATP | 0.89 | 0.01 | $7.06 | -$6.98 | No | Feb 28 |
| 2 | Nakashima vs Tiafoe (Mexican Open) | ATP | 0.92 | 0.015 | $4.60 | -$4.53 | No | Feb 28 |
| 3 | Acosta vs Guillen Meza (Tigre 2) | ATP | 0.916 | 0.09 | $66.03 | -$59.54 | Yes (1x) | Feb 28 |
| 4 | Iowa Hawkeyes vs Penn State | NCAA_CBB | 0.90 | 0.01 | $37.13 | -$36.71 | No | Feb 28 |
| 5 | NC State vs Notre Dame | NCAA_CBB | 0.858 | 0.05 | $62.98 | -$59.31 | Yes (1x) | Feb 28 |
| 6 | Bucks vs Bulls | NBA | 0.89 | 0.035 | $22.03 | -$21.17 | No | Mar 1 |

## Scale-In Analysis

| Metric | Scaled (8) | Non-Scaled (94) |
|--------|-----------|-----------------|
| Win rate | 75.0% | 95.7% |
| Total PnL | -$94.69 | +$104.78 |

The 2 scaled losses (#3 and #5) accounted for 63% of all losses (-$118.85 of -$188.24).

## Entry Price Distribution

| | Min | Median | Max | Mean |
|---|-----|--------|-----|------|
| Wins (96) | 0.767 | 0.940 | 0.970 | 0.924 |
| Losses (6) | 0.858 | 0.900 | 0.920 | 0.896 |

## Key Findings

1. **Win/loss asymmetry is extreme**: One loss erases 15 wins. Net PnL is barely positive despite 94% win rate.
2. **Scale-in is destructive**: Scaled trades have 75% win rate and -$94.69 PnL vs non-scaled at 95.7% and +$104.78.
3. **ATP is net negative** (-$14.55) due to 3 losses in low-tier tournaments (Mexican Open 250, Tigre 2 Challenger).
4. **NCAA_CBB is worst performer** (-$53.52) due to 2 large losses with low entry prices (0.858 and 0.90).
5. **WTA is the best performer** (+$34.81, 19-0) — most consistent sport.
6. **NBA is strong** (+$31.43, 24W/1L) — only loss was Bucks at 0.89 entry.
7. **All 6 losses clustered in 2 days** (Feb 28 - Mar 1) causing 96% drawdown.
8. **No losses since Mar 1** — v6 thresholds are working but sample is small.

## Changes Applied (March 4, 2026)

### Entry Thresholds
| Sport | Before | After |
|-------|--------|-------|
| ATP | 0.93 | 0.94 |
| NCAA_CBB | 0.92 | 0.93 |
| CWBB | 0.85 | 0.90 |
| NBA | 0.88 | 0.91 |
| WTT_Women | 0.83 | 0.88 |
| WTT_Men | 0.83 | 0.88 |

### Stop-Loss
- Before: sell when prob <= 0.10 (10%)
- After: sell when prob <= 0.40 (40%)
- Rationale: markets like tennis fluctuate but still resolve the same way; 40% catches losses earlier without being triggered by normal volatility

### Liquidity
- Before: MIN_LIQUIDITY = $20,000
- After: MIN_LIQUIDITY = $50,000
- Rationale: filters out thin Challenger/ITF markets where all 3 ATP losses occurred
