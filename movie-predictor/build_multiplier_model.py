"""
Build a multiplier model for in-weekend box office projection.

Instead of a fixed Fri→Weekend multiplier, we train a regression that takes:
  - Thursday preview size (% of Friday = front-loading signal)
  - Genre flags (family, horror, action, animation, etc.)
  - MPAA rating
  - Release month / holiday weekend
  - Friday gross tier (log scale)

Outputs three separate models:
  1. Thu-only → full weekend (very rough, high uncertainty)
  2. Fri → Sat+Sun (main pre-Saturday model)
  3. Fri+Sat → Sun (tightest, Saturday morning use)

Also produces a live projection function used by predict_live.py
"""

import pandas as pd
import numpy as np
import json
import os
from xgboost import XGBRegressor
from sklearn.model_selection import KFold
from sklearn.metrics import mean_absolute_error, r2_score
import warnings
warnings.filterwarnings("ignore")

os.makedirs("models", exist_ok=True)

TOP_GENRES = ["Action","Adventure","Animation","Comedy","Crime",
              "Drama","Family","Fantasy","Horror","Musical","Romance","Sci-Fi","Thriller"]
MPAA_ORDER = {"G":1,"PG":2,"PG-13":3,"R":4,"NC-17":5,"NR":3,"UR":3}
HOLIDAY_MONTHS = {5,7,11,12,1,2,3}  # months with major US holidays


def build_features(df: pd.DataFrame) -> pd.DataFrame:
    """Engineer features for multiplier prediction."""
    X = pd.DataFrame()

    # Log Friday gross (scale signal)
    X["log_fri"] = np.log1p(df["fri"].fillna(0))

    # Thursday preview ratio (front-loading signal)
    # High thu/fri ratio = heavily front-loaded franchise = lower multiplier
    X["thu_fri_ratio"] = (df["thu_preview"].fillna(0) / df["fri"].replace(0, np.nan)).fillna(0)
    X["has_thu_preview"] = (df["thu_preview"].fillna(0) > 0).astype(int)

    # MPAA ordinal
    X["mpaa_ordinal"] = df["mpaa_rating"].map(MPAA_ORDER).fillna(3)

    # Release month
    X["release_month"] = pd.to_datetime(df["release_date"], errors="coerce").dt.month.fillna(6)
    X["is_summer"] = X["release_month"].isin([5,6,7,8]).astype(int)
    X["is_holiday"] = X["release_month"].isin(HOLIDAY_MONTHS).astype(int)
    X["is_december"] = (X["release_month"] == 12).astype(int)

    # Genre flags (multi-hot — handles Family+Action overlap)
    for g in TOP_GENRES:
        X[f"genre_{g}"] = df["genre"].str.contains(g, na=False).astype(int)

    # Opening theater count (wide vs limited)
    theaters_col = df["opening_theaters"] if "opening_theaters" in df.columns else df.get("max_theaters", pd.Series(0, index=df.index))
    X["log_theaters"] = np.log1p(theaters_col.fillna(df.get("max_theaters", pd.Series(0, index=df.index))).fillna(0))

    return X


def cross_val(model, X, y, label):
    kf = KFold(n_splits=5, shuffle=True, random_state=42)
    maes, r2s = [], []
    for train_idx, val_idx in kf.split(X):
        model.fit(X.iloc[train_idx], y.iloc[train_idx])
        preds = model.predict(X.iloc[val_idx])
        maes.append(mean_absolute_error(y.iloc[val_idx], preds))
        r2s.append(r2_score(y.iloc[val_idx], preds))
    print(f"  {label}: MAE={np.mean(maes):.4f} R²={np.mean(r2s):.3f} (n={len(y)})")
    return np.mean(maes), np.mean(r2s)


def train_multiplier_model(df: pd.DataFrame, target_col: str, model_name: str):
    """Train and save a multiplier prediction model."""
    valid = df[df[target_col].notna() & df["fri"].notna() & (df["fri"] > 10000)].copy()
    print(f"\n{model_name}: {len(valid)} training rows")

    X = build_features(valid)
    y = valid[target_col]

    # Remove extreme outliers (>5 std from mean — usually data errors)
    z = (y - y.mean()) / y.std()
    mask = z.abs() < 4
    X, y = X[mask], y[mask]
    print(f"  After outlier removal: {len(y)} rows")
    print(f"  Target range: {y.min():.2f}x – {y.max():.2f}x (median {y.median():.2f}x)")

    model = XGBRegressor(
        n_estimators=300, max_depth=4, learning_rate=0.05,
        subsample=0.8, colsample_bytree=0.8, min_child_weight=3,
        reg_alpha=0.1, reg_lambda=1.0, random_state=42, n_jobs=-1, verbosity=0
    )

    cross_val(model, X, y, "5-fold CV")

    # Train final on all data
    model.fit(X, y)

    # Feature importance
    importance = pd.Series(model.feature_importances_, index=X.columns).sort_values(ascending=False)
    print(f"  Top 8 features:")
    for feat, imp in importance.head(8).items():
        print(f"    {feat:30s}: {imp:.4f}")

    path = f"models/multiplier_{model_name}.json"
    model.save_model(path)
    with open(f"models/multiplier_{model_name}_features.json", "w") as f:
        json.dump(list(X.columns), f)
    print(f"  Saved → {path}")

    return model, list(X.columns)


def main():
    # Load opening weekends from date-based chart (better coverage)
    import re
    df = pd.read_csv("data/opening_weekends.csv")
    bom = pd.read_csv("data/bom_movies.csv")

    def normalize(t):
        t = str(t).lower().strip()
        t = re.sub(r"[^a-z0-9 ]", " ", t)
        return re.sub(r"\s+", " ", t).strip()

    df["title_norm"] = df["title"].apply(normalize)
    bom["title_norm"] = bom["title"].apply(normalize)

    # Merge to get genre, mpaa, release_date from BOM
    df = df.merge(
        bom[["title_norm","genre","mpaa_rating","max_theaters","release_date"]],
        on="title_norm", how="left"
    )
    # Use opening_date as release_date if BOM release_date missing
    df["release_date"] = df["release_date"].fillna(df["opening_date"])

    # Rename opening_theaters from opening_weekends if present
    if "opening_theaters" in df.columns and "max_theaters" in df.columns:
        df["max_theaters"] = df["max_theaters"].fillna(df["opening_theaters"])

    print(f"Total rows: {len(df)}")
    print(f"With Fri data: {df['fri'].notna().sum()}")
    print(f"With Fri+Sat+Sun: {(df['fri'].notna() & df['sat'].notna() & df['sun'].notna()).sum()}")

    # ── Model 1: Fri → Sat+Sun multiplier ────────────────────────────────────
    # fri_sat_sun_mult = (fri+sat+sun) / fri
    # Use case: Saturday morning, you know Friday gross, predict rest of weekend
    m1, f1 = train_multiplier_model(df, "fri_sat_sun_mult", "fri_to_frisatsun")

    # ── Model 2: Sat → Sun ratio ──────────────────────────────────────────────
    # sat_to_remaining_mult = (sat+sun) / sat
    # Use case: Saturday night, you know Fri+Sat, predict Sunday
    df["sat_to_sun_ratio"] = df["sun"] / df["sat"]
    m2, f2 = train_multiplier_model(df, "sat_to_sun_ratio", "sat_to_sun")

    # ── Model 3: Thu preview → full weekend ──────────────────────────────────
    # Use case: Thursday night, you only know previews, rough early estimate
    total_col = "weekend_total" if "weekend_total" in df.columns else "weekend_total_daily"
    df["thu_to_full_mult"] = df[total_col] / df["thu_preview"].replace(0, np.nan)
    m3, f3 = train_multiplier_model(
        df[df["thu_preview"].notna() & (df["thu_preview"] > 0)],
        "thu_to_full_mult", "thu_to_full"
    )

    print("\n✓ All multiplier models saved to models/")

    # ── Sanity check on known films ───────────────────────────────────────────
    print("\nSanity check (known films):")
    check = [
        ("Inside Out 2",    2024, 63558115,  51175086, 39468472, 13000000),
        ("Deadpool & Wolverine", 2024, 96189710, 61644783, 53600798, 38500000),
        ("Barbie",          2023, 70491519,  35989265, 35983280,  0),
        ("Top Gun: Maverick",2022, 51165632,  44174502, 31368073,  0),
    ]
    for title, year, fri, sat, sun, thu in check:
        actual = thu + fri + sat + sun
        # Get genre/mpaa from bom
        row = bom[bom["title"]==title]
        genre = row["genre"].values[0] if len(row) else ""
        mpaa  = row["mpaa_rating"].values[0] if len(row) else "PG-13"
        rel   = row["release_date"].values[0] if len(row) else "2023-07-21"
        theaters = row["max_theaters"].values[0] if len(row) else 4000

        # Build feature row for fri model
        feat_row = pd.DataFrame([{
            "fri": fri, "thu_preview": thu,
            "genre": genre, "mpaa_rating": mpaa,
            "release_date": rel,
            "opening_theaters": theaters, "max_theaters": theaters,
        }])
        X_row = build_features(feat_row)[f1]
        fri_mult_pred = float(m1.predict(X_row)[0])
        proj_total = thu + fri * fri_mult_pred

        # Also sat→sun if we have sat
        X_sat = build_features(feat_row)[f2]
        sun_ratio_pred = float(m2.predict(X_sat)[0])
        proj_total_with_sat = thu + fri + sat + sat * sun_ratio_pred

        print(f"  {title:35s} actual=${actual/1e6:.1f}M | "
              f"fri-model=${proj_total/1e6:.1f}M | "
              f"fri+sat-model=${proj_total_with_sat/1e6:.1f}M")


if __name__ == "__main__":
    main()
