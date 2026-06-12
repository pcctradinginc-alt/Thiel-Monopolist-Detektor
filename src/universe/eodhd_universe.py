"""
EODHD — EU universe fetcher.

Replaces FMP for EU stock universe building.
EODHD provides full exchange symbol lists via a single API call per exchange.

Exchanges covered:
  XETRA (DE), SW (CH), PA (FR), AS (NL), ST (SE), HE (FI),
  CO (DK), OL (NO), BR (BE), VI (AT)

Total: ~3.200+ EU common stocks.
Cost: 10 API calls/week (one per exchange, cached in DB).
"""

import logging
import os
import time

import requests

logger = logging.getLogger(__name__)

EODHD_BASE = "https://eodhd.com/api"

# EODHD exchange code → (exchange_id, yfinance_suffix, country)
EXCHANGE_MAP = {
    "XETRA": ("xetra",  ".DE", "DE"),
    "SW":    ("six",    ".SW", "CH"),
    "PA":    ("paris",  ".PA", "FR"),
    "AS":    ("aex",    ".AS", "NL"),
    "ST":    ("omxs",   ".ST", "SE"),
    "HE":    ("omxh",   ".HE", "FI"),
    "CO":    ("omxc",   ".CO", "DK"),
    "OL":    ("ose",    ".OL", "NO"),
    "BR":    ("bru",    ".BR", "BE"),
    "VI":    ("vienna", ".VI", "AT"),
    "LSE":   ("lse",    ".L",  "GB"),
    "MI":    ("milan",  ".MI", "IT"),
    "MC":    ("madrid", ".MC", "ES"),
}

# Noise filter: names that indicate non-stocks
NOISE_KEYWORDS = [
    "etf", "fund", "index", "warrant", "certificate",
    "note", "bond", "zertifikat", "fonds", "trust",
    "reit", "sicav", "ucits", "structured",
]


def fetch_eodhd_eu_universe(api_key: str = None) -> list[dict]:
    """
    Fetch all EU common stocks from EODHD.
    One API call per exchange (~10 total).
    Returns list of company dicts ready for eu_universe_builder.
    """
    key = api_key or os.environ.get("EODHD_API_KEY", "")
    if not key:
        logger.warning("EODHD_API_KEY not set — skipping EODHD universe fetch")
        return []

    companies = []
    seen = set()

    for eodhd_code, (exchange_id, suffix, country) in EXCHANGE_MAP.items():
        try:
            url = f"{EODHD_BASE}/exchange-symbol-list/{eodhd_code}"
            resp = requests.get(url, params={
                "api_token": key,
                "fmt": "json",
                "type": "common_stock",
            }, timeout=20)

            if resp.status_code != 200:
                logger.warning(f"EODHD {eodhd_code}: HTTP {resp.status_code}")
                continue

            stocks = resp.json()
            if not isinstance(stocks, list):
                logger.warning(f"EODHD {eodhd_code}: unexpected response format")
                continue

            added = 0
            for stock in stocks:
                base_ticker = stock.get("Code", "").strip()
                name = stock.get("Name", "").strip()
                isin = stock.get("Isin", "")

                if not base_ticker or not name:
                    continue

                # Filter non-stocks by name
                name_lower = name.lower()
                if any(kw in name_lower for kw in NOISE_KEYWORDS):
                    continue

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
                    "isin": isin,
                    "source": "eodhd",
                })
                added += 1

            logger.info(f"EODHD {eodhd_code} ({country}): {added} Aktien")
            time.sleep(0.2)  # gentle rate limiting

        except Exception as e:
            logger.error(f"EODHD {eodhd_code} fetch failed: {e}")

    logger.info(f"EODHD EU universe: {len(companies)} Aktien über {len(EXCHANGE_MAP)} Börsen")
    return companies
