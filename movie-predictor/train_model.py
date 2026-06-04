"""
Train XGBoost model to predict opening weekend box office.

Input:  data/movies_merged.csv
Output: models/opening_weekend_model.json   (XGBoost model)
        models/feature_cols.json            (feature list for inference)
        models/model_report.txt             (eval metrics + feature importance)

Target: log1p(opening_weekend) → expm1 to get dollars back

Features used:
  - rt_score_prerelease      (tomatometer at release)
  - rt_reviews_prerelease    (review count proxy = press coverage)
  - rt_audience_prerelease   (audience score at release)
  - log_budget / budget_known
  - mpaa_ordinal
  - is_sequel
  - is_holiday_weekend
  - is_summer / is_awards_season / is_spring_break
  - release_year (year trend)
  - genre_* flags
  - dist_* flags
  - month_* flags
"""

import pandas as pd
import numpy as np
import json
import os
import warnings
warnings.filterwarnings("ignore")

from xgboost import XGBRegressor
from sklearn.model_selection import KFold, cross_val_score
from sklearn.metrics import mean_absolute_error, r2_score
from sklearn.preprocessing import LabelEncoder
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

os.makedirs("models", exist_ok=True)
os.makedirs("data/plots", exist_ok=True)


# ── Feature list ──────────────────────────────────────────────────────────

BASE_FEATURES = [
    "rt_score_prerelease",
    "rt_reviews_prerelease",
    "rt_audience_prerelease",
    "log_budget",
    "budget_known",
    "mpaa_ordinal",
    "is_sequel",
    "is_holiday_weekend",
    "is_summer",
    "is_awards_season",
    "is_spring_break",
    "release_year",
]

GENRE_FEATURES = [
    f"genre_{g}" for g in [
        "Action", "Adventure", "Animation", "Comedy", "Crime",
        "Drama", "Family", "Fantasy", "Horror", "Musical",
        "Romance", "Sci-Fi", "Thriller",
    ]
]

TARGET = "log_opening_weekend"


def get_feature_cols(df: pd.DataFrame) -> list[str]:
    """Return all available feature columns from BASE + genre + dist + month."""
    dist_cols = [c for c in df.columns if c.startswith("dist_")]
    month_cols = [c for c in df.columns if c.startswith("month_")]
    all_features = BASE_FEATURES + GENRE_FEATURES + dist_cols + month_cols
    # Keep only columns that exist in df
    return [c for c in all_features if c in df.columns]


def fill_missing_features(df: pd.DataFrame, feature_cols: list[str]) -> pd.DataFrame:
    """Fill NaN in feature columns with sensible defaults."""
    df = df.copy()
    for col in feature_cols:
        if col not in df.columns:
            df[col] = 0
        if col in ["rt_score_prerelease", "rt_score_final", "rt_audience_prerelease"]:
            # Fill with median — missing RT score is informative (limited release / no buzz)
            median_val = df[col].median()
            df[col] = df[col].fillna(median_val)
        elif col == "rt_reviews_prerelease":
            df[col] = df[col].fillna(0)
        else:
            df[col] = df[col].fillna(0)
    return df


def eval_model(model, X, y, label=""):
    preds = model.predict(X)
    mae_log = mean_absolute_error(y, preds)
    r2 = r2_score(y, preds)
    # Convert back to dollars for interpretable MAE
    mae_dollars = np.mean(np.abs(np.expm1(y) - np.expm1(preds)))
    return {
        "label": label,
        "mae_log": mae_log,
        "r2": r2,
        "mae_dollars_M": mae_dollars / 1e6,
        "n": len(y),
    }


def main():
    # ── Load data ─────────────────────────────────────────────────────────
    print("Loading data/movies_merged.csv...")
    df = pd.read_csv("data/movies_merged.csv")
    print(f"  {len(df)} total rows")

    # Must have target and at least one RT signal
    df = df[df[TARGET].notna() & df["opening_weekend"].notna()]
    has_rt = df["rt_score_prerelease"].notna() | df["rt_score_final"].notna()
    df_with_rt = df[has_rt].copy()
    df_no_rt = df[~has_rt].copy()
    print(f"  {len(df_with_rt)} rows with RT scores")
    print(f"  {len(df_no_rt)} rows without RT scores (will be excluded from primary model)")

    df_model = df_with_rt.copy()

    # Use release-day score; fall back to final score if release-day missing
    df_model["rt_score_prerelease"] = df_model["rt_score_prerelease"].fillna(
        df_model["rt_score_final"]
    )
    df_model["rt_audience_prerelease"] = df_model["rt_audience_prerelease"].fillna(
        df_model["rt_audience_final"]
    )

    feature_cols = get_feature_cols(df_model)
    df_model = fill_missing_features(df_model, feature_cols)

    X = df_model[feature_cols].astype(float)
    y = df_model[TARGET].astype(float)

    print(f"\n  Features: {len(feature_cols)}")
    print(f"  Training rows: {len(X)}")
    print(f"  Target range: ${np.expm1(y.min())/1e6:.1f}M – ${np.expm1(y.max())/1e6:.0f}M")

    # ── Cross-validation ──────────────────────────────────────────────────
    print("\nRunning 5-fold cross-validation...")
    model_cv = XGBRegressor(
        n_estimators=400,
        max_depth=5,
        learning_rate=0.05,
        subsample=0.8,
        colsample_bytree=0.8,
        min_child_weight=3,
        reg_alpha=0.1,
        reg_lambda=1.0,
        random_state=42,
        n_jobs=-1,
        verbosity=0,
    )

    kf = KFold(n_splits=5, shuffle=True, random_state=42)
    cv_maes_log = []
    cv_maes_dollars = []
    cv_r2s = []
    oof_preds = np.zeros(len(y))

    for fold, (train_idx, val_idx) in enumerate(kf.split(X)):
        X_tr, X_val = X.iloc[train_idx], X.iloc[val_idx]
        y_tr, y_val = y.iloc[train_idx], y.iloc[val_idx]

        model_cv.fit(X_tr, y_tr, eval_set=[(X_val, y_val)], verbose=False)
        val_preds = model_cv.predict(X_val)
        oof_preds[val_idx] = val_preds

        mae_log = mean_absolute_error(y_val, val_preds)
        mae_dollars = np.mean(np.abs(np.expm1(y_val) - np.expm1(val_preds))) / 1e6
        r2 = r2_score(y_val, val_preds)
        cv_maes_log.append(mae_log)
        cv_maes_dollars.append(mae_dollars)
        cv_r2s.append(r2)
        print(f"  Fold {fold+1}: MAE={mae_log:.3f} (${mae_dollars:.1f}M), R²={r2:.3f}")

    print(f"\n  CV MAE (log):    {np.mean(cv_maes_log):.3f} ± {np.std(cv_maes_log):.3f}")
    print(f"  CV MAE (dollars): ${np.mean(cv_maes_dollars):.1f}M ± ${np.std(cv_maes_dollars):.1f}M")
    print(f"  CV R²:            {np.mean(cv_r2s):.3f} ± {np.std(cv_r2s):.3f}")

    # ── Train final model on all data ─────────────────────────────────────
    print("\nTraining final model on full dataset...")
    model_final = XGBRegressor(
        n_estimators=500,
        max_depth=5,
        learning_rate=0.05,
        subsample=0.8,
        colsample_bytree=0.8,
        min_child_weight=3,
        reg_alpha=0.1,
        reg_lambda=1.0,
        random_state=42,
        n_jobs=-1,
        verbosity=0,
    )
    model_final.fit(X, y)
    train_metrics = eval_model(model_final, X, y, "train")
    print(f"  Train MAE (log): {train_metrics['mae_log']:.3f}, R²: {train_metrics['r2']:.3f}")

    # ── Feature importance ────────────────────────────────────────────────
    importance = pd.Series(model_final.feature_importances_, index=feature_cols)
    importance = importance.sort_values(ascending=False)

    print("\nTop 20 features by importance:")
    for feat, imp in importance.head(20).items():
        print(f"  {feat:40s}: {imp:.4f}")

    # ── Error analysis ────────────────────────────────────────────────────
    df_model["oof_pred_log"] = oof_preds
    df_model["oof_pred"] = np.expm1(oof_preds)
    df_model["actual"] = np.expm1(y)
    df_model["error_pct"] = (df_model["oof_pred"] - df_model["actual"]) / df_model["actual"] * 100
    df_model["abs_error_M"] = np.abs(df_model["oof_pred"] - df_model["actual"]) / 1e6

    print("\nWorst predictions (OOF):")
    worst = df_model.nlargest(10, "abs_error_M")[
        ["title", "year", "actual", "oof_pred", "error_pct", "rt_score_prerelease"]
    ].copy()
    worst["actual_M"] = worst["actual"] / 1e6
    worst["pred_M"] = worst["oof_pred"] / 1e6
    print(worst[["title", "year", "actual_M", "pred_M", "error_pct", "rt_score_prerelease"]].to_string())

    # ── Plots ─────────────────────────────────────────────────────────────
    fig, axes = plt.subplots(1, 3, figsize=(16, 5))

    # 1. Predicted vs Actual
    ax = axes[0]
    ax.scatter(np.expm1(y) / 1e6, df_model["oof_pred"] / 1e6, alpha=0.4, s=15)
    max_val = max(np.expm1(y).max(), df_model["oof_pred"].max()) / 1e6
    ax.plot([0, max_val], [0, max_val], "r--", linewidth=1)
    ax.set_xlabel("Actual Opening Weekend ($M)")
    ax.set_ylabel("Predicted ($M)")
    ax.set_title(f"Predicted vs Actual (OOF)\nR²={np.mean(cv_r2s):.3f}")
    ax.set_xlim(0, max_val)
    ax.set_ylim(0, max_val)

    # 2. Feature importance (top 15)
    ax = axes[1]
    top_feats = importance.head(15)
    ax.barh(range(len(top_feats)), top_feats.values)
    ax.set_yticks(range(len(top_feats)))
    ax.set_yticklabels(top_feats.index, fontsize=8)
    ax.invert_yaxis()
    ax.set_title("Top 15 Feature Importances")
    ax.set_xlabel("Importance")

    # 3. Error distribution
    ax = axes[2]
    errors = df_model["error_pct"].clip(-200, 200)
    ax.hist(errors, bins=40, edgecolor="black", linewidth=0.3)
    ax.axvline(0, color="red", linestyle="--")
    ax.set_xlabel("Prediction Error (%)")
    ax.set_ylabel("Count")
    ax.set_title(f"Error Distribution\nMAE=${np.mean(cv_maes_dollars):.1f}M")

    plt.tight_layout()
    plt.savefig("data/plots/model_report.png", dpi=150, bbox_inches="tight")
    print("\n✓ Plot saved to data/plots/model_report.png")

    # ── Save model + metadata ─────────────────────────────────────────────
    model_final.save_model("models/opening_weekend_model.json")
    with open("models/feature_cols.json", "w") as f:
        json.dump(feature_cols, f, indent=2)

    # Save report
    report_lines = [
        "=" * 60,
        "OPENING WEEKEND PREDICTION MODEL — REPORT",
        "=" * 60,
        f"Training rows:     {len(X)}",
        f"Features:          {len(feature_cols)}",
        "",
        "── Cross-validation (5-fold) ──",
        f"CV MAE (log):       {np.mean(cv_maes_log):.4f} ± {np.std(cv_maes_log):.4f}",
        f"CV MAE (dollars):  ${np.mean(cv_maes_dollars):.1f}M ± ${np.std(cv_maes_dollars):.1f}M",
        f"CV R²:              {np.mean(cv_r2s):.4f} ± {np.std(cv_r2s):.4f}",
        "",
        "── Top 20 Features ──",
    ]
    for feat, imp in importance.head(20).items():
        report_lines.append(f"  {feat:40s}: {imp:.4f}")
    report_lines += [
        "",
        "── CV Fold Results ──",
    ]
    for i, (m_log, m_dol, r2) in enumerate(zip(cv_maes_log, cv_maes_dollars, cv_r2s)):
        report_lines.append(f"  Fold {i+1}: MAE={m_log:.3f} (${m_dol:.1f}M), R²={r2:.3f}")

    with open("models/model_report.txt", "w") as f:
        f.write("\n".join(report_lines))

    print("✓ Model saved to models/opening_weekend_model.json")
    print("✓ Report saved to models/model_report.txt")

    return model_final, feature_cols


if __name__ == "__main__":
    main()
