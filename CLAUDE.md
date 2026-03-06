# CLAUDE.md

This file provides guidance for AI assistants working with this repository.

## Repository Overview

- **Name**: anthropic
- **Owner**: kuchak
- **Status**: New project (initial setup)

## Project Structure

This repository is in its initial state. As the project grows, document the directory layout here:

```
/
├── CLAUDE.md          # AI assistant guidance (this file)
├── planning/          # Plans, strategies, and roadmaps (.md files)
└── (project files)    # To be added
```

## Development Setup

### Prerequisites

<!-- Update with actual requirements as the project develops -->
- Git

### Getting Started

```bash
git clone <repository-url>
cd anthropic
# Add setup steps as project develops (e.g., dependency installation)
```

## Build & Run

<!-- Update these sections as build tooling is added -->

| Task    | Command |
| ------- | ------- |
| Build   | TBD     |
| Test    | TBD     |
| Lint    | TBD     |
| Format  | TBD     |

## Testing

<!-- Document testing patterns once a test framework is adopted -->
- Test framework: TBD
- Test location: TBD
- Run all tests: TBD
- Run a single test: TBD

## Code Conventions

<!-- Update as the team establishes conventions -->
- Follow consistent formatting (configure a formatter once the language/framework is chosen)
- Write clear commit messages describing the "why" not just the "what"
- Keep changes focused — one logical change per commit

## Architecture

<!-- Document key architectural decisions and patterns as they are made -->

No architecture decisions have been recorded yet. Update this section as the project takes shape.

## Key Files

<!-- List important files and their purposes as they are created -->

| File        | Purpose                                  |
| ----------- | ---------------------------------------- |
| CLAUDE.md   | AI assistant guidance                    |
| planning/   | Plans, strategies, and roadmap documents |

## Planning & Strategy Documents

All planning documents, strategy write-ups, and architectural plans live in the `./planning/` folder as Markdown files.

- When the user asks for a plan, strategy, roadmap, or any forward-looking document, save it as a `.md` file in `./planning/`
- Use descriptive filenames with kebab-case (e.g., `api-migration-plan.md`, `q2-growth-strategy.md`)
- If the `./planning/` folder doesn't exist yet, create it before writing the document

## Bot Components

| Bot | Script | Purpose |
|-----|--------|---------|
| Monitor | `polymarket_monitor.py` | Market data collection (30s cycles) |
| Trader | `polymarket_trader.py` | Live sports trading bot |
| Whale Tracker | `whale-tracker/whale_tracker.py` | Whale & insider trade alerts (60s cycles) |
| Copy Trade | `copy-trade-monitor/copy_trade_monitor.py` | Leaderboard copy-trade tracker |

### Running Bots

```bash
# Monitor
nohup python3 polymarket_monitor.py > nohup.out 2>&1 &

# Trader (--no-confirm for background mode)
nohup python3 polymarket_trader.py --no-confirm > trader_output.log 2>&1 &

# Whale Tracker
cd whale-tracker && nohup python3 whale_tracker.py > ../whale.log 2>&1 &
```

## Trader Bot Parameters (as of March 4, 2026)

### Risk Controls
| Parameter | Value |
|-----------|-------|
| MIN_LIQUIDITY | $50,000 |
| MAX_TOTAL_EXPOSURE | 100% |
| MAX_PER_MARKET | 20% |
| DEFAULT_BET | 15% |
| Stop-loss | Sell when prob <= 40% |

### Entry Thresholds
| Sport | Threshold | Min Elapsed | Hist Winrate |
|-------|-----------|-------------|-------------|
| ATP | 94% | 45 min | 95.8% |
| WTA | 92% | 30 min | 94.1% |
| NCAA_CBB | 93% | 60 min | 96.4% |
| CWBB | 90% | 45 min | 94.7% |
| NBA | 91% | 0 min | 100% |
| NHL | 93% | 30 min | 90% |
| WTT_Women | 88% | 0 min | 100% |
| WTT_Men | 88% | 0 min | 100% |

### Performance (102 trades, Feb 26 - Mar 4, 2026)
- 96W/6L (94.1%), net PnL +$10.09
- Best: WTA (+$34.81, 19-0), NBA (+$31.43, 24-1)
- Worst: NCAA_CBB (-$53.52, 28-2), ATP (-$14.55, 23-3)
- Full analysis: `planning/trading-bot-performance-analysis.md`

## Changelog

### 2026-03-04: Parameter Optimization v7
- Entry thresholds raised: ATP 93%→94%, NCAA_CBB 92%→93%, CWBB 85%→90%, NBA 88%→91%, WTT 83%→88%
- Stop-loss: sell at prob <= 40% (was 10%) — catches losses earlier
- MIN_LIQUIDITY: $20k→$50k — filters thin Challenger/ITF markets
- Scale-in: remains disabled
- Added `--no-confirm` flag to skip interactive GO prompt for background mode

## Notes for AI Assistants

- This is a new repository — verify what files exist before assuming project structure
- When adding new tooling or frameworks, update this CLAUDE.md with relevant commands and conventions
- Always read existing code before proposing modifications
- Prefer minimal, focused changes over large refactors
