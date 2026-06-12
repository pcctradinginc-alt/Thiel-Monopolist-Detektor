# Thiel Monopolist Detector

Ein automatisiertes Screening-System, das börsennotierte US-Unternehmen auf Anzeichen eines Thiel-artigen Monopols untersucht und per E-Mail alertet.

## Was dieses System ist — und was nicht

**Was es ist:** Ein Aufmerksamkeits-Filter. Das System sucht systematisch nach Anomalien, die auf einen frühen, noch verborgenen Moat hindeuten — und liefert 3–5 Kandidaten pro Monat, die eine tiefere menschliche Analyse rechtfertigen.

**Was es nicht ist:** Ein autonomer Entscheider. Die finale Einschätzung "Das ist ein Thiel-Monopolist" bleibt beim Menschen.

## Thiel's vier Kriterien

1. **Proprietary Technology** — mindestens 10x besser als das nächste Substitut in einer wichtigen Dimension
2. **Network Effects** — das Produkt wird wertvoller, je mehr Menschen es nutzen
3. **Economies of Scale** — das Unternehmen wird stärker, je größer es wird (Fixkosten hoch, Grenzkosten ≈ 0)
4. **Branding** — echte, dauerhafte Markenidentität (allein nicht ausreichend)

## Architektur

```
Universe (Lanes) 
  → Evidence Collection (edgartools + yfinance + SEC EDGAR)
    → market_hypothesis_generator (LLM Schritt 1)
      → Widerspruchs-Detektion (Risk Factors vs. Business Description vs. Zahlen)
        → Substitute-Analyse (LLM Schritt 2)
          → Scoring mit Evidence + Counter-Evidence (LLM Schritt 3)
            → SQLite / Turso
              → GitHub Issue Alert
                → Human Feedback → Recalibration
```

## Candidate Lanes (kein harter Ausschluss, nur Priorisierung)

| Lane | Kriterium |
|------|-----------|
| `hidden_wedge` | Enger Use Case, wenige Substitute, starke Kundenbindung |
| `emerging_platform` | Plattform/Ökosystem-Sprache + Lock-in-Signale |
| `scale_inflection` | Kostenbasis skaliert besser als Umsatz (S&M fällt, Margin steigt) |
| `ipo_narrow` | Junges Unternehmen, enge Marktdefinition, S-1 Analyse |
| `filing_change` | Neue Keywords in 10-K: platform, ecosystem, retention, standard |
| `manual_watchlist` | Manuell gepinnte Unternehmen |

## Scores

- `monopoly_score` (0–100): Stärke der Moat-Evidenz
- `confidence_score` (0–100): Datensicherheit des Systems
- `data_quality_score` (0–100): Vollständigkeit der Datenlage

Alert-Schwelle: alle drei Scores > 65 + Bestätigung in ≥ 2 aufeinanderfolgenden Runs

## Wöchentlicher Kaufkandidaten-Report

Unabhängig von Alert-Schwellen nennt das System nach jedem Batch-Lauf die
**Top 15 Kandidaten** (US + EU) per GitHub Issue (Label `weekly-report`) und
E-Mail — mit Scores, Status, Kurzthese und engem Markt. Damit liefert jeder
Wochenlauf konkrete Namen für die menschliche Tiefenanalyse, auch wenn kein
Kandidat die harten Alert-Schwellen reißt.

## Persistenz

Die SQLite-DB wird nach jedem Lauf als GitHub-Release-Asset `db-latest`
gesichert und beim nächsten Lauf wiederhergestellt. Nur so funktionieren
Rotation (2/8-Wochen-Tiers), die ≥2-Runs-Bestätigung und die Alert-Hysterese
über Wochen hinweg. EU-Kandidaten haben eine feste Quote von 25 % des
LLM-Call-Budgets pro Lauf.

## Alert-Typen

```
HIDDEN_WEDGE_DETECTED
SUBSTITUTE_GAP_DETECTED
LOCK_IN_STRENGTHENING
SCALE_INFLECTION
CUSTOMER_EXPANSION_SIGNAL
IPO_WITH_NARROW_DOMINANCE
MOAT_EVIDENCE_IMPROVED
MOAT_RISK_DETECTED
```

## Setup

### 1. Repository klonen

```bash
git clone https://github.com/pcctradinginc-alt/Thiel-Monopolist-Detektor.git
cd Thiel-Monopolist-Detektor
pip install -r requirements.txt
```

### 2. Secrets in GitHub setzen

| Secret | Beschreibung |
|--------|-------------|
| `ANTHROPIC_API_KEY` | Claude API Key (Haiku für Screening, Sonnet für finale Kandidaten) |
| `EMAIL_SENDER` | Gmail-Adresse für Alerts |
| `EMAIL_PASSWORD` | Gmail App-Passwort |
| `EMAIL_RECIPIENT` | Empfänger-Adresse |
| `SEC_CONTACT_EMAIL` | Echte Kontakt-E-Mail für den SEC-EDGAR-User-Agent (von der SEC für automatisierte Abrufe verlangt — Platzhalter riskieren IP-Sperren) |
| `TURSO_URL` | (Optional) Turso DB URL für Produktion |
| `TURSO_TOKEN` | (Optional) Turso Auth Token |

### 3. Konfiguration anpassen

```bash
cp config/config.example.yaml config/config.yaml
# Anpassen nach Bedarf
```

### 4. Erster Run (lokal)

```bash
python src/main.py --mode full --dry-run
```

## GitHub Actions

Das System läuft automatisch jeden **Montag um 06:00 UTC**.

- `screening.yml` — Wöchentliches Screening
- `feedback.yml` — Verarbeitet GitHub Issue Labels als Human Feedback

## Human Feedback

Alerts werden als GitHub Issues erstellt. Labels:
- `confirmed` — Echter Moat bestätigt
- `rejected` — False Positive
- `watchlist` — Weiter beobachten
- `too-early` — Zu früh, nochmal in 6 Monaten

## Datenbankschema

Siehe `docs/schema.md`

## Kosten-Schätzung

| Komponente | Kosten/Woche |
|------------|-------------|
| Claude Haiku (Screening ~100 Calls) | ~$0.20 |
| Claude Sonnet (Finale ~10 Calls) | ~$0.50 |
| Turso Free Tier | $0 |
| GitHub Actions | $0 |
| **Gesamt** | **~$0.70/Woche** |

## Lizenz

MIT
