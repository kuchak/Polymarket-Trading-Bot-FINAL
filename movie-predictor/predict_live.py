"""
predict_live.py
===============
CLI + importable inference tool for the subsequent_weekend XGBoost model.

Predicts Fri+Sat+Sun total for a film's 2nd/3rd/4th weekend using features
known by Saturday morning (Friday gross + weekday data already published).

Usage (manual mode):
    python3 predict_live.py \\
        --title "Backrooms" --week 2 \\
        --fri 7974589 \\
        --weekday-avg 6928125 \\
        --prior-weekend 81402424 \\
        --prior-fri 38412634 \\
        --theaters 3565 \\
        --genre "Horror" --mpaa PG-13 \\
        --competition 44000000

Usage (auto mode — scrapes The Numbers for all known daily data):
    python3 predict_live.py --title "Backrooms" --week 2 --auto

Importable:
    from predict_live import run_prediction, build_inputs_from_tn, scrape_the_numbers_movie
    result = run_prediction(inputs_dict)
"""

import argparse
import json
import math
import re
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

import numpy as np
import requests
from bs4 import BeautifulSoup

try:
    import xgboost as xgb
except ImportError:
    raise SystemExit("xgboost not installed — run: pip install xgboost")

# ── Paths ──────────────────────────────────────────────────────────────────────
BASE = Path(__file__).parent
MODEL_PATH = BASE / "models" / "subsequent_weekend_model.json"
FEATURES_PATH = BASE / "models" / "subsequent_weekend_features.json"

# ── HTTP headers ───────────────────────────────────────────────────────────────
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    )
}

# ── Constants matching build_subsequent_weekend_model.py ──────────────────────
TOP_GENRES = [
    "Action", "Comedy", "Drama", "Horror", "Animation",
    "Thriller", "Adventure", "Fantasy", "Sci-Fi", "Romance",
    "Crime", "Family", "Documentary",
]
MPAA_ORDER = {"G": 0, "PG": 1, "PG-13": 2, "R": 3, "NC-17": 4}

# Holiday Friday dates (must match build script exactly)
HOLIDAY_WEEKENDS = {
    "2020-05-22", "2021-05-28", "2022-05-27", "2023-05-26", "2024-05-24", "2025-05-23", "2026-05-22",
    "2021-07-02", "2022-07-01", "2023-06-30", "2024-07-05", "2025-07-04", "2026-07-03",
    "2020-09-04", "2021-09-03", "2022-09-02", "2023-09-01", "2024-08-30", "2025-08-29",
    "2020-11-20", "2021-11-19", "2022-11-18", "2023-11-17", "2024-11-22", "2025-11-21",
    "2020-12-25", "2021-12-24", "2022-12-23", "2023-12-22", "2024-12-27", "2025-12-26",
    "2020-12-31", "2021-12-31", "2022-12-30", "2023-12-29",
    "2020-02-14", "2021-02-12", "2022-02-18", "2023-02-17", "2024-02-16", "2025-02-14",
    "2020-04-10", "2021-04-02", "2022-04-15", "2023-04-07", "2024-03-29", "2025-04-18",
    "2020-01-17", "2021-01-15", "2022-01-14", "2023-01-13", "2024-01-12", "2025-01-17",
}

# Week-specific MAE from CV (in dollars) — used for uncertainty bands
WEEK_MAE = {
    2: 1_500_000,
    3:   880_000,
    4:   760_000,
}

# Normal distribution z-score for 80% interval (one-sided 90th percentile)
Z_80 = 1.2816


# ── Money formatting ───────────────────────────────────────────────────────────

def fmt_money(n: float) -> str:
    """Format a dollar amount as $XM or $X,XXX."""
    if n >= 1e9:
        return f"${n / 1e9:.2f}B"
    elif n >= 1e6:
        return f"${n / 1e6:.1f}M"
    else:
        return f"${n:,.0f}"


# ── Holiday helper ─────────────────────────────────────────────────────────────

def _is_holiday_friday(d: date) -> bool:
    return d.isoformat() in HOLIDAY_WEEKENDS


def _find_prev_friday(d: date) -> date:
    """Return the most recent Friday on or before d."""
    offset = (d.weekday() - 4) % 7  # 4 = Friday
    return d - timedelta(days=offset)


# ── The Numbers scraper ────────────────────────────────────────────────────────

def _title_to_tn_slug(title: str) -> str:
    """Convert a movie title to a The Numbers URL slug."""
    slug = title.strip()
    slug = re.sub(r"[:'\"!?,.]", "", slug)
    slug = slug.replace("&", "and")
    slug = re.sub(r"\s+", "-", slug)
    return re.sub(r"-+", "-", slug).strip("-")


def _parse_dollars(s: str) -> float | None:
    s = re.sub(r"[^0-9]", "", s)
    return float(s) if s else None


def _parse_tn_daily_table(soup: BeautifulSoup) -> list[dict]:
    """
    Parse The Numbers movie page daily performance table.
    The Numbers table has columns like: Day | Date | Rank | Gross | % | Theaters | Avg | Cumulative
    """
    rows_out = []
    for tbl in soup.find_all("table"):
        rows = tbl.find_all("tr")
        if len(rows) < 3:
            continue

        header_cells = rows[0].find_all(["th", "td"])
        header_cols = [h.get_text(strip=True).lower() for h in header_cells]

        # Need a date column and a gross column
        date_idx = next((i for i, h in enumerate(header_cols)
                         if h in ("date", "day", "release date")), None)
        gross_idx = next((i for i, h in enumerate(header_cols)
                          if "gross" in h and "cumul" not in h and "total" not in h), None)
        if date_idx is None:
            # Try finding a column that looks like a date by scanning first data row
            for tr in rows[1:3]:
                data_cols = [td.get_text(strip=True) for td in tr.find_all(["td", "th"])]
                for i, val in enumerate(data_cols):
                    if re.search(r'\b(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\b', val):
                        date_idx = i
                        break
                if date_idx is not None:
                    break

        if date_idx is None or gross_idx is None:
            continue

        theater_idx = next((i for i, h in enumerate(header_cols)
                            if "theater" in h), None)
        day_num_idx = next((i for i, h in enumerate(header_cols)
                            if h in ("#", "day #", "days", "day")), None)

        for tr in rows[1:]:
            cols = [td.get_text(strip=True) for td in tr.find_all(["td", "th"])]
            if not cols or len(cols) <= max(c for c in [date_idx, gross_idx] if c is not None):
                continue
            try:
                date_raw = cols[date_idx]
                gross_raw = cols[gross_idx] if gross_idx < len(cols) else ""
                gross_val = _parse_dollars(gross_raw)
                if not gross_val or gross_val < 1000:
                    continue

                parsed_date = None
                for fmt in ("%b %d, %Y", "%B %d, %Y", "%Y-%m-%d", "%m/%d/%Y",
                             "%b %d,%Y", "%B %d,%Y"):
                    try:
                        parsed_date = datetime.strptime(date_raw.strip(), fmt).date()
                        break
                    except ValueError:
                        continue
                if parsed_date is None:
                    continue

                theaters_val = None
                if theater_idx is not None and theater_idx < len(cols):
                    theaters_val = _parse_dollars(cols[theater_idx])
                    if theaters_val and (theaters_val < 10 or theaters_val > 10000):
                        theaters_val = None

                days_val = None
                if day_num_idx is not None and day_num_idx < len(cols):
                    try:
                        days_val = int(re.sub(r"[^0-9]", "", cols[day_num_idx]))
                        if days_val < 1 or days_val > 365:
                            days_val = None
                    except (ValueError, TypeError):
                        pass

                rows_out.append({
                    "date": parsed_date,
                    "gross": gross_val,
                    "theaters": theaters_val,
                    "days_in_release": days_val,
                })
            except Exception:
                continue

        if rows_out:
            return rows_out

    return rows_out


def scrape_the_numbers_movie(title: str, year: int | None = None) -> dict | None:
    """
    Scrape the daily box office table for a film from The Numbers.

    Tries URL patterns:
        https://www.the-numbers.com/movie/TITLE-(YEAR)#tab=box-office
        https://www.the-numbers.com/movie/TITLE#tab=box-office

    Returns a dict with keys:
        daily: list of {date, gross, theaters, days_in_release}
        title: str
        year: int | None
        url: str
    Returns None on failure.
    """
    slug = _title_to_tn_slug(title)
    candidates = []
    if year:
        candidates.append(f"https://www.the-numbers.com/movie/{slug}-({year})#tab=box-office")
    candidates.append(f"https://www.the-numbers.com/movie/{slug}#tab=box-office")
    if year:
        candidates.append(f"https://www.the-numbers.com/movie/{slug}-{year}#tab=box-office")

    for url in candidates:
        try:
            resp = requests.get(url, headers=HEADERS, timeout=20)
            if resp.status_code != 200:
                continue

            soup = BeautifulSoup(resp.text, "html.parser")
            daily_rows = _parse_tn_daily_table(soup)

            if daily_rows:
                daily_rows.sort(key=lambda r: r["date"])
                return {"daily": daily_rows, "title": title, "year": year, "url": url}

        except requests.RequestException as e:
            print(f"  Network error fetching {url}: {e}", file=sys.stderr)

    return None


def build_inputs_from_tn(tn_data: dict, week_num: int,
                          competition: float = 0.0,
                          genre: str = "", mpaa: str = "PG-13") -> dict | None:
    """
    From scraped The Numbers daily data, derive all model inputs for week_num.

    Returns a dict suitable for build_feature_vector() and run_prediction().
    Returns None if insufficient data for the requested week.
    """
    daily = sorted(tn_data["daily"], key=lambda r: r["date"])
    if not daily:
        return None

    # Identify Fridays in release history
    fridays = [r for r in daily if r["date"].weekday() == 4]
    if len(fridays) < week_num:
        print(
            f"  Not enough Friday data yet (found {len(fridays)}, need {week_num}). "
            "Numbers may not have posted yet.",
            file=sys.stderr,
        )
        return None

    cur_fri_row = fridays[week_num - 1]   # 0-based index: week 2 → index 1
    prior_fri_row = fridays[week_num - 2]

    cur_fri_date = cur_fri_row["date"]
    prior_fri_date = prior_fri_row["date"]

    gap = (cur_fri_date - prior_fri_date).days
    if gap < 5 or gap > 10:
        print(f"  Warning: irregular Friday gap ({gap} days) — data may be incomplete.",
              file=sys.stderr)

    fri_gross = cur_fri_row["gross"] or 0.0
    theaters = cur_fri_row["theaters"]

    # Prior weekend total
    prior_fri_gross = prior_fri_row["gross"] or 0.0
    prior_sat = next((r["gross"] for r in daily
                      if r["date"] == prior_fri_date + timedelta(days=1)), 0.0) or 0.0
    prior_sun = next((r["gross"] for r in daily
                      if r["date"] == prior_fri_date + timedelta(days=2)), 0.0) or 0.0
    prior_weekend_total = prior_fri_gross + prior_sat + prior_sun

    # Weekday avg (Mon–Thu between prior and current Friday)
    weekday_grosses = [
        r["gross"] for r in daily
        if prior_fri_date < r["date"] < cur_fri_date
        and r["date"].weekday() in (0, 1, 2, 3)
        and r["gross"] is not None
    ]
    weekday_avg = float(np.mean(weekday_grosses)) if weekday_grosses else 0.0

    # Thursday preview immediately before current Friday
    thu_date = cur_fri_date - timedelta(days=1)
    thu_gross = next((r["gross"] for r in daily if r["date"] == thu_date), 0.0) or 0.0
    thu_fri_ratio = (thu_gross / fri_gross) if fri_gross > 0 else 0.0

    # Cumulative gross before this weekend
    cum_before = sum(r["gross"] for r in daily
                     if r["date"] < cur_fri_date and r["gross"] is not None)

    # Opening weekend total (first Friday's Fri+Sat+Sun)
    open_fri = fridays[0]["date"]
    opening_total = sum(
        r["gross"] for r in daily
        if open_fri <= r["date"] <= open_fri + timedelta(days=2)
        and r["gross"] is not None
    )
    cumulative_as_pct_opening = (cum_before / opening_total) if opening_total > 0 else 0.0

    # Days since first day in data (proxy for days since release)
    release_date_approx = daily[0]["date"]
    days_since_release = (cur_fri_date - release_date_approx).days

    # Friday drop pct vs prior Friday
    fri_drop_pct = ((fri_gross - prior_fri_gross) / prior_fri_gross
                    if prior_fri_gross > 0 else 0.0)

    return {
        "title": tn_data["title"],
        "week_num": week_num,
        "cur_fri_date": cur_fri_date,
        "fri_gross": fri_gross,
        "fri_drop_pct": fri_drop_pct,
        "prior_weekend_total": prior_weekend_total,
        "prior_fri_gross": prior_fri_gross,
        "weekday_avg": weekday_avg,
        "thu_gross": thu_gross,
        "thu_fri_ratio": thu_fri_ratio,
        "log_cumulative_gross": math.log1p(cum_before),
        "cumulative_as_pct_opening": cumulative_as_pct_opening,
        "per_theater_fri": (fri_gross / theaters) if theaters and theaters > 0 else 0.0,
        "competition_gross": competition,
        "is_holiday_weekend": int(_is_holiday_friday(cur_fri_date)),
        "prior_was_holiday": int(_is_holiday_friday(prior_fri_date)),
        "log_theaters": math.log1p(theaters) if theaters else 0.0,
        "release_month": cur_fri_date.month,
        "days_since_release": days_since_release,
        "mpaa": mpaa,
        "genre": genre,
        "theaters": theaters,
    }


# ── Feature vector builder ─────────────────────────────────────────────────────

def build_feature_vector(inputs: dict, feature_cols: list) -> np.ndarray:
    """
    Build a (1, n_features) numpy array from an inputs dict.
    Feature names match models/subsequent_weekend_features.json exactly.
    """
    row = {col: 0.0 for col in feature_cols}

    # Direct numeric features (names match feature_cols exactly)
    for key in [
        "week_num", "fri_gross", "fri_drop_pct", "prior_weekend_total",
        "weekday_avg", "thu_gross", "thu_fri_ratio", "log_cumulative_gross",
        "cumulative_as_pct_opening", "per_theater_fri", "competition_gross",
        "is_holiday_weekend", "prior_was_holiday", "log_theaters",
        "release_month", "days_since_release",
    ]:
        if key in inputs and key in row:
            val = inputs[key]
            row[key] = float(val) if val is not None else 0.0

    # MPAA ordinal
    mpaa = inputs.get("mpaa", "PG-13")
    row["mpaa_ordinal"] = float(MPAA_ORDER.get(mpaa, 2))

    # Genre one-hot flags
    genre_str = str(inputs.get("genre", ""))
    for g in TOP_GENRES:
        col = f"genre_{g}"
        if col in row:
            row[col] = 1.0 if g.lower() in genre_str.lower() else 0.0

    return np.array([[row[col] for col in feature_cols]], dtype=float)


# ── Model loader (module-level cache) ─────────────────────────────────────────
_model_cache = None
_features_cache = None


def _load_model():
    """Load and cache the XGBoost model and feature list."""
    global _model_cache, _features_cache
    if _model_cache is None:
        if not MODEL_PATH.exists():
            raise FileNotFoundError(
                f"Model not found at {MODEL_PATH}. "
                "Run build_subsequent_weekend_model.py first."
            )
        _model_cache = xgb.XGBRegressor()
        _model_cache.load_model(str(MODEL_PATH))
        with open(FEATURES_PATH) as f:
            _features_cache = json.load(f)
    return _model_cache, _features_cache


# ── Core prediction function ───────────────────────────────────────────────────

def run_prediction(inputs: dict) -> dict:
    """
    Run the subsequent_weekend model on a prepared inputs dict.

    Returns a dict with:
        predicted   — point estimate (Fri+Sat+Sun total, dollars)
        low_80      — lower bound of 80% prediction interval
        high_80     — upper bound of 80% prediction interval
        predicted_sat — projected Saturday gross
        predicted_sun — projected Sunday gross
        week_num    — weekend number used
        mae         — week-specific MAE used for uncertainty
        sigma       — std dev used for interval
        inputs      — the original inputs dict
    """
    model, feature_cols = _load_model()

    X = build_feature_vector(inputs, feature_cols)
    pred = float(model.predict(X)[0])

    week_num = int(inputs.get("week_num", 2))
    mae = WEEK_MAE.get(week_num, 1_200_000)
    # MAE ≈ sigma * sqrt(2/pi) for a half-normal ≈ 0.798 * sigma, so sigma ≈ mae / 0.8
    sigma = mae / 0.8

    low_80 = max(0.0, pred - Z_80 * sigma)
    high_80 = pred + Z_80 * sigma

    # Project Saturday and Sunday from the total
    # By Saturday morning we already know Friday; remainder splits ~58/42
    fri_gross = float(inputs.get("fri_gross", 0))
    remaining = max(0.0, pred - fri_gross)
    predicted_sat = remaining * 0.58
    predicted_sun = remaining * 0.42

    return {
        "predicted": pred,
        "low_80": low_80,
        "high_80": high_80,
        "predicted_sat": predicted_sat,
        "predicted_sun": predicted_sun,
        "week_num": week_num,
        "mae": mae,
        "sigma": sigma,
        "inputs": inputs,
    }


# ── Bracket probability helper ─────────────────────────────────────────────────

def compute_bracket_probs(pred: float, sigma: float,
                           brackets: list) -> list:
    """
    Compute normal-distribution probabilities for (lo, hi) bracket tuples.
    Pass None for open-ended bounds.
    """
    from scipy import stats
    dist = stats.norm(loc=pred, scale=sigma)
    probs = []
    for lo, hi in brackets:
        cdf_hi = dist.cdf(hi) if hi is not None else 1.0
        cdf_lo = dist.cdf(lo) if lo is not None else 0.0
        probs.append(max(0.0, cdf_hi - cdf_lo))
    return probs


def _auto_brackets(pred: float):
    """Generate four sensible Polymarket-style brackets centered on the prediction."""
    # Round to nearest $3M increment for clean bracket edges
    center = round(pred / 3_000_000) * 3_000_000
    step = 3_000_000
    return [
        (None, center - step,
         f"<${(center - step) / 1e6:.0f}M"),
        (center - step, center,
         f"${(center - step) / 1e6:.0f}-{center / 1e6:.0f}M"),
        (center, center + step,
         f"${center / 1e6:.0f}-{(center + step) / 1e6:.0f}M"),
        (center + step, None,
         f">${(center + step) / 1e6:.0f}M"),
    ]


# ── Pretty printer ─────────────────────────────────────────────────────────────

def print_result(result: dict, title: str) -> None:
    """Print a formatted prediction card to stdout."""
    from scipy import stats

    pred = result["predicted"]
    low_80 = result["low_80"]
    high_80 = result["high_80"]
    week_num = result["week_num"]
    sigma = result["sigma"]
    inputs = result["inputs"]

    cur_fri_date = inputs.get("cur_fri_date")
    if cur_fri_date is None:
        cur_fri_date = _find_prev_friday(date.today())
    elif isinstance(cur_fri_date, str):
        cur_fri_date = date.fromisoformat(cur_fri_date)

    sat_date = cur_fri_date + timedelta(days=1)
    sun_date = cur_fri_date + timedelta(days=2)

    dist = stats.norm(loc=pred, scale=sigma)
    brackets = _auto_brackets(pred)

    print()
    print("══════════════════════════════════════════════")
    print(f"  \U0001f3ac  {title}  (Week {week_num})")
    print(f"  \U0001f4c5  Weekend: "
          f"{cur_fri_date.strftime('%b %-d')}-{sun_date.strftime('%-d, %Y')}")
    print(f"  \U0001f39f️  Friday actual: {fmt_money(inputs.get('fri_gross', 0))}")
    print("══════════════════════════════════════════════")
    print(f"  Predicted Fri+Sat+Sun:  {fmt_money(pred)}")
    print(f"  80% range:              {fmt_money(low_80)} – {fmt_money(high_80)}")
    print(f"  Projected Saturday:     {fmt_money(result['predicted_sat'])}")
    print(f"  Projected Sunday:       {fmt_money(result['predicted_sun'])}")
    print("══════════════════════════════════════════════")
    print("  Polymarket bracket guide:")
    for lo, hi, label in brackets:
        cdf_hi = dist.cdf(hi) if hi is not None else 1.0
        cdf_lo = dist.cdf(lo) if lo is not None else 0.0
        p = max(0.0, cdf_hi - cdf_lo)
        print(f"    {label:<14}  → probability ~{p:.0%}")
    print("══════════════════════════════════════════════")
    print()


# ── CLI entry point ────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Predict subsequent-weekend box office (Fri+Sat+Sun) using XGBoost model."
    )
    parser.add_argument("--title", required=True, help="Movie title")
    parser.add_argument("--week", type=int, required=True, choices=[2, 3, 4],
                        help="Weekend number (2, 3, or 4)")
    parser.add_argument("--year", type=int,
                        help="Release year (helps The Numbers URL lookup)")

    # Manual mode
    parser.add_argument("--fri", type=float,
                        help="This Friday's gross ($)")
    parser.add_argument("--weekday-avg", type=float, dest="weekday_avg",
                        help="Average Mon-Thu gross between prior and this Friday ($)")
    parser.add_argument("--prior-weekend", type=float, dest="prior_weekend",
                        help="Prior Fri+Sat+Sun total ($)")
    parser.add_argument("--prior-fri", type=float, dest="prior_fri",
                        help="Prior Friday gross — used to compute fri_drop_pct ($)")
    parser.add_argument("--theaters", type=int,
                        help="Theater count this weekend")
    parser.add_argument("--genre", type=str, default="",
                        help="Genre string e.g. 'Horror' or 'Action, Adventure'")
    parser.add_argument("--mpaa", type=str, default="PG-13",
                        help="MPAA rating: G / PG / PG-13 / R / NC-17")
    parser.add_argument("--competition", type=float, default=0.0,
                        help="Sum of all other films' Friday gross this weekend ($)")
    parser.add_argument("--cumulative", type=float, default=None,
                        help="Total gross before this weekend — auto-estimated if omitted ($)")
    parser.add_argument("--opening", type=float, default=None,
                        help="Opening Fri+Sat+Sun total, for cumulative-pct feature ($)")
    parser.add_argument("--thu", type=float, default=0.0,
                        help="Thursday preview gross before this Friday ($)")
    parser.add_argument("--holiday", action="store_true",
                        help="Flag: this is a holiday weekend")
    parser.add_argument("--prior-holiday", action="store_true",
                        help="Flag: prior weekend was a holiday weekend")
    parser.add_argument("--days-since-release", type=int, dest="days_since_release",
                        default=None)
    parser.add_argument("--release-month", type=int, dest="release_month",
                        default=None, help="Month of release (1-12)")

    # Auto mode
    parser.add_argument("--auto", action="store_true",
                        help="Auto-pull all known daily data from The Numbers by title")

    args = parser.parse_args()

    if args.auto:
        print(f"  Fetching data for '{args.title}' from The Numbers ...")
        tn_data = scrape_the_numbers_movie(args.title, args.year)
        if tn_data is None:
            print(
                f"ERROR: Could not retrieve daily data for '{args.title}' from The Numbers.",
                file=sys.stderr,
            )
            print(
                "  Try manual mode: supply --fri, --weekday-avg, --prior-weekend, etc.",
                file=sys.stderr,
            )
            sys.exit(1)
        print(f"  Found {len(tn_data['daily'])} daily rows from {tn_data['url']}")

        inputs = build_inputs_from_tn(
            tn_data, args.week,
            competition=args.competition,
            genre=args.genre,
            mpaa=args.mpaa,
        )
        if inputs is None:
            print("ERROR: Could not build model inputs from scraped data.", file=sys.stderr)
            sys.exit(1)

    else:
        # Manual mode
        if args.fri is None:
            parser.error("--fri is required in manual mode (or use --auto)")

        fri_gross = args.fri
        prior_fri = args.prior_fri or 0.0
        prior_weekend = args.prior_weekend or 0.0
        weekday_avg = args.weekday_avg or 0.0
        thu_gross = args.thu or 0.0
        theaters = args.theaters
        competition = args.competition

        thu_fri_ratio = (thu_gross / fri_gross) if fri_gross > 0 else 0.0
        fri_drop_pct = ((fri_gross - prior_fri) / prior_fri) if prior_fri > 0 else 0.0

        # Estimate cumulative gross if not provided
        if args.cumulative is None:
            weekdays_est = weekday_avg * 4
            cumulative = prior_weekend + weekdays_est
        else:
            cumulative = args.cumulative

        opening = args.opening or prior_weekend
        cum_as_pct = (cumulative / opening) if opening > 0 else 0.0

        today = date.today()
        cur_fri_date = _find_prev_friday(today)
        prior_fri_date = cur_fri_date - timedelta(days=7)

        is_holiday = int(args.holiday) or int(_is_holiday_friday(cur_fri_date))
        prior_holiday = int(args.prior_holiday) or int(_is_holiday_friday(prior_fri_date))

        release_month = args.release_month or cur_fri_date.month
        days_since_release = (
            args.days_since_release
            if args.days_since_release is not None
            else (args.week - 1) * 7
        )

        inputs = {
            "title": args.title,
            "week_num": args.week,
            "cur_fri_date": cur_fri_date,
            "fri_gross": fri_gross,
            "fri_drop_pct": fri_drop_pct,
            "prior_weekend_total": prior_weekend,
            "prior_fri_gross": prior_fri,
            "weekday_avg": weekday_avg,
            "thu_gross": thu_gross,
            "thu_fri_ratio": thu_fri_ratio,
            "log_cumulative_gross": math.log1p(cumulative),
            "cumulative_as_pct_opening": cum_as_pct,
            "per_theater_fri": (fri_gross / theaters) if theaters and theaters > 0 else 0.0,
            "competition_gross": competition,
            "is_holiday_weekend": is_holiday,
            "prior_was_holiday": prior_holiday,
            "log_theaters": math.log1p(theaters) if theaters else 0.0,
            "release_month": release_month,
            "days_since_release": days_since_release,
            "mpaa": args.mpaa,
            "genre": args.genre,
            "theaters": theaters,
        }

    result = run_prediction(inputs)
    print_result(result, args.title)
    return result


if __name__ == "__main__":
    main()
