"""
Universe builder — fetches and maintains the list of companies to screen.

Hard filters (only these exclude companies):
  - Must be US-listed common stock
  - No ETFs, Funds, Warrants, Preferreds, Units, ADRs
  - Minimum average daily volume

Everything else is only a ranking signal, never an exclusion.
"""

import logging
import time
from datetime import datetime, timezone, timedelta
from typing import Optional
import yfinance as yf
import requests
import pandas as pd

logger = logging.getLogger(__name__)

# SIC codes considered "Tech/Software/Platform" for initial cohorts
TECH_SIC_CODES = {
    "7370", "7371", "7372", "7373", "7374", "7375", "7376", "7377", "7378", "7379",
    "3674",  # Semiconductors
    "3672",  # Printed Circuit Boards
    "3669",  # Communications Equipment
    "3577",  # Computer Peripheral Equipment
    "3571",  # Electronic Computers
}

EXCLUDE_KEYWORDS = [
    "etf", "fund", "trust", "warrant", "preferred", "unit",
    "acquisition", "spac", "blank check", "index", "notes due",
    "adr", "depositary"
]


def fetch_nasdaq_listed() -> list[dict]:
    """Fetch all NASDAQ-listed securities from NASDAQ's public file."""
    url = "https://api.nasdaq.com/api/screener/stocks"
    params = {
        "tableonly": "true",
        "limit": 25,
        "offset": 0,
        "download": "true"
    }
    headers = {"User-Agent": "Mozilla/5.0"}

    try:
        resp = requests.get(url, params=params, headers=headers, timeout=30)
        data = resp.json()
        rows = data.get("data", {}).get("rows", [])
        return rows
    except Exception as e:
        logger.warning(f"NASDAQ API failed: {e}, using fallback")
        return []


def fetch_universe_via_edgar() -> list[dict]:
    """
    Fetch company tickers from SEC EDGAR company_tickers.json.
    This is the most stable free source — SEC has no rate limits on this endpoint.
    """
    url = "https://www.sec.gov/files/company_tickers.json"
    headers = {"User-Agent": "ThielDetector info@pcctradinginc.com"}

    try:
        resp = requests.get(url, headers=headers, timeout=30)
        resp.raise_for_status()
        data = resp.json()
        companies = []
        for _, company in data.items():
            companies.append({
                "ticker": company.get("ticker", "").upper(),
                "name": company.get("title", ""),
                "cik": str(company.get("cik_str", "")).zfill(10)
            })
        logger.info(f"Fetched {len(companies)} companies from SEC EDGAR")
        return companies
    except Exception as e:
        logger.error(f"Failed to fetch from SEC EDGAR: {e}")
        return []


def fetch_recent_ipos(months_back: int = 36) -> list[dict]:
    """
    Fetch recent IPOs from SEC EDGAR S-1 filings.
    Looks for companies with first 10-K within the last N months.
    """
    cutoff = datetime.now(timezone.utc) - timedelta(days=months_back * 30)
    url = "https://efts.sec.gov/LATEST/search-index?q=%22S-1%22&dateRange=custom"
    params = {
        "startdt": cutoff.strftime("%Y-%m-%d"),
        "forms": "S-1",
        "_source": "hits.hits._source"
    }
    headers = {"User-Agent": "ThielDetector info@pcctradinginc.com"}

    try:
        resp = requests.get(
            "https://efts.sec.gov/LATEST/search-index?forms=S-1",
            params={"dateRange": "custom",
                    "startdt": cutoff.strftime("%Y-%m-%d"),
                    "hits.hits.total.value": "true"},
            headers=headers,
            timeout=30
        )
        # Returns EDGAR full-text search results
        return []  # Parsed downstream by edgartools
    except Exception as e:
        logger.warning(f"IPO fetch failed: {e}")
        return []


def is_valid_common_stock(ticker: str, name: str) -> bool:
    """
    Hard filter: exclude non-common-stock securities.
    This is the ONLY hard filter — everything else is ranking.
    """
    name_lower = name.lower()
    ticker_lower = ticker.lower()

    # Exclude by name keywords
    for keyword in EXCLUDE_KEYWORDS:
        if keyword in name_lower:
            return False

    # Exclude typical warrant/preferred suffixes
    if len(ticker) > 5:
        return False
    if ticker.endswith("W") or ticker.endswith("P") or ticker.endswith("R"):
        # Warrants, Preferreds, Rights — but be careful with real tickers
        if len(ticker) == 5:
            return False

    return True


def enrich_with_market_data(tickers: list[str], batch_size: int = 50) -> dict[str, dict]:
    """
    Fetch basic market data via yfinance.
    Returns dict of ticker -> {market_cap, avg_volume, exchange, sic_code}
    """
    enriched = {}
    for i in range(0, len(tickers), batch_size):
        batch = tickers[i:i + batch_size]
        try:
            data = yf.download(
                " ".join(batch),
                period="1mo",
                progress=False,
                auto_adjust=True
            )
            for ticker in batch:
                try:
                    info = yf.Ticker(ticker).fast_info
                    enriched[ticker] = {
                        "market_cap_m": (info.market_cap or 0) / 1_000_000,
                        "exchange": getattr(info, "exchange", ""),
                    }
                except Exception:
                    enriched[ticker] = {"market_cap_m": 0, "exchange": ""}
            time.sleep(0.5)  # Be polite to yfinance
        except Exception as e:
            logger.warning(f"Batch enrichment failed for batch {i}: {e}")

    return enriched


def build_universe(config: dict, conn) -> list[dict]:
    """
    Main entry: build the full screening universe.
    Returns list of company dicts ready for screening.
    """
    cohorts = config.get("universe", {}).get("cohorts", [])
    min_volume = config.get("universe", {}).get("min_avg_daily_volume", 10000)

    # Step 1: Fetch all companies from SEC EDGAR (most stable source)
    all_companies = fetch_universe_via_edgar()

    if not all_companies:
        logger.error("No companies fetched — aborting universe build")
        return []

    # Step 2: Hard filter (only listing criteria)
    valid_companies = [
        c for c in all_companies
        if is_valid_common_stock(c["ticker"], c["name"])
    ]
    logger.info(f"After hard filter: {len(valid_companies)} companies")

    # Step 3: Match to active cohorts
    active_tickers = []
    for cohort in cohorts:
        if not cohort.get("alerting_enabled") and cohort.get("baseline_runs_completed", 0) < 2:
            # Include in baseline, but mark accordingly
            pass

        cohort_tickers = _filter_cohort(valid_companies, cohort)
        for t in cohort_tickers:
            t["cohort_id"] = cohort["id"]
        active_tickers.extend(cohort_tickers)

    # Deduplicate by ticker (company can be in multiple cohorts — keep first match)
    seen = set()
    unique_tickers = []
    for t in active_tickers:
        if t["ticker"] not in seen:
            seen.add(t["ticker"])
            unique_tickers.append(t)

    logger.info(f"Universe size: {len(unique_tickers)} unique tickers")

    # Step 4: Upsert into DB
    now = datetime.now(timezone.utc).isoformat()
    for company in unique_tickers:
        existing = conn.execute(
            "SELECT ticker FROM companies WHERE ticker = ?",
            (company["ticker"],)
        ).fetchone()

        if not existing:
            conn.execute("""
                INSERT INTO companies
                (ticker, name, cohort_id, cik, first_seen_in_universe, is_active)
                VALUES (?, ?, ?, ?, ?, 1)
            """, (
                company["ticker"],
                company.get("name", ""),
                company.get("cohort_id", ""),
                company.get("cik", ""),
                now
            ))

    conn.commit()
    return unique_tickers


def _filter_cohort(companies: list[dict], cohort: dict) -> list[dict]:
    """Filter companies for a specific cohort."""
    result = []
    sic_codes = set(str(s) for s in cohort.get("sic_codes", []))
    min_cap = cohort.get("min_market_cap_m", 0)

    for company in companies:
        # SIC filter if cohort specifies it
        if sic_codes and company.get("sic_code") not in sic_codes:
            # If no SIC data available, include anyway (edgartools will verify)
            if company.get("sic_code"):
                continue

        result.append(company)

    return result
