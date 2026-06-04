"""
The Numbers daily box office scraper.

Two modes:
  1. HISTORICAL: scrape Thu/Fri/Sat/Sun daily breakdown for all movies
     in bom_movies.csv → builds multiplier model by genre/tier
     Output: data/daily_boxoffice.csv

  2. LIVE: scrape current daily data for a specific movie (e.g. Masters of the Universe)
     to project full opening weekend from partial data (Fri gross → weekend total)
     Usage: python3 scrape_daily_boxoffice.py --live "Masters of the Universe" 2026
"""

import requests
from bs4 import BeautifulSoup
import pandas as pd
import numpy as np
import re
import os
import time
import argparse
from datetime import datetime, date
from concurrent.futures import ThreadPoolExecutor, as_completed
import threading

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    )
}

BASE_URL = "https://www.the-numbers.com/movie"
DAILY_CHART = "https://www.the-numbers.com/daily-box-office-chart"
MAX_WORKERS = 5
print_lock = threading.Lock()


def log(msg):
    with print_lock:
        print(msg, flush=True)


# ── URL builder ───────────────────────────────────────────────────────────

def title_to_numbers_slug(title: str, year: int) -> str:
    """Convert title to The Numbers URL format: 'Inside Out 2' → 'Inside-Out-2-(2024)'"""
    slug = str(title).strip()
    # Replace & with and
    slug = slug.replace("&", "and")
    # Remove special chars except hyphens and spaces
    slug = re.sub(r"[:'\"!?,.]", "", slug)
    # Replace spaces with hyphens
    slug = re.sub(r"\s+", "-", slug)
    # Remove double hyphens
    slug = re.sub(r"-+", "-", slug).strip("-")
    return f"{slug}-({year})"


def fetch_daily_data(title: str, year: int, idx: int = 0, total: int = 0) -> dict:
    """
    Fetch daily box office breakdown from The Numbers for a single movie.
    Returns dict with keys: title, year, thu_preview, fri, sat, sun,
    weekend_total, fri_to_weekend_mult, sat_mult, opening_rank
    """
    slug = title_to_numbers_slug(title, year)
    url = f"{BASE_URL}/{slug}"

    result = {
        "title": title,
        "year": year,
        "numbers_slug": slug,
        "thu_preview": None,
        "fri": None,
        "sat": None,
        "sun": None,
        "weekend_total_daily": None,  # thu+fri+sat+sun
        "fri_sat_sun_total": None,    # fri+sat+sun only
        "fri_to_weekend_mult": None,  # (thu+fri+sat+sun) / fri
        "fri_sat_sun_mult": None,     # (fri+sat+sun) / fri
        "sat_to_remaining_mult": None, # (sat+sun) / sat
        "opening_rank": None,
        "opening_theaters": None,
        "per_theater": None,
    }

    try:
        resp = requests.get(url, headers=HEADERS, timeout=20)
        if resp.status_code != 200:
            # Try without year
            resp2 = requests.get(f"{BASE_URL}/{title_to_numbers_slug(title, year).replace(f'-({year})', '')}", headers=HEADERS, timeout=20)
            if resp2.status_code != 200:
                log(f"  [{idx}/{total}] ✗ 404: {title} ({slug})")
                return result
            resp = resp2

        soup = BeautifulSoup(resp.text, "html.parser")

        # Find "Daily Box Office Performance" section
        daily_heading = None
        for h2 in soup.find_all("h2"):
            if "Daily Box Office" in h2.get_text():
                daily_heading = h2
                break

        if not daily_heading:
            log(f"  [{idx}/{total}] ✗ No daily section: {title}")
            return result

        table = daily_heading.find_next("table")
        if not table:
            return result

        rows = table.find_all("tr")
        if len(rows) < 2:
            return result

        def parse_dollars(s):
            s = re.sub(r"[^0-9]", "", s)
            return int(s) if s else None

        # Parse first 4-5 rows (Thu preview + Fri + Sat + Sun)
        daily_rows = []
        for row in rows[1:6]:
            cols = [td.get_text(strip=True) for td in row.find_all(["td", "th"])]
            if len(cols) >= 3:
                daily_rows.append(cols)

        if not daily_rows:
            return result

        # Identify Thu/Fri/Sat/Sun rows by date
        # Row 0 might be Thursday preview (rank='P'), Row 1+ are Fri/Sat/Sun
        thu = None
        fri = None
        sat = None
        sun = None
        rank = None
        theaters = None
        per_theater = None

        for i, cols in enumerate(daily_rows):
            date_str = cols[0].replace("\xa0", " ").strip()
            rank_str = cols[1].strip() if len(cols) > 1 else ""
            gross_str = cols[2].strip() if len(cols) > 2 else ""
            gross = parse_dollars(gross_str)

            if rank_str == "P":
                thu = gross
            elif i == 0 or (i == 1 and thu is not None):
                # First non-preview row = Friday
                fri = gross
                rank = rank_str
                theaters = parse_dollars(cols[5]) if len(cols) > 5 else None
                per_theater = parse_dollars(cols[6]) if len(cols) > 6 else None
            elif fri is not None and sat is None:
                sat = gross
            elif sat is not None and sun is None:
                sun = gross

        result["thu_preview"] = thu
        result["fri"] = fri
        result["sat"] = sat
        result["sun"] = sun
        result["opening_rank"] = rank
        result["opening_theaters"] = theaters
        result["per_theater"] = per_theater

        # Compute totals and multipliers
        if fri and sat and sun:
            fri_sat_sun = fri + sat + sun
            result["fri_sat_sun_total"] = fri_sat_sun
            result["fri_sat_sun_mult"] = round(fri_sat_sun / fri, 4)

            weekend_with_thu = fri_sat_sun + (thu or 0)
            result["weekend_total_daily"] = weekend_with_thu
            result["fri_to_weekend_mult"] = round(weekend_with_thu / fri, 4)

        if sat and sun:
            result["sat_to_remaining_mult"] = round((sat + sun) / sat, 4)

        def fmt(v): return f"${v:,}" if v else "N/A"
        log(f"  [{idx}/{total}] {title}: Thu={fmt(thu)} Fri={fmt(fri)} Sat={fmt(sat)} Sun={fmt(sun)} | mult={result['fri_to_weekend_mult']}")

    except Exception as e:
        log(f"  [{idx}/{total}] ERROR {title}: {e}")

    return result


# ── Live mode: project weekend from partial data ──────────────────────────

def project_weekend(title: str, year: int, known_thu: float = None,
                    known_fri: float = None, known_sat: float = None,
                    genre: str = None):
    """
    Given partial opening weekend data, project the full weekend total.
    Uses historical multipliers from data/daily_boxoffice.csv.
    """
    if not os.path.exists("data/daily_boxoffice.csv"):
        print("ERROR: Run historical scrape first (python3 scrape_daily_boxoffice.py)")
        return

    df = pd.read_csv("data/daily_boxoffice.csv")
    df = df[df["fri"].notna() & df["fri_to_weekend_mult"].notna()]

    # Filter to similar tier by Friday gross
    if known_fri:
        # Use movies with Friday gross within 50% of known
        lo, hi = known_fri * 0.4, known_fri * 2.5
        subset = df[df["fri"].between(lo, hi)]
        if len(subset) < 10:
            subset = df  # fall back to all
    else:
        subset = df

    fri_mult_median = subset["fri_to_weekend_mult"].median()
    fri_mult_p25 = subset["fri_to_weekend_mult"].quantile(0.25)
    fri_mult_p75 = subset["fri_to_weekend_mult"].quantile(0.75)
    sat_mult_median = subset["sat_to_remaining_mult"].median() if "sat_to_remaining_mult" in subset.columns else None

    print(f"\n{'='*55}")
    print(f"  🎬  {title} ({year}) — Weekend Projection")
    print(f"{'='*55}")

    if known_thu:
        print(f"  Thu previews:  ${known_thu/1e6:.2f}M")
    if known_fri:
        print(f"  Friday gross:  ${known_fri/1e6:.2f}M")
    if known_sat:
        print(f"  Saturday gross: ${known_sat/1e6:.2f}M")

    print(f"\n  Based on {len(subset)} comparable films:")

    if known_fri and not known_sat:
        proj_median = known_fri * fri_mult_median + (known_thu or 0)
        proj_low = known_fri * fri_mult_p25 + (known_thu or 0)
        proj_high = known_fri * fri_mult_p75 + (known_thu or 0)
        print(f"  Fri→Weekend multiplier: {fri_mult_median:.2f}x (p25={fri_mult_p25:.2f}x, p75={fri_mult_p75:.2f}x)")
        print(f"\n  Projected opening weekend:")
        print(f"    Low  (p25): ${proj_low/1e6:.1f}M")
        print(f"    Mid  (p50): ${proj_median/1e6:.1f}M")
        print(f"    High (p75): ${proj_high/1e6:.1f}M")

    if known_sat:
        # With both Fri and Sat, much tighter projection
        sat_mult = subset["sat_to_remaining_mult"].median()
        sun_proj = known_sat / sat_mult - known_sat  # implied Sunday
        # Actually: sat_mult = (sat+sun)/sat → sun = sat*(sat_mult-1)
        sun_proj = known_sat * (sat_mult - 1)
        total_proj = (known_thu or 0) + (known_fri or known_sat * 1.2) + known_sat + sun_proj
        print(f"  Sat→(Sat+Sun) multiplier: {sat_mult:.2f}x")
        print(f"  Implied Sunday: ${sun_proj/1e6:.1f}M")
        print(f"  Projected opening weekend: ${total_proj/1e6:.1f}M")

    print(f"{'='*55}")


# ── Historical scrape ─────────────────────────────────────────────────────

def scrape_all_historical():
    os.makedirs("data", exist_ok=True)

    df_bom = pd.read_csv("data/bom_movies.csv")
    df_bom = df_bom[df_bom["opening_weekend"].notna()].copy()
    print(f"Movies to process: {len(df_bom)}")

    # Resume from checkpoint
    checkpoint_path = "data/daily_boxoffice_checkpoint.csv"
    output_path = "data/daily_boxoffice.csv"
    done_titles = set()
    results = []

    if os.path.exists(checkpoint_path):
        df_done = pd.read_csv(checkpoint_path)
        done_titles = set(df_done["title"].tolist())
        results = df_done.to_dict("records")
        print(f"Resuming: {len(done_titles)} already done")

    movies = df_bom.to_dict("records")
    remaining = [(m, i+1) for i, m in enumerate(movies) if m["title"] not in done_titles]
    total = len(remaining)
    print(f"Remaining: {total} | Workers: {MAX_WORKERS}\n")

    results_lock = threading.Lock()
    counter = [0]

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        futures = {
            executor.submit(fetch_daily_data, m["title"], int(m["year"]), idx, total): m
            for m, idx in remaining
        }
        for future in as_completed(futures):
            try:
                row = future.result()
            except Exception as e:
                m = futures[future]
                log(f"  ERROR {m['title']}: {e}")
                row = {"title": m["title"], "year": m.get("year")}

            with results_lock:
                results.append(row)
                counter[0] += 1
                if counter[0] % 100 == 0:
                    pd.DataFrame(results).to_csv(checkpoint_path, index=False)
                    log(f"\n✓ Checkpoint: {counter[0]}/{total}\n")

    df = pd.DataFrame(results)
    df.to_csv(output_path, index=False)
    print(f"\n✓ Done. {len(df)} rows → {output_path}")

    # Summary stats
    has_fri = df["fri"].notna().sum()
    has_mult = df["fri_to_weekend_mult"].notna().sum()
    print(f"  With Friday data:    {has_fri}/{len(df)}")
    print(f"  With multipliers:    {has_mult}/{len(df)}")
    if has_mult > 0:
        print(f"  Median Fri→Weekend mult: {df['fri_to_weekend_mult'].median():.2f}x")
        print(f"  Median Fri→Sat+Sun mult: {df['fri_sat_sun_mult'].median():.2f}x")


# ── Live scrape for current movie ─────────────────────────────────────────

def scrape_live(title: str, year: int):
    """Fetch current daily data for a movie in theaters right now."""
    print(f"\nFetching live daily data for: {title} ({year})")
    result = fetch_daily_data(title, year, 1, 1)

    print(f"\n{'='*50}")
    print(f"  {title} ({year}) — Current Daily Data")
    print(f"{'='*50}")
    for key in ["thu_preview", "fri", "sat", "sun", "weekend_total_daily",
                "fri_to_weekend_mult", "opening_theaters", "per_theater"]:
        val = result.get(key)
        if val is not None:
            if isinstance(val, (int, float)) and val > 1000:
                print(f"  {key:30s}: ${val:,.0f}")
            else:
                print(f"  {key:30s}: {val}")
    print(f"{'='*50}")
    return result


# ── Main ──────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--live", type=str, help="Movie title for live projection")
    parser.add_argument("--year", type=int, default=2026)
    parser.add_argument("--fri", type=float, help="Known Friday gross for projection")
    parser.add_argument("--sat", type=float, help="Known Saturday gross for projection")
    parser.add_argument("--thu", type=float, help="Known Thursday preview gross")
    args = parser.parse_args()

    if args.live:
        result = scrape_live(args.live, args.year)
        if args.fri or result.get("fri"):
            project_weekend(
                args.live, args.year,
                known_thu=args.thu or result.get("thu_preview"),
                known_fri=args.fri or result.get("fri"),
                known_sat=args.sat or result.get("sat"),
            )
    else:
        scrape_all_historical()


if __name__ == "__main__":
    main()
