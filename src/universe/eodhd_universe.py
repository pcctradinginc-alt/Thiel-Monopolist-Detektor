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
import re
import time

import requests

logger = logging.getLogger(__name__)

EODHD_BASE = "https://eodhd.com/api"

# EODHD exchange code → (exchange_id, yfinance_suffix, country)
# Reihenfolge = Dedupe-Priorität: Heimatbörsen zuerst, LSE zuletzt —
# bei Doppellistings gewinnt das Heimatlisting (bessere Filing-Quellen).
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
    "WAR":   ("gpw",    ".WA", "PL"),
    "MI":    ("milan",  ".MI", "IT"),
    "MC":    ("madrid", ".MC", "ES"),
    "LSE":   ("lse",    ".L",  "GB"),
}

# ISIN-Länderpräfixe europäischer Emittenten (inkl. Kanalinseln/IoM als
# übliche UK-Holding-Domizile). Nicht-europäische ISINs (US, CN, KY, ...)
# sind Zweitlistings — deren Analyse gehört in die US-Pipeline.
EUROPEAN_ISIN_PREFIXES = {
    "AT", "BE", "BG", "CH", "CY", "CZ", "DE", "DK", "EE", "ES", "FI",
    "FR", "GB", "GG", "GI", "GR", "HR", "HU", "IE", "IM", "IS", "IT",
    "JE", "LI", "LT", "LU", "LV", "MT", "NL", "NO", "PL", "PT", "RO",
    "SE", "SI", "SK",
}

# LSE International Order Book: Zweitlistings ausländischer Firmen
# (Alibaba=0HCI, Datadog=0A3O, Boeing=0BOE ...) tragen Ticker der Form 0XXX.
_LSE_IOB_PATTERN = re.compile(r"^0[A-Z0-9]{2,4}$")

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
    seen_isins = set()
    skipped_foreign = 0
    skipped_iob = 0

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

                # Nur europäische Emittenten: Nicht-EU-ISINs (US/CN/KY...)
                # sind Zweitlistings und haben die EU-Quote bisher komplett
                # aufgefressen (100% der EU-Evaluationen waren IOB-Ticker).
                if isin and isin[:2].upper() not in EUROPEAN_ISIN_PREFIXES:
                    skipped_foreign += 1
                    continue

                # LSE IOB-Zweitlistings (0XXX) raus — auch bei europäischer
                # ISIN existiert ein Heimatlisting mit besseren Filing-Quellen.
                # Ohne ISIN ist Herkunft unklar → IOB-Muster entscheidet.
                if eodhd_code == "LSE" and _LSE_IOB_PATTERN.match(base_ticker):
                    skipped_iob += 1
                    continue

                full_ticker = f"{base_ticker}{suffix}"
                if full_ticker in seen:
                    continue
                seen.add(full_ticker)

                # Dedupe über Börsen hinweg: Heimatbörse (frühere Map-Position)
                # gewinnt gegen spätere Zweitlistings derselben ISIN.
                if isin:
                    if isin in seen_isins:
                        continue
                    seen_isins.add(isin)

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

    logger.info(
        f"EODHD EU universe: {len(companies)} Aktien über {len(EXCHANGE_MAP)} Börsen "
        f"(gefiltert: {skipped_foreign} Nicht-EU-ISINs, {skipped_iob} LSE-IOB-Zweitlistings)"
    )
    return companies
