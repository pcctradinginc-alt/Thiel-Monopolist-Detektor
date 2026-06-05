"""
Financial Modeling Prep (FMP) — EU universe fetcher.

FMP Free Tier: 250 requests/day, covers 90+ exchanges including XETRA,
Euronext, SIX, VSE. One call to /v3/stock/list returns all listed stocks
with exchange info — this is the EU equivalent of SEC EDGAR's ticker list.

Usage: set FMP_API_KEY environment variable (GitHub Secret).
"""

import logging
import os
import time
from typing import Optional

import requests

logger = logging.getLogger(__name__)

FMP_BASE = "https://financialmodelingprep.com/api/v3"

# FMP exchange short names → our exchange IDs + yfinance suffixes
EXCHANGE_MAP = {
    "XETRA":    ("xetra",  ".DE", "DE"),
    "EURONEXT": ("aex",    ".AS", "NL"),  # Euronext Amsterdam default
    "SIX":      ("six",    ".SW", "CH"),
    "VSE":      ("vienna", ".VI", "AT"),
    "EPA":      ("paris",  ".PA", "FR"),  # Euronext Paris
    "BRU":      ("bru",    ".BR", "BE"),  # Euronext Brussels
    "LSE":      ("lse",    ".L",  "GB"),  # London Stock Exchange
}

# Sectors to exclude (commodity, no Thiel moat possible)
EXCLUDED_SECTORS = {
    "Energy", "Utilities", "Basic Materials", "Real Estate",
    "Consumer Staples", "Industrials",  # narrow Industrials exclusion
}

# Minimum market cap in USD (FMP reports in USD)
MIN_MARKET_CAP_USD = 40_000_000  # ~€37M


def fetch_fmp_eu_universe(api_key: str = None) -> list[dict]:
    """
    Fetch all EU-listed equities from FMP in one API call.
    Returns list of company dicts ready for eu_universe_builder.

    Cost: 1 FMP API call (out of 250/day free tier).
    """
    key = api_key or os.environ.get("FMP_API_KEY", "")
    if not key:
        logger.warning("FMP_API_KEY not set — skipping FMP universe fetch")
        return []

    try:
        resp = requests.get(
            f"{FMP_BASE}/stock/list",
            params={"apikey": key},
            timeout=30,
        )
        resp.raise_for_status()
        all_stocks = resp.json()

        if isinstance(all_stocks, dict) and "Error" in str(all_stocks):
            logger.error(f"FMP API error: {all_stocks}")
            return []

        logger.info(f"FMP: {len(all_stocks)} total stocks fetched")

    except Exception as e:
        logger.error(f"FMP stock list fetch failed: {e}")
        return []

    companies = []
    seen = set()

    for stock in all_stocks:
        exchange = stock.get("exchangeShortName", "")
        if exchange not in EXCHANGE_MAP:
            continue

        ticker_raw = stock.get("symbol", "")
        name = stock.get("name", "")
        stock_type = stock.get("type", "")
        price = stock.get("price", 0) or 0

        # Only common stocks
        if stock_type not in ("stock", ""):
            continue

        # Filter obvious non-stocks
        if not ticker_raw or not name:
            continue
        if any(kw in name.lower() for kw in ["etf", "fund", "index", "warrant",
                                               "certificate", "note", "bond"]):
            continue

        exchange_id, suffix, country = EXCHANGE_MAP[exchange]

        # FMP tickers for European stocks sometimes already include suffix
        # Normalize: strip existing suffix if present, then re-add
        base_ticker = ticker_raw
        for known_suffix in [".DE", ".AS", ".SW", ".VI", ".PA", ".BR", ".L"]:
            if base_ticker.endswith(known_suffix):
                base_ticker = base_ticker[:-len(known_suffix)]
                break

        full_ticker = f"{base_ticker}{suffix}"

        if full_ticker in seen:
            continue
        seen.add(full_ticker)

        companies.append({
            "ticker": full_ticker,
            "base_ticker": base_ticker,
            "name": name,
            "exchange": exchange_id,
            "exchange_suffix": suffix,
            "cohort_id": f"eu_{exchange_id}",
            "country": country,
            "source": "fmp",
        })

    logger.info(f"FMP EU universe: {len(companies)} stocks across "
                f"{len(set(c['exchange'] for c in companies))} exchanges")

    # Log breakdown by exchange
    from collections import Counter
    counts = Counter(c["exchange"] for c in companies)
    for ex, count in sorted(counts.items(), key=lambda x: -x[1]):
        logger.info(f"  {ex}: {count} stocks")

    return companies


def get_fmp_financials(ticker: str, api_key: str = None) -> dict:
    """
    Fetch key financial metrics for a single ticker from FMP.
    Used as enrichment after yfinance pre-filter for companies
    where yfinance data is incomplete.

    Cost: 1 FMP API call.
    """
    key = api_key or os.environ.get("FMP_API_KEY", "")
    if not key:
        return {}

    try:
        resp = requests.get(
            f"{FMP_BASE}/profile/{ticker}",
            params={"apikey": key},
            timeout=15,
        )
        resp.raise_for_status()
        data = resp.json()
        if not data or isinstance(data, dict):
            return {}

        profile = data[0]
        return {
            "gross_margin": profile.get("grossProfitMargin"),
            "operating_margin": profile.get("operatingProfitMargin"),
            "revenue_growth": profile.get("revenueGrowth"),
            "market_cap_m": (profile.get("mktCap") or 0) / 1_000_000,
            "sector": profile.get("sector", ""),
            "industry": profile.get("industry", ""),
            "description": profile.get("description", "")[:2000],
            "employees": profile.get("fullTimeEmployees"),
            "ipo_date": profile.get("ipoDate"),
            "country": profile.get("country", ""),
        }
    except Exception as e:
        logger.debug(f"FMP profile fetch failed for {ticker}: {e}")
        return {}
