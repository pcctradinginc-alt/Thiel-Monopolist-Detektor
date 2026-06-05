# Datenbankschema

## Tabellen

### `universe_cohorts`
Kontrolliert die schrittweise Erweiterung des Universums.
Neue Kohorten machen erst nach `min_baseline_runs` Durchläufen Alerts scharf.

| Spalte | Typ | Beschreibung |
|--------|-----|-------------|
| cohort_id | TEXT PK | Eindeutige ID (z.B. "tech_software") |
| name | TEXT | Lesbarer Name |
| sector_filter | TEXT | JSON-Array der SIC-Codes |
| added_at | TEXT | ISO-Datum der Hinzufügung |
| alerting_enabled | INTEGER | 0=Baseline, 1=Alerts aktiv |
| baseline_runs_completed | INTEGER | Zähler absolvierter Baseline-Runs |

### `companies`
Alle Unternehmen im Universum.

| Spalte | Typ | Beschreibung |
|--------|-----|-------------|
| ticker | TEXT PK | Börsenkürzel |
| cohort_id | TEXT FK | Zugehörige Kohorte |
| first_seen_in_universe | TEXT | Wann erstmals erfasst |
| is_active | INTEGER | 1=aktiv, 0=delistet |

### `evaluations`
Jedes LLM-Screening-Ergebnis pro Unternehmen und Run.

| Spalte | Typ | Beschreibung |
|--------|-----|-------------|
| id | INTEGER PK | Auto-increment |
| ticker | TEXT FK | Unternehmen |
| run_id | TEXT | UUID des Runs |
| lanes_triggered | TEXT | JSON-Array der aktiven Lanes |
| monopoly_score | INTEGER | 0-100 |
| confidence_score | INTEGER | 0-100 |
| data_quality_score | INTEGER | 0-100 |
| market_hypotheses | TEXT | JSON aus Step 1 |
| llm_assessment | TEXT | Vollständiges JSON aus Step 3 |
| alert_type | TEXT | z.B. HIDDEN_WEDGE_DETECTED |
| status | TEXT | STRONG/PARTIAL/WEAK/NONE/BASELINE |

### `company_status`
Aktueller Status je Unternehmen (letzte Bewertung).

| Spalte | Typ | Beschreibung |
|--------|-----|-------------|
| ticker | TEXT PK | Unternehmen |
| consecutive_high_score_runs | INTEGER | Wie viele Runs in Folge Score ≥ 65 |
| last_alert_date | TEXT | Wann zuletzt alertet (für Hysterese) |

### `human_feedback`
Menschliches Feedback via GitHub Issue Labels.

| Spalte | Typ | Beschreibung |
|--------|-----|-------------|
| verdict | TEXT | CONFIRMED/REJECTED/WATCHLIST/TOO_EARLY |
| strongest_wrong_assumption | TEXT | Was war die stärkste Fehleinschätzung? |
| weakest_missing_evidence | TEXT | Was fehlte am meisten? |
| github_issue_number | INTEGER | Referenz zum GitHub Issue |

### `calibration_events`
Protokoll von Prompt-Anpassungen basierend auf Feedback.

### `run_state`
Resumability: wo ein unterbrochener Job weitermachen kann.

## Alert-Schwellen

Alert wird ausgelöst wenn **alle drei** Bedingungen erfüllt sind:
- `monopoly_score` ≥ 65
- `confidence_score` ≥ 60  
- `data_quality_score` ≥ 55

Plus Hysterese:
- Kein erneuter Alert innerhalb von 21 Tagen
- Außer bei `MOAT_RISK_DETECTED` oder Score-Delta ≥ 8

## Alert-Typen

| Typ | Bedeutung |
|-----|-----------|
| `HIDDEN_WEDGE_DETECTED` | Enge Marktdominanz unter breiten Marktbehauptungen |
| `SUBSTITUTE_GAP_DETECTED` | Kein echtes Substitut — Kunden wahrscheinlich eingesperrt |
| `LOCK_IN_STRENGTHENING` | Zunehmendes Kunden-Lock-in |
| `SCALE_INFLECTION` | Kostenbasis skaliert besser als Umsatz |
| `CUSTOMER_EXPANSION_SIGNAL` | Bestandskunden wachsen ohne proportionale S&M-Ausgaben |
| `IPO_WITH_NARROW_DOMINANCE` | Junges Unternehmen mit fokussierter Marktposition |
| `MOAT_EVIDENCE_IMPROVED` | Moat-Evidenz stärker als vorherige Bewertung |
| `MOAT_RISK_DETECTED` | Bisher starker Moat zeigt Schwächezeichen |
