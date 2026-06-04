"""
Predict opening weekend gross for a new (or upcoming) movie.

Usage:
  python3 predict.py --title "Mission: Impossible 8" --release-date 2025-05-23 \
                     --rt-score 88 --rt-reviews 120 --rt-audience 91 \
                     --budget 300000000 --mpaa PG-13 --genre "Action, Adventure" \
                     --distributor "Paramount Pictures" --sequel

  python3 predict.py  (interactive mode — prompts for each input)

Output: predicted opening weekend + confidence range
"""

import argparse
import json
import os
import re
import sys
import numpy as np
import pandas as pd
from xgboost import XGBRegressor
import requests
from bs4 import BeautifulSoup

MODEL_PATH = "models/opening_weekend_model.json"
FEATURE_PATH = "models/feature_cols.json"

MPAA_ORDER = {"G": 1, "PG": 2, "PG-13": 3, "R": 4, "NC-17": 5, "NR": 3, "UR": 3}

TOP_GENRES = [
    "Action", "Adventure", "Animation", "Comedy", "Crime",
    "Drama", "Family", "Fantasy", "Horror", "Musical",
    "Romance", "Sci-Fi", "Thriller",
]

MAJOR_DISTRIBUTORS = {
    "Walt Disney Studios Motion Pictures": "Disney",
    "Universal Pictures": "Universal",
    "Warner Bros.": "WB",
    "Sony Pictures": "Sony",
    "Paramount Pictures": "Paramount",
    "Lionsgate": "Lionsgate",
    "20th Century Studios": "Disney",
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

HOLIDAY_WEEKENDS = [
    (5, 23, 31), (7, 1, 7), (9, 1, 7),
    (11, 22, 30), (12, 20, 31), (1, 14, 21),
    (2, 14, 22), (3, 13, 21),
]

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    )
}


# ── RT live lookup ─────────────────────────────────────────────────────────

def title_to_rt_slug(title: str) -> str:
    slug = title.lower().strip()
    slug = re.sub(r"[:'\"!?.,]", "", slug)
    slug = slug.replace("&", "and")
    slug = re.sub(r"[\s\-]+", "_", slug)
    slug = re.sub(r"[^\w]", "", slug)
    return re.sub(r"_+", "_", slug).strip("_")


def fetch_rt_scores(title: str, year: int = None) -> dict:
    """Auto-fetch current RT scores for a movie title."""
    candidate = title_to_rt_slug(title)
    slugs = [candidate]
    if year:
        slugs.append(f"{candidate}_{year}")

    for slug in slugs:
        try:
            resp = requests.get(
                f"https://www.rottentomatoes.com/m/{slug}",
                headers=HEADERS, timeout=20
            )
            if resp.status_code != 200:
                continue
            soup = BeautifulSoup(resp.text, "html.parser")

            # Critic score from JSON-LD
            rt_score = None
            rt_reviews = None
            for script in soup.find_all("script", type="application/ld+json"):
                try:
                    d = json.loads(script.string or "")
                    agg = d.get("aggregateRating", {})
                    if agg.get("ratingValue"):
                        rt_score = int(agg["ratingValue"])
                        rt_reviews = agg.get("reviewCount")
                        break
                except Exception:
                    pass

            # Audience score from inline script
            rt_audience = None
            for script in soup.find_all("script"):
                text = script.string or ""
                if "audienceScore" in text and '"score"' in text:
                    m = re.search(r'"audienceScore"\s*:\s*\{[^}]*"score"\s*:\s*"(\d+)"', text)
                    if m:
                        rt_audience = int(m.group(1))
                    break

            if rt_score is not None:
                print(f"  ✓ Found RT scores for '{title}': {rt_score}% ({rt_reviews} reviews), audience {rt_audience}%")
                return {"rt_score": rt_score, "rt_reviews": rt_reviews, "rt_audience": rt_audience, "slug": slug}
        except Exception as e:
            print(f"  RT lookup error: {e}")

    print(f"  ✗ Could not find RT scores for '{title}'")
    return {}


# ── Feature builder ────────────────────────────────────────────────────────

def build_feature_row(inputs: dict, feature_cols: list[str]) -> pd.DataFrame:
    """Build a single-row feature DataFrame from user inputs."""
    row = {col: 0.0 for col in feature_cols}

    # RT scores
    row["rt_score_monday"] = inputs.get("rt_score", 0) or 0
    row["rt_reviews_monday"] = inputs.get("rt_reviews", 0) or 0
    row["rt_audience_monday"] = inputs.get("rt_audience", 0) or 0

    # Budget
    budget = inputs.get("budget")
    row["log_budget"] = np.log1p(budget) if budget else 0
    row["budget_known"] = 1 if budget else 0

    # MPAA
    mpaa = inputs.get("mpaa", "PG-13")
    row["mpaa_ordinal"] = MPAA_ORDER.get(mpaa, 3)

    # Flags
    row["is_sequel"] = int(inputs.get("is_sequel", False))
    row["release_year"] = inputs.get("release_year", 2025)

    # Release date features
    release_date = inputs.get("release_date")
    if release_date:
        try:
            dt = pd.to_datetime(release_date)
            row["release_month"] = dt.month
            row["release_dow"] = dt.dayofweek
            month = dt.month
            row["is_summer"] = 1 if month in [5, 6, 7, 8] else 0
            row["is_awards_season"] = 1 if month in [11, 12] else 0
            row["is_spring_break"] = 1 if month == 3 else 0
            for m, d1, d2 in HOLIDAY_WEEKENDS:
                if dt.month == m and d1 <= dt.day <= d2:
                    row["is_holiday_weekend"] = 1
                    break
            row["release_year"] = dt.year
        except Exception:
            pass

    # Genre flags
    genre_str = inputs.get("genre", "")
    for g in TOP_GENRES:
        key = f"genre_{g}"
        if key in row:
            row[key] = 1 if g.lower() in str(genre_str).lower() else 0

    # Distributor
    dist_label = "Other"
    dist_raw = inputs.get("distributor", "")
    for key, label in MAJOR_DISTRIBUTORS.items():
        if key.lower() in str(dist_raw).lower():
            dist_label = label
            break
    dist_col = f"dist_{dist_label}"
    if dist_col in row:
        row[dist_col] = 1

    # Month dummies
    release_month = inputs.get("release_month")
    if release_date:
        try:
            release_month = pd.to_datetime(release_date).month
        except Exception:
            pass
    if release_month:
        month_col = f"month_{int(release_month)}"
        if month_col in row:
            row[month_col] = 1

    return pd.DataFrame([row])[feature_cols]


# ── Prediction with uncertainty ────────────────────────────────────────────

def predict_with_range(model, X: pd.DataFrame) -> dict:
    """
    Predict opening weekend with a rough confidence range.
    XGBoost doesn't give native prediction intervals, so we use
    ±0.5 in log space as a heuristic (calibrated from CV MAE ~0.4-0.6).
    """
    log_pred = float(model.predict(X)[0])
    prediction = np.expm1(log_pred)

    # Approximate 80% range: ±0.65 log units (from CV error distribution)
    log_lo = log_pred - 0.65
    log_hi = log_pred + 0.65

    return {
        "predicted": prediction,
        "low_80pct": np.expm1(log_lo),
        "high_80pct": np.expm1(log_hi),
        "log_pred": log_pred,
    }


def fmt_money(n: float) -> str:
    if n >= 1e9:
        return f"${n/1e9:.2f}B"
    elif n >= 1e6:
        return f"${n/1e6:.1f}M"
    else:
        return f"${n:,.0f}"


# ── Interactive / CLI ──────────────────────────────────────────────────────

def prompt(label, default=None, cast=None):
    suffix = f" [{default}]" if default is not None else ""
    val = input(f"  {label}{suffix}: ").strip()
    if not val and default is not None:
        return default
    if cast and val:
        try:
            return cast(val)
        except Exception:
            return default
    return val or default


def collect_inputs_interactive() -> dict:
    print("\n── Movie Details ────────────────────────────────────────")
    title = prompt("Movie title")
    release_date = prompt("Release date (YYYY-MM-DD)", default="2025-07-04")
    year = int(release_date[:4]) if release_date else 2025

    print("\n  Fetching RT scores automatically... (enter manually to override)")
    rt_data = fetch_rt_scores(title, year)

    rt_score = prompt("RT critic score (%)", default=rt_data.get("rt_score"), cast=int)
    rt_reviews = prompt("RT review count", default=rt_data.get("rt_reviews") or 0, cast=int)
    rt_audience = prompt("RT audience score (%)", default=rt_data.get("rt_audience"), cast=int)
    budget = prompt("Production budget ($)", default=None, cast=lambda x: float(x.replace(",", "").replace("$", "")))
    mpaa = prompt("MPAA rating (G/PG/PG-13/R/NR)", default="PG-13")
    genre = prompt("Genre(s) e.g. 'Action, Adventure'", default="Action")
    distributor = prompt("Distributor", default="Universal Pictures")
    is_sequel_val = prompt("Is it a sequel? (y/n)", default="n").lower().startswith("y")

    return {
        "title": title,
        "release_date": release_date,
        "release_year": year,
        "rt_score": rt_score,
        "rt_reviews": rt_reviews,
        "rt_audience": rt_audience,
        "budget": budget,
        "mpaa": mpaa,
        "genre": genre,
        "distributor": distributor,
        "is_sequel": is_sequel_val,
    }


def main():
    parser = argparse.ArgumentParser(description="Predict movie opening weekend")
    parser.add_argument("--title", type=str)
    parser.add_argument("--release-date", type=str)
    parser.add_argument("--rt-score", type=int)
    parser.add_argument("--rt-reviews", type=int, default=0)
    parser.add_argument("--rt-audience", type=int)
    parser.add_argument("--budget", type=float)
    parser.add_argument("--mpaa", type=str, default="PG-13")
    parser.add_argument("--genre", type=str, default="")
    parser.add_argument("--distributor", type=str, default="")
    parser.add_argument("--sequel", action="store_true")
    parser.add_argument("--auto-rt", action="store_true",
                        help="Auto-fetch current RT scores by title")
    args = parser.parse_args()

    # ── Load model ────────────────────────────────────────────────────────
    if not os.path.exists(MODEL_PATH):
        print(f"ERROR: Model not found at {MODEL_PATH}. Run train_model.py first.")
        sys.exit(1)

    model = XGBRegressor()
    model.load_model(MODEL_PATH)
    with open(FEATURE_PATH) as f:
        feature_cols = json.load(f)

    # ── Collect inputs ────────────────────────────────────────────────────
    if args.title:
        inputs = {
            "title": args.title,
            "release_date": args.release_date,
            "release_year": int(args.release_date[:4]) if args.release_date else 2025,
            "rt_score": args.rt_score,
            "rt_reviews": args.rt_reviews,
            "rt_audience": args.rt_audience,
            "budget": args.budget,
            "mpaa": args.mpaa,
            "genre": args.genre,
            "distributor": args.distributor,
            "is_sequel": args.sequel,
        }
        if args.auto_rt or args.rt_score is None:
            rt_data = fetch_rt_scores(args.title, inputs["release_year"])
            inputs["rt_score"] = inputs["rt_score"] or rt_data.get("rt_score")
            inputs["rt_reviews"] = inputs["rt_reviews"] or rt_data.get("rt_reviews", 0)
            inputs["rt_audience"] = inputs["rt_audience"] or rt_data.get("rt_audience")
    else:
        inputs = collect_inputs_interactive()

    # ── Predict ───────────────────────────────────────────────────────────
    X = build_feature_row(inputs, feature_cols)
    result = predict_with_range(model, X)

    # ── Output ────────────────────────────────────────────────────────────
    print("\n" + "=" * 50)
    print(f"  🎬  {inputs.get('title', 'Unknown')}")
    print(f"  📅  Release: {inputs.get('release_date', 'Unknown')}")
    print(f"  🍅  RT Score: {inputs.get('rt_score', '?')}% ({inputs.get('rt_reviews', '?')} reviews)")
    print(f"  👥  Audience: {inputs.get('rt_audience', '?')}%")
    print("=" * 50)
    print(f"  Predicted opening weekend: {fmt_money(result['predicted'])}")
    print(f"  80% range: {fmt_money(result['low_80pct'])} – {fmt_money(result['high_80pct'])}")
    print("=" * 50)

    # Comparable films context
    if os.path.exists("data/movies_merged.csv"):
        df = pd.read_csv("data/movies_merged.csv")
        rt = inputs.get("rt_score")
        if rt:
            similar = df[
                (df["rt_score_monday"].between(rt - 8, rt + 8)) &
                (df["opening_weekend"].notna())
            ].nlargest(5, "opening_weekend")
            if not similar.empty:
                print("\n  Similar RT-scored films (for context):")
                for _, row in similar.iterrows():
                    print(f"    {row['title']:35s} RT={row.get('rt_score_monday', '?'):>3}%  "
                          f"Opening={fmt_money(row['opening_weekend'])}")

    return result


if __name__ == "__main__":
    main()
