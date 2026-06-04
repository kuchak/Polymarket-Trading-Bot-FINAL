"""
Scrape The Numbers daily box office charts by DATE.

Instead of hitting individual movie pages (60% 404 rate), we pull the
daily chart for every day from 2020-01-01 to 2026-05-31 and extract
each movie's opening weekend (Thu preview + Fri + Sat + Sun).

Output: data/daily_by_date.csv  — one row per movie-day
        data/opening_weekends.csv — one row per movie with Thu/Fri/Sat/Sun/total

~2,300 days × 1 request = ~45 min at 1.2s/request with 5 workers
"""

import requests
from bs4 import BeautifulSoup
import pandas as pd
import numpy as np
import re
import os
import time
from datetime import date, timedelta
from concurrent.futures import ThreadPoolExecutor, as_completed
import threading

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    )
}

BASE = "https://www.the-numbers.com/box-office-chart/daily"
START_DATE = date(2020, 1, 1)
END_DATE   = date(2026, 5, 31)
MAX_WORKERS = 5
print_lock = threading.Lock()


def log(msg):
    with print_lock:
        print(msg, flush=True)


def parse_dollars(s: str) -> float | None:
    s = re.sub(r"[^0-9]", "", s)
    return float(s) if s else None


def scrape_day(d: date) -> list[dict]:
    """Fetch the daily chart for a single date. Returns list of {date, title, gross, rank, theaters, days_in_release}."""
    url = f"{BASE}/{d.strftime('%Y/%m/%d')}"
    try:
        resp = requests.get(url, headers=HEADERS, timeout=20)
        if resp.status_code != 200:
            return []
        soup = BeautifulSoup(resp.text, "html.parser")
        table = soup.find("table")
        if not table:
            return []

        rows = []
        for tr in table.find_all("tr")[1:]:  # skip header
            cols = [td.get_text(strip=True) for td in tr.find_all(["td", "th"])]
            if len(cols) < 6:
                continue
            try:
                # Cols: Rank, Prev, Title, Gross, DailyChange, WeeklyChange, Theaters, AvgPerTheater, TotalGross, DaysInRelease
                rank          = cols[0]
                title         = cols[2]
                gross         = parse_dollars(cols[3])
                theaters      = parse_dollars(cols[6]) if len(cols) > 6 else None
                total_gross   = parse_dollars(cols[8]) if len(cols) > 8 else None
                days_in_rel   = parse_dollars(cols[9]) if len(cols) > 9 else None

                # "new" in prev rank means day 1 (opening day)
                is_new = "(new)" in cols[1].lower() if len(cols) > 1 else False

                rows.append({
                    "date": d.isoformat(),
                    "rank": rank,
                    "title": title,
                    "gross": gross,
                    "theaters": theaters,
                    "total_gross_to_date": total_gross,
                    "days_in_release": int(days_in_rel) if days_in_rel else None,
                    "is_opening_day": 1 if is_new else 0,
                })
            except Exception:
                continue
        return rows
    except Exception as e:
        log(f"  ERROR {d}: {e}")
        return []


def build_opening_weekends(df: pd.DataFrame) -> pd.DataFrame:
    """
    From daily data, reconstruct Thu preview / Fri / Sat / Sun for each film.
    Opening weekend = first Fri-Sun (days_in_release 1-3, or 0-3 with Thu preview).
    """
    df["date"] = pd.to_datetime(df["date"])
    df["dow"] = df["date"].dt.dayofweek  # 0=Mon, 4=Fri, 5=Sat, 6=Sun, 3=Thu

    results = []
    for title, group in df.groupby("title"):
        group = group.sort_values("date")

        # Find opening day (days_in_release == 1, or is_opening_day == 1)
        opening_rows = group[group["is_opening_day"] == 1]
        if opening_rows.empty:
            opening_rows = group[group["days_in_release"] == 1]
        if opening_rows.empty:
            continue

        open_date = opening_rows.iloc[0]["date"]
        open_dow  = open_date.dayofweek  # 4=Fri, 3=Thu

        # Get Thu/Fri/Sat/Sun by days_in_release or date offset
        thu = fri = sat = sun = None
        fri_date = None

        if open_dow == 3:  # opened Thursday
            thu_row = group[group["date"] == open_date]
            thu = thu_row["gross"].values[0] if len(thu_row) else None
            fri_date = open_date + timedelta(days=1)
        elif open_dow == 4:  # opened Friday
            fri_date = open_date
            # Check for Thursday preview (day before, labeled as preview)
            thu_date = open_date - timedelta(days=1)
            thu_row = group[group["date"] == thu_date]
            if not thu_row.empty:
                thu = thu_row["gross"].values[0]
        else:
            fri_date = open_date  # fallback

        if fri_date is not None:
            fri_row = group[group["date"] == fri_date]
            fri = fri_row["gross"].values[0] if not fri_row.empty else None

            sat_row = group[group["date"] == fri_date + timedelta(days=1)]
            sat = sat_row["gross"].values[0] if not sat_row.empty else None

            sun_row = group[group["date"] == fri_date + timedelta(days=2)]
            sun = sun_row["gross"].values[0] if not sun_row.empty else None

        # Compute totals and multipliers
        weekend_total = sum(x for x in [thu, fri, sat, sun] if x)
        fri_sat_sun   = sum(x for x in [fri, sat, sun] if x)

        results.append({
            "title": title,
            "opening_date": open_date.date().isoformat(),
            "thu_preview": thu,
            "fri": fri,
            "sat": sat,
            "sun": sun,
            "weekend_total": weekend_total or None,
            "fri_sat_sun_total": fri_sat_sun or None,
            "fri_to_weekend_mult": round(weekend_total / fri, 4) if fri and weekend_total else None,
            "fri_sat_sun_mult":    round(fri_sat_sun / fri, 4)   if fri and fri_sat_sun else None,
            "sat_to_sun_ratio":    round(sun / sat, 4)           if sat and sun else None,
            "opening_theaters":    opening_rows.iloc[0]["theaters"],
        })

    return pd.DataFrame(results)


def main():
    os.makedirs("data", exist_ok=True)

    # Generate all dates
    all_dates = []
    d = START_DATE
    while d <= END_DATE:
        all_dates.append(d)
        d += timedelta(days=1)

    total_days = len(all_dates)
    print(f"Dates to scrape: {total_days} ({START_DATE} → {END_DATE})")

    # Resume from checkpoint
    raw_checkpoint = "data/daily_by_date_checkpoint.csv"
    done_dates = set()
    all_rows = []

    if os.path.exists(raw_checkpoint):
        df_done = pd.read_csv(raw_checkpoint)
        done_dates = set(df_done["date"].tolist())
        all_rows = df_done.to_dict("records")
        print(f"Resuming: {len(done_dates)} dates already done")

    remaining = [d for d in all_dates if d.isoformat() not in done_dates]
    print(f"Remaining: {len(remaining)} dates | Workers: {MAX_WORKERS}\n")

    results_lock = threading.Lock()
    counter = [0]

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        futures = {executor.submit(scrape_day, d): d for d in remaining}
        for future in as_completed(futures):
            d = futures[future]
            try:
                rows = future.result()
            except Exception as e:
                log(f"  ERROR {d}: {e}")
                rows = []

            with results_lock:
                all_rows.extend(rows)
                counter[0] += 1
                if counter[0] % 100 == 0:
                    pd.DataFrame(all_rows).to_csv(raw_checkpoint, index=False)
                    log(f"✓ Checkpoint: {counter[0]}/{len(remaining)} dates ({len(all_rows)} rows)")

            time.sleep(0.3)  # polite rate limit per worker

    # Save raw daily data
    df_raw = pd.DataFrame(all_rows)
    df_raw.to_csv("data/daily_by_date.csv", index=False)
    print(f"\n✓ Raw daily data: {len(df_raw)} rows → data/daily_by_date.csv")

    # Build opening weekend summary
    print("\nBuilding opening weekend summary...")
    df_weekends = build_opening_weekends(df_raw)
    df_weekends.to_csv("data/opening_weekends.csv", index=False)
    print(f"✓ Opening weekends: {len(df_weekends)} films → data/opening_weekends.csv")

    # Stats
    has_full = df_weekends[df_weekends["fri"].notna() & df_weekends["sat"].notna() & df_weekends["sun"].notna()]
    print(f"  Films with full Thu/Fri/Sat/Sun: {len(has_full)}")
    print(f"  Median Fri→Weekend mult: {df_weekends['fri_to_weekend_mult'].median():.2f}x")
    print(f"  Median Fri→FriSatSun mult: {df_weekends['fri_sat_sun_mult'].median():.2f}x")
    print(df_weekends[["title","opening_date","thu_preview","fri","sat","sun","fri_to_weekend_mult"]].head(10).to_string())


if __name__ == "__main__":
    main()
