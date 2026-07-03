"""
EU Universe Builder — DACH + NL + FR equity universe for Thiel screening.

Strategy:
  1. Static seed lists (always works, no external dependency)
  2. Try to fetch live index compositions from free public sources
  3. yfinance pre-filter removes ~80% before any Eulerpool call
  4. Survivors written to DB with eu_ cohort prefix

Exchange suffixes:
  XETRA (DE):  .DE
  SIX (CH):    .SW
  Euronext NL: .AS
  Vienna (AT): .VI
  Euronext FR: .PA
"""

import logging
import time
from datetime import datetime, timezone
from typing import Optional

import requests

logger = logging.getLogger(__name__)

try:
    import yfinance as yf
    YFINANCE_AVAILABLE = True
except ImportError:
    YFINANCE_AVAILABLE = False


# ─── Static Seed Lists ───────────────────────────────────────────────────────
# Source: index compositions as of 2026-Q1. Updated manually when index changes.
# Format: (base_ticker, company_name)

XETRA_SEEDS = [
    # ── DAX 40 ───────────────────────────────────────────────────────────────
    ("ADS", "Adidas AG"), ("AIR", "Airbus SE"), ("ALV", "Allianz SE"),
    ("BAS", "BASF SE"), ("BAYN", "Bayer AG"), ("BEI", "Beiersdorf AG"),
    ("BMW", "BMW AG"), ("BNR", "Brenntag SE"), ("CON", "Continental AG"),
    ("1COV", "Covestro AG"), ("DHER", "Delivery Hero SE"), ("DB1", "Deutsche Boerse AG"),
    ("DBK", "Deutsche Bank AG"), ("DHL", "Deutsche Post AG"), ("DTE", "Deutsche Telekom AG"),
    ("EOAN", "E.ON SE"), ("FRE", "Fresenius SE"), ("FME", "Fresenius Medical Care AG"),
    ("HEIA", "HeidelbergMaterials AG"), ("HEN3", "Henkel AG"), ("IFX", "Infineon Technologies AG"),
    ("MBG", "Mercedes-Benz Group AG"), ("MRK", "Merck KGaA"), ("MTX", "MTU Aero Engines AG"),
    ("MUV2", "Munich Re AG"), ("PAH3", "Porsche Automobil Holding SE"),
    ("P911", "Porsche AG"), ("PUM", "PUMA SE"), ("RWE", "RWE AG"),
    ("SAP", "SAP SE"), ("SHL", "Siemens Healthineers AG"), ("SIE", "Siemens AG"),
    ("SY1", "Symrise AG"), ("VNA", "Vonovia SE"), ("VOW3", "Volkswagen AG"),
    ("ZAL", "Zalando SE"), ("ENR", "Siemens Energy AG"), ("QIA", "Qiagen NV"),

    # ── MDAX — hohe Thiel-Dichte ─────────────────────────────────────────────
    ("AFX", "Carl Zeiss Meditec AG"), ("AIXA", "Aixtron SE"),
    ("AOF", "Atoss Software AG"),     # HR-Software, ~80% GM, Lock-in
    ("BC8", "Bechtle AG"), ("BOSS", "Hugo Boss AG"),
    ("CWC", "Cancom SE"), ("DWS", "DWS Group GmbH"),
    ("ECK", "Eckert & Ziegler AG"),   # Radioaktive Isotope, regulatorischer Moat
    ("EVD", "CTS Eventim AG"), ("EVK", "Evonik Industries AG"),
    ("FNTN", "freenet AG"), ("GBF", "Bilfinger SE"), ("GFJ", "Grenke AG"),
    ("HAG", "Hensoldt AG"), ("HAW", "Hawesko Holding AG"),
    ("HFG", "HelloFresh SE"), ("HOT", "Hochtief AG"),
    ("HYQ", "Hypoport SE"),           # Kredit-Plattform für Banken
    ("KGX", "Kion Group AG"), ("KSB3", "KSB SE"), ("LEG", "LEG Immobilien SE"),
    ("LHA", "Lufthansa AG"), ("MED", "Medios AG"),
    ("NEM", "Nemetschek SE"),         # BIM-Software, starker Lock-in
    ("OHB", "OHB SE"), ("PSAN", "ProSiebenSat.1 Media SE"),
    ("RAA", "RATIONAL AG"),           # Profi-Küchentech, 50%+ Weltmarktanteil
    ("RRTL", "RTL Group SA"),
    ("S92", "SMA Solar Technology AG"), ("SBS", "Stratec SE"),  # OEM Sole-Source
    ("SGCG", "Siltronic AG"), ("SIX2", "Sixt SE"),
    ("SMHN", "SUESS MicroTec SE"), ("SOW", "Software AG"), ("SPM", "Stabilus SE"),
    ("SRT3", "Sartorius AG"),
    ("TAG", "TAG Immobilien AG"), ("TIM", "Traton SE"), ("TLX", "Talanx AG"),
    ("TUI1", "TUI AG"), ("VBK", "Verbio SE"),
    ("VIB3", "Villeroy & Boch AG"), ("VOS", "Vossloh AG"),
    ("WBAG", "Westwing Group SE"), ("WCH", "Wacker Chemie AG"),

    # ── TecDAX — höchste Thiel-Dichte ────────────────────────────────────────
    ("BFSA", "Befesa SA"), ("DBAN", "Deutsche Beteiligungs AG"),
    ("GFT", "GFT Technologies SE"),   # Fintech-IT tief in Banken integriert
    ("IOS", "IONOS Group SE"),        # Cloud-Hosting KMU, Switching Costs
    ("MBB", "MBB SE"),                # Beteiligungsges. Hidden Champions
    ("NA9", "Nagarro SE"),            # Software Engineering Platform
    ("PSH", "PSI Software SE"),       # Energie/Industrie ERP
    ("R3NK", "Renk Group AG"),        # Getriebe Verteidigung/Marine
    ("SFQ", "SAF-Holland SE"),
    ("UTDI", "United Internet AG"),
    ("VIE", "Vienna International Airport"),
    ("WDP", "Warehouses De Pauw"),

    # ── Hidden Champions (bestätigte Thiel-Relevanz) ─────────────────────────
    ("ADN1", "Adesso SE"),            # IT-Consulting Branchen-Spezialist
    ("CLIQ", "CLIQ Digital AG"),      # Digital-Abo Nischenplattform
    ("DBO", "Drägerwerk AG"),         # Medizin/Sicherheitstechnik, Mission-Critical
    ("ECV", "Encavis AG"),
    ("HBH", "Hamburger Hafen und Logistik AG"),
    ("ILM1", "Iliad SA (XETRA)"),
    ("KBC", "KBC Groep (XETRA)"),
    ("KTN", "Kontron AG"),            # Embedded Computing B2B
    ("NDX1", "Nordex SE"),
    ("PGN", "Paragon GmbH & Co"),     # Automotive Elektronik OEM
    ("PSI", "PSI Software AG"),       # Energie/Industrie ERP
    ("PTRO", "Petro Welt Technologies AG"),
    ("PWO", "Progress-Werk Oberkirch AG"),
    ("SBO", "Schoeller-Bleckmann Oilfield"),
    ("SHEL", "Shell PLC (XETRA)"),
    ("SKB", "Skan Group AG"),         # Pharma-Isolatoren, Marktführer
    ("TKA", "Thyssenkrupp AG"),
    ("VBH", "VBH Holding AG"),
    ("ZEG", "Zeal Network SE"),       # Lotterie-Plattform DE, Monopol
]

SIX_SEEDS = [
    # SMI + SMIM + SPI Extra (Switzerland)
    ("ABBN", "ABB Ltd"), ("ADEN", "Adecco Group AG"), ("ALC", "Alcon Inc"),
    ("CFR", "Compagnie Financiere Richemont SA"), ("CSGN", "Credit Suisse Group AG"),
    ("GEBN", "Geberit AG"), ("GIVN", "Givaudan SA"), ("HOLN", "Holcim Ltd"),
    ("KNIN", "Kuehne + Nagel International AG"), ("LONN", "Lonza Group AG"),
    ("NESN", "Nestle SA"), ("NOVN", "Novartis AG"), ("ROG", "Roche Holding AG"),
    ("SCMN", "Swisscom AG"), ("SGSN", "SGS SA"), ("SIKA", "Sika AG"),
    ("SLHN", "Swiss Life Holding AG"), ("SRENH", "Swiss Re AG"),
    ("UBSG", "UBS Group AG"), ("UHRN", "Swatch Group AG"), ("ZURN", "Zurich Insurance Group AG"),
    # SMIM
    ("AMS", "ams OSRAM AG"), ("BAER", "Julius Baer Group AG"), ("BARN", "Barry Callebaut AG"),
    ("BCHN", "Belimo Holding AG"), ("CFRN", "Cembra Money Bank AG"),
    ("DKSH", "DKSH Holding AG"), ("EMMN", "Emmi AG"), ("FLUGN", "Flughafen Zurich AG"),
    ("HELN", "Helvetia Holding AG"), ("HIAG", "HIAG Immobilien Holding AG"),
    ("HOCN", "Hochdorf Holding AG"), ("HOEHN", "Schindler Holding AG"),
    ("IELN", "Interroll Holding AG"), ("INRN", "Inficon Holding AG"),
    ("MBTN", "Meyer Burger Technology AG"), ("MOBN", "Mobimo Holding AG"),
    ("NBEN", "Neue Helvetische Bank AG"), ("OFN", "Orell Fuessli AG"),
    ("PGHN", "Partners Group Holding AG"), ("PSPN", "PSP Swiss Property AG"),
    ("SFPN", "SF Urban Properties AG"), ("SIGN", "SIG Group AG"),
    ("SOFN", "SoftwareONE Holding AG"), ("SRCG", "Straumann Holding AG"),
    ("TEMN", "Temenos AG"), ("VACN", "VAT Group AG"), ("VARN", "Varian Medical (SIX)"),
    ("WKBN", "Wolseley (SIX)"), ("YPSN", "Ypsomed Holding AG"),
    ("ZEHN", "Zehnder Group AG"), ("ZURN2", "Zurich Insurance B"),
]

AEX_SEEDS = [
    # AEX + AMX + AScX (Netherlands)
    ("AALB", "Aalberts Industries NV"), ("ABN", "ABN AMRO Bank NV"),
    ("ADYEN", "Adyen NV"), ("AGN", "Aegon NV"), ("AD", "Ahold Delhaize NV"),
    ("AKZA", "Akzo Nobel NV"), ("AMG", "Advanced Metallurgical Group NV"),
    ("APAM", "Aperam SA"), ("ASM", "ASM International NV"), ("ASME", "ASML Holding NV"),
    ("BESI", "BE Semiconductor Industries NV"), ("BOKA", "Boskalis Westminster NV"),
    ("CMCOM", "CM.com NV"), ("DSFIR", "DSM-Firmenich AG"), ("EXEL", "Exel Industries SA"),
    ("FLOW", "Flow Traders NV"), ("HEIJM", "Heijmans NV"), ("IM", "Imcd Group NV"),
    ("INGA", "ING Groep NV"), ("JDEP", "Just Eat Takeaway NV"),
    ("KPN", "Koninklijke KPN NV"), ("NN", "NN Group NV"), ("NSG", "NSG Group (AEX)"),
    ("NVMI", "Nova Measuring (AEX)"), ("OCI", "OCI NV"), ("PHIA", "Philips NV"),
    ("PRX", "Prosus NV"), ("RAND", "Randstad NV"), ("REN", "Relx NV"),
    ("SBMO", "SBM Offshore NV"), ("TKWY", "Takeaway.com NV"),
    ("UMG", "Universal Music Group NV"), ("URW", "Unibail-Rodamco-Westfield SE"),
    ("VPK", "Koninklijke Vopak NV"), ("WKL", "Wolters Kluwer NV"),
    ("HYDRA", "Hydratight Group"), ("KENDR", "Kendrion NV"),
    ("MTRX", "Matrix IT (AEX)"), ("NEXIA", "Nexia International"),
    ("ORDINA", "Ordina NV"), ("SLIGRO", "Sligro Food Group NV"),
    ("SOFTI", "Softimat SA"), ("STLAM", "Stellantis NV"),
    ("TOM2", "TomTom NV"), ("TWEKA", "TKH Group NV"),
    ("VASTNED", "Vastned Retail NV"), ("VIFOR", "Vifor Pharma (AEX)"),
    ("VMLY", "VML&Y (AEX)"), ("WDP", "Warehouses De Pauw"),
]

VIENNA_SEEDS = [
    # ATX + ATX Prime (Austria)
    ("ABS", "Andritz AG"), ("AGS", "AT&S Austria Technologie AG"),
    ("AMS", "ams AG"), ("ANDR", "Andritz AG"), ("AUA", "Austrian Airlines AG"),
    ("BG", "BAWAG Group AG"), ("BWT", "BWT AG"), ("CA", "CA Immo AG"),
    ("CMDI", "Cembra Money Bank (Vienna)"), ("DO", "DO & CO AG"),
    ("EBS", "Erste Group Bank AG"), ("EVN", "EVN AG"), ("FMB", "Frequentis AG"),
    ("FLUG", "Flughafen Wien AG"), ("IIA", "IIASA (Vienna)"),
    ("IPS", "Ipsidy Inc (Vienna)"), ("KSB", "KSB SE (Vienna)"),
    ("KTCG", "Kapsch TrafficCom AG"), ("LNZ", "Lenzing AG"),
    ("MMK", "Mayr-Melnhof Karton AG"), ("OMV", "OMV AG"),
    ("POS", "Polytec Holding AG"), ("POST", "Oesterreichische Post AG"),
    ("RBI", "Raiffeisen Bank International AG"), ("RHI", "RHI Magnesita NV"),
    ("S&T", "S&T AG"), ("SBO", "Schoeller-Bleckmann Oilfield (Vienna)"),
    ("TKA2", "Telekom Austria AG"), ("UIAG", "Uniqa Insurance Group AG"),
    ("VIG", "Vienna Insurance Group AG"), ("VIR", "Vir Biotechnology (Vienna)"),
    ("VOE", "voestalpine AG"), ("WAB", "Wüstenrot & Württembergische (Vienna)"),
    ("WIE", "Wienerberger AG"), ("ZAG", "Zumtobel Group AG"),
]


# ─── Exchange Configuration ──────────────────────────────────────────────────

NORDIC_SEEDS = [
    # OMX Stockholm — Sweden has many Hidden Champions (Addtech, Lagercrantz etc.)
    ("ADDTECH-B", "Addtech AB"),       ("LAGERCRANTZ-B", "Lagercrantz Group AB"),
    ("INDUTRADE", "Indutrade AB"),     ("LIFCO-B", "Lifco AB"),
    ("NIBE-B", "NIBE Industrier AB"),  ("HEXAGON-B", "Hexagon AB"),
    ("ALFA", "Alfa Laval AB"),         ("ASSA-B", "ASSA ABLOY AB"),
    ("ATCO-A", "Atlas Copco AB"),      ("EPIROC-A", "Epiroc AB"),
    ("EVO", "Evolution AB"),           ("SWECO-B", "Sweco AB"),
    ("NENT-B", "NENT Group AB"),       ("SINCH", "Sinch AB"),
    ("TOBII", "Tobii AB"),             ("EMBRACER", "Embracer Group AB"),
    ("BONESUPPORT", "BoneSupport AB"), ("CELLINK", "BICO Group AB"),
    ("VITROLIFE", "Vitrolife AB"),     ("IMMUNOVIA", "Immunovia AB"),
    ("RAYSEARCH-B", "RaySearch AB"),   ("PROBI", "Probi AB"),
    ("FORTNOX", "Fortnox AB"),         ("VISMA", "Visma AS"),
    ("LIME", "Lime Technologies AB"),  ("AGILLIC", "Agillic A/S"),
    # OMX Helsinki — Finland
    ("NOKIA", "Nokia Oyj"), ("KONE", "KONE Oyj"), ("SAMPO", "Sampo Oyj"),
    ("NESTE", "Neste Oyj"), ("UPM", "UPM-Kymmene Oyj"),
    ("METSO", "Metso Outotec Oyj"), ("WARTSILA", "Wartsila Oyj"),
    ("TIETOEVRY", "TietoEVRY Oyj"), ("TOKMANNI", "Tokmanni Group Oyj"),
    ("REVENIO", "Revenio Group Oyj"), ("ENEDO", "Enedo Oyj"),
    # OMX Copenhagen — Denmark
    ("NOVO-B", "Novo Nordisk A/S"), ("ORSTED", "Orsted A/S"),
    ("COLOPLAST-B", "Coloplast A/S"), ("DSV", "DSV A/S"),
    ("DEMANT", "Demant A/S"), ("GN", "GN Store Nord A/S"),
    ("AMBU-B", "Ambu A/S"), ("ROCKWOOL-B", "Rockwool A/S"),
    ("VESTAS", "Vestas Wind Systems A/S"), ("SIMCORP", "SimCorp A/S"),
    ("NNIT", "NNIT A/S"), ("NETCOMPANY", "Netcompany Group A/S"),
    # Oslo — Norway
    ("EQNR", "Equinor ASA"), ("DNB", "DNB Bank ASA"),
    ("MOWI", "Mowi ASA"), ("TOMRA", "TOMRA Systems ASA"),
    ("KAHOOT", "Kahoot AS"), ("VISTIN", "Vistin Pharma ASA"),
    ("IDEX", "IDEX Biometrics ASA"), ("NORDIC-SEMI", "Nordic Semiconductor ASA"),
    ("TELENOR", "Telenor ASA"), ("AUTOSTORE", "AutoStore Holdings Ltd"),
    ("ORDORO", "Ordoro AS"),
]

BENELUX_EXTRA_SEEDS = [
    # BEL20 + additional Belgian/Luxembourg companies
    ("AB-INBEV", "Anheuser-Busch InBev SA"),
    ("UCB", "UCB SA"), ("AGS", "Ageas SA"),
    ("BEKAERT", "Bekaert SA"), ("COLRUYT", "Colruyt SA"),
    ("ELIA", "Elia Group SA"), ("GBL", "Groupe Bruxelles Lambert SA"),
    ("MELEXIS", "Melexis NV"), ("SOFINA", "Sofina SA"),
    ("LOTUS-BAKERIES", "Lotus Bakeries NV"),
    ("RECTICEL", "Recticel NV"), ("SOLVAY", "Solvay SA"),
    ("TINC", "TINC Comm VA"), ("XIOR", "Xior Student Housing NV"),
    # Strong Belgian tech/niche
    ("BARCO", "Barco NV"),         # Professional display systems
    ("ECONOCOM", "Econocom Group SE"),
    ("EVS", "EVS Broadcast Equipment SA"),  # Live video production
    ("OPTION", "Option NV"),
    ("PICANOL", "Picanol NV"),     # Weaving machines, global leader
]

# London Stock Exchange — UK-Filing-Texte via Companies House bereits unterstützt
LSE_SEEDS = [
    # Daten-/Informations-Monopole
    ("REL", "RELX PLC"),               # Wissenschafts-/Rechts-Datenbanken
    ("LSEG", "London Stock Exchange Group PLC"),
    ("EXPN", "Experian PLC"),          # Credit Bureau Oligopol
    ("ITRK", "Intertek Group PLC"),    # Testing/Inspection/Certification
    ("YOU", "YouGov PLC"),
    # Marktplatz-Monopole
    ("AUTO", "Auto Trader Group PLC"), # De-facto-Monopol Gebrauchtwagen UK
    ("RMV", "Rightmove PLC"),          # De-facto-Monopol Immobilienportale UK
    ("ATG", "Auction Technology Group PLC"),
    ("MONY", "MONY Group PLC"),
    ("CKN", "Clarkson PLC"),           # Weltgrößter Shipbroker
    # Software / Vertical SaaS
    ("SGE", "Sage Group PLC"),
    ("KNOS", "Kainos Group PLC"),
    ("GBG", "GB Group PLC"),           # Identity Verification
    ("ALFA", "Alfa Financial Software Holdings PLC"),
    ("CRW", "Craneware PLC"),          # US-Krankenhaus-Billing-Software
    ("RWS", "RWS Holdings PLC"),       # Patent-Übersetzungen
    ("BYIT", "Bytes Technology Group PLC"),
    ("CCC", "Computacenter PLC"),
    ("WISE", "Wise PLC"),
    # Spezialisierte Industrie / Hidden Champions
    ("HLMA", "Halma PLC"),             # Safety-Nischen-Serienkäufer
    ("SPX", "Spirax Group PLC"),       # Dampftechnik Weltmarktführer
    ("RSW", "Renishaw PLC"),           # Messtechnik
    ("ROR", "Rotork PLC"),             # Stellantriebe
    ("SXS", "Spectris PLC"),
    ("OXIG", "Oxford Instruments PLC"),
    ("JDG", "Judges Scientific PLC"),  # Nischen-Instrumente
    ("DPLM", "Diploma PLC"),           # Nischen-Distribution
    ("CRDA", "Croda International PLC"),
    ("WEIR", "Weir Group PLC"),
    ("SMIN", "Smiths Group PLC"),
    ("RR", "Rolls-Royce Holdings PLC"),   # Triebwerks-Duopol + Aftermarket-Lock-in
    ("BA", "BAE Systems PLC"),         # Regulatorischer Moat
    # Brand-/IP-Monopole
    ("GAW", "Games Workshop Group PLC"),  # Warhammer-IP-Monopol
    # Plattform-Lock-in Finanzen
    ("IHP", "Integrafin Holdings PLC"),   # Transact-Plattform: hohe Wechselkosten
    ("AJB", "AJ Bell PLC"),
    ("PAY", "PayPoint PLC"),
    ("BOKU", "Boku Inc"),              # Carrier-Billing-Netzwerk
    # Distribution
    ("BNZL", "Bunzl PLC"),
    ("HWDN", "Howden Joinery Group PLC"),
    ("AHT", "Ashtead Group PLC"),
    ("FOUR", "4imprint Group PLC"),
]

# Borsa Italiana — Hidden Champions + regulatorische Monopole
MILAN_SEEDS = [
    ("RACE", "Ferrari NV"),            # Brand + Pricing Power
    ("MONC", "Moncler SpA"),
    ("BC", "Brunello Cucinelli SpA"),
    ("AMP", "Amplifon SpA"),           # Hörgeräte-Retail Weltmarktführer
    ("DIA", "DiaSorin SpA"),           # Diagnostik-Nischen
    ("REC", "Recordati SpA"),          # Orphan Drugs
    ("IP", "Interpump Group SpA"),     # Hochdruckpumpen Weltmarktführer
    ("TGYM", "Technogym SpA"),
    ("ENAV", "ENAV SpA"),              # Flugsicherung — staatliches Monopol
    ("INW", "Infrastrutture Wireless Italiane SpA"),  # Funkturm-Quasi-Monopol
    ("MARR", "MARR SpA"),              # Foodservice-Distribution Marktführer
    ("WIIT", "WIIT SpA"),              # Critical-Cloud-Hosting-Nische
    ("REY", "Reply SpA"),
    ("CPR", "Davide Campari-Milano NV"),
    ("DAL", "Datalogic SpA"),
]

# GPW Warschau — größter CEE-Markt, analytisch kaum abgedeckt
WARSAW_SEEDS = [
    ("ALE", "Allegro.eu SA"),          # Dominanter Marketplace PL/CEE, Netzwerkeffekte
    ("DNP", "Dino Polska SA"),         # Ländliche Discounter-Dichte, regionales Quasi-Monopol
    ("TXT", "Text SA"),                # LiveChat — SaaS mit globaler Nische
    ("CDR", "CD Projekt SA"),          # IP-Monopol (Witcher/Cyberpunk)
    ("ACP", "Asseco Poland SA"),       # Behörden-/Banken-IT, tiefer Lock-in
    ("KRU", "Kruk SA"),                # Forderungsmanagement CEE-Marktführer
    ("PLW", "PlayWay SA"),             # Games-Publishing-Plattform
    ("XTB", "XTB SA"),                 # Retail-Broker CEE
]

# Bolsa de Madrid — Infrastruktur-Monopole + Hidden Champions
MADRID_SEEDS = [
    ("AMS", "Amadeus IT Group SA"),    # GDS-Oligopol, massiver Lock-in
    ("AENA", "Aena SME SA"),           # Flughafen-Monopol
    ("CLNX", "Cellnex Telecom SA"),    # Funktürme
    ("RED", "Redeia Corporacion SA"),  # Stromnetz-Monopol
    ("ENG", "Enagas SA"),              # Gasnetz-Monopol
    ("LOG", "Logista Holdings SA"),    # Distributions-Monopol Iberia
    ("VIS", "Viscofan SA"),            # Wursthüllen Weltmarktführer
    ("FDR", "Fluidra SA"),             # Pool-Equipment
    ("GRF", "Grifols SA"),             # Plasma-Oligopol
    ("ITX", "Inditex SA"),
    ("IDR", "Indra Sistemas SA"),
]

EXCHANGES = {
    "lse": {
        "suffix": ".L",
        "seeds": LSE_SEEDS,
        "min_market_cap_m": 50,
        "country": "GB",
    },
    "milan": {
        "suffix": ".MI",
        "seeds": MILAN_SEEDS,
        "min_market_cap_m": 50,
        "country": "IT",
    },
    "madrid": {
        "suffix": ".MC",
        "seeds": MADRID_SEEDS,
        "min_market_cap_m": 50,
        "country": "ES",
    },
    "gpw": {
        "suffix": ".WA",
        "seeds": WARSAW_SEEDS,
        "min_market_cap_m": 50,
        "country": "PL",
    },
    "xetra": {
        "suffix": ".DE",
        "seeds": XETRA_SEEDS,
        "min_market_cap_m": 50,
        "country": "DE",
    },
    "six": {
        "suffix": ".SW",
        "seeds": SIX_SEEDS,
        "min_market_cap_m": 50,
        "country": "CH",
    },
    "aex": {
        "suffix": ".AS",
        "seeds": AEX_SEEDS,
        "min_market_cap_m": 50,
        "country": "NL",
    },
    "vienna": {
        "suffix": ".VI",
        "seeds": VIENNA_SEEDS,
        "min_market_cap_m": 30,
        "country": "AT",
    },
    "omxs": {
        "suffix": ".ST",
        "seeds": [(t, n) for t, n in NORDIC_SEEDS if not any(
            t.endswith(x) for x in ["-A", "-B", "KET", "SEMI"])
            or t.endswith(("-A", "-B"))],
        "min_market_cap_m": 30,
        "country": "SE",
    },
    "omxh": {
        "suffix": ".HE",
        "seeds": [(t, n) for t, n in NORDIC_SEEDS if any(
            n_check in n for n_check in ["Oyj", "Finland"])],
        "min_market_cap_m": 30,
        "country": "FI",
    },
    "omxc": {
        "suffix": ".CO",
        "seeds": [(t, n) for t, n in NORDIC_SEEDS if any(
            n_check in n for n_check in ["A/S", "Denmark"])],
        "min_market_cap_m": 30,
        "country": "DK",
    },
    "ose": {
        "suffix": ".OL",
        "seeds": [(t, n) for t, n in NORDIC_SEEDS if any(
            n_check in n for n_check in ["ASA", "Norway", "AS"])],
        "min_market_cap_m": 20,
        "country": "NO",
    },
    "bru": {
        "suffix": ".BR",
        "seeds": BENELUX_EXTRA_SEEDS,
        "min_market_cap_m": 30,
        "country": "BE",
    },
    "paris": {
        "suffix": ".PA",
        "seeds": [
            # CAC 40 + SBF 120 highlights (high Thiel potential)
            ("AI", "Air Liquide SA"), ("AIR", "Airbus SE"), ("ALO", "Alstom SA"),
            ("ATO", "Atos SE"), ("AXA", "AXA SA"), ("BN", "Danone SA"),
            ("BNP", "BNP Paribas SA"), ("CA", "Carrefour SA"), ("CAP", "Capgemini SE"),
            ("CS", "AXA SA"), ("DSY", "Dassault Systemes SE"), ("ENGI", "Engie SA"),
            ("EDEN", "Edenred SE"), ("ERF", "Eurofins Scientific SE"),
            ("GLE", "Societe Generale SA"), ("HO", "Thales SA"), ("KER", "Kering SA"),
            ("LR", "Legrand SA"), ("MC", "LVMH Moet Hennessy SE"), ("ML", "Michelin SA"),
            ("OR", "L'Oreal SA"), ("ORA", "Orange SA"), ("PUB", "Publicis Groupe SA"),
            ("RMS", "Hermes International SCA"), ("SAF", "Safran SA"),
            ("SAN", "Sanofi SA"), ("SGO", "Compagnie de Saint-Gobain SA"),
            ("STLAM", "Stellantis NV"), ("SU", "Schneider Electric SE"),
            ("TTE", "TotalEnergies SE"), ("VIE", "Veolia Environnement SA"),
            ("VIV", "Vivendi SE"), ("WLN", "Worldline SA"),
            # SBF 120 additions
            ("ALFEN", "Alfen NV"), ("ALSTEF", "Alstef Group"),
            ("AMUN", "Amundi SA"), ("APAM", "Aperam SA"),
            ("BFCM", "Credit Mutuel SA"), ("BIOC", "Biocartis Group NV"),
            ("COFA", "Coface SA"), ("CRDI", "Credit Agricole SA"),
            ("DBV", "DBV Technologies SA"), ("DDOG", "Datadog (Paris)"),
            ("EDF", "Electricite de France SA"), ("ELIS", "Elis SA"),
            ("FDJ", "Francaise des Jeux SA"), ("FTI", "TechnipFMC PLC"),
            ("GTT", "GTT SA"), ("ITRK", "Intertek Group (Paris)"),
            ("KOF", "Korian SA"), ("LHN", "LafargeHolcim Ltd"),
            ("NEOEN", "Neoen SA"), ("NEXITY", "Nexity SA"),
            ("NOKIA", "Nokia Oyj (Paris)"), ("OPM", "OPmobility SE"),
            ("PARRO", "Parrot SA"), ("PERNOD", "Pernod Ricard SA"),
            ("POOL", "Poolia AB (Paris)"), ("REXEL", "Rexel SA"),
            ("RNO", "Renault SA"), ("SCOR", "SCOR SE"),
            ("SOI", "Soitec SA"), ("SPIE", "SPIE SA"),
            ("STM", "STMicroelectronics NV"), ("TKTT", "Tikehau Capital"),
            ("VLTSA", "Vallourec SA"), ("VK", "Voltalia SA"),
        ],
        "min_market_cap_m": 100,
        "country": "FR",
    },
}


# ─── Live Index Fetch (best-effort, falls back to seeds) ─────────────────────

def _fetch_xetra_live() -> list[tuple[str, str]]:
    """
    Fetch all XETRA-listed equities from Deutsche Börse's public instruments
    reference data. Falls back to index components if bulk download fails.
    Returns list of (base_ticker, name).
    """
    results = []
    headers = {"User-Agent": "ThielDetector info@pcctradinginc.com"}

    # Strategy 1: Deutsche Börse index components API (reliable, ~600 stocks)
    indices = ["DAX", "MDAX", "SDAX", "TECDAX", "SDAX"]
    index_url = "https://api.deutsche-boerse.com/prod/v1/indices/components"
    seen = set()
    for index in indices:
        try:
            resp = requests.get(index_url, params={"index": index},
                                headers=headers, timeout=15)
            if resp.status_code == 200:
                for item in resp.json().get("data", []):
                    sym = item.get("symbol", "")
                    name = item.get("name", "")
                    if sym and name and sym not in seen:
                        seen.add(sym)
                        results.append((sym, name))
                logger.info(f"Deutsche Börse {index}: {len(results)} total so far")
        except Exception as e:
            logger.debug(f"DB index API failed for {index}: {e}")

    # Strategy 2: Wikipedia index tables via HTML (more reliable than wikitext)
    import re
    wiki_pages = ["DAX", "MDAX", "SDAX", "TecDAX"]
    for wiki_page in wiki_pages:
        try:
            resp = requests.get(
                f"https://en.wikipedia.org/wiki/{wiki_page}",
                headers=headers, timeout=15
            )
            if resp.status_code != 200:
                continue
            from bs4 import BeautifulSoup
            soup = BeautifulSoup(resp.text, "lxml")
            # Wikipedia index tables: look for wikitable rows with ticker symbols
            for table in soup.select("table.wikitable"):
                headers_row = table.select("th")
                header_texts = [th.get_text(strip=True).lower() for th in headers_row]
                # Find ticker column index
                ticker_col = next((i for i, h in enumerate(header_texts)
                                   if any(k in h for k in ["ticker", "symbol", "kürzel", "isin"])), None)
                name_col = next((i for i, h in enumerate(header_texts)
                                 if any(k in h for k in ["company", "name", "unternehmen"])), 0)
                for row in table.select("tr")[1:]:
                    cells = row.select("td")
                    if not cells:
                        continue
                    name = cells[name_col].get_text(strip=True) if name_col < len(cells) else ""
                    # Extract ticker: prefer known column, else find 2-6 uppercase chars
                    sym = ""
                    if ticker_col is not None and ticker_col < len(cells):
                        sym = cells[ticker_col].get_text(strip=True)
                    if not sym or not re.match(r'^[A-Z0-9]{2,6}$', sym):
                        # Try to find ticker in any cell
                        for cell in cells:
                            t = cell.get_text(strip=True)
                            if re.match(r'^[A-Z]{2,6}[0-9]?$', t) and t not in seen:
                                sym = t
                                break
                    name = re.sub(r'\[.*?\]', '', name).strip()
                    # Filter out noise: currency codes, generic words, too short
                    noise = {"EUR", "USD", "GBP", "CHF", "TBD", "AG", "SE", "N/A", "NA"}
                    if (sym and name and len(name) > 3
                            and re.match(r'^[A-Z]{2,6}[0-9]?$', sym)
                            and sym not in seen
                            and sym not in noise):
                        seen.add(sym)
                        results.append((sym, name))
        except Exception as e:
            logger.debug(f"Wikipedia HTML fetch failed for {wiki_page}: {e}")

    logger.info(f"XETRA live fetch: {len(results)} unique tickers")
    return results


def _fetch_euronext_live(mics: list[str], suffix_map: dict) -> list[tuple[str, str, str]]:
    """
    Fetch equities from Euronext markets via their public data service.
    Returns list of (base_ticker, name, exchange_suffix).
    mics: list of MIC codes e.g. ["XAMS", "XPAR"]
    suffix_map: {mic: yfinance_suffix} e.g. {"XAMS": ".AS", "XPAR": ".PA"}
    """
    results = []
    headers = {"User-Agent": "ThielDetector info@pcctradinginc.com"}

    for mic in mics:
        suffix = suffix_map.get(mic, ".AS")
        try:
            # Euronext public product list (paginated JSON)
            url = f"https://live.euronext.com/en/pd_ajax/stocks?mics={mic}&start=0&length=2000"
            resp = requests.get(url, headers={
                **headers,
                "Accept": "application/json",
                "X-Requested-With": "XMLHttpRequest",
                "Referer": "https://live.euronext.com/en/products/equities/list",
            }, timeout=20)

            if resp.status_code == 200:
                data = resp.json()
                for item in data.get("data", []):
                    # Euronext returns HTML fragments — extract ticker from link
                    import re
                    ticker_match = re.search(r'>([A-Z0-9]{2,8})<', str(item))
                    name_match = re.search(r'<td[^>]*>([^<]{5,60})</td>', str(item))
                    if ticker_match:
                        ticker = ticker_match.group(1)
                        name = name_match.group(1).strip() if name_match else ticker
                        results.append((ticker, name, suffix))
                logger.info(f"Euronext {mic}: fetched {len([r for r in results if r[2]==suffix])} tickers")
            else:
                logger.debug(f"Euronext {mic} HTTP {resp.status_code}")
        except Exception as e:
            logger.debug(f"Euronext {mic} fetch failed: {e}")

    return results


def _fetch_dax_components() -> list[tuple[str, str]]:
    """Legacy wrapper — calls _fetch_xetra_live."""
    return _fetch_xetra_live()


def _fetch_stoxx_components() -> list[tuple[str, str]]:
    """Legacy stub — kept for compatibility."""
    return []


# ─── EU IPO Fetchers ─────────────────────────────────────────────────────────

def fetch_eu_ipos(months_back: int = 18) -> list[dict]:
    """
    Fetch recent EU IPOs from Deutsche Börse and Euronext new listings.
    Returns list of company dicts ready for the screening pipeline.

    Sources:
      - Deutsche Börse: neue Zulassungen (XETRA)
      - Euronext: new listings (AEX + Paris)
      - BaFin prospectus database (for German IPOs with text)

    months_back: how far back to look for new listings
    """
    from datetime import timedelta
    cutoff = datetime.now(timezone.utc) - timedelta(days=months_back * 30)
    headers = {"User-Agent": "ThielDetector info@pcctradinginc.com"}
    ipos = []
    seen = set()

    # ── Source 1: Deutsche Börse new listings RSS/API ─────────────────────────
    try:
        # Deutsche Börse publishes new XETRA listings via their public API
        resp = requests.get(
            "https://api.deutsche-boerse.com/prod/v1/instrument/newlistings",
            params={"limit": 200},
            headers=headers,
            timeout=15,
        )
        if resp.status_code == 200:
            for item in resp.json().get("data", []):
                sym = item.get("symbol", "")
                name = item.get("name", "")
                listing_date = item.get("listingDate", "")
                if sym and sym not in seen:
                    seen.add(sym)
                    ipos.append({
                        "ticker": f"{sym}.DE",
                        "base_ticker": sym,
                        "name": name,
                        "exchange": "xetra",
                        "exchange_suffix": ".DE",
                        "cohort_id": "eu_ipo",
                        "country": "DE",
                        "source": "ipo",
                        "ipo_date": listing_date,
                        "has_prospectus": False,
                    })
            logger.info(f"Deutsche Börse new listings: {len(ipos)} IPOs")
    except Exception as e:
        logger.debug(f"Deutsche Börse new listings failed: {e}")

    # ── Source 2: Euronext new listings ───────────────────────────────────────
    for mic, suffix, country in [("XAMS", ".AS", "NL"), ("XPAR", ".PA", "FR")]:
        try:
            resp = requests.get(
                "https://live.euronext.com/en/pd_ajax/ipos",
                params={"mics": mic, "start": 0, "length": 200},
                headers={**headers, "X-Requested-With": "XMLHttpRequest"},
                timeout=15,
            )
            if resp.status_code == 200:
                import re
                for item in resp.json().get("data", []):
                    item_str = str(item)
                    ticker_m = re.search(r'>([A-Z0-9]{2,8})<', item_str)
                    name_m = re.search(r'<td[^>]*>([^<]{5,60})</td>', item_str)
                    if ticker_m:
                        sym = ticker_m.group(1)
                        full = f"{sym}{suffix}"
                        if full not in seen:
                            seen.add(full)
                            ipos.append({
                                "ticker": full,
                                "base_ticker": sym,
                                "name": name_m.group(1).strip() if name_m else sym,
                                "exchange": mic.lower(),
                                "exchange_suffix": suffix,
                                "cohort_id": "eu_ipo",
                                "country": country,
                                "source": "ipo",
                                "has_prospectus": False,
                            })
        except Exception as e:
            logger.debug(f"Euronext {mic} IPO fetch failed: {e}")

    # ── Source 3: BaFin Prospektdatenbank (German IPO prospectuses) ───────────
    try:
        resp = requests.get(
            "https://www.bafin.de/SiteGlobals/Functions/Prospekte/DE/prospektsuche.html",
            params={
                "gtp": "134578_list%3D1",
                "documentType": "PROSP",
                "language": "DE",
                "sorting": "dateOfApproval_dt+desc",
                "resultsPerPage": "100",
            },
            headers=headers,
            timeout=20,
        )
        if resp.status_code == 200:
            from bs4 import BeautifulSoup
            soup = BeautifulSoup(resp.text, "lxml")
            for row in soup.select("table tr")[1:51]:  # top 50 entries
                cells = row.select("td")
                if len(cells) >= 3:
                    name = cells[0].get_text(strip=True)
                    # BaFin doesn't always have ticker — flag for manual lookup
                    if name and len(name) > 3:
                        ipos.append({
                            "ticker": f"BAFIN_{name[:10].replace(' ','_').upper()}.DE",
                            "base_ticker": name[:10].replace(' ', '_').upper(),
                            "name": name,
                            "exchange": "xetra",
                            "exchange_suffix": ".DE",
                            "cohort_id": "eu_ipo_bafin",
                            "country": "DE",
                            "source": "bafin_prospectus",
                            "has_prospectus": True,
                        })
            logger.info(f"BaFin prospectus database: added {len([i for i in ipos if i.get('source')=='bafin_prospectus'])} entries")
    except Exception as e:
        logger.debug(f"BaFin prospectus fetch failed: {e}")

    logger.info(f"EU IPOs total: {len(ipos)} across all sources")
    return ipos


# ─── Main Builder ────────────────────────────────────────────────────────────

def build_eu_universe(config: dict, conn) -> list[dict]:
    """
    Build EU screening universe. Returns list of company dicts.

    Each company dict includes:
      ticker (with exchange suffix), name, exchange, cohort_id, country
    """
    eu_config = config.get("eu_universe", {})
    if not eu_config.get("enabled", False):
        logger.info("EU universe disabled in config — skipping")
        return []

    enabled_exchanges = eu_config.get("exchanges", list(EXCHANGES.keys()))
    companies = []
    seen_tickers = set()

    for exchange_id in enabled_exchanges:
        exchange = EXCHANGES.get(exchange_id)
        if not exchange:
            logger.warning(f"Unknown exchange: {exchange_id}")
            continue

        suffix = exchange["suffix"]
        min_cap = exchange.get("min_market_cap_m", 50)
        country = exchange["country"]
        seeds = exchange["seeds"]

        # Try to enrich with live data — fall back to seeds
        live_tickers = []
        if exchange_id == "xetra":
            live_tickers = _fetch_xetra_live()
        elif exchange_id == "aex":
            euronext_results = _fetch_euronext_live(["XAMS"], {"XAMS": ".AS"})
            live_tickers = [(t, n) for t, n, _ in euronext_results]
        elif exchange_id == "paris":
            euronext_results = _fetch_euronext_live(["XPAR"], {"XPAR": ".PA"})
            live_tickers = [(t, n) for t, n, _ in euronext_results]

        all_seeds = seeds
        if live_tickers:
            existing_base = {t for t, _ in seeds}
            new_tickers = [(t, n) for t, n in live_tickers if t not in existing_base]
            all_seeds = seeds + new_tickers
            logger.info(f"{exchange_id}: {len(new_tickers)} live tickers added to {len(seeds)} seeds → {len(all_seeds)} total")

        for base_ticker, name in all_seeds:
            full_ticker = f"{base_ticker}{suffix}"
            if full_ticker in seen_tickers:
                continue
            seen_tickers.add(full_ticker)

            companies.append({
                "ticker": full_ticker,
                "base_ticker": base_ticker,
                "name": name,
                "exchange": exchange_id,
                "exchange_suffix": suffix,
                "cohort_id": f"eu_{exchange_id}",
                "country": country,
                "min_market_cap_m": min_cap,
                "source": "seed",
            })

    logger.info(f"EU universe (seeds+live): {len(companies)} candidates across {len(enabled_exchanges)} exchanges")

    # ── EODHD: bulk EU stock list (~3.200 Aktien, 10 API calls) ─────────────
    import os
    eodhd_key = os.environ.get("EODHD_API_KEY", "")
    if eodhd_key:
        from universe.eodhd_universe import fetch_eodhd_eu_universe
        eodhd_companies = fetch_eodhd_eu_universe(eodhd_key)
        eodhd_added = 0
        for company in eodhd_companies:
            if company["ticker"] not in seen_tickers:
                seen_tickers.add(company["ticker"])
                companies.append(company)
                eodhd_added += 1
        logger.info(f"EODHD added {eodhd_added} new EU tickers (total now: {len(companies)})")
    else:
        logger.info("EODHD_API_KEY not set — using seeds only for EU universe")

    # ── EU IPOs: upcoming + recent via all sources ────────────────────────────
    ipo_months = eu_config.get("ipo_months_back", 18)
    from universe.ipo_monitor import fetch_all_upcoming_ipos
    ipo_candidates = fetch_all_upcoming_ipos(months_back=ipo_months)
    ipo_added = 0
    for ipo in ipo_candidates:
        full_ticker = ipo["ticker"]
        # Skip BaFin entries without real ticker (can't yfinance-lookup them)
        if ipo.get("source") == "bafin_prospectus":
            continue
        if full_ticker not in seen_tickers:
            seen_tickers.add(full_ticker)
            companies.append(ipo)
            ipo_added += 1
    if ipo_added:
        logger.info(f"EU IPOs added: {ipo_added} recent listings (last {ipo_months} months)")

    # Upsert into DB
    _upsert_eu_companies(conn, companies)

    return companies


def _upsert_eu_companies(conn, companies: list[dict]):
    """Write EU companies to DB if not already present."""
    now = datetime.now(timezone.utc).isoformat()

    # Seed EU cohorts into universe_cohorts to satisfy FK constraint
    eu_cohort_ids = {c.get("cohort_id") for c in companies if c.get("cohort_id")}
    for cohort_id in eu_cohort_ids:
        conn.execute("""
            INSERT OR IGNORE INTO universe_cohorts
            (cohort_id, name, added_at, alerting_enabled)
            VALUES (?, ?, ?, 1)
        """, (cohort_id, cohort_id.replace("eu_", "EU ").title(), now))
    conn.commit()

    for company in companies:
        existing = conn.execute(
            "SELECT ticker FROM companies WHERE ticker = ?",
            (company["ticker"],)
        ).fetchone()
        if not existing:
            conn.execute("""
                INSERT INTO companies
                (ticker, name, cohort_id, first_seen_in_universe, is_active)
                VALUES (?, ?, ?, ?, 1)
            """, (
                company["ticker"],
                company.get("name", ""),
                company.get("cohort_id", ""),
                now,
            ))
    conn.commit()
