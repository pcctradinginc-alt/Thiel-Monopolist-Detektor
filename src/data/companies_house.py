"""
Companies House (UK) — free structured filing data for UK companies.

The best free European filing source. Comparable to SEC EDGAR:
- Machine-readable company profiles
- Annual reports (Confirmation Statements, Accounts)
- Officer/director data → family ownership signal
- Filing history

API: https://api.company-information.service.gov.uk/
Free API key: https://developer.company-information.service.gov.uk/
Rate limit: 600 requests/5 minutes (generous)

Secret: COMPANIES_HOUSE_API_KEY
"""

import logging
import os
import time
from typing import Optional

import requests
from requests.auth import HTTPBasicAuth

logger = logging.getLogger(__name__)

CH_BASE = "https://api.company-information.service.gov.uk"
CH_DOC_BASE = "https://document-api.company-information.service.gov.uk"


def _ch_get(path: str, api_key: str, params: dict = None) -> Optional[dict]:
    """Authenticated GET to Companies House API."""
    try:
        resp = requests.get(
            f"{CH_BASE}{path}",
            auth=HTTPBasicAuth(api_key, ""),
            params=params or {},
            timeout=15,
            headers={"User-Agent": "ThielDetector info@pcctradinginc.com"},
        )
        if resp.status_code == 200:
            return resp.json()
        logger.debug(f"CH API {path}: {resp.status_code}")
        return None
    except Exception as e:
        logger.debug(f"CH API error {path}: {e}")
        return None


def search_company(name: str, api_key: str) -> Optional[dict]:
    """Find a company by name, return best match."""
    data = _ch_get("/search/companies", api_key,
                   params={"q": name, "items_per_page": 5})
    if not data:
        return None
    items = data.get("items", [])
    if not items:
        return None
    # Prefer active companies
    active = [i for i in items if i.get("company_status") == "active"]
    return (active or items)[0]


def get_filing_text(ticker: str, company_name: str,
                    api_key: str = None) -> dict:
    """
    Fetch annual report text from Companies House for a UK company.
    Returns dict compatible with filing_collector result structure.
    """
    key = api_key or os.environ.get("COMPANIES_HOUSE_API_KEY", "")
    result = {
        "business_description": "",
        "risk_factors": "",
        "mda": "",
        "filing_date": None,
        "source": "companies_house",
        "error": None,
    }

    if not key:
        result["error"] = "COMPANIES_HOUSE_API_KEY not set"
        return result

    # Step 1: Find company number
    company = search_company(company_name, key)
    if not company:
        result["error"] = f"Company not found: {company_name}"
        return result

    company_number = company.get("company_number", "")
    if not company_number:
        result["error"] = "No company number"
        return result

    # Step 2: Get company profile (officers = family ownership signal)
    profile = _ch_get(f"/company/{company_number}", key)
    if profile:
        sic_codes = profile.get("sic_codes", [])
        result["sic_codes"] = sic_codes

    # Step 3: Get officers (directors) — high insider count = family business
    officers = _ch_get(f"/company/{company_number}/officers", key,
                       params={"items_per_page": 20})
    if officers:
        active_officers = [o for o in officers.get("items", [])
                           if not o.get("resigned_on")]
        result["officer_count"] = len(active_officers)
        result["officer_names"] = [o.get("name", "") for o in active_officers[:5]]

    # Step 4: Get latest annual accounts filing
    filings = _ch_get(f"/company/{company_number}/filing-history", key,
                      params={"category": "accounts", "items_per_page": 5})
    if not filings:
        result["error"] = "No filing history"
        return result

    latest = None
    for filing in filings.get("items", []):
        if filing.get("type") in ("AA", "AA01", "LLP AA"):  # Annual accounts
            latest = filing
            break

    if not latest:
        result["error"] = "No annual accounts found"
        return result

    result["filing_date"] = latest.get("date")

    # Step 5: Try to fetch document text
    doc_url = latest.get("links", {}).get("document_metadata", "")
    if doc_url:
        try:
            doc_resp = requests.get(
                doc_url,
                auth=HTTPBasicAuth(key, ""),
                timeout=15,
                headers={"User-Agent": "ThielDetector info@pcctradinginc.com"},
            )
            if doc_resp.status_code == 200:
                doc_data = doc_resp.json()
                # Get text/html version if available
                for resource in doc_data.get("resources", {}).values():
                    if resource.get("content_type") in ("text/html", "application/xhtml+xml"):
                        content_url = resource.get("links", {}).get("self", "")
                        if content_url:
                            text_resp = requests.get(
                                content_url,
                                auth=HTTPBasicAuth(key, ""),
                                timeout=20,
                            )
                            if text_resp.status_code == 200:
                                from bs4 import BeautifulSoup
                                soup = BeautifulSoup(text_resp.text, "lxml")
                                text = soup.get_text(separator=" ", strip=True)
                                # Extract key sections
                                result["business_description"] = _extract_section(
                                    text, ["Strategic Report", "Business Review",
                                           "Our Business", "Principal Activities"],
                                    4000
                                )
                                result["risk_factors"] = _extract_section(
                                    text, ["Principal Risks", "Risk Factors",
                                           "Key Risks", "Risks and Uncertainties"],
                                    2000
                                )
                                result["mda"] = _extract_section(
                                    text, ["Financial Review", "Performance Review",
                                           "Chief Financial Officer"],
                                    2000
                                )
                                if not result["business_description"] and text:
                                    result["business_description"] = text[:4000]
                                logger.info(f"{ticker}: Companies House text extracted")
                                return result
        except Exception as e:
            logger.debug(f"{ticker}: CH document fetch failed: {e}")

    # Fallback: use company description from profile
    if profile:
        desc = (f"UK company registered under number {company_number}. "
                f"SIC codes: {', '.join(sic_codes)}. "
                f"Status: {profile.get('company_status', 'unknown')}.")
        result["business_description"] = desc

    return result


def _extract_section(text: str, markers: list, max_chars: int) -> str:
    text_lower = text.lower()
    for marker in markers:
        idx = text_lower.find(marker.lower())
        if idx != -1:
            section = text[idx:idx + max_chars * 2]
            if len(section) > max_chars:
                cut = section.rfind(". ", 0, max_chars)
                section = section[:cut + 1] if cut > max_chars * 0.7 else section[:max_chars]
            return section.strip()
    return ""
