"""
Bundesanzeiger scraper — free German annual report text for EU filing analysis.

bundesanzeiger.de is the official German company register. All listed AGs must
publish their Geschäftsbericht (annual report) there. This is the EU equivalent
of SEC EDGAR for text extraction — no API key required.

Returns the same dict structure as filing_collector.fetch_filing_data so EU
companies flow through the identical LLM pipeline.
"""

import logging
import re
import time
from typing import Optional

import requests
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

BASE_URL = "https://www.bundesanzeiger.de"
SEARCH_URL = f"{BASE_URL}/pub/de/suche"

HEADERS = {
    "User-Agent": "ThielDetector contact@example.com",
    "Accept-Language": "de-DE,de;q=0.9",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}

# German Thiel-relevant section markers in annual reports
SECTION_MARKERS = {
    "business": [
        "Geschäftstätigkeit", "Geschäftsmodell", "Unternehmensprofil",
        "Unser Unternehmen", "Das Unternehmen", "Über uns",
        "Geschäftsfeld", "Geschäftsbereiche",
    ],
    "competitive_position": [
        "Wettbewerbsposition", "Marktposition", "Wettbewerb",
        "Marktumfeld", "Branchenumfeld", "Competitive",
    ],
    "risk_factors": [
        "Risikobericht", "Risiken", "Chancen und Risiken",
        "Wesentliche Risiken", "Risk Factors",
    ],
    "mda": [
        "Wirtschaftsbericht", "Geschäftsverlauf", "Ertrags-, Finanz- und Vermögenslage",
        "Management Discussion", "Lagebericht",
    ],
}


def fetch_bundesanzeiger_text(company_name: str, ticker: str) -> dict:
    """
    Search Bundesanzeiger for a company and extract annual report text.

    Returns dict compatible with filing_collector result structure:
      business_description, risk_factors, mda, filing_date, source
    """
    result = {
        "business_description": "",
        "risk_factors": "",
        "mda": "",
        "filing_date": None,
        "source": "bundesanzeiger",
        "error": None,
    }

    try:
        # Step 1: Search for company
        filing_url = _search_company(company_name, ticker)
        if not filing_url:
            result["error"] = f"No Bundesanzeiger filing found for {company_name}"
            return result

        # Step 2: Fetch filing page
        time.sleep(1)  # polite delay
        resp = requests.get(filing_url, headers=HEADERS, timeout=20)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "lxml")

        # Step 3: Extract text sections
        full_text = _extract_text(soup)
        if not full_text:
            result["error"] = "Could not extract text from filing"
            return result

        # Step 4: Split into sections
        result["business_description"] = _extract_section(full_text, SECTION_MARKERS["business"], max_chars=4000)
        result["risk_factors"] = _extract_section(full_text, SECTION_MARKERS["risk_factors"], max_chars=2000)
        result["mda"] = _extract_section(full_text, SECTION_MARKERS["mda"], max_chars=2000)

        # If section extraction fails, use beginning of document as business description
        if not result["business_description"] and full_text:
            result["business_description"] = full_text[:4000]

        logger.info(f"{ticker}: Bundesanzeiger text extracted ({len(full_text)} chars)")
        return result

    except requests.RequestException as e:
        result["error"] = f"Request failed: {e}"
        logger.warning(f"{ticker}: Bundesanzeiger fetch failed: {e}")
        return result
    except Exception as e:
        result["error"] = f"Unexpected error: {e}"
        logger.warning(f"{ticker}: Bundesanzeiger error: {e}")
        return result


def _search_company(company_name: str, ticker: str) -> Optional[str]:
    """
    Search Bundesanzeiger for the most recent annual report (Jahresabschluss)
    of a company. Returns URL of the filing page or None.
    """
    # Clean company name for search (remove AG, SE, GmbH suffixes for better results)
    search_name = re.sub(r"\s+(AG|SE|GmbH|KGaA|KG|NV|SA|PLC)$", "", company_name, flags=re.IGNORECASE).strip()

    try:
        resp = requests.get(
            SEARCH_URL,
            params={
                "fulltext": search_name,
                "ftTyp": "FT_GB",  # Geschäftsbericht
                "releaseType": "EB",
            },
            headers=HEADERS,
            timeout=20,
        )
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "lxml")

        # Find first result link
        results = soup.select("a.result_link, .result-entry a, table.result_table a")
        if results:
            href = results[0].get("href", "")
            if href:
                return href if href.startswith("http") else BASE_URL + href

        # Fallback: try without ftTyp filter
        resp2 = requests.get(
            SEARCH_URL,
            params={"fulltext": search_name},
            headers=HEADERS,
            timeout=20,
        )
        soup2 = BeautifulSoup(resp2.text, "lxml")
        results2 = soup2.select("a.result_link, .result-entry a")
        if results2:
            href = results2[0].get("href", "")
            if href:
                return href if href.startswith("http") else BASE_URL + href

    except Exception as e:
        logger.debug(f"Bundesanzeiger search failed for {company_name}: {e}")

    return None


def _extract_text(soup: BeautifulSoup) -> str:
    """Extract readable text from a Bundesanzeiger filing page."""
    # Remove navigation, headers, footers
    for tag in soup.select("nav, header, footer, script, style, .navigation, .breadcrumb"):
        tag.decompose()

    # Try main content area first
    main = soup.select_one("main, .content, #content, .publication-text, article")
    if main:
        return _clean_text(main.get_text(separator=" ", strip=True))

    # Fallback: body text
    body = soup.find("body")
    if body:
        return _clean_text(body.get_text(separator=" ", strip=True))

    return ""


def _extract_section(text: str, markers: list[str], max_chars: int = 2000) -> str:
    """
    Extract a section from full text by finding the first matching marker
    and taking the next max_chars characters.
    """
    text_lower = text.lower()
    for marker in markers:
        idx = text_lower.find(marker.lower())
        if idx != -1:
            # Find the end of the section (next major heading or max_chars)
            section = text[idx:idx + max_chars * 2]
            # Try to cut at a natural boundary
            if len(section) > max_chars:
                cutoff = section.rfind(". ", 0, max_chars)
                if cutoff > max_chars * 0.7:
                    section = section[:cutoff + 1]
                else:
                    section = section[:max_chars] + "..."
            return section.strip()
    return ""


def _clean_text(text: str) -> str:
    """Normalize whitespace and remove noise from extracted text."""
    text = re.sub(r"\s+", " ", text)
    text = re.sub(r"(\n\s*){3,}", "\n\n", text)
    # Remove page numbers and common artifacts
    text = re.sub(r"\b\d{1,3}\s*\|\s*", "", text)
    return text.strip()
