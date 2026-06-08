"""
scrape_live_friday.py
=====================
Polls The Numbers every 10 minutes on Saturday morning until Friday gross
numbers post for a target film, then runs predict_live.py inference and
prints the projection.

Usage:
    python3 scrape_live_friday.py --title "Masters of the Universe" --year 2026 --week 1
    python3 scrape_live_friday.py --title "Backrooms" --year 2026 --week 2 --notify
    python3 scrape_live_friday.py --title "Obsession" --week 3 --competition 18000000

Options:
    --title         Movie title (required)
    --year          Release year (optional, improves The Numbers URL match)
    --week          Weekend number: 1, 2, 3, or 4 (default: 2)
    --competition   Estimated Friday gross of all other films combined ($)
    --genre         Genre string e.g. 'Horror'
    --mpaa          MPAA rating (default: PG-13)
    --notify        Send a macOS notification when numbers post
    --interval      Poll interval in seconds (default: 600 = 10 min)
    --timeout       Give up after this many hours (default: 12)
    --once          Check once and exit (no loop)

The script is also importable:
    from scrape_live_friday import poll_until_friday, check_friday_posted
"""

import argparse
import subprocess
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

import requests
from bs4 import BeautifulSoup

# Make predict_live importable from same directory
sys.path.insert(0, str(Path(__file__).parent))
from predict_live import (
    HEADERS,
    build_inputs_from_tn,
    print_result,
    run_prediction,
    scrape_the_numbers_movie,
)

DEFAULT_INTERVAL_SECS = 600   # 10 minutes
DEFAULT_TIMEOUT_HOURS = 12


# ── macOS notification helper ──────────────────────────────────────────────────

def _notify(title: str, subtitle: str, message: str) -> None:
    """Send a macOS notification via osascript (no-op if not on macOS)."""
    try:
        script = (
            f'display notification "{message}" '
            f'with title "{title}" subtitle "{subtitle}"'
        )
        subprocess.run(["osascript", "-e", script], check=False, capture_output=True)
    except Exception:
        pass


# ── Check whether Friday data has posted ──────────────────────────────────────

def check_friday_posted(title: str, year: int | None, week_num: int) -> dict | None:
    """
    Scrape The Numbers for the film and check whether the target Friday
    row exists.

    Returns scraped tn_data dict if the Friday for week_num is present,
    or None if data is missing / page unavailable.
    """
    tn_data = scrape_the_numbers_movie(title, year)
    if tn_data is None:
        return None

    daily = tn_data["daily"]
    fridays = [r for r in daily if r["date"].weekday() == 4]

    if len(fridays) >= week_num:
        target_fri = fridays[week_num - 1]
        if target_fri["gross"] and target_fri["gross"] > 10_000:
            return tn_data

    return None


# ── Core polling loop ──────────────────────────────────────────────────────────

def poll_until_friday(
    title: str,
    year: int | None = None,
    week_num: int = 2,
    competition: float = 0.0,
    genre: str = "",
    mpaa: str = "PG-13",
    interval_secs: int = DEFAULT_INTERVAL_SECS,
    timeout_hours: float = DEFAULT_TIMEOUT_HOURS,
    notify: bool = False,
) -> dict | None:
    """
    Poll The Numbers until Friday gross for week_num appears, then run the
    subsequent_weekend model and return the prediction result dict.

    Parameters
    ----------
    title         : Movie title (as it appears on The Numbers)
    year          : Release year (optional, helps URL matching)
    week_num      : 1–4 (1 = opening weekend Friday)
    competition   : Estimated other-films Friday gross ($) for competition feature
    genre         : Genre string e.g. 'Horror'
    mpaa          : MPAA rating string
    interval_secs : Seconds between polls (default 600)
    timeout_hours : Hours before giving up (default 12)
    notify        : Send macOS notification on success

    Returns the prediction result dict (from run_prediction) or None on timeout.
    """
    deadline = datetime.now() + timedelta(hours=timeout_hours)
    attempt = 0

    print(f"\n{'═' * 58}")
    print(f"  FRIDAY GROSS POLLER — {title}  (Week {week_num})")
    print(f"  Polling every {interval_secs // 60} min | timeout {timeout_hours}h")
    print(f"  Started: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"{'═' * 58}\n")

    while datetime.now() < deadline:
        attempt += 1
        now_str = datetime.now().strftime("%H:%M:%S")
        print(f"  [{now_str}] Attempt {attempt} — checking The Numbers …", end=" ", flush=True)

        tn_data = check_friday_posted(title, year, week_num)

        if tn_data is not None:
            daily = tn_data["daily"]
            fridays = [r for r in daily if r["date"].weekday() == 4]
            target_fri = fridays[week_num - 1]
            fri_gross = target_fri["gross"]

            print(f"FOUND! Friday gross = ${fri_gross:,.0f}")

            if notify:
                _notify(
                    "Friday Numbers Posted",
                    title,
                    f"Week {week_num} Friday: ${fri_gross / 1e6:.1f}M — running projection…",
                )

            # For week 1 (opening weekend) we can't use the subsequent_weekend model
            # (it's trained on wk 2-4).  Print raw Friday and multiplier estimate only.
            if week_num == 1:
                print("\n  Opening weekend Friday detected.")
                print(f"  Friday gross: ${fri_gross:,.0f}")
                est_weekend = fri_gross * 3.0  # rough opening multiplier
                print(f"  Estimated opening Fri+Sat+Sun (3× multiplier): "
                      f"${est_weekend / 1e6:.1f}M")
                print("  (Use predict.py for opening weekend model with RT scores)\n")
                return {"opening_fri": fri_gross, "est_weekend_3x": est_weekend}

            # Build inputs and run model
            inputs = build_inputs_from_tn(
                tn_data, week_num,
                competition=competition,
                genre=genre,
                mpaa=mpaa,
            )
            if inputs is None:
                print("  WARNING: Could not build model inputs from scraped data.")
                return None

            result = run_prediction(inputs)
            print_result(result, title)

            if notify:
                pred_m = result["predicted"] / 1e6
                _notify(
                    "Box Office Projection Ready",
                    title,
                    f"Week {week_num} projected: ${pred_m:.1f}M",
                )

            return result

        else:
            print("not posted yet.")

        # Check remaining time
        remaining = deadline - datetime.now()
        remaining_min = int(remaining.total_seconds() / 60)
        next_check = datetime.now() + timedelta(seconds=interval_secs)
        print(f"       Next check at {next_check.strftime('%H:%M:%S')} "
              f"({remaining_min} min until timeout)")

        time.sleep(interval_secs)

    print(f"\n  Timeout reached after {timeout_hours}h — Friday numbers never posted.")
    print("  Check manually at https://www.the-numbers.com/")
    return None


# ── CLI entry point ────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description=(
            "Poll The Numbers until Friday box office numbers post, "
            "then run the subsequent_weekend prediction model."
        )
    )
    parser.add_argument("--title", required=True,
                        help="Movie title (as it appears on The Numbers)")
    parser.add_argument("--year", type=int,
                        help="Release year (improves URL matching)")
    parser.add_argument("--week", type=int, default=2, choices=[1, 2, 3, 4],
                        help="Weekend number to wait for (default: 2)")
    parser.add_argument("--competition", type=float, default=0.0,
                        help="Estimated combined Friday gross of all other films ($)")
    parser.add_argument("--genre", type=str, default="",
                        help="Genre string e.g. 'Horror, Thriller'")
    parser.add_argument("--mpaa", type=str, default="PG-13",
                        help="MPAA rating (G/PG/PG-13/R/NC-17)")
    parser.add_argument("--notify", action="store_true",
                        help="Send macOS notification when numbers post")
    parser.add_argument("--interval", type=int, default=DEFAULT_INTERVAL_SECS,
                        help=f"Seconds between polls (default: {DEFAULT_INTERVAL_SECS})")
    parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT_HOURS,
                        help=f"Hours before giving up (default: {DEFAULT_TIMEOUT_HOURS})")
    parser.add_argument("--once", action="store_true",
                        help="Check once and exit immediately (no polling loop)")

    args = parser.parse_args()

    if args.once:
        print(f"  Checking once for '{args.title}' Week {args.week} …")
        tn_data = check_friday_posted(args.title, args.year, args.week)
        if tn_data is None:
            print("  Friday data not available yet.")
            sys.exit(1)

        if args.week == 1:
            daily = tn_data["daily"]
            fridays = [r for r in daily if r["date"].weekday() == 4]
            fri_gross = fridays[0]["gross"]
            print(f"  Opening Friday gross: ${fri_gross:,.0f}")
            print(f"  Estimated opening weekend (3×): ${fri_gross * 3 / 1e6:.1f}M")
            return

        inputs = build_inputs_from_tn(
            tn_data, args.week,
            competition=args.competition,
            genre=args.genre,
            mpaa=args.mpaa,
        )
        if inputs:
            result = run_prediction(inputs)
            print_result(result, args.title)
        return

    poll_until_friday(
        title=args.title,
        year=args.year,
        week_num=args.week,
        competition=args.competition,
        genre=args.genre,
        mpaa=args.mpaa,
        interval_secs=args.interval,
        timeout_hours=args.timeout,
        notify=args.notify,
    )


if __name__ == "__main__":
    main()
