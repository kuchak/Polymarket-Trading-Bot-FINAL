"""
RT + Wayback Machine scraper
For each movie in bom_movies.csv:
  1. Find its Rotten Tomatoes URL slug
  2. Find the nearest Wayback snapshot on/after release date
  3. Scrape tomatometer score + review count from that snapshot
  4. Also scrape current RT page for final score + audience score

Output: data/rt_scores.csv
Columns:
  title, release_date, rt_slug,
  rt_score_release_day, rt_reviews_release_day,
  rt_audience_release_day,
  rt_score_final, rt_reviews_final, rt_audience_final,
  wayback_timestamp
"""

import requests
from bs4 import BeautifulSoup
import pandas as pd
import time
import re
import os
import json
from datetime import datetime, timedelta
from urllib.parse import quote

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


# ── Slug utilities ─────────────────────────────────────────────────────────

def title_to_rt_slug(title: str) -> str:
    """Convert movie title to likely RT slug. e.g. 'Inside Out 2' → 'inside_out_2'"""
    slug = title.lower().strip()
    # Remove common punctuation
    slug = re.sub(r"[:'\"!?.,]", "", slug)
    # Replace & with and
    slug = slug.replace("&", "and")
    # Replace spaces/hyphens with underscore
    slug = re.sub(r"[\s\-]+", "_", slug)
    # Remove any remaining non-alphanumeric except underscore
    slug = re.sub(r"[^\w]", "", slug)
    # Collapse multiple underscores
    slug = re.sub(r"_+", "_", slug).strip("_")
    return slug


def find_rt_slug(title: str, year: int) -> str | None:
    """
    Search RT to find the correct slug for a movie.
    Uses RT's search endpoint.
    """
    # First try the obvious slug
    candidate = title_to_rt_slug(title)
    url = f"{RT_BASE}/{candidate}"
    try:
        resp = requests.get(url, headers=HEADERS, timeout=20, allow_redirects=True)
        if resp.status_code == 200 and "/m/" in resp.url:
            # Verify it's the right year via JSON-LD
            soup = BeautifulSoup(resp.text, "html.parser")
            page_year = extract_year_from_page(soup)
            if page_year is None or abs(page_year - year) <= 1:
                # Extract the actual slug from the final URL
                return resp.url.split("/m/")[-1].split("?")[0].rstrip("/")
    except Exception:
        pass

    # If not found, try with year suffix
    candidate_year = f"{candidate}_{year}"
    url = f"{RT_BASE}/{candidate_year}"
    try:
        resp = requests.get(url, headers=HEADERS, timeout=20, allow_redirects=True)
        if resp.status_code == 200 and "/m/" in resp.url:
            return resp.url.split("/m/")[-1].split("?")[0].rstrip("/")
    except Exception:
        pass

    return None


def extract_year_from_page(soup: BeautifulSoup) -> int | None:
    for script in soup.find_all("script", type="application/ld+json"):
        try:
            d = json.loads(script.string)
            date = d.get("datePublished") or d.get("dateCreated") or ""
            if date:
                return int(date[:4])
        except Exception:
            pass
    return None


# ── Wayback CDX lookup ─────────────────────────────────────────────────────

def find_wayback_snapshot(rt_slug: str, release_date: str) -> str | None:
    """
    Find the nearest Wayback snapshot of an RT movie page at/after release date.
    Returns a timestamp string like '20240614012224', or None.
    """
    rt_url = f"rottentomatoes.com/m/{rt_slug}"

    # Try: snapshot on release date ± 3 days
    try:
        dt = datetime.strptime(release_date, "%Y-%m-%d")
    except ValueError:
        return None

    from_ts = (dt - timedelta(days=1)).strftime("%Y%m%d")
    to_ts = (dt + timedelta(days=4)).strftime("%Y%m%d")

    try:
        resp = requests.get(
            WAYBACK_CDX,
            params={
                "url": rt_url,
                "output": "json",
                "limit": 5,
                "from": from_ts,
                "to": to_ts,
                "fl": "timestamp,statuscode",
                "filter": "statuscode:200",
                "collapse": "timestamp:8",  # one per day
            },
            timeout=45,
        )
        data = resp.json()
        if len(data) > 1:  # first row is header
            return data[1][0]  # earliest good snapshot
    except Exception as e:
        print(f"  CDX error for {rt_slug}: {e}")

    # Fallback: look a week before (for early reviews / press screenings)
    from_ts2 = (dt - timedelta(days=7)).strftime("%Y%m%d")
    try:
        resp = requests.get(
            WAYBACK_CDX,
            params={
                "url": rt_url,
                "output": "json",
                "limit": 3,
                "from": from_ts2,
                "to": to_ts,
                "fl": "timestamp,statuscode",
                "filter": "statuscode:200",
                "collapse": "timestamp:8",
            },
            timeout=45,
        )
        data = resp.json()
        if len(data) > 1:
            return data[1][0]
    except Exception as e:
        print(f"  CDX fallback error for {rt_slug}: {e}")

    return None


# ── RT page score extraction ───────────────────────────────────────────────

def extract_scores_from_soup(soup: BeautifulSoup) -> dict:
    """Extract tomatometer, audience score, review count from a parsed RT page."""
    result = {
        "rt_score": None,
        "rt_audience": None,
        "rt_reviews": None,
        "rt_audience_count": None,
    }

    # Primary: JSON-LD (most reliable in archived pages)
    for script in soup.find_all("script", type="application/ld+json"):
        try:
            d = json.loads(script.string)
            agg = d.get("aggregateRating", {})
            if agg.get("ratingValue") is not None:
                result["rt_score"] = int(agg["ratingValue"])
                result["rt_reviews"] = agg.get("reviewCount")
                break
        except Exception:
            pass

    # Audience score — from rt-text slot="audienceScore"
    audience_tags = soup.find_all("rt-text", attrs={"slot": "audienceScore"})
    for tag in reversed(audience_tags):  # last one is usually the main scoreboard
        text = tag.get_text(strip=True).replace("%", "")
        if text.isdigit():
            result["rt_audience"] = int(text)
            break

    # If JSON-LD didn't give us the score, try rt-text slot="criticsScore"
    if result["rt_score"] is None:
        critic_tags = soup.find_all("rt-text", attrs={"slot": "criticsScore"})
        for tag in reversed(critic_tags):
            text = tag.get_text(strip=True).replace("%", "")
            if text.isdigit():
                result["rt_score"] = int(text)
                break

    # Review count fallback — slot="criticsCount"
    if result["rt_reviews"] is None:
        for tag in soup.find_all(attrs={"slot": "criticsCount"}):
            text = re.sub(r"[^\d]", "", tag.get_text())
            if text:
                result["rt_reviews"] = int(text)
                break

    return result


def scrape_wayback_rt(rt_slug: str, wayback_ts: str) -> dict:
    """Fetch the archived RT page and extract scores."""
    url = f"{WAYBACK_BASE}/{wayback_ts}/https://www.rottentomatoes.com/m/{rt_slug}"
    try:
        resp = requests.get(url, headers=HEADERS, timeout=45)
        if resp.status_code != 200:
            return {}
        soup = BeautifulSoup(resp.text, "html.parser")
        return extract_scores_from_soup(soup)
    except Exception as e:
        print(f"  Wayback fetch error [{rt_slug}]: {e}")
        return {}


def scrape_current_rt(rt_slug: str) -> dict:
    """Fetch current live RT page for final/current scores."""
    url = f"https://www.rottentomatoes.com/m/{rt_slug}"
    try:
        resp = requests.get(url, headers=HEADERS, timeout=20)
        if resp.status_code != 200:
            return {}
        soup = BeautifulSoup(resp.text, "html.parser")
        return extract_scores_from_soup(soup)
    except Exception as e:
        print(f"  Current RT error [{rt_slug}]: {e}")
        return {}


# ── Main ───────────────────────────────────────────────────────────────────

def main():
    os.makedirs("data", exist_ok=True)

    # Load BOM data — prefer full file, fall back to checkpoint
    if os.path.exists("data/bom_movies.csv"):
        df_bom = pd.read_csv("data/bom_movies.csv")
    elif os.path.exists("data/bom_movies_checkpoint.csv"):
        df_bom = pd.read_csv("data/bom_movies_checkpoint.csv")
        print("WARNING: Using BOM checkpoint (not final). Re-run after BOM scrape completes.")
    else:
        print("ERROR: No BOM data found. Run scrape_boxofficemojo.py first.")
        return

    # Only keep movies with an opening weekend gross (wide releases)
    if "opening_weekend" in df_bom.columns:
        df_bom = df_bom[df_bom["opening_weekend"].notna()]

    print(f"Processing {len(df_bom)} movies with opening weekend data")

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
    print(f"Remaining: {len(remaining)} movies\n")

    for i, movie in enumerate(remaining):
        title = movie["title"]
        release_date = str(movie.get("release_date", ""))
        year = int(movie.get("year", 2020))

        print(f"[{i+1}/{len(remaining)}] {title} ({release_date})")

        row = {
            "title": title,
            "year": year,
            "release_date": release_date,
            "rt_slug": None,
            "wayback_timestamp": None,
            "rt_score_release_day": None,
            "rt_reviews_release_day": None,
            "rt_audience_release_day": None,
            "rt_score_final": None,
            "rt_reviews_final": None,
            "rt_audience_final": None,
        }

        # Step 1: Find RT slug
        rt_slug = find_rt_slug(title, year)
        if not rt_slug:
            print(f"  ✗ RT slug not found")
            results.append(row)
            time.sleep(1)
            continue

        row["rt_slug"] = rt_slug
        print(f"  RT slug: {rt_slug}")
        time.sleep(0.8)

        # Step 2: Find Wayback snapshot
        wayback_ts = find_wayback_snapshot(rt_slug, release_date)
        if not wayback_ts:
            print(f"  ✗ No Wayback snapshot found")
        else:
            row["wayback_timestamp"] = wayback_ts
            print(f"  Wayback: {wayback_ts}")
            time.sleep(1.0)

            # Step 3: Scrape archived page
            archived_scores = scrape_wayback_rt(rt_slug, wayback_ts)
            row["rt_score_release_day"] = archived_scores.get("rt_score")
            row["rt_reviews_release_day"] = archived_scores.get("rt_reviews")
            row["rt_audience_release_day"] = archived_scores.get("rt_audience")
            print(f"  Release-day score: {row['rt_score_release_day']}% ({row['rt_reviews_release_day']} reviews)")
            time.sleep(1.2)

        # Step 4: Current RT score (final)
        current_scores = scrape_current_rt(rt_slug)
        row["rt_score_final"] = current_scores.get("rt_score")
        row["rt_reviews_final"] = current_scores.get("rt_reviews")
        row["rt_audience_final"] = current_scores.get("rt_audience")
        print(f"  Final score: {row['rt_score_final']}% ({row['rt_reviews_final']} reviews)")

        results.append(row)
        time.sleep(1.0)

        # Checkpoint every 50
        if (i + 1) % 50 == 0:
            pd.DataFrame(results).to_csv(checkpoint_path, index=False)
            print(f"  ✓ Checkpoint saved ({len(results)} total)\n")

    df = pd.DataFrame(results)
    df.to_csv(output_path, index=False)
    print(f"\n✓ Done. {len(df)} rows saved to {output_path}")

    # Summary stats
    has_release = df["rt_score_release_day"].notna().sum()
    has_final = df["rt_score_final"].notna().sum()
    print(f"  Release-day scores found: {has_release}/{len(df)}")
    print(f"  Final scores found:       {has_final}/{len(df)}")
    print(df[["title", "rt_score_release_day", "rt_reviews_release_day", "rt_score_final"]].head(10).to_string())


if __name__ == "__main__":
    main()
