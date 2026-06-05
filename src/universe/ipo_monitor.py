"""
IPO Monitor — finds upcoming EU and US IPOs before they start trading.

Sources checked weekly:
  EU:
    - ESMA Prospectus Register (EU-wide, 2-8 weeks before listing)
    - BaFin Prospektdatenbank (Germany, 2-4 weeks before listing)
    - Deutsche Börse IPO calendar (XETRA, 1-4 weeks)
    - Euronext new listings (AEX/Paris/Brussels)
  US:
    - SEC EDGAR S-1/F-1 filings (already covered by ipo_recent cohort)

Why this matters:
  The prospectus moment is the single richest signal for hidden Thiel moats.
  A company describing its market for the FIRST TIME cannot yet disguise its
  competitive position as effectively as a 5-year public company can.
  Early detection = maximum signal strength.

Returns list of pre-IPO company dicts ready for the screening pipeline.
"""

import logging
import re
import time
from datetime import datetime, timezone, timedelta
from typing import Optional

import requests
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

HEADERS = {"User-Agent": "ThielDetector info@pcctradinginc.com"}


# ─── ESMA Prospectus Register ────────────────────────────────────────────────

def fetch_esma_prospectuses(months_back: int = 3) -> list[dict]:
    """
    Query ESMA's public prospectus register for recent equity prospectuses.
    Returns list of upcoming/recent IPO candidates.

    ESMA register: https://registers.esma.europa.eu/publication/searchRegister
    """
    results = []
    cutoff = (datetime.now(timezone.utc) - timedelta(days=months_back * 30)
              ).strftime("%Y-%m-%d")

    try:
        resp = requests.get(
            "https://registers.esma.europa.eu/publication/searchRegister",
            params={
                "core": "esma_registers_priii_brochures",
                "q": "*",
                "fq": f"date_approval:[{cutoff}T00:00:00Z TO NOW]",
                "fq2": "type_instrument:Equity",
                "rows": 200,
                "start": 0,
                "wt": "json",
            },
            headers=HEADERS,
            timeout=20,
        )
        if resp.status_code != 200:
            logger.debug(f"ESMA register HTTP {resp.status_code}")
            return []

        data = resp.json()
        docs = data.get("response", {}).get("docs", [])

        for doc in docs:
            name = doc.get("issuer_name", "")
            country = doc.get("home_member_state", "")
            approval_date = doc.get("date_approval", "")
            isin = doc.get("isin", "")

            if not name:
                continue

            # Map country to exchange
            country_exchange = {
                "DE": ("xetra", ".DE"), "CH": ("six", ".SW"),
                "NL": ("aex", ".AS"), "FR": ("paris", ".PA"),
                "AT": ("vienna", ".VI"), "SE": ("omxs", ".ST"),
                "FI": ("omxh", ".HE"), "DK": ("omxc", ".CO"),
                "NO": ("ose", ".OL"), "BE": ("bru", ".BR"),
                "GB": ("lse", ".L"), "IE": ("ise", ".IR"),
                "ES": ("bme", ".MC"), "IT": ("mil", ".MI"),
            }
            exchange_id, suffix = country_exchange.get(country, ("eu", ""))

            results.append({
                "ticker": f"IPO_{isin or name[:8].replace(' ','_').upper()}",
                "name": name,
                "exchange": exchange_id,
                "exchange_suffix": suffix,
                "cohort_id": "eu_ipo",
                "country": country,
                "source": "esma_prospectus",
                "ipo_date": approval_date[:10] if approval_date else None,
                "isin": isin,
                "has_prospectus": True,
            })

        logger.info(f"ESMA: {len(results)} equity prospectuses in last {months_back} months")
        return results

    except Exception as e:
        logger.warning(f"ESMA prospectus fetch failed: {e}")
        return []


# ─── Deutsche Börse IPO Calendar ─────────────────────────────────────────────

def fetch_deutsche_boerse_ipos() -> list[dict]:
    """
    Fetch upcoming and recent IPOs from Deutsche Börse's public IPO calendar.
    """
    results = []
    try:
        resp = requests.get(
            "https://www.xetra.com/xetra-en/instruments/shares/ipo-new-listings",
            headers=HEADERS,
            timeout=15,
        )
        if resp.status_code != 200:
            return []

        soup = BeautifulSoup(resp.text, "lxml")

        # Parse IPO table
        for table in soup.select("table"):
            for row in table.select("tr")[1:]:
                cells = row.select("td")
                if len(cells) < 3:
                    continue
                name = cells[0].get_text(strip=True)
                ticker_raw = cells[1].get_text(strip=True) if len(cells) > 1 else ""
                date = cells[2].get_text(strip=True) if len(cells) > 2 else ""

                if name and len(name) > 2:
                    sym = re.sub(r'[^A-Z0-9]', '', ticker_raw.upper())[:6]
                    results.append({
                        "ticker": f"{sym}.DE" if sym else f"IPO_{name[:8].replace(' ','_').upper()}.DE",
                        "name": name,
                        "exchange": "xetra",
                        "exchange_suffix": ".DE",
                        "cohort_id": "eu_ipo",
                        "country": "DE",
                        "source": "deutsche_boerse_ipo",
                        "ipo_date": date,
                        "has_prospectus": False,
                    })

        logger.info(f"Deutsche Börse IPO calendar: {len(results)} entries")
        return results

    except Exception as e:
        logger.debug(f"Deutsche Börse IPO calendar failed: {e}")
        return []


# ─── Euronext New Listings ────────────────────────────────────────────────────

def fetch_euronext_new_listings() -> list[dict]:
    """Fetch new listings from Euronext Amsterdam, Paris, Brussels."""
    results = []
    markets = [
        ("XAMS", ".AS", "NL", "aex"),
        ("XPAR", ".PA", "FR", "paris"),
        ("XBRU", ".BR", "BE", "bru"),
    ]
    for mic, suffix, country, exchange_id in markets:
        try:
            resp = requests.get(
                "https://live.euronext.com/en/pd_ajax/new-listings",
                params={"mics": mic, "start": 0, "length": 50},
                headers={**HEADERS, "X-Requested-With": "XMLHttpRequest"},
                timeout=15,
            )
            if resp.status_code == 200:
                for item in resp.json().get("data", []):
                    item_str = str(item)
                    ticker_m = re.search(r'>([A-Z0-9]{2,8})<', item_str)
                    name_m = re.search(r'<td[^>]*>([^<]{5,60})</td>', item_str)
                    if ticker_m:
                        sym = ticker_m.group(1)
                        results.append({
                            "ticker": f"{sym}{suffix}",
                            "name": name_m.group(1).strip() if name_m else sym,
                            "exchange": exchange_id,
                            "exchange_suffix": suffix,
                            "cohort_id": "eu_ipo",
                            "country": country,
                            "source": "euronext_new_listing",
                            "has_prospectus": False,
                        })
        except Exception as e:
            logger.debug(f"Euronext {mic} new listings failed: {e}")

    logger.info(f"Euronext new listings: {len(results)} entries")
    return results


# ─── Main Entry Point ─────────────────────────────────────────────────────────

def fetch_all_upcoming_ipos(months_back: int = 3) -> list[dict]:
    """
    Aggregate IPOs from all sources. Deduplicates by name similarity.
    Called weekly as part of universe build.
    """
    all_ipos = []
    seen_names = set()

    sources = [
        ("ESMA", fetch_esma_prospectuses, {"months_back": months_back}),
        ("Deutsche Börse", fetch_deutsche_boerse_ipos, {}),
        ("Euronext", fetch_euronext_new_listings, {}),
    ]

    for source_name, fetch_fn, kwargs in sources:
        try:
            ipos = fetch_fn(**kwargs)
            added = 0
            for ipo in ipos:
                name_key = re.sub(r'[^a-z]', '', ipo.get("name", "").lower())[:12]
                if name_key and name_key not in seen_names:
                    seen_names.add(name_key)
                    all_ipos.append(ipo)
                    added += 1
            logger.info(f"IPO Monitor — {source_name}: {added} unique candidates")
        except Exception as e:
            logger.warning(f"IPO Monitor — {source_name} failed: {e}")

    logger.info(f"IPO Monitor total: {len(all_ipos)} upcoming/recent IPOs")
    return all_ipos
