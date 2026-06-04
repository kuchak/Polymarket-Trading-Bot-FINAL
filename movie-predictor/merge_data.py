"""
Merge BOM + RT data into a single clean dataset ready for modeling.

Input:  data/bom_movies.csv, data/rt_scores.csv
Output: data/movies_merged.csv

Feature engineering applied here:
  - release_month, release_dow, is_holiday_weekend
  - is_sequel (title contains number or colon-subtitle pattern)
  - is_franchise (distributor + title patterns)
  - major_distributor flag
  - genre_* one-hot columns (top genres)
  - mpaa_rating ordinal
  - log_opening_weekend (target, log-transformed)
  - log_budget
"""

import pandas as pd
import numpy as np
import re
import os
from datetime import datetime, date

MAJOR_DISTRIBUTORS = {
    "Walt Disney Studios Motion Pictures": "Disney",
    "Universal Pictures": "Universal",
    "Warner Bros.": "WB",
    "Sony Pictures": "Sony",
    "Paramount Pictures": "Paramount",
    "Lionsgate": "Lionsgate",
    "20th Century Studios": "Disney",   # now Disney
    "Marvel Studios": "Disney",
    "Columbia Pictures": "Sony",
    "DreamWorks": "Universal",
    "Focus Features": "Universal",
    "Searchlight Pictures": "Disney",
    "A24": "A24",
    "Netflix": "Netflix",
    "Amazon": "Amazon",
    "Apple": "Apple",
}

MPAA_ORDER = {"G": 1, "PG": 2, "PG-13": 3, "R": 4, "NC-17": 5, "NR": 3, "UR": 3}

TOP_GENRES = [
    "Action", "Adventure", "Animation", "Comedy", "Crime",
    "Drama", "Family", "Fantasy", "Horror", "Musical",
    "Romance", "Sci-Fi", "Thriller",
]

# US holiday weekends that boost box office
HOLIDAY_WEEKENDS = [
    # (month, day_start, day_end) — Memorial Day, July 4, Labor Day, Thanksgiving, Christmas, MLK, Presidents Day
    (5, 23, 31),   # Memorial Day weekend
    (7, 1, 7),     # July 4th
    (9, 1, 7),     # Labor Day
    (11, 22, 30),  # Thanksgiving
    (12, 20, 31),  # Christmas
    (1, 14, 21),   # MLK weekend
    (2, 14, 22),   # Presidents Day weekend
    (3, 13, 21),   # Spring break (approx)
]


def normalize_title(t: str) -> str:
    """Lowercase, strip punctuation for fuzzy matching."""
    return re.sub(r"[^a-z0-9 ]", "", str(t).lower().strip())


def is_holiday_weekend(dt) -> int:
    if pd.isna(dt):
        return 0
    try:
        d = pd.to_datetime(dt)
        for m, d1, d2 in HOLIDAY_WEEKENDS:
            if d.month == m and d1 <= d.day <= d2:
                return 1
    except Exception:
        pass
    return 0


def is_sequel(title: str) -> int:
    """Heuristic: title has number, 'Part', 'Chapter', or colon subtitle."""
    t = str(title)
    patterns = [
        r"\b[2-9]\b",           # standalone digit
        r"\b(II|III|IV|VI?I?I?|IX|X)\b",  # Roman numerals
        r"\bPart\s+[2-9IVX]",
        r"\bChapter\s+[2-9IVX]",
        r":\s+\w+",             # colon subtitle (often sequel)
        r"\bReturns\b|\bRises\b|\bAgain\b|\bRebirth\b|\bReloaded\b|\bRevolutions\b",
    ]
    for p in patterns:
        if re.search(p, t, re.I):
            return 1
    return 0


def map_distributor(dist: str) -> str:
    if pd.isna(dist):
        return "Other"
    dist = str(dist).strip()
    for key, label in MAJOR_DISTRIBUTORS.items():
        if key.lower() in dist.lower():
            return label
    return "Other"


def extract_primary_genre(genre_str: str) -> str:
    """Return the first genre listed."""
    if pd.isna(genre_str) or not str(genre_str).strip():
        return "Unknown"
    genres = [g.strip() for g in str(genre_str).split(",")]
    return genres[0] if genres else "Unknown"


def genre_flags(genre_str: str) -> dict:
    """Return {genre_Action: 0/1, genre_Comedy: 0/1, ...} for top genres."""
    flags = {f"genre_{g}": 0 for g in TOP_GENRES}
    if pd.isna(genre_str):
        return flags
    for g in TOP_GENRES:
        if g.lower() in str(genre_str).lower():
            flags[f"genre_{g}"] = 1
    return flags


def main():
    os.makedirs("data", exist_ok=True)

    # ── Load ──────────────────────────────────────────────────────────────
    print("Loading BOM data...")
    df_bom = pd.read_csv("data/bom_movies.csv")
    print(f"  BOM: {len(df_bom)} rows")

    print("Loading RT scores...")
    df_rt = pd.read_csv("data/rt_scores.csv")
    print(f"  RT:  {len(df_rt)} rows")

    # ── Merge ─────────────────────────────────────────────────────────────
    # Normalize titles for matching
    df_bom["title_norm"] = df_bom["title"].apply(normalize_title)
    df_rt["title_norm"] = df_rt["title"].apply(normalize_title)

    # Primary join: normalized title + year
    df = df_bom.merge(
        df_rt[["title_norm", "year", "rt_slug", "wayback_timestamp",
               "rt_score_prerelease", "rt_reviews_prerelease",
               "rt_audience_prerelease", "rt_score_final",
               "rt_reviews_final", "rt_audience_final"]],
        on=["title_norm", "year"],
        how="left",
    )

    print(f"\nAfter merge: {len(df)} rows")
    rt_matched = df["rt_slug"].notna().sum()
    print(f"  RT matched: {rt_matched}/{len(df)} ({rt_matched/len(df)*100:.1f}%)")

    # ── Filter ────────────────────────────────────────────────────────────
    # Keep only movies with opening weekend data (wide releases)
    df = df[df["opening_weekend"].notna()].copy()
    print(f"  With opening weekend: {len(df)}")

    # ── Feature Engineering ───────────────────────────────────────────────
    print("\nEngineering features...")

    # Parse release date
    df["release_dt"] = pd.to_datetime(df["release_date"], errors="coerce")
    df["release_month"] = df["release_dt"].dt.month
    df["release_dow"] = df["release_dt"].dt.dayofweek   # 0=Mon, 4=Fri
    df["release_year"] = df["release_dt"].dt.year.fillna(df["year"])
    df["is_holiday_weekend"] = df["release_dt"].apply(is_holiday_weekend)

    # Summer (May-Aug) and awards season (Nov-Dec)
    df["is_summer"] = df["release_month"].apply(lambda m: 1 if m in [5, 6, 7, 8] else 0)
    df["is_awards_season"] = df["release_month"].apply(lambda m: 1 if m in [11, 12] else 0)
    df["is_spring_break"] = df["release_month"].apply(lambda m: 1 if m == 3 else 0)

    # Sequel flag
    df["is_sequel"] = df["title"].apply(is_sequel)

    # Distributor
    df["distributor_group"] = df["distributor"].apply(map_distributor)

    # MPAA rating ordinal
    df["mpaa_ordinal"] = df["mpaa_rating"].map(MPAA_ORDER).fillna(3).astype(int)

    # Genre flags
    genre_dicts = df["genre"].apply(genre_flags)
    genre_df = pd.DataFrame(genre_dicts.tolist())
    df = pd.concat([df.reset_index(drop=True), genre_df.reset_index(drop=True)], axis=1)
    df["primary_genre"] = df["genre"].apply(extract_primary_genre)

    # Budget — fill missing with year-genre median later; log-transform
    df["budget"] = df["budget"].fillna(df["budget_chart"])
    # Log budget (fill missing with -1 flag via separate column)
    df["budget_known"] = df["budget"].notna().astype(int)
    df["log_budget"] = np.log1p(df["budget"].fillna(0))

    # RT score delta (how much score changed from release day to final)
    df["rt_score_delta"] = df["rt_score_final"] - df["rt_score_prerelease"]

    # Review count at release (proxy for how wide press coverage was)
    df["log_rt_reviews_release"] = np.log1p(df["rt_reviews_prerelease"].fillna(0))

    # Target: opening weekend gross — log-transformed for better model behavior
    df["log_opening_weekend"] = np.log1p(df["opening_weekend"])
    df["log_total_gross"] = np.log1p(df["total_gross_domestic"])

    # ── One-hot encode distributor group ──────────────────────────────────
    dist_dummies = pd.get_dummies(df["distributor_group"], prefix="dist")
    df = pd.concat([df, dist_dummies], axis=1)

    # Month dummies (captures seasonality nonlinearly)
    month_dummies = pd.get_dummies(df["release_month"], prefix="month")
    df = pd.concat([df, month_dummies], axis=1)

    # ── Save ──────────────────────────────────────────────────────────────
    df.to_csv("data/movies_merged.csv", index=False)
    print(f"\n✓ Saved {len(df)} rows to data/movies_merged.csv")

    # Summary
    print("\nFeature completeness:")
    key_cols = [
        "opening_weekend", "rt_score_prerelease", "rt_score_final",
        "rt_audience_prerelease", "budget", "mpaa_rating", "genre",
        "distributor", "is_sequel",
    ]
    for col in key_cols:
        if col in df.columns:
            pct = df[col].notna().mean() * 100
            print(f"  {col:35s}: {pct:5.1f}% complete")

    print("\nSample rows:")
    sample_cols = ["title", "year", "release_date", "opening_weekend",
                   "rt_score_prerelease", "rt_score_final", "budget",
                   "mpaa_rating", "is_sequel", "distributor_group"]
    print(df[sample_cols].head(10).to_string())


if __name__ == "__main__":
    main()
