"""
Box Office Mojo scraper — wide release movies, 2020–May 2026
Output: data/bom_movies.csv

Columns:
  title, year, release_date, opening_weekend, opening_theaters,
  total_gross_domestic, budget, mpaa_rating, runtime_min,
  distributor, genre, detail_url
"""

import requests
from bs4 import BeautifulSoup
import pandas as pd
import time
import re
import os
from datetime import datetime

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    )
}

BASE_URL = "https://www.boxofficemojo.com"
YEARS = list(range(2020, 2027))  # 2020–2026 inclusive
CUTOFF_DATE = datetime(2026, 5, 31)


def parse_dollars(s: str) -> float | None:
    s = re.sub(r"[^0-9.]", "", s)
    try:
        return float(s) if s else None
    except ValueError:
        return None


def scrape_yearly_chart(year: int) -> list[dict]:
    """Get all movies from BOM yearly domestic chart."""
    url = f"{BASE_URL}/year/{year}/"
    print(f"  GET {url}")
    resp = requests.get(url, headers=HEADERS, timeout=30)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")

    table = soup.find("table")
    if not table:
        print(f"  WARNING: no table for {year}")
        return []

    # Column indices from header
    headers_els = table.find_all("th")
    col_names = [th.get_text(strip=True) for th in headers_els]
    # Find relevant column indices
    def col_idx(name):
        for i, h in enumerate(col_names):
            if name.lower() in h.lower():
                return i
        return None

    release_col = col_idx("Release") or 1
    gross_col = col_idx("Total Gross") or col_idx("Gross") or 5
    theaters_col = col_idx("Theaters") or 6
    date_col = col_idx("Release Date") or 8
    genre_col = col_idx("Genre")
    budget_col = col_idx("Budget")

    movies = []
    for row in table.find_all("tr")[1:]:  # skip header row
        cols = row.find_all("td")
        if not cols:
            continue
        try:
            # Title + detail URL
            title_cell = cols[release_col] if release_col < len(cols) else cols[1]
            a_tag = title_cell.find("a")
            title = a_tag.get_text(strip=True) if a_tag else title_cell.get_text(strip=True)
            href = a_tag["href"] if a_tag and a_tag.get("href") else None
            detail_url = BASE_URL + href if href else None

            # Total gross
            total_gross = parse_dollars(cols[gross_col].get_text(strip=True)) if gross_col and gross_col < len(cols) else None

            # Max theaters
            theaters = None
            if theaters_col and theaters_col < len(cols):
                t = cols[theaters_col].get_text(strip=True).replace(",", "")
                theaters = int(t) if t.isdigit() else None

            # Release date
            release_date_raw = cols[date_col].get_text(strip=True) if date_col and date_col < len(cols) else ""

            # Genre (hidden col, may be empty in HTML)
            genre = cols[genre_col].get_text(strip=True) if genre_col and genre_col < len(cols) else None

            # Budget (hidden col)
            budget = parse_dollars(cols[budget_col].get_text(strip=True)) if budget_col and budget_col < len(cols) else None

            # Parse release date for cutoff filter
            release_dt = None
            for fmt in ("%b %d, %Y", "%B %d, %Y", "%m/%d/%Y"):
                try:
                    release_dt = datetime.strptime(release_date_raw, fmt)
                    break
                except ValueError:
                    pass

            if release_dt and release_dt > CUTOFF_DATE:
                continue  # Skip movies after May 2026

            movies.append({
                "title": title,
                "year": year,
                "release_date": release_dt.strftime("%Y-%m-%d") if release_dt else release_date_raw,
                "total_gross_domestic": total_gross,
                "max_theaters": theaters,
                "genre": genre,
                "budget_chart": budget,
                "detail_url": detail_url,
            })
        except Exception as e:
            print(f"  Row parse error: {e}")
            continue

    print(f"  → {len(movies)} movies for {year}")
    return movies


def scrape_movie_detail(movie: dict) -> dict:
    """Fetch detail page: opening weekend, budget, rating, runtime, distributor."""
    if not movie.get("detail_url"):
        return movie

    try:
        resp = requests.get(movie["detail_url"], headers=HEADERS, timeout=30)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "html.parser")

        # "Summary Details" section contains: Distributor, Opening, Budget, Release Date, MPAA, Runtime, Genres
        # It renders as a series of label/value spans
        summary_text = ""
        for div in soup.find_all("div", class_=re.compile(r"mojo-summary", re.I)):
            summary_text += " " + div.get_text(" ", strip=True)

        # Opening weekend + theater count
        # Format: "Opening $154,201,673 4,440 theaters"
        opening_m = re.search(r"Opening\s+\$([\d,]+)\s+([\d,]+)\s+theaters", summary_text)
        if opening_m:
            movie["opening_weekend"] = float(opening_m.group(1).replace(",", ""))
            movie["opening_theaters"] = int(opening_m.group(2).replace(",", ""))
        else:
            # Try without theaters
            opening_m2 = re.search(r"Opening\s+\$([\d,]+)", summary_text)
            if opening_m2:
                movie["opening_weekend"] = float(opening_m2.group(1).replace(",", ""))

        # Budget
        budget_m = re.search(r"Budget\s+\$([\d,]+)", summary_text)
        if budget_m:
            movie["budget"] = float(budget_m.group(1).replace(",", ""))

        # MPAA rating
        rating_m = re.search(r"\b(G|PG|PG-13|R|NC-17|NR|UR)\b", summary_text)
        if rating_m:
            movie["mpaa_rating"] = rating_m.group(1)

        # Runtime
        runtime_m = re.search(r"(\d+)\s*hr\s*(\d+)\s*min", summary_text)
        if runtime_m:
            movie["runtime_min"] = int(runtime_m.group(1)) * 60 + int(runtime_m.group(2))
        else:
            runtime_m2 = re.search(r"(\d+)\s*min", summary_text)
            if runtime_m2:
                movie["runtime_min"] = int(runtime_m2.group(1))

        # Release date from detail page (overrides the partial date from chart)
        rel_date_m = re.search(r"Release Date\s+([A-Za-z]+ \d+, \d{4})", summary_text)
        if rel_date_m:
            for fmt in ("%b %d, %Y", "%B %d, %Y"):
                try:
                    dt = datetime.strptime(rel_date_m.group(1), fmt)
                    movie["release_date"] = dt.strftime("%Y-%m-%d")
                    break
                except ValueError:
                    pass

        # Distributor
        dist_m = re.search(r"Distributor\s+([A-Za-z0-9 &',.\-]+?)(?:See full|Budget|Opening|Release Date|MPAA|$)", summary_text)
        if dist_m:
            movie["distributor"] = dist_m.group(1).strip()

        # Genre — find the dedicated summary div (3rd one) and extract genres cleanly
        summary_divs = soup.find_all("div", class_=re.compile(r"mojo-summary-values", re.I))
        if not summary_divs:
            # fallback: use the longest summary div
            summary_divs = sorted(
                soup.find_all("div", class_=re.compile(r"mojo-summary", re.I)),
                key=lambda d: len(d.get_text()), reverse=True
            )[:1]
        for sdiv in summary_divs:
            genre_span = None
            # Look for the label "Genres" then grab sibling spans
            for span in sdiv.find_all("span"):
                if re.match(r"Genres?$", span.get_text(strip=True), re.I):
                    # Next sibling div or span contains the genre list
                    parent = span.parent
                    # Grab all anchor tags after this span (BOM links each genre)
                    genre_links = parent.find_all("a", href=re.compile(r"/genre/"))
                    if genre_links:
                        movie["genre"] = ", ".join(a.get_text(strip=True) for a in genre_links)
                        break
                    # Fallback: text after the label
                    text_after = span.parent.get_text(" ").split("Genres")[-1]
                    clean = re.sub(r"\s+", " ", text_after).strip()
                    # Only keep the part before the next known label
                    clean = re.split(r"Widest Release|IMDbPro|Distributor|Opening|Budget|MPAA", clean)[0]
                    genres = re.findall(r"[A-Z][a-z]+(?:[ \-][A-Z][a-z]+)*", clean)
                    if genres:
                        movie["genre"] = ", ".join(genres)
                    break
            if movie.get("genre"):
                break

    except Exception as e:
        print(f"  Detail error [{movie['title']}]: {e}")

    return movie


def main():
    os.makedirs("data", exist_ok=True)

    # ── Step 1: Yearly charts ──────────────────────────────────────────────
    checkpoint_raw = "data/bom_movies_raw.csv"
    if os.path.exists(checkpoint_raw):
        print(f"Loading existing raw list from {checkpoint_raw}")
        all_movies = pd.read_csv(checkpoint_raw).to_dict("records")
    else:
        all_movies = []
        for year in YEARS:
            print(f"\n=== {year} ===")
            movies = scrape_yearly_chart(year)
            all_movies.extend(movies)
            time.sleep(1.5)

        pd.DataFrame(all_movies).to_csv(checkpoint_raw, index=False)
        print(f"\nSaved {len(all_movies)} movies to {checkpoint_raw}")

    # ── Step 2: Detail pages ───────────────────────────────────────────────
    output_path = "data/bom_movies.csv"
    checkpoint_detail = "data/bom_movies_checkpoint.csv"

    # Resume from checkpoint if exists
    done_titles = set()
    enriched = []
    if os.path.exists(checkpoint_detail):
        df_done = pd.read_csv(checkpoint_detail)
        done_titles = set(df_done["title"].tolist())
        enriched = df_done.to_dict("records")
        print(f"Resuming from checkpoint: {len(done_titles)} movies already done")

    remaining = [m for m in all_movies if m["title"] not in done_titles]
    print(f"\nFetching detail pages for {len(remaining)} movies...")

    for i, movie in enumerate(remaining):
        print(f"[{i+1}/{len(remaining)}] {movie['title']}")
        enriched.append(scrape_movie_detail(movie))
        time.sleep(1.2)

        if (i + 1) % 50 == 0:
            pd.DataFrame(enriched).to_csv(checkpoint_detail, index=False)
            print(f"  ✓ Checkpoint saved ({len(enriched)} total)")

    df = pd.DataFrame(enriched)
    df.to_csv(output_path, index=False)
    print(f"\n✓ Done. {len(df)} movies saved to {output_path}")

    # Quick summary
    has_opening = df["opening_weekend"].notna().sum() if "opening_weekend" in df.columns else 0
    print(f"  Movies with opening weekend data: {has_opening}/{len(df)}")
    print(df[["title", "year", "release_date", "opening_weekend", "total_gross_domestic"]].head(10).to_string())


if __name__ == "__main__":
    main()
