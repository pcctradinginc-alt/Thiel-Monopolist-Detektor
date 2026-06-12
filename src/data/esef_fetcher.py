"""
ESEF-Geschäftsberichte via filings.xbrl.org — kostenlose Volltexte für EU-Firmen.

Seit 2021 müssen EU-Emittenten Jahresberichte im ESEF-Format (iXBRL/XHTML)
einreichen. filings.xbrl.org indexiert diese zentral und kostenlos; GLEIF
liefert den LEI-Lookup (Firmenname → LEI), ebenfalls kostenlos und ohne Key.

Das schließt die größte Datenlücke der EU-Pipeline: Statt Wikipedia-Zweizeilern
bekommt das LLM echte Geschäftsberichts-Abschnitte (Geschäftsmodell, Risiko-
bericht, Lagebericht) — auf Deutsch/Englisch/Lokalsprache, was die Keyword-
Lanes (deutsche Keywords vorhanden) und Claude problemlos verarbeiten.

Abdeckung ist je nach Land unvollständig (best effort) — die Quelle reiht
sich hinter Bundesanzeiger (DE) und Companies House (UK) ein.
"""

import html as html_lib
import logging
import re
from typing import Optional

import requests

logger = logging.getLogger(__name__)

GLEIF_API = "https://api.gleif.org/api/v1/lei-records"
XBRL_API = "https://filings.xbrl.org/api"
XBRL_BASE = "https://filings.xbrl.org"

MAX_REPORT_BYTES = 15 * 1024 * 1024   # Größen-Guard für XHTML-Downloads
_HEADERS = {"User-Agent": "ThielDetector research (github.com/pcctradinginc-alt)"}

# Mengenschutz: Die EU-Queue kann ~500 Kandidaten enthalten — ungebremst wären
# das hunderte GLEIF/xbrl.org-Lookups + potenziell GB an Downloads pro Lauf.
# Budget pro Prozess; nicht abgedeckte Firmen kommen in späteren Runs dran
# (Rotation), der Rest fällt auf Wikipedia/yfinance zurück.
MAX_LOOKUPS_PER_RUN = 60
_lookups_this_run = 0

# Abschnitts-Anker (DE/EN/FR/IT/ES) — Fenster um den ersten Treffer wird extrahiert
_SECTION_ANCHORS = {
    "business_description": [
        "geschäftsmodell", "geschäftstätigkeit", "unternehmensprofil",
        "business model", "our business", "principal activities",
        "description of the business", "modèle d'affaires", "modello di business",
        "modelo de negocio",
    ],
    "risk_factors": [
        "risikobericht", "risikomanagement", "wesentliche risiken",
        "principal risks", "risk management", "risk factors",
        "facteurs de risque", "fattori di rischio", "factores de riesgo",
    ],
    "mda": [
        "ertragslage", "geschäftsverlauf", "wirtschaftsbericht",
        "financial performance", "review of operations", "business review",
        "financial review", "andamento della gestione", "evolución del negocio",
    ],
}


def _strip_xhtml(raw: str) -> str:
    """iXBRL/XHTML → Fließtext."""
    text = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", raw, flags=re.S | re.I)
    text = re.sub(r"<[^>]+>", " ", text)
    text = html_lib.unescape(text)
    return re.sub(r"\s+", " ", text).strip()


def _extract_section(text: str, anchors: list[str], window: int) -> str:
    """Fenster ab dem ersten Anker-Treffer (Inhaltsverzeichnis überspringen)."""
    lower = text.lower()
    # Erste 3% überspringen — dort steht meist nur das Inhaltsverzeichnis
    start_search = len(text) // 33
    for anchor in anchors:
        idx = lower.find(anchor, start_search)
        if idx >= 0:
            return text[idx:idx + window]
    return ""


def find_lei(company_name: str) -> list[str]:
    """
    Firmenname → LEI-Kandidaten via GLEIF (kostenlos, kein Key).
    Gibt bis zu 3 Kandidaten zurück — die Zuordnung Name→LEI ist nicht
    eindeutig (Stiftungen, Töchter), der Caller probiert sie der Reihe nach.
    """
    try:
        resp = requests.get(GLEIF_API, params={
            "filter[entity.legalName]": company_name,
            "page[size]": 3,
        }, headers=_HEADERS, timeout=15)
        resp.raise_for_status()
        records = resp.json().get("data", [])
        leis = []
        for r in records:
            attrs = r.get("attributes", {})
            if attrs.get("entity", {}).get("category") == "FUND":
                continue
            leis.append(attrs.get("lei"))
        return [l for l in leis if l]
    except Exception as e:
        logger.debug(f"GLEIF lookup failed for {company_name!r}: {e}")
        return []


def _latest_filing_url(lei: str) -> Optional[tuple[str, str]]:
    """Jüngstes ESEF-Filing eines LEI: (report_url, period_end) oder None."""
    try:
        resp = requests.get(f"{XBRL_API}/entities/{lei}/filings",
                            params={"page[size]": 30},
                            headers=_HEADERS, timeout=15)
        if resp.status_code != 200:
            return None
        filings = resp.json().get("data", [])
        best = None
        for f in filings:
            a = f.get("attributes", {})
            url = a.get("report_url")
            period = a.get("period_end") or ""
            if url and (best is None or period > best[1]):
                best = (url, period)
        return best
    except Exception as e:
        logger.debug(f"xbrl.org filings lookup failed for {lei}: {e}")
        return None


def fetch_esef_text(company_name: str, ticker: str = "") -> dict:
    """
    Volltext-Abschnitte aus dem jüngsten ESEF-Geschäftsbericht.
    Gibt {business_description, risk_factors, mda, filing_date, error} zurück.
    Best effort: {} -Felder bleiben leer, wenn Firma nicht im Index ist.
    """
    global _lookups_this_run
    result = {"business_description": "", "risk_factors": "", "mda": "",
              "filing_date": None, "error": None}
    if not company_name:
        result["error"] = "no company name"
        return result

    if _lookups_this_run >= MAX_LOOKUPS_PER_RUN:
        result["error"] = "ESEF lookup budget exhausted"
        return result
    _lookups_this_run += 1

    filing = None
    for lei in find_lei(company_name):
        filing = _latest_filing_url(lei)
        if filing:
            break
    if not filing:
        result["error"] = "not in ESEF index"
        return result

    report_url, period_end = filing
    try:
        resp = requests.get(f"{XBRL_BASE}{report_url}", headers=_HEADERS,
                            timeout=60, stream=True)
        resp.raise_for_status()
        size = int(resp.headers.get("content-length") or 0)
        if size > MAX_REPORT_BYTES:
            result["error"] = f"report too large ({size} bytes)"
            return result
        # Stückweise lesen mit hartem Cap — resp.content würde trotz
        # stream=True den kompletten Body laden, auch ohne content-length
        chunks, read = [], 0
        for chunk in resp.iter_content(chunk_size=1 << 20):
            chunks.append(chunk)
            read += len(chunk)
            if read > MAX_REPORT_BYTES:
                break
        raw = b"".join(chunks)[:MAX_REPORT_BYTES].decode("utf-8", errors="ignore")
    except Exception as e:
        result["error"] = f"download failed: {e}"
        return result

    text = _strip_xhtml(raw)
    if len(text.split()) < 500:
        result["error"] = "extracted text too short"
        return result

    result["business_description"] = _extract_section(
        text, _SECTION_ANCHORS["business_description"], window=8000)
    result["risk_factors"] = _extract_section(
        text, _SECTION_ANCHORS["risk_factors"], window=6000)
    result["mda"] = _extract_section(
        text, _SECTION_ANCHORS["mda"], window=6000)

    # Fallback: kein Anker gefunden → Berichtsanfang (nach dem Inhaltsverzeichnis)
    if not result["business_description"]:
        start = len(text) // 20
        result["business_description"] = text[start:start + 8000]

    result["filing_date"] = period_end
    logger.info(
        f"{ticker or company_name}: ESEF-Bericht {period_end} geladen "
        f"({len(text.split())} Wörter Volltext)"
    )
    return result
