"""
RT + Wayback Machine scraper — corrected version
Snapshot target: Monday 10am ET (= Monday 15:00 UTC) after opening weekend
This matches exactly when Polymarket/Kalshi box office markets resolve.

Concurrency: 5 parallel workers for ~5x speedup (~2.5hr for 1,251 movies)

Input:  data/bom_movies.csv
Output: data/rt_scores.csv

Columns:
  title, year, release_date, rt_slug,
  wayback_timestamp, wayback_date,
  rt_score_monday, rt_reviews_monday, rt_audience_monday,
  rt_score_final, rt_reviews_final, rt_audience_final
"""

import requests
from bs4 import BeautifulSoup
import pandas as pd
import time
import re
import os
import json
from datetime import datetime, timedelta
from concurrent.futures import ThreadPoolExecutor, as_completed
import threading

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    )
}

RT_BASE = "https://www.rottentomatoes.com/m"
WAYBACK_CDX = "https://web.archive.org/cdx/search/cdx"
WAYBACK_BASE = "https://web.archive.org/web"

MAX_WORKERS = 5
CHECKPOINT_EVERY = 50
print_lock = threading.Lock()


def cdx_get(params: dict, max_retries: int = 5) -> list | None:
    """Wayback CDX request with exponential backoff on connection errors."""
    for attempt in range(max_retries):
        try:
            resp = requests.get(WAYBACK_CDX, params=params, timeout=30)
            data = resp.json()
            return data
        except Exception as e:
            wait = 2 ** attempt  # 1, 2, 4, 8, 16 seconds
            if attempt < max_retries - 1:
                log(f"  CDX retry {attempt+1}/{max_retries} (wait {wait}s): {type(e).__name__}")
                time.sleep(wait)
            else:
                log(f"  CDX failed after {max_retries} attempts: {e}")
    return None


def log(msg):
    with print_lock:
        print(msg, flush=True)


# ── Slug utilities ────────────────────────────────────────────────────────

def title_to_rt_slug(title: str) -> str:
    slug = title.lower().strip()
    slug = re.sub(r"[:'\"!?.,]", "", slug)
    slug = slug.replace("&", "and")
    slug = re.sub(r"[\s\-]+", "_", slug)
    slug = re.sub(r"[^\w]", "", slug)
    return re.sub(r"_+", "_", slug).strip("_")


def find_rt_slug(title: str, year: int) -> str | None:
    candidate = title_to_rt_slug(title)
    for slug in [candidate, f"{candidate}_{year}"]:
        url = f"{RT_BASE}/{slug}"
        try:
            resp = requests.get(url, headers=HEADERS, timeout=20, allow_redirects=True)
            if resp.status_code != 200 or "/m/" not in resp.url:
                continue
            soup = BeautifulSoup(resp.text, "html.parser")
            has_rating = any(
                "aggregateRating" in (s.string or "")
                for s in soup.find_all("script", type="application/ld+json")
            )
            page_year = None
            for script in soup.find_all("script", type="application/ld+json"):
                try:
                    d = json.loads(script.string or "")
                    if d.get("@type") == "Movie" and d.get("aggregateRating"):
                        date = d.get("datePublished") or ""
                        if date:
                            page_year = int(date[:4])
                        break
                except Exception:
                    pass
            if page_year is not None and abs(page_year - year) <= 1:
                return resp.url.split("/m/")[-1].split("?")[0].rstrip("/")
            elif page_year is None and has_rating:
                return resp.url.split("/m/")[-1].split("?")[0].rstrip("/")
            elif slug == f"{candidate}_{year}" and resp.status_code == 200:
                return resp.url.split("/m/")[-1].split("?")[0].rstrip("/")
        except Exception:
            pass
    return None


# ── Wednesday/Thursday embargo lift snapshot logic ─────────────────────────
# For pre-release box office prediction, we want the RT score as it existed
# just BEFORE opening weekend — specifically Wed/Thu when press embargoes lift.
# This is the score a bettor would actually see when placing a pre-release bet.

def get_embargo_window(release_date: str) -> tuple[datetime | None, datetime | None]:
    """
    Given a release date (typically Friday), return the Wed-Thu window
    immediately before opening weekend (embargo lift period).
    Returns (wednesday, thursday) datetimes, or (None, None).
    """
    try:
        dt = datetime.strptime(release_date, "%Y-%m-%d")
    except ValueError:
        return None, None

    # Find the Friday of opening weekend (release day is usually Friday)
    # weekday(): Mon=0, Tue=1, Wed=2, Thu=3, Fri=4, Sat=5, Sun=6
    dow = dt.weekday()
    if dow == 4:   # already Friday
        friday = dt
    elif dow == 3: # Thursday (some movies open Thu)
        friday = dt + timedelta(days=1)
    else:
        # Find next Friday
        days_to_fri = (4 - dow) % 7
        friday = dt + timedelta(days=days_to_fri)

    wednesday = friday - timedelta(days=2)
    thursday  = friday - timedelta(days=1)
    return wednesday, thursday


def find_wayback_snapshot(rt_slug: str, release_date: str) -> tuple[str | None, str | None]:
    """
    Find Wayback snapshot on Wed or Thu before opening weekend (embargo lift).
    Target: Thursday evening US time = Thursday 22:00-23:59 UTC.
    Falls back to Wednesday, then any snapshot in the Wed-Fri window.
    Returns (timestamp, readable_date) or (None, None).
    """
    wednesday, thursday = get_embargo_window(release_date)
    if not thursday:
        return None, None

    rt_url = f"rottentomatoes.com/m/{rt_slug}"

    # Primary: Thursday 20:00-23:59 UTC (4pm-7pm ET — after embargo lifts)
    from_ts = thursday.strftime("%Y%m%d") + "200000"
    to_ts   = thursday.strftime("%Y%m%d") + "235959"

    # Primary: Monday 14:00-20:00 UTC window
    data = cdx_get({
        "url": rt_url, "output": "json", "limit": 3,
        "from": from_ts, "to": to_ts,
        "fl": "timestamp,statuscode", "filter": "statuscode:200",
    })
    if data and len(data) > 1:
        ts = data[1][0]
        return ts, f"{ts[:4]}-{ts[4:6]}-{ts[6:8]} {ts[8:10]}:{ts[10:12]} UTC"

    # Fallback 1: full Monday 08:00-23:59 UTC
    from_ts2 = monday.strftime("%Y%m%d") + "080000"
    to_ts2   = monday.strftime("%Y%m%d") + "235959"
    data = cdx_get({
        "url": rt_url, "output": "json", "limit": 5,
        "from": from_ts2, "to": to_ts2,
        "fl": "timestamp,statuscode", "filter": "statuscode:200",
        "collapse": "timestamp:2",
    })
    if data and len(data) > 1:
        best, best_diff = None, float("inf")
        for row in data[1:]:
            ts = row[0]
            hour = int(ts[8:10]) + int(ts[10:12]) / 60
            diff = abs(hour - 15.0)
            if diff < best_diff:
                best_diff, best = diff, ts
        if best:
            return best, f"{best[:4]}-{best[4:6]}-{best[6:8]} {best[8:10]}:{best[10:12]} UTC"

    # Fallback 2: Sun–Tue window
    from_ts3 = (monday - timedelta(days=1)).strftime("%Y%m%d")
    to_ts3   = (monday + timedelta(days=2)).strftime("%Y%m%d")
    data = cdx_get({
        "url": rt_url, "output": "json", "limit": 5,
        "from": from_ts3, "to": to_ts3,
        "fl": "timestamp,statuscode", "filter": "statuscode:200",
        "collapse": "timestamp:8",
    })
    if data and len(data) > 1:
        ts = data[1][0]
        return ts, f"{ts[:4]}-{ts[4:6]}-{ts[6:8]} {ts[8:10]}:{ts[10:12]} UTC (fallback)"

    return None, None


# ── Score extraction ──────────────────────────────────────────────────────

def extract_scores_from_soup(soup: BeautifulSoup) -> dict:
    result = {"rt_score": None, "rt_audience": None, "rt_reviews": None}

    # Critic score: JSON-LD
    for script in soup.find_all("script", type="application/ld+json"):
        try:
            d = json.loads(script.string or "")
            agg = d.get("aggregateRating", {})
            if agg.get("ratingValue") is not None:
                result["rt_score"] = int(agg["ratingValue"])
                result["rt_reviews"] = agg.get("reviewCount")
                break
        except Exception:
            pass

    # Audience score: inline script JSON (live pages)
    for script in soup.find_all("script"):
        text = script.string or ""
        if "audienceScore" in text and '"score"' in text:
            m = re.search(r'"audienceScore"\s*:\s*\{[^}]*"score"\s*:\s*"(\d+)"', text)
            if m:
                result["rt_audience"] = int(m.group(1))
            break

    # Audience score fallback: rt-text slot (archived pages)
    if result["rt_audience"] is None:
        for tag in reversed(soup.find_all("rt-text", attrs={"slot": "audienceScore"})):
            text = tag.get_text(strip=True).replace("%", "")
            if text.isdigit():
                result["rt_audience"] = int(text)
                break

    # Critic score fallback: rt-text slot
    if result["rt_score"] is None:
        for tag in reversed(soup.find_all("rt-text", attrs={"slot": "criticsScore"})):
            text = tag.get_text(strip=True).replace("%", "")
            if text.isdigit():
                result["rt_score"] = int(text)
                break

    # Review count fallback
    if result["rt_reviews"] is None:
        for tag in soup.find_all(attrs={"slot": "criticsCount"}):
            text = re.sub(r"[^\d]", "", tag.get_text())
            if text:
                result["rt_reviews"] = int(text)
                break

    return result


def scrape_wayback_rt(rt_slug: str, wayback_ts: str) -> dict:
    url = f"{WAYBACK_BASE}/{wayback_ts}/https://www.rottentomatoes.com/m/{rt_slug}"
    try:
        resp = requests.get(url, headers=HEADERS, timeout=40)
        if resp.status_code != 200:
            return {}
        return extract_scores_from_soup(BeautifulSoup(resp.text, "html.parser"))
    except Exception as e:
        log(f"  Wayback fetch error [{rt_slug}]: {e}")
        return {}


def scrape_current_rt(rt_slug: str) -> dict:
    url = f"https://www.rottentomatoes.com/m/{rt_slug}"
    try:
        resp = requests.get(url, headers=HEADERS, timeout=20)
        if resp.status_code != 200:
            return {}
        return extract_scores_from_soup(BeautifulSoup(resp.text, "html.parser"))
    except Exception as e:
        log(f"  Current RT error [{rt_slug}]: {e}")
        return {}


# ── Per-movie worker ──────────────────────────────────────────────────────

def process_movie(movie: dict, idx: int, total: int) -> dict:
    title = movie["title"]
    release_date = str(movie.get("release_date", ""))
    year = int(movie.get("year", 2020))

    log(f"[{idx}/{total}] {title} ({release_date})")

    row = {
        "title": title,
        "year": year,
        "release_date": release_date,
        "rt_slug": None,
        "wayback_timestamp": None,
        "wayback_date": None,
        "rt_score_monday": None,
        "rt_reviews_monday": None,
        "rt_audience_monday": None,
        "rt_score_final": None,
        "rt_reviews_final": None,
        "rt_audience_final": None,
    }

    # Step 1: RT slug
    rt_slug = find_rt_slug(title, year)
    if not rt_slug:
        log(f"  ✗ No RT slug: {title}")
        return row
    row["rt_slug"] = rt_slug
    time.sleep(0.5)

    # Step 2: Monday 10am ET Wayback snapshot
    wayback_ts, wayback_date = find_wayback_snapshot(rt_slug, release_date)
    if wayback_ts:
        row["wayback_timestamp"] = wayback_ts
        row["wayback_date"] = wayback_date
        log(f"  Wayback: {wayback_date}")
        time.sleep(0.5)

        # Step 3: Scrape archived page
        monday_scores = scrape_wayback_rt(rt_slug, wayback_ts)
        row["rt_score_monday"] = monday_scores.get("rt_score")
        row["rt_reviews_monday"] = monday_scores.get("rt_reviews")
        row["rt_audience_monday"] = monday_scores.get("rt_audience")
        log(f"  Monday score: {row['rt_score_monday']}% ({row['rt_reviews_monday']} reviews) aud={row['rt_audience_monday']}%")
        time.sleep(0.5)
    else:
        log(f"  ✗ No Wayback snapshot: {title}")

    # Step 4: Current final score
    final_scores = scrape_current_rt(rt_slug)
    row["rt_score_final"] = final_scores.get("rt_score")
    row["rt_reviews_final"] = final_scores.get("rt_reviews")
    row["rt_audience_final"] = final_scores.get("rt_audience")
    log(f"  Final score:  {row['rt_score_final']}% ({row['rt_reviews_final']} reviews) aud={row['rt_audience_final']}%")
    time.sleep(0.5)

    return row


# ── Main ──────────────────────────────────────────────────────────────────

def main():
    os.makedirs("data", exist_ok=True)

    # Load BOM
    df_bom = pd.read_csv("data/bom_movies.csv")
    df_bom = df_bom[df_bom["opening_weekend"].notna()].copy()
    print(f"Movies with opening weekend data: {len(df_bom)}")

    # Resume from checkpoint
    output_path = "data/rt_scores.csv"
    checkpoint_path = "data/rt_scores_checkpoint.csv"
    done_titles = set()
    results = []

    if os.path.exists(checkpoint_path):
        df_done = pd.read_csv(checkpoint_path)
        done_titles = set(df_done["title"].tolist())
        results = df_done.to_dict("records")
        print(f"Resuming: {len(done_titles)} movies already done")

    movies = df_bom.to_dict("records")
    remaining = [m for m in movies if m["title"] not in done_titles]
    total = len(remaining)
    print(f"Remaining: {total} movies | Workers: {MAX_WORKERS}\n")

    results_lock = threading.Lock()
    counter = [0]  # mutable for closure

    def worker(args):
        movie, idx = args
        return process_movie(movie, idx, total)

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        futures = {
            executor.submit(process_movie, movie, i + 1, total): movie
            for i, movie in enumerate(remaining)
        }

        for future in as_completed(futures):
            try:
                row = future.result()
            except Exception as e:
                movie = futures[future]
                log(f"  ERROR processing {movie['title']}: {e}")
                row = {"title": movie["title"], "year": movie.get("year")}

            with results_lock:
                results.append(row)
                counter[0] += 1
                if counter[0] % CHECKPOINT_EVERY == 0:
                    pd.DataFrame(results).to_csv(checkpoint_path, index=False)
                    log(f"\n✓ Checkpoint: {counter[0]}/{total} done\n")

    # Final save
    df = pd.DataFrame(results)
    df.to_csv(output_path, index=False)
    print(f"\n✓ Done. {len(df)} rows saved to {output_path}")

    has_monday = df["rt_score_monday"].notna().sum()
    has_final = df["rt_score_final"].notna().sum()
    print(f"  Monday scores:  {has_monday}/{len(df)}")
    print(f"  Final scores:   {has_final}/{len(df)}")
    print(df[["title", "rt_score_monday", "rt_reviews_monday", "rt_audience_monday", "rt_score_final"]].head(10).to_string())


if __name__ == "__main__":
    main()
