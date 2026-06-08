"""
build_subsequent_weekend_model.py
==================================
XGBoost model for predicting 2nd / 3rd / 4th weekend box-office gross
(Fri + Sat + Sun total) using features known by Saturday morning of
the target weekend.

Outputs
-------
  models/subsequent_weekend_model.json   — trained XGBoost model
  models/subsequent_weekend_features.json — ordered feature list
"""

import json
import os
import warnings
from pathlib import Path

import numpy as np
import pandas as pd
from sklearn.model_selection import KFold
from sklearn.metrics import mean_absolute_error, r2_score

warnings.filterwarnings("ignore")

try:
    import xgboost as xgb
except ImportError:
    raise SystemExit("xgboost not installed — run: pip install xgboost")

# ── Paths ──────────────────────────────────────────────────────────────────────
BASE = Path(__file__).parent
DATA = BASE / "data"
MODELS = BASE / "models"
MODELS.mkdir(exist_ok=True)

DAILY_CSV = DATA / "daily_by_date.csv"
BOM_CSV   = DATA / "bom_movies.csv"

# ── Holiday weekend dates (US major holidays) — extend as needed ───────────────
HOLIDAY_WEEKENDS = {
    # Memorial Day Fridays
    "2020-05-22", "2021-05-28", "2022-05-27", "2023-05-26", "2024-05-24", "2025-05-23", "2026-05-22",
    # July 4th adjacent
    "2021-07-02", "2022-07-01", "2023-06-30", "2024-07-05", "2025-07-04", "2026-07-03",
    # Labor Day Fridays
    "2020-09-04", "2021-09-03", "2022-09-02", "2023-09-01", "2024-08-30", "2025-08-29",
    # Thanksgiving adjacent
    "2020-11-20", "2021-11-19", "2022-11-18", "2023-11-17", "2024-11-22", "2025-11-21",
    # Christmas / New Year
    "2020-12-25", "2021-12-24", "2022-12-23", "2023-12-22", "2024-12-27", "2025-12-26",
    "2020-12-31", "2021-12-31", "2022-12-30", "2023-12-29",
    # Presidents Day
    "2020-02-14", "2021-02-12", "2022-02-18", "2023-02-17", "2024-02-16", "2025-02-14",
    # Easter (Fri before)
    "2020-04-10", "2021-04-02", "2022-04-15", "2023-04-07", "2024-03-29", "2025-04-18",
    # MLK weekend
    "2020-01-17", "2021-01-15", "2022-01-14", "2023-01-13", "2024-01-12", "2025-01-17",
}
HOLIDAY_FRI = pd.to_datetime(list(HOLIDAY_WEEKENDS))


def is_holiday(date_series: pd.Series) -> pd.Series:
    """Flag Fridays that fall on or within 1 day of a known holiday weekend."""
    return date_series.isin(HOLIDAY_FRI).astype(int)


# ══════════════════════════════════════════════════════════════════════════════
# 1. Load & prepare daily data
# ══════════════════════════════════════════════════════════════════════════════
print("Loading daily data …")
daily = pd.read_csv(DAILY_CSV, parse_dates=["date"])
daily["gross"] = pd.to_numeric(daily["gross"], errors="coerce")
daily["theaters"] = pd.to_numeric(daily["theaters"], errors="coerce")
daily["total_gross_to_date"] = pd.to_numeric(daily["total_gross_to_date"], errors="coerce")

# Derive day-of-week (0=Mon … 4=Fri … 6=Sun)
daily["dow"] = daily["date"].dt.dayofweek

# Sort for per-film chronological work
daily = daily.sort_values(["title", "date"]).reset_index(drop=True)

print(f"  {len(daily):,} rows across {daily['title'].nunique():,} films")

# ── Load BOM metadata ──────────────────────────────────────────────────────────
print("Loading BOM metadata …")
bom = pd.read_csv(BOM_CSV)
bom["release_date"] = pd.to_datetime(bom["release_date"], errors="coerce")

# One-hot genre flags (top genres only)
TOP_GENRES = ["Action", "Comedy", "Drama", "Horror", "Animation",
              "Thriller", "Adventure", "Fantasy", "Sci-Fi", "Romance",
              "Crime", "Family", "Documentary"]
for g in TOP_GENRES:
    bom[f"genre_{g}"] = bom["genre"].fillna("").str.contains(g, case=False).astype(int)

# Ordinal MPAA
mpaa_order = {"G": 0, "PG": 1, "PG-13": 2, "R": 3, "NC-17": 4}
bom["mpaa_ordinal"] = bom["mpaa_rating"].map(mpaa_order).fillna(2)

BOM_COLS = (["title", "release_date", "mpaa_ordinal", "budget"]
            + [f"genre_{g}" for g in TOP_GENRES])
bom_slim = bom[BOM_COLS].drop_duplicates("title")

# ── Precompute total daily gross per date (for competition signal) ─────────────
print("Computing competition gross per date …")
date_total = daily.groupby("date")["gross"].sum().rename("total_gross_that_day")
daily = daily.merge(date_total, on="date", how="left")
daily["competition_gross"] = daily["total_gross_that_day"] - daily["gross"].fillna(0)

# ══════════════════════════════════════════════════════════════════════════════
# 2. Build one row per (film, weekend_number) for weeks 2, 3, 4
# ══════════════════════════════════════════════════════════════════════════════
print("Building training rows …")

def build_rows(film_df: pd.DataFrame) -> list[dict]:
    """
    Given the full daily history for one film (sorted by date),
    extract feature rows for weekend 2, 3, 4.

    We define weekend N as the Fri/Sat/Sun cluster with
    days_in_release in [(7N-6), (7N)].  More precisely we
    look at actual calendar DOW: find all Fridays in the
    film's history and label them week 1, 2, 3, …
    """
    film_df = film_df.sort_values("date").copy()
    title = film_df["title"].iloc[0]

    # Identify Fridays
    fridays = film_df[film_df["dow"] == 4].copy()
    if len(fridays) < 2:
        return []

    fridays = fridays.reset_index(drop=True)

    rows = []
    for wk_idx in range(1, len(fridays)):       # week index 0-based; wk_idx>=1 → wk2+
        week_num = wk_idx + 1                    # 2, 3, 4 …
        if week_num > 4:
            break

        cur_fri_row   = fridays.iloc[wk_idx]
        prior_fri_row = fridays.iloc[wk_idx - 1]

        cur_fri_date   = cur_fri_row["date"]
        prior_fri_date = prior_fri_row["date"]

        # ── Sanity: Fridays should be ~7 days apart ──────────────────────────
        gap = (cur_fri_date - prior_fri_date).days
        if gap < 5 or gap > 10:
            # skip if scheduling is weird (re-releases etc.)
            continue

        # ── Current weekend (Fri/Sat/Sun) ────────────────────────────────────
        cur_weekend = film_df[
            (film_df["date"] >= cur_fri_date) &
            (film_df["date"] <= cur_fri_date + pd.Timedelta(days=2))
        ]
        fri_gross = cur_weekend[cur_weekend["dow"] == 4]["gross"].sum()
        sat_gross = cur_weekend[cur_weekend["dow"] == 5]["gross"].sum()
        sun_gross = cur_weekend[cur_weekend["dow"] == 6]["gross"].sum()
        fri_sat_sun_total = fri_gross + sat_gross + sun_gross

        if fri_gross < 1_000_000:
            # Skip limited releases — ratios blow up
            continue
        if fri_sat_sun_total == 0:
            continue

        # ── Prior weekend ─────────────────────────────────────────────────────
        prior_weekend = film_df[
            (film_df["date"] >= prior_fri_date) &
            (film_df["date"] <= prior_fri_date + pd.Timedelta(days=2))
        ]
        prior_fri_gross = prior_weekend[prior_weekend["dow"] == 4]["gross"].sum()
        prior_sat_gross = prior_weekend[prior_weekend["dow"] == 5]["gross"].sum()
        prior_sun_gross = prior_weekend[prior_weekend["dow"] == 6]["gross"].sum()
        prior_weekend_total = prior_fri_gross + prior_sat_gross + prior_sun_gross

        if prior_fri_gross == 0:
            continue

        # ── Weekday avg (Mon–Thu between prior Fri and current Fri) ───────────
        weekdays = film_df[
            (film_df["date"] > prior_fri_date) &
            (film_df["date"] < cur_fri_date) &
            (film_df["dow"].isin([0, 1, 2, 3]))
        ]
        weekday_avg = weekdays["gross"].mean() if len(weekdays) > 0 else 0.0

        # ── Thursday preview before this weekend ─────────────────────────────
        thu_row = film_df[film_df["date"] == cur_fri_date - pd.Timedelta(days=1)]
        thu_gross = thu_row["gross"].sum() if len(thu_row) > 0 else 0.0
        thu_fri_ratio = (thu_gross / fri_gross) if fri_gross > 0 else 0.0

        # ── Cumulative gross before this weekend ──────────────────────────────
        cum_before = film_df[film_df["date"] < cur_fri_date]["gross"].sum()

        # ── Opening weekend (first Fri+Sat+Sun) ───────────────────────────────
        open_fri_date = fridays.iloc[0]["date"]
        open_weekend = film_df[
            (film_df["date"] >= open_fri_date) &
            (film_df["date"] <= open_fri_date + pd.Timedelta(days=2))
        ]
        opening_total = open_weekend["gross"].sum()

        cum_as_pct_opening = (cum_before / opening_total) if opening_total > 0 else 0.0

        # ── Per-theater Friday ────────────────────────────────────────────────
        theaters = cur_fri_row["theaters"] if cur_fri_row["theaters"] > 0 else np.nan
        per_theater_fri = (fri_gross / theaters) if pd.notna(theaters) and theaters > 0 else 0.0

        # ── Competition ───────────────────────────────────────────────────────
        competition_gross = cur_fri_row["competition_gross"]

        # ── Holiday flags ─────────────────────────────────────────────────────
        is_holiday_wknd = int(cur_fri_date in HOLIDAY_FRI)
        prior_was_holiday = int(prior_fri_date in HOLIDAY_FRI)

        # ── Misc ──────────────────────────────────────────────────────────────
        release_month = cur_fri_date.month
        days_since_release = (cur_fri_date - film_df["date"].min()).days

        # ── Drop pct ──────────────────────────────────────────────────────────
        fri_drop_pct = (fri_gross - prior_fri_gross) / prior_fri_gross  # negative = drop

        rows.append({
            "title": title,
            "week_num": week_num,
            "cur_fri_date": cur_fri_date,
            # Target
            "fri_sat_sun_total": fri_sat_sun_total,
            # Features
            "fri_gross": fri_gross,
            "fri_drop_pct": fri_drop_pct,
            "prior_weekend_total": prior_weekend_total,
            "weekday_avg": weekday_avg,
            "thu_gross": thu_gross,
            "thu_fri_ratio": thu_fri_ratio,
            "log_cumulative_gross": np.log1p(cum_before),
            "cumulative_as_pct_opening": cum_as_pct_opening,
            "per_theater_fri": per_theater_fri,
            "competition_gross": competition_gross,
            "is_holiday_weekend": is_holiday_wknd,
            "prior_was_holiday": prior_was_holiday,
            "log_theaters": np.log1p(theaters) if pd.notna(theaters) else 0.0,
            "release_month": release_month,
            "days_since_release": days_since_release,
        })

    return rows


all_rows = []
for title, grp in daily.groupby("title"):
    all_rows.extend(build_rows(grp))

df = pd.DataFrame(all_rows)
print(f"  Built {len(df):,} training rows "
      f"(wk2={len(df[df.week_num==2])}, wk3={len(df[df.week_num==3])}, wk4={len(df[df.week_num==4])})")

# ── Merge BOM metadata ─────────────────────────────────────────────────────────
df = df.merge(bom_slim, on="title", how="left")

GENRE_COLS = [f"genre_{g}" for g in TOP_GENRES]
for c in GENRE_COLS + ["mpaa_ordinal"]:
    df[c] = df[c].fillna(0)

# ── Baseline: simple weekday_avg × 4.35 ───────────────────────────────────────
df["baseline_pred"] = df["weekday_avg"] * 4.35

# ══════════════════════════════════════════════════════════════════════════════
# 3. Feature matrix
# ══════════════════════════════════════════════════════════════════════════════
FEATURE_COLS = [
    "week_num",
    "fri_gross",
    "fri_drop_pct",
    "prior_weekend_total",
    "weekday_avg",
    "thu_gross",
    "thu_fri_ratio",
    "log_cumulative_gross",
    "cumulative_as_pct_opening",
    "per_theater_fri",
    "competition_gross",
    "is_holiday_weekend",
    "prior_was_holiday",
    "log_theaters",
    "release_month",
    "days_since_release",
    "mpaa_ordinal",
] + GENRE_COLS

TARGET = "fri_sat_sun_total"

# Drop rows where we're missing the target or key features
df_clean = df.dropna(subset=[TARGET, "fri_gross", "weekday_avg"]).copy()
for c in FEATURE_COLS:
    df_clean[c] = df_clean[c].fillna(0)

print(f"  Clean rows for modeling: {len(df_clean):,}")

X = df_clean[FEATURE_COLS].values
y = df_clean[TARGET].values
y_baseline = df_clean["baseline_pred"].values

# ══════════════════════════════════════════════════════════════════════════════
# 4. 5-fold CV
# ══════════════════════════════════════════════════════════════════════════════
print("\nRunning 5-fold cross-validation …")

params = dict(
    n_estimators=600,
    learning_rate=0.05,
    max_depth=5,
    subsample=0.8,
    colsample_bytree=0.7,
    min_child_weight=5,
    reg_alpha=0.1,
    reg_lambda=1.0,
    random_state=42,
    tree_method="hist",
    verbosity=0,
)

kf = KFold(n_splits=5, shuffle=True, random_state=42)

mae_xgb_folds, mae_base_folds = [], []
r2_xgb_folds, r2_base_folds   = [], []

oof_preds = np.zeros(len(df_clean))

for fold, (tr_idx, va_idx) in enumerate(kf.split(X), 1):
    X_tr, X_va = X[tr_idx], X[va_idx]
    y_tr, y_va = y[tr_idx], y[va_idx]

    model = xgb.XGBRegressor(**params)
    model.fit(X_tr, y_tr,
              eval_set=[(X_va, y_va)],
              verbose=False)

    preds = model.predict(X_va)
    oof_preds[va_idx] = preds

    mae_xgb_folds.append(mean_absolute_error(y_va, preds))
    r2_xgb_folds.append(r2_score(y_va, preds))
    mae_base_folds.append(mean_absolute_error(y_va, y_baseline[va_idx]))
    r2_base_folds.append(r2_score(y_va, y_baseline[va_idx]))
    print(f"  Fold {fold}  XGB MAE=${mae_xgb_folds[-1]/1e6:.2f}M  R²={r2_xgb_folds[-1]:.3f} "
          f"| Baseline MAE=${mae_base_folds[-1]/1e6:.2f}M  R²={r2_base_folds[-1]:.3f}")

print(f"\n{'─'*65}")
print(f"  Overall XGB   MAE = ${np.mean(mae_xgb_folds)/1e6:.2f}M  R² = {np.mean(r2_xgb_folds):.3f}")
print(f"  Baseline      MAE = ${np.mean(mae_base_folds)/1e6:.2f}M  R² = {np.mean(r2_base_folds):.3f}")
print(f"  Improvement         MAE reduced by ${(np.mean(mae_base_folds)-np.mean(mae_xgb_folds))/1e6:.2f}M "
      f"({100*(1-np.mean(mae_xgb_folds)/np.mean(mae_base_folds)):.0f}%)")

# ── By week number ─────────────────────────────────────────────────────────────
print(f"\n{'─'*65}")
print("  Per-week breakdown:")
for wk in [2, 3, 4]:
    mask = df_clean["week_num"].values == wk
    if mask.sum() < 10:
        continue
    mae_w = mean_absolute_error(y[mask], oof_preds[mask])
    r2_w  = r2_score(y[mask], oof_preds[mask])
    mae_b = mean_absolute_error(y[mask], y_baseline[mask])
    print(f"    Week {wk}  n={mask.sum():4d}  XGB MAE=${mae_w/1e6:.2f}M  R²={r2_w:.3f}  "
          f"| Baseline MAE=${mae_b/1e6:.2f}M")

# ══════════════════════════════════════════════════════════════════════════════
# 5. Train final model on all data & save
# ══════════════════════════════════════════════════════════════════════════════
print("\nTraining final model on full dataset …")
final_model = xgb.XGBRegressor(**params)
final_model.fit(X, y, verbose=False)
final_model.save_model(str(MODELS / "subsequent_weekend_model.json"))

with open(MODELS / "subsequent_weekend_features.json", "w") as f:
    json.dump(FEATURE_COLS, f, indent=2)

print(f"  Saved model → {MODELS / 'subsequent_weekend_model.json'}")
print(f"  Saved features → {MODELS / 'subsequent_weekend_features.json'}")

# ══════════════════════════════════════════════════════════════════════════════
# 6. Feature importances
# ══════════════════════════════════════════════════════════════════════════════
print(f"\n{'─'*65}")
print("Feature importances (gain):")
importances = final_model.get_booster().get_score(importance_type="gain")
imp_df = (pd.DataFrame({"feature": FEATURE_COLS,
                         "importance": [importances.get(f"f{i}", 0)
                                        for i in range(len(FEATURE_COLS))]})
          .sort_values("importance", ascending=False))
for _, row in imp_df.iterrows():
    bar = "█" * int(row["importance"] / imp_df["importance"].max() * 30)
    print(f"  {row['feature']:35s} {bar}")

# ══════════════════════════════════════════════════════════════════════════════
# 7. Sanity checks on known films
# ══════════════════════════════════════════════════════════════════════════════
print(f"\n{'─'*65}")
print("Sanity checks on known films:")

sanity_cases = [
    ("Backrooms",  2),
    ("Obsession",  4),
    ("Obsession",  3),
    ("Obsession",  2),
]

for check_title, check_wk in sanity_cases:
    mask = (df_clean["title"] == check_title) & (df_clean["week_num"] == check_wk)
    sub = df_clean[mask]
    if len(sub) == 0:
        print(f"  {check_title} Wk{check_wk}: no data found")
        continue
    row = sub.iloc[0]
    x_row = np.array([[row[c] for c in FEATURE_COLS]])
    pred = final_model.predict(x_row)[0]
    actual = row[TARGET]
    base = row["baseline_pred"]
    print(f"  {check_title} Wk{check_wk}:")
    print(f"    Actual   = ${actual/1e6:.2f}M")
    print(f"    XGB pred = ${pred/1e6:.2f}M   (error ${abs(pred-actual)/1e6:.2f}M)")
    print(f"    Baseline = ${base/1e6:.2f}M   (error ${abs(base-actual)/1e6:.2f}M)")
    print(f"    Key features: fri_gross=${row['fri_gross']/1e6:.2f}M, "
          f"fri_drop_pct={row['fri_drop_pct']:.1%}, "
          f"weekday_avg=${row['weekday_avg']/1e6:.2f}M")

print("\nDone.")
