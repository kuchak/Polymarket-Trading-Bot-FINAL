#!/usr/bin/env python3
"""
Crypto Polymarket Monitor — Phase 1 Passive Data Collection

Monitors daily BTC/ETH "Above X" markets on Polymarket every 5 minutes.
Logs probability snapshots to crypto_snapshots.csv for backtesting.

Markets resolve at 12:00 PM ET (17:00 UTC) daily based on Binance 1-min candle.

Usage:
    python3 crypto_monitor.py                              # foreground
    nohup python3 crypto_monitor.py > crypto_monitor.log 2>&1 &  # background
"""

import csv
import json
import os
import signal
import sys
import time
import traceback
import urllib.request
from datetime import datetime, timezone, timedelta

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
GAMMA_EVENTS_API = "https://gamma-api.polymarket.com/events"
CYCLE_INTERVAL = 300  # 5 minutes
DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
SNAPSHOTS_CSV = os.path.join(DATA_DIR, "crypto_snapshots.csv")
LOG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "crypto_monitor.log")

SNAPSHOT_FIELDS = [
    "timestamp", "slug", "asset", "threshold_price", "outcome",
    "implied_prob", "liquidity", "volume_24h", "minutes_to_expiry",
    "btc_price_approx", "final_outcome",
]

# ---------------------------------------------------------------------------
# Graceful shutdown
# ---------------------------------------------------------------------------
_shutdown = False

def _handle_signal(signum, frame):
    global _shutdown
    _shutdown = True
    log("Shutdown signal received, finishing current cycle...")

signal.signal(signal.SIGTERM, _handle_signal)
signal.signal(signal.SIGINT, _handle_signal)

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
def log(msg):
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    line = f"[{ts}] {msg}"
    print(line, flush=True)
    try:
        with open(LOG_FILE, "a") as f:
            f.write(line + "\n")
    except Exception:
        pass

# ---------------------------------------------------------------------------
# HTTP helper
# ---------------------------------------------------------------------------
def fetch_json(url, timeout=30):
    """Fetch JSON from URL using stdlib only."""
    req = urllib.request.Request(url, headers={"User-Agent": "crypto-monitor/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode())
    except Exception as e:
        log(f"  HTTP error fetching {url}: {e}")
        return None

# ---------------------------------------------------------------------------
# Slug generation
# ---------------------------------------------------------------------------
def get_slugs_to_watch():
    """Generate slugs for today, tomorrow, and yesterday's crypto markets."""
    now = datetime.now(timezone.utc)
    dates = {
        "today": now,
        "tomorrow": now + timedelta(days=1),
        "yesterday": now - timedelta(days=1),
    }
    slugs = []
    for label, dt in dates.items():
        # Format: "march-6" (no leading zero, lowercase)
        month = dt.strftime("%B").lower()
        day = dt.day
        for asset in ["bitcoin", "ethereum"]:
            slugs.append({
                "slug": f"{asset}-above-on-{month}-{day}",
                "asset": "BTC" if asset == "bitcoin" else "ETH",
                "label": label,
            })
    return slugs

# ---------------------------------------------------------------------------
# Parse threshold price from market question or groupItemTitle
# ---------------------------------------------------------------------------
def parse_threshold(market):
    """Extract numeric threshold from market data."""
    # Try groupItemTitle first: "64,000"
    git = market.get("groupItemTitle", "")
    if git:
        try:
            return int(git.replace(",", "").replace("$", "").strip())
        except (ValueError, TypeError):
            pass
    # Fallback: parse from question "...above $64,000 on..."
    question = market.get("question", "")
    import re
    m = re.search(r'\$([0-9,]+)', question)
    if m:
        try:
            return int(m.group(1).replace(",", ""))
        except (ValueError, TypeError):
            pass
    return None

# ---------------------------------------------------------------------------
# Estimate current price from the ~50% boundary outcome
# ---------------------------------------------------------------------------
def estimate_price(markets_data):
    """Find the threshold where Yes prob is closest to 0.50."""
    best_diff = 1.0
    best_price = None
    for threshold, yes_prob, _ in markets_data:
        if threshold is None:
            continue
        diff = abs(yes_prob - 0.50)
        if diff < best_diff:
            best_diff = diff
            best_price = threshold
    return best_price

# ---------------------------------------------------------------------------
# CSV helpers
# ---------------------------------------------------------------------------
def ensure_csv():
    """Create CSV with headers if it doesn't exist."""
    os.makedirs(DATA_DIR, exist_ok=True)
    if not os.path.exists(SNAPSHOTS_CSV):
        with open(SNAPSHOTS_CSV, "w", newline="") as f:
            writer = csv.writer(f)
            writer.writerow(SNAPSHOT_FIELDS)

def append_rows(rows):
    """Append rows to the snapshots CSV."""
    with open(SNAPSHOTS_CSV, "a", newline="") as f:
        writer = csv.writer(f)
        for row in rows:
            writer.writerow(row)

# ---------------------------------------------------------------------------
# Resolution backfill
# ---------------------------------------------------------------------------
def backfill_resolutions(slug, event):
    """For closed events, backfill final_outcome in existing CSV rows."""
    if not os.path.exists(SNAPSHOTS_CSV):
        return 0

    # Determine winning outcomes from the resolved markets
    winners = {}  # threshold_price -> "YES" or "NO"
    for market in event.get("markets", []):
        threshold = parse_threshold(market)
        if threshold is None:
            continue
        try:
            prices = json.loads(market.get("outcomePrices", "[]"))
            yes_price = float(prices[0]) if prices else 0
            # If yes resolved to 1.0, YES won; if 0.0, NO won
            if yes_price >= 0.99:
                winners[threshold] = "YES"
            elif yes_price <= 0.01:
                winners[threshold] = "NO"
        except (json.JSONDecodeError, IndexError, ValueError):
            continue

    if not winners:
        return 0

    # Read all rows, update matching ones
    rows = []
    updated = 0
    with open(SNAPSHOTS_CSV, "r", newline="") as f:
        reader = csv.reader(f)
        header = next(reader)
        rows.append(header)
        final_idx = header.index("final_outcome") if "final_outcome" in header else -1
        slug_idx = header.index("slug") if "slug" in header else -1
        thresh_idx = header.index("threshold_price") if "threshold_price" in header else -1

        if final_idx == -1 or slug_idx == -1 or thresh_idx == -1:
            return 0

        for row in reader:
            if len(row) > max(final_idx, slug_idx, thresh_idx):
                if row[slug_idx] == slug and row[final_idx] == "":
                    try:
                        t = int(row[thresh_idx])
                        if t in winners:
                            row[final_idx] = winners[t]
                            updated += 1
                    except (ValueError, IndexError):
                        pass
            rows.append(row)

    if updated > 0:
        with open(SNAPSHOTS_CSV, "w", newline="") as f:
            writer = csv.writer(f)
            writer.writerows(rows)

    return updated

# ---------------------------------------------------------------------------
# Main cycle
# ---------------------------------------------------------------------------
def run_cycle():
    """One monitoring cycle: fetch all relevant events, log snapshots."""
    now = datetime.now(timezone.utc)
    ts = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    slugs = get_slugs_to_watch()
    total_rows = 0
    rows_to_write = []

    for entry in slugs:
        slug = entry["slug"]
        asset = entry["asset"]
        label = entry["label"]

        url = f"{GAMMA_EVENTS_API}?slug={slug}"
        data = fetch_json(url)
        if not data or not isinstance(data, list) or len(data) == 0:
            continue

        event = data[0]
        is_active = event.get("active", False)
        is_closed = event.get("closed", False)

        # Handle resolution for yesterday's closed events
        if is_closed and label == "yesterday":
            updated = backfill_resolutions(slug, event)
            if updated > 0:
                log(f"  Backfilled {updated} resolution rows for {slug}")
            continue

        if not is_active:
            continue

        # Parse end date for minutes_to_expiry
        end_date_str = event.get("endDate", "")
        try:
            end_date = datetime.fromisoformat(end_date_str.replace("Z", "+00:00"))
            minutes_to_expiry = max(0, (end_date - now).total_seconds() / 60)
        except (ValueError, TypeError):
            minutes_to_expiry = 0

        # Parse all markets
        markets_data = []  # [(threshold, yes_prob, market_dict), ...]
        for market in event.get("markets", []):
            threshold = parse_threshold(market)
            if threshold is None:
                continue
            try:
                prices = json.loads(market.get("outcomePrices", "[]"))
                yes_prob = float(prices[0]) if prices else 0
            except (json.JSONDecodeError, IndexError, ValueError):
                yes_prob = 0

            liq = market.get("liquidityNum", 0) or 0
            vol = market.get("volume24hr", 0) or 0

            markets_data.append((threshold, yes_prob, {
                "liquidity": round(liq, 2),
                "volume_24h": round(vol, 2),
            }))

        # Estimate current price from ~50% boundary
        price_approx = estimate_price(markets_data)

        # Write one row per threshold (Yes outcome only — No is just 1-Yes)
        for threshold, yes_prob, extra in markets_data:
            row = [
                ts,                          # timestamp
                slug,                        # slug
                asset,                       # asset
                threshold,                   # threshold_price
                "Yes",                       # outcome
                round(yes_prob, 6),          # implied_prob
                extra["liquidity"],          # liquidity
                extra["volume_24h"],         # volume_24h
                round(minutes_to_expiry, 1), # minutes_to_expiry
                price_approx or "",          # btc_price_approx
                "",                          # final_outcome (empty until resolved)
            ]
            rows_to_write.append(row)
            total_rows += 1

    if rows_to_write:
        append_rows(rows_to_write)

    return total_rows

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    log("=" * 60)
    log("Crypto Polymarket Monitor starting")
    log(f"  Cycle interval: {CYCLE_INTERVAL}s")
    log(f"  CSV output: {SNAPSHOTS_CSV}")
    log("=" * 60)

    ensure_csv()
    cycle = 0

    while not _shutdown:
        cycle += 1
        try:
            t0 = time.time()
            rows = run_cycle()
            elapsed = time.time() - t0
            log(f"Cycle {cycle}: {rows} rows logged in {elapsed:.1f}s")
        except Exception as e:
            log(f"Cycle {cycle} ERROR: {e}")
            traceback.print_exc()

        # Sleep in 1s increments to respond to shutdown quickly
        for _ in range(CYCLE_INTERVAL):
            if _shutdown:
                break
            time.sleep(1)

    log("Crypto monitor stopped.")

if __name__ == "__main__":
    main()
