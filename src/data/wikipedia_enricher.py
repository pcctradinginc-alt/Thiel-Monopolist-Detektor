"""
Wikipedia-basierte Unternehmensbeschreibungen als kostenloser Fallback.

Ziel: EU-Firmen die weder Bundesanzeiger noch yfinance-Beschreibung haben
      (v.a. kleinere Nordics, Benelux, österreichische Firmen).

Kosten: $0 — Wikipedia REST API, kein Key nötig.
Abdeckung: ~60-70% aller gelisteten EU-Firmen mit validen Beschreibungen.

Qualitätssicherung:
- Beschreibung muss Firmenname oder Ticker-Base enthalten (kein Fehlzugriff)
- Mindestlänge 80 Zeichen
- Muss nach Unternehmens-Kontext klingen
"""

import logging
import time
from typing import Optional

import requests

logger = logging.getLogger(__name__)

_BASE = "https://en.wikipedia.org/api/rest_v1/page/summary"
_SEARCH = "https://en.wikipedia.org/w/api.php"
_HEADERS = {"User-Agent": "ThielDetector info@pcctradinginc.com"}

# Keywords die signalisieren: das ist ein Unternehmens-Artikel
_COMPANY_SIGNALS = [
    "company", "corporation", "manufactur", "provider", "software",
    "founded", "headquarter", "listed", "products", "services",
    "group", "subsidiary", "revenue", "employees", "publicly",
]


def _is_valid_company_article(extract: str, company_name: str, ticker: str) -> bool:
    """
    Strenge Prüfung: Wikipedia-Artikel muss eindeutig das gesuchte Unternehmen beschreiben.
    Verhindert Fehlzugriffe auf Personen-Artikel oder ähnlich benannte Firmen.
    """
    if not extract or len(extract) < 80:
        return False

    lower = extract.lower()

    # Muss nach Unternehmen klingen
    if not any(w in lower for w in _COMPANY_SIGNALS):
        return False

    # Erstes signifikantes Wort des Firmennamens muss im Text stehen
    # (nicht generische Wörter wie "Group", "AG", "AB", "Holdings")
    skip_generic = {"group", "holding", "holdings", "ag", "ab", "nv", "se",
                    "plc", "ltd", "inc", "corp", "gmbh", "asa", "a/s", "oyj",
                    "the", "and", "for", "new"}
    name_words = [w.lower() for w in company_name.split() if w.lower() not in skip_generic]

    if not name_words:
        return False

    # Wikipedia-Unternehmensartikel beginnen mit "CompanyName is a..."
    # → erster signifikanter Name-Teil muss in den ersten 60 Zeichen stehen
    opening = lower[:60]
    first_word = name_words[0]
    if len(first_word) < 4:
        return False

    return first_word in opening


def _fetch_summary(title: str) -> str:
    """Holt Wikipedia-Summary für einen exakten Titel."""
    try:
        r = requests.get(
            f"{_BASE}/{title.replace(' ', '_')}",
            headers=_HEADERS,
            timeout=6,
        )
        if r.status_code == 200:
            return r.json().get("extract", "")
    except Exception:
        pass
    return ""


def _search_wikipedia(query: str) -> list[str]:
    """Sucht Wikipedia nach Titeln, gibt bis zu 3 zurück."""
    try:
        r = requests.get(
            _SEARCH,
            params={
                "action": "query",
                "list": "search",
                "srsearch": query,
                "format": "json",
                "srlimit": 3,
            },
            headers=_HEADERS,
            timeout=6,
        )
        hits = r.json().get("query", {}).get("search", [])
        return [h["title"] for h in hits]
    except Exception:
        return []


def get_wikipedia_description(company_name: str, ticker: str) -> str:
    """
    Sucht Wikipedia-Beschreibung für ein Unternehmen.
    Gibt leeren String zurück wenn nichts Passendes gefunden.

    Strategie:
    1. Direkt nach Firmennamen
    2. Search → validiere Top-3 Treffer
    3. Nur zurückgeben wenn Artikel wirklich zum Unternehmen passt
    """
    base_ticker = ticker.split(".")[0]

    # Suchvarianten: von spezifisch zu allgemein
    search_queries = [
        company_name,
        f"{company_name} company",
        f"{base_ticker} {company_name.split()[0]}",
    ]

    for query in search_queries:
        # Direkter Abruf
        extract = _fetch_summary(query)
        if extract and _is_valid_company_article(extract, company_name, ticker):
            logger.debug(f"{ticker}: Wikipedia found via direct '{query}' ({len(extract)} chars)")
            return extract[:3000]

        # Search-Fallback
        titles = _search_wikipedia(query)
        for title in titles:
            extract = _fetch_summary(title)
            if extract and _is_valid_company_article(extract, company_name, ticker):
                logger.debug(f"{ticker}: Wikipedia found via search '{title}' ({len(extract)} chars)")
                return extract[:3000]

        time.sleep(0.1)  # sanftes Rate Limiting

    logger.debug(f"{ticker}: Wikipedia — no valid article found")
    return ""
