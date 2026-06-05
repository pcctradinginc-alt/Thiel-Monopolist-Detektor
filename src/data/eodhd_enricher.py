"""
EODHD (EOD Historical Data) — English company descriptions for EU stocks.

Solves the biggest EU data gap: missing business_description text for
non-German companies. EODHD's fundamentals endpoint returns a General.Description
field in English for virtually all European companies.

Free tier: 20 API calls/day = 140/week
Usage: enrich EU companies that have no filing text from Bundesanzeiger/CH

Secret: EODHD_API_KEY
Get free key: https://eodhd.com/r/?ref=thiel (or eodhd.com → Sign Up Free)
"""

import logging
import os
import time
from typing import Optional

import requests

logger = logging.getLogger(__name__)

EODHD_BASE = "https://eodhd.com/api"

# Track calls this session to stay within free tier
_calls_today = 0
MAX_CALLS_PER_RUN = 100  # Conservative limit per run (well under 140/week)


def get_company_description(ticker: str, api_key: str = None) -> dict:
    """
    Fetch English company description and key metrics from EODHD.
    Ticker format: "SAP.XETRA" or "ASML.AS" or "AZN.LSE"

    Returns dict with business_description + financial signals.
    """
    global _calls_today
    key = api_key or os.environ.get("EODHD_API_KEY", "")

    if not key:
        return {}
    if _calls_today >= MAX_CALLS_PER_RUN:
        logger.debug("EODHD call limit reached for this run")
        return {}

    # Convert yfinance ticker format to EODHD format
    eodhd_ticker = _to_eodhd_ticker(ticker)
    if not eodhd_ticker:
        return {}

    try:
        resp = requests.get(
            f"{EODHD_BASE}/fundamentals/{eodhd_ticker}",
            params={"api_token": key, "fmt": "json",
                    "filter": "General,Highlights"},
            timeout=15,
        )
        _calls_today += 1

        if resp.status_code != 200:
            return {}

        data = resp.json()
        general = data.get("General", {})
        highlights = data.get("Highlights", {})

        description = general.get("Description", "")
        if not description or len(description) < 50:
            return {}

        result = {
            "business_description": description[:4000],
            "source": "eodhd",
            "company_name": general.get("Name", ""),
            "sector": general.get("Sector", ""),
            "industry": general.get("Industry", ""),
            "country": general.get("CountryISO", ""),
            "employees": general.get("FullTimeEmployees"),
            "ipo_date": general.get("IPODate"),
            "financial_highlights": {
                "gross_margin": highlights.get("ProfitMargin"),
                "revenue_ttm": highlights.get("RevenueTTM"),
                "eps_ttm": highlights.get("EarningsShare"),
                "market_cap": highlights.get("MarketCapitalization"),
            }
        }
        logger.debug(f"{ticker}: EODHD description fetched "
                     f"({len(description)} chars, calls today: {_calls_today})")
        return result

    except Exception as e:
        logger.debug(f"EODHD fetch failed for {ticker}: {e}")
        return {}


def _to_eodhd_ticker(yf_ticker: str) -> str:
    """
    Convert yfinance ticker (SAP.DE) to EODHD format (SAP.XETRA).
    """
    suffix_map = {
        ".DE": "XETRA",
        ".SW": "SIX",
        ".AS": "AS",
        ".PA": "PA",
        ".BR": "BR",
        ".VI": "VI",
        ".L":  "LSE",
        ".ST": "ST",
        ".HE": "HE",
        ".CO": "CO",
        ".OL": "OL",
        ".MC": "MC",
        ".MI": "MI",
    }
    for yf_suffix, eodhd_exchange in suffix_map.items():
        if yf_ticker.endswith(yf_suffix):
            base = yf_ticker[:-len(yf_suffix)]
            return f"{base}.{eodhd_exchange}"
    return yf_ticker  # Pass through as-is


def enrich_filing_data(filing_data: dict, ticker: str,
                       api_key: str = None) -> dict:
    """
    Enrich filing_data with EODHD description if business_description is empty.
    Only called when Bundesanzeiger/Companies House returned nothing.
    """
    if filing_data.get("business_description"):
        return filing_data  # Already have text, don't waste EODHD call

    eodhd_data = get_company_description(ticker, api_key)
    if not eodhd_data:
        return filing_data

    filing_data["business_description"] = eodhd_data.get("business_description", "")
    filing_data["source_enriched"] = "eodhd"

    # Merge financial highlights if available
    if eodhd_data.get("financial_highlights"):
        existing = filing_data.get("financial_signals", {})
        highlights = eodhd_data["financial_highlights"]
        if highlights.get("gross_margin") and not existing.get("gross_margin_current"):
            existing["gross_margin_current"] = round(
                (highlights["gross_margin"] or 0) * 100, 1)
        filing_data["financial_signals"] = existing

    return filing_data
