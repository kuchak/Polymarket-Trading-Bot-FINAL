# Movie Box Office Prediction System — Handoff Document

## What This Is
A machine learning system for predicting box office weekend grosses to inform betting on Polymarket/Kalshi markets. Built June 2026.

---

## Directory Structure
```
movie-predictor/
├── data/                          # All raw + processed data (15MB total)
│   ├── daily_by_date.csv          # CORE: 75,513 rows — daily gross for every film, Oct 2019–May 2026
│   ├── bom_movies.csv             # 1,400 films scraped from Box Office Mojo (genre, budget, MPAA, distributor)
│   ├── opening_weekends.csv       # 1,691 films — Thu preview + Fri + Sat + Sun opening weekend
│   ├── movies_merged.csv          # BOM + RT scores merged, feature-engineered, ready for training
│   └── rt_scores.csv              # Rotten Tomatoes critic + audience scores (Wayback Machine scraped)
│
├── models/                        # Trained models (3.8MB total)
│   ├── opening_weekend_model.json          # XGBoost: pre-release → opening weekend prediction
│   ├── feature_cols.json                   # Feature list for opening_weekend_model
│   ├── multiplier_fri_to_frisatsun.json    # XGBoost: Friday gross → Fri+Sat+Sun total
│   ├── multiplier_fri_to_frisatsun_features.json
│   ├── multiplier_sat_to_sun.json          # XGBoost: Saturday gross → Sunday gross
│   ├── multiplier_sat_to_sun_features.json
│   ├── multiplier_thu_to_full.json         # XGBoost: Thursday preview → full weekend
│   ├── multiplier_thu_to_full_features.json
│   ├── subsequent_weekend_model.json       # XGBoost: Wk2/3/4 prediction (BEST MODEL)
│   ├── subsequent_weekend_features.json    # Feature list for subsequent_weekend_model
│   └── model_report.txt                    # CV metrics + feature importances for opening model
│
├── scrape_boxofficemojo.py        # Scrapes BOM yearly charts 2020–2026 → data/bom_movies.csv
├── scrape_daily_chart.py          # Scrapes The Numbers daily charts → data/daily_by_date.csv
├── scrape_rt_wayback.py           # Scrapes RT scores via Wayback Machine → data/rt_scores.csv
├── merge_data.py                  # Joins BOM + RT, feature engineering → data/movies_merged.csv
├── train_model.py                 # Trains opening weekend XGBoost model
├── build_multiplier_model.py      # Trains Fri/Sat/Thu multiplier models
├── build_subsequent_weekend_model.py  # Trains Wk2/3/4 model (NEW — best accuracy)
└── predict.py                     # CLI inference tool for pre-release predictions
```

---

## The Two Use Cases

### Use Case 1: Pre-Release Prediction
**Question:** Before a movie opens, what will its opening weekend be?

**Script:** `predict.py`
**Model:** `models/opening_weekend_model.json`
**Input:** RT score, budget, genre, distributor, release date, MPAA rating, sequel flag
**Accuracy:** CV R²=0.558, MAE≈$8.3M (wide range — harder problem)

```bash
cd movie-predictor
python3 predict.py --title "Movie Name" --release-date 2026-07-04 \
  --rt-score 85 --budget 150000000 --mpaa PG-13 \
  --genre "Action, Adventure" --distributor "Universal Pictures" --sequel
```

### Use Case 2: In-Weekend Projection (MAIN BETTING TOOL)
**Question:** Given Friday actuals (and weekday data), what will the full Fri+Sat+Sun be?

**Two sub-models:**

**A) Opening Weekend (Week 1):**
- Model: `multiplier_fri_to_frisatsun.json`
- Input: Friday gross, Thursday preview, genre, MPAA, theaters, release month
- Once you have Friday: MAE≈$1.0M

**B) Subsequent Weekends (Weeks 2/3/4) — BEST MODEL:**
- Model: `subsequent_weekend_model.json`
- Accuracy: MAE $1.14M overall, improves to $0.76M by Week 4
- Input: Friday gross of current week + weekday Mon-Thu grosses + prior weekend total
- Key insight: pull all inputs live from The Numbers daily chart

---

## Data Sources

| Source | URL | What it provides |
|--------|-----|-----------------|
| The Numbers daily chart | `https://www.the-numbers.com/box-office-chart/daily/YYYY/MM/DD` | Daily gross for every film in release |
| The Numbers movie page | `https://www.the-numbers.com/movie/TITLE-(YEAR)` | Full daily breakdown per film |
| Box Office Mojo | `https://www.boxofficemojo.com/year/YYYY/` | Opening weekend, budget, genre, distributor |
| Rotten Tomatoes | `https://www.rottentomatoes.com/m/SLUG` | Live critic + audience scores |
| Wayback Machine CDX API | `http://web.archive.org/cdx/search/cdx` | Historical RT snapshots (pre-release scores) |

**Key fact:** The Numbers "Weekend Box Office Performance" = Fri+Sat+Sun ONLY. Thursday previews are NOT included in weekend totals despite how Polymarket sometimes words their market rules. Verified mathematically.

---

## Automation Ideas for Mac Mini

### 1. Weekly Data Refresh (Sunday night after weekend estimates post)
```bash
# Add to crontab: 0 23 * * 0 (11pm every Sunday)
cd "/path/to/movie-predictor"
python3 scrape_daily_chart.py   # Pulls new daily data, resumes from checkpoint
python3 build_subsequent_weekend_model.py  # Retrain with fresh data
```

### 2. Saturday Morning Live Projection
When Friday numbers post (~9-11am ET Saturday), run inference:
```bash
# After pulling Friday gross from The Numbers:
python3 predict_live.py --title "Movie Name" \
  --week 2 \
  --fri 11750000 \
  --weekday-avg 5500000 \
  --prior-weekend 29000000 \
  --theaters 3677 \
  --genre "Action" --mpaa PG-13
# (predict_live.py inference script still needs to be built for subsequent_weekend_model)
```

### 3. RT Score Monitor (check every 30min during embargo lift)
```bash
# Add to crontab: */30 * * * 3,4 (every 30min Wed+Thu)
python3 -c "
from predict import fetch_rt_scores
scores = fetch_rt_scores('Movie Title', 2026)
print(scores)
# Alert if score changes by >3 points
"
```

---

## Model Performance Summary

| Model | Use case | MAE | R² |
|-------|----------|-----|----|
| opening_weekend_model | Pre-release prediction | $8.3M | 0.558 |
| multiplier_fri_to_frisatsun | Wk1 Fri → weekend | $1.0M | ~0.85 |
| multiplier_sat_to_sun | Sat → Sunday | $0.4M | ~0.92 |
| **subsequent_weekend_model** | **Wk2/3/4 prediction** | **$1.14M** | **0.968** |

**Subsequent model by week:**
- Week 2 MAE: $1.50M
- Week 3 MAE: $0.88M
- Week 4 MAE: $0.76M

---

## What Still Needs to Be Built

1. **`predict_live.py`** — inference CLI for `subsequent_weekend_model.json`. Takes Friday gross + weekday avg + prior weekend total, outputs Fri+Sat+Sun prediction with confidence interval.

2. **Auto-scraper for live Friday data** — polls The Numbers movie page every 15min Saturday morning until Friday numbers post, then auto-runs projection.

3. **Polymarket market scanner** — scrapes open box office markets, compares bracket prices to model predictions, flags markets with >20% edge.

4. **RT pre-release score coverage** — only 2.1% of historical films have pre-release RT snapshots via Wayback Machine (heavily throttled). Options: pay for a data provider, scrape more slowly over weeks, or accept that pre-release model will use final RT scores as proxy.

---

## Dependencies
```bash
pip install requests beautifulsoup4 pandas numpy xgboost scikit-learn matplotlib
```
Python 3.11+ recommended.

---

## Key Lessons Learned

1. **The Numbers is the authoritative source** — BOM and The Numbers match dollar-for-dollar for weekend totals. Use The Numbers for daily data (better coverage).

2. **Holiday opening weekends corrupt multiplier ratios** — films that open on Memorial Day have inflated Fridays. Their Wk2 drop looks catastrophic (-79%) but the weekday multipliers overestimate recovery. Use the LR model (Fri + weekday_avg as features) not simple ratio × median.

3. **Competition matters** — when 2+ major films open the same weekend as a holdover, the holdover's Sat/Sun gets squeezed. The subsequent_weekend_model captures this via `competition_gross` feature.

4. **$3M Polymarket brackets require $1-2M MAE to have reliable edge** — the opening_weekend_model ($8.3M MAE) is not precise enough for bracket betting. The subsequent_weekend_model ($0.76-1.50M MAE) is.

5. **Faith-based films have inverted Sat/Sun patterns** — Sat > Fri, Sun ≈ Sat. These films build on word-of-mouth through church communities. Factor this in manually when you identify the film type.

6. **Don't capitulate to market commentary without checking data first.** The model is the model. Update it when data contradicts it, not when someone says so.
