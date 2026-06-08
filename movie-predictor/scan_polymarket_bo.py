"""
scan_polymarket_bo.py
=====================
Scans open Polymarket box office markets, compares bracket prices to the
subsequent_weekend model predictions, and flags edges worth betting on.

Usage:
    python3 scan_polymarket_bo.py
    python3 scan_polymarket_bo.py --min-edge 0.10   # show edges ≥10%
    python3 scan_polymarket_bo.py --manual           # skip API, enter markets manually

Manual fallback (when Polymarket API is down):
    python3 scan_polymarket_bo.py --manual \\
        --market "Backrooms Wk2" \\
        --title "Backrooms" --week 2 \\
        --brackets "<27M:0.89,27-30M:0.11" \\
        --genre Horror --mpaa PG-13 --competition 44000000

Importable:
    from scan_polymarket_bo import fetch_bo_markets, run_scan
"""

import argparse
import sys
from datetime import date
from pathlib import Path
from typing import Optional

import requests

# Make predict_live importable from same directory
sys.path.insert(0, str(Path(__file__).parent))
from predict_live import (
    HEADERS,
    WEEK_MAE,
    build_inputs_from_tn,
    compute_bracket_probs,
    fmt_money,
    run_prediction,
    scrape_the_numbers_movie,
)

DEFAULT_MIN_EDGE = 0.15  # Flag edges ≥15%

# ── Polymarket API endpoints ──────────────────────────────────────────────────
GAMMA_EVENTS_URL = "https://gamma-api.polymarket.com/events"
GAMMA_MARKETS_URL = "https://gamma-api.polymarket.com/markets"


# ── Polymarket API fetcher ─────────────────────────────────────────────────────

def fetch_bo_markets() -> list[dict]:
    """
    Fetch open box-office markets from Polymarket Gamma API.

    Tries multiple endpoint / tag combinations and returns a list of
    normalized market dicts:
        {title, description, outcomes: [{label, price}]}

    Returns an empty list on total failure — caller handles fallback.
    """
    attempts = [
        # (url, params)
        (GAMMA_EVENTS_URL, {"tag": "box-office", "active": "true", "limit": "50"}),
        (GAMMA_EVENTS_URL, {"tag_slug": "box-office", "active": "true", "limit": "50"}),
        (GAMMA_MARKETS_URL, {"tag": "box-office", "active": "true", "limit": "50"}),
        (GAMMA_MARKETS_URL, {"tag_slug": "box-office", "active": "true", "limit": "50"}),
        (GAMMA_EVENTS_URL, {"category": "entertainment", "active": "true", "limit": "100"}),
    ]

    for url, params in attempts:
        try:
            resp = requests.get(url, params=params, headers=HEADERS, timeout=15)
            if resp.status_code != 200:
                continue
            data = resp.json()

            # Gamma API can return a list directly or {"events": [...]} etc.
            if isinstance(data, list):
                items = data
            elif isinstance(data, dict):
                items = (data.get("events") or data.get("markets")
                         or data.get("data") or [])
            else:
                continue

            if not items:
                continue

            markets = _normalize_gamma_response(items)
            if markets:
                return markets

        except requests.RequestException:
            continue
        except Exception:
            continue

    return []


def _normalize_gamma_response(items: list) -> list[dict]:
    """
    Normalize raw Gamma API items into a standard list of market dicts.
    Filters to box-office-looking markets by keyword.
    """
    BO_KEYWORDS = [
        "box office", "weekend gross", "opening weekend", "domestic gross",
        "million", "wk ", "week ", "billion",
    ]

    markets = []
    for item in items:
        title = (item.get("title") or item.get("name") or item.get("question") or "").strip()
        description = item.get("description") or ""

        if not title:
            continue

        # Filter: must look like a box-office market
        combined = (title + " " + description).lower()
        if not any(kw in combined for kw in BO_KEYWORDS):
            continue

        # Try to extract outcomes with prices
        outcomes_raw = (item.get("outcomes") or item.get("markets") or [])
        outcomes = []

        if isinstance(outcomes_raw, list):
            for o in outcomes_raw:
                if isinstance(o, dict):
                    label = (o.get("outcome") or o.get("title") or
                             o.get("name") or o.get("label") or "")
                    # Price is usually 0–1 float; sometimes expressed as cents
                    price = None
                    for pkey in ("price", "lastTradePrice", "bestAsk", "bestBid", "midprice"):
                        raw = o.get(pkey)
                        if raw is not None:
                            try:
                                price = float(raw)
                                if price > 1.0:
                                    price /= 100.0  # convert cents to fraction
                                break
                            except (TypeError, ValueError):
                                pass
                    if label and price is not None:
                        outcomes.append({"label": label, "price": price})
                elif isinstance(o, str):
                    outcomes.append({"label": o, "price": None})

        # Also handle flat event structure (single binary market)
        if not outcomes:
            yes_price = item.get("outcomePrices") or item.get("yes_price")
            if yes_price:
                try:
                    yp = float(yes_price)
                    if yp > 1:
                        yp /= 100
                    outcomes = [
                        {"label": "Yes", "price": yp},
                        {"label": "No", "price": round(1 - yp, 4)},
                    ]
                except (TypeError, ValueError):
                    pass

        markets.append({
            "title": title,
            "description": description,
            "outcomes": outcomes,
            "raw": item,
        })

    return markets


# ── Market parser: extract film + week + brackets from market title ────────────

def _parse_market_title(title: str) -> dict:
    """
    Attempt to extract:
        film_title: str
        week_num: int | None  (None = opening weekend)
        brackets: list of (lo, hi, label) guessed from outcome labels

    Heuristics cover titles like:
        "Backrooms Week 2 Box Office"
        "Masters of the Universe Opening Weekend"
        "Obsession Wk3 Domestic Gross"
    """
    import re

    # Week number
    week_num = None
    wk_match = re.search(r"\bwk\.?\s*(\d)\b|\bweek\s+(\d)\b", title, re.IGNORECASE)
    if wk_match:
        week_num = int(wk_match.group(1) or wk_match.group(2))

    # Film title = everything before the week/weekend/box/domestic keyword
    strip_pattern = re.compile(
        r"\s*(wk\.?\s*\d+|week\s+\d+|opening weekend|box office|domestic gross"
        r"|weekend gross|wknd|total gross).*$",
        re.IGNORECASE,
    )
    film_title = strip_pattern.sub("", title).strip(" -–—")
    if not film_title:
        film_title = title

    return {
        "film_title": film_title,
        "week_num": week_num,
    }


def _parse_bracket_label(label: str) -> tuple:
    """
    Parse a bracket label like '<$24M', '$24-27M', '>$30M' into (lo, hi).
    Returns (None, None) if unparseable.
    """
    import re

    label = label.strip().replace(",", "")
    # Remove leading currency symbols / spaces
    label_clean = re.sub(r"[$\s]", "", label).upper()

    # ">$30M" or "30M+" etc.
    over_match = re.match(r"[>+](\d+(?:\.\d+)?)(M|B)?", label_clean)
    if over_match:
        val = float(over_match.group(1))
        mult = 1e9 if (over_match.group(2) or "M").upper() == "B" else 1e6
        return (val * mult, None)

    # "<$24M" or "under 24M"
    under_match = re.match(r"[<](\d+(?:\.\d+)?)(M|B)?", label_clean)
    if under_match:
        val = float(under_match.group(1))
        mult = 1e9 if (under_match.group(2) or "M").upper() == "B" else 1e6
        return (None, val * mult)

    # "$24-27M" or "24M-27M"
    range_match = re.match(r"(\d+(?:\.\d+)?)(M|B)?[-–](\d+(?:\.\d+)?)(M|B)?", label_clean)
    if range_match:
        lo = float(range_match.group(1))
        lo_mult = 1e9 if (range_match.group(2) or "M").upper() == "B" else 1e6
        hi = float(range_match.group(3))
        hi_mult = 1e9 if (range_match.group(4) or "M").upper() == "B" else 1e6
        return (lo * lo_mult, hi * hi_mult)

    return (None, None)


# ── Per-film edge analysis ────────────────────────────────────────────────────

def analyze_market(market: dict, min_edge: float = DEFAULT_MIN_EDGE) -> list[dict]:
    """
    For a single Polymarket market dict, attempt to:
    1. Parse film title + week from market title
    2. Look up The Numbers daily data
    3. Run model prediction
    4. Compare bracket model probs vs market prices
    5. Return a list of finding dicts

    Returns [] if analysis is not possible.
    """
    from scipy import stats
    import math

    meta = _parse_market_title(market["title"])
    film_title = meta["film_title"]
    week_num = meta["week_num"]

    if not week_num or week_num < 2 or week_num > 4:
        # Opening weekend or can't determine — skip model
        return []

    outcomes = market.get("outcomes", [])
    if not outcomes:
        return []

    # Check whether any prices are available
    priced_outcomes = [o for o in outcomes if o.get("price") is not None]
    if not priced_outcomes:
        return []

    # Fetch The Numbers data
    tn_data = scrape_the_numbers_movie(film_title)
    if tn_data is None:
        return []

    # Build model inputs
    inputs = build_inputs_from_tn(tn_data, week_num)
    if inputs is None:
        return []

    try:
        result = run_prediction(inputs)
    except Exception as e:
        print(f"  Model error for {film_title}: {e}", file=sys.stderr)
        return []

    pred = result["predicted"]
    sigma = result["sigma"]
    dist = stats.norm(loc=pred, scale=sigma)

    findings = []
    for outcome in priced_outcomes:
        label = outcome["label"]
        market_price = outcome["price"]  # 0–1

        lo, hi = _parse_bracket_label(label)
        if lo is None and hi is None:
            continue

        cdf_hi = dist.cdf(hi) if hi is not None else 1.0
        cdf_lo = dist.cdf(lo) if lo is not None else 0.0
        model_prob = max(0.0, min(1.0, cdf_hi - cdf_lo))

        edge = model_prob - market_price

        findings.append({
            "film": film_title,
            "week": week_num,
            "market_title": market["title"],
            "bracket": label,
            "market_prob": market_price,
            "model_prob": model_prob,
            "edge": edge,
            "predicted": pred,
            "low_80": result["low_80"],
            "high_80": result["high_80"],
        })

    return findings


# ── Manual bracket parser ─────────────────────────────────────────────────────

def parse_manual_brackets(brackets_str: str) -> list[dict]:
    """
    Parse a comma-separated bracket string like:
        "<27M:0.89,27-30M:0.11"
    into a list of outcome dicts.
    """
    outcomes = []
    for part in brackets_str.split(","):
        part = part.strip()
        if ":" not in part:
            continue
        label, price_str = part.rsplit(":", 1)
        try:
            price = float(price_str.strip())
            if price > 1.0:
                price /= 100.0
        except ValueError:
            continue
        outcomes.append({"label": label.strip(), "price": price})
    return outcomes


# ── Output formatter ──────────────────────────────────────────────────────────

def _verdict(edge: float, min_edge: float) -> str:
    if edge >= min_edge:
        return "BET YES"
    elif edge <= -min_edge:
        return "BET NO"
    else:
        return "no edge"


def print_scan_results(findings: list[dict], min_edge: float = DEFAULT_MIN_EDGE) -> None:
    """Print a formatted edge table to stdout."""
    today = date.today().strftime("%Y-%m-%d")
    # Get current time without timezone dependency
    now = __import__("datetime").datetime.now().strftime("%H:%M")

    findings_sorted = sorted(findings, key=lambda x: x["edge"], reverse=True)

    width = 71
    print()
    print("═" * width)
    print(f"  POLYMARKET BOX OFFICE EDGE SCANNER — {today} {now} ET")
    print("═" * width)

    if not findings_sorted:
        print("  No actionable findings — no data available or no open BO markets found.")
        print("═" * width)
        return

    header = f"  {'Film':<24} {'Bracket':<14} {'Market%':>7} {'Model%':>7} {'Edge':>7}    Verdict"
    print(header)
    print("  " + "─" * (width - 2))

    for f in findings_sorted:
        film_wk = f"{f['film']} Wk{f['week']}"
        bracket = f['bracket']
        market_p = f"{f['market_prob']:.0%}"
        model_p = f"{f['model_prob']:.0%}"
        edge = f['edge']
        edge_str = f"{edge:+.0%}"
        verdict = _verdict(edge, min_edge)

        # Flags
        if edge >= min_edge:
            flag = "✅"
        elif edge <= -min_edge:
            flag = "⚠️ "
        else:
            flag = "  "

        print(f"  {film_wk:<24} {bracket:<14} {market_p:>7} {model_p:>7} "
              f"{edge_str:>7}    {flag} {verdict}")

    print("═" * width)

    # Summary of strong edges
    strong = [f for f in findings_sorted if abs(f["edge"]) >= min_edge]
    if strong:
        print(f"\n  Strong edges (≥{min_edge:.0%}):")
        for f in strong:
            direction = "YES" if f["edge"] > 0 else "NO"
            print(f"    BET {direction}  {f['film']} Wk{f['week']} {f['bracket']!s:<14}  "
                  f"edge={f['edge']:+.0%}  "
                  f"(model predicts {fmt_money(f['predicted'])}, "
                  f"80% CI {fmt_money(f['low_80'])}–{fmt_money(f['high_80'])})")
    print()


# ── Full scan orchestrator ────────────────────────────────────────────────────

def run_scan(min_edge: float = DEFAULT_MIN_EDGE) -> list[dict]:
    """
    Fetch all open Polymarket BO markets, run model analysis, and return findings.
    """
    print("  Fetching Polymarket box office markets …")
    markets = fetch_bo_markets()

    if not markets:
        print(
            "  WARNING: Could not fetch markets from Polymarket API.\n"
            "  Check your connection or try --manual mode to enter bracket prices directly.",
            file=sys.stderr,
        )
        return []

    print(f"  Found {len(markets)} potential box office markets.")

    all_findings = []
    for mkt in markets:
        print(f"  Analyzing: {mkt['title'][:60]} …", end=" ", flush=True)
        findings = analyze_market(mkt, min_edge=min_edge)
        if findings:
            print(f"{len(findings)} brackets")
            all_findings.extend(findings)
        else:
            print("skipped (no data or not applicable)")

    return all_findings


# ── CLI entry point ────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description=(
            "Scan open Polymarket box office markets, compare bracket prices "
            "to the subsequent_weekend model, and flag edges."
        )
    )
    parser.add_argument("--min-edge", type=float, default=DEFAULT_MIN_EDGE,
                        dest="min_edge",
                        help=f"Minimum edge to flag (default: {DEFAULT_MIN_EDGE:.0%})")
    parser.add_argument("--manual", action="store_true",
                        help="Skip Polymarket API — enter market data manually via args")

    # Manual mode args
    parser.add_argument("--market", type=str,
                        help="Market label for display e.g. 'Backrooms Wk2'")
    parser.add_argument("--title", type=str,
                        help="Film title for The Numbers lookup")
    parser.add_argument("--week", type=int, choices=[2, 3, 4],
                        help="Weekend number")
    parser.add_argument("--brackets", type=str,
                        help="Bracket prices as '<27M:0.89,27-30M:0.08,>30M:0.03'")
    parser.add_argument("--genre", type=str, default="",
                        help="Genre string (for model features)")
    parser.add_argument("--mpaa", type=str, default="PG-13",
                        help="MPAA rating")
    parser.add_argument("--competition", type=float, default=0.0,
                        help="Estimated other-films Friday gross ($)")
    parser.add_argument("--year", type=int,
                        help="Release year (for The Numbers URL)")

    args = parser.parse_args()

    if args.manual:
        # Manual mode: user supplies title, week, and bracket prices directly
        if not all([args.title, args.week, args.brackets]):
            parser.error("--manual requires --title, --week, and --brackets")

        print(f"\n  Manual mode: {args.title} Week {args.week}")

        tn_data = scrape_the_numbers_movie(args.title, args.year)
        if tn_data is None:
            print(f"  ERROR: Could not fetch The Numbers data for '{args.title}'",
                  file=sys.stderr)
            sys.exit(1)

        inputs = build_inputs_from_tn(
            tn_data, args.week,
            competition=args.competition,
            genre=args.genre,
            mpaa=args.mpaa,
        )
        if inputs is None:
            print("  ERROR: Insufficient daily data for this week.", file=sys.stderr)
            sys.exit(1)

        result = run_prediction(inputs)
        pred = result["predicted"]
        sigma = result["sigma"]

        outcomes = parse_manual_brackets(args.brackets)
        market_label = args.market or f"{args.title} Wk{args.week}"

        findings = []
        from scipy import stats
        dist = stats.norm(loc=pred, scale=sigma)
        for outcome in outcomes:
            lo, hi = _parse_bracket_label(outcome["label"])
            if lo is None and hi is None:
                print(f"  Could not parse bracket label: {outcome['label']}")
                continue
            cdf_hi = dist.cdf(hi) if hi is not None else 1.0
            cdf_lo = dist.cdf(lo) if lo is not None else 0.0
            model_prob = max(0.0, min(1.0, cdf_hi - cdf_lo))
            edge = model_prob - outcome["price"]
            findings.append({
                "film": args.title,
                "week": args.week,
                "market_title": market_label,
                "bracket": outcome["label"],
                "market_prob": outcome["price"],
                "model_prob": model_prob,
                "edge": edge,
                "predicted": pred,
                "low_80": result["low_80"],
                "high_80": result["high_80"],
            })

        print_scan_results(findings, min_edge=args.min_edge)
        return

    # Auto mode: hit Polymarket API
    findings = run_scan(min_edge=args.min_edge)

    if not findings:
        print(
            "\n  No model-scorable markets found automatically.\n"
            "  Possible causes:\n"
            "    • Polymarket API down or tag changed\n"
            "    • The Numbers data not yet posted for active films\n"
            "    • No open wk2-4 markets at this time\n"
            "\n"
            "  Use --manual mode to score a specific market:\n"
            "    python3 scan_polymarket_bo.py --manual \\\n"
            "        --title 'Backrooms' --week 2 \\\n"
            "        --brackets '<27M:0.89,27-30M:0.08,>30M:0.03' \\\n"
            "        --genre Horror --mpaa PG-13\n"
        )
        return

    print_scan_results(findings, min_edge=args.min_edge)


if __name__ == "__main__":
    main()
