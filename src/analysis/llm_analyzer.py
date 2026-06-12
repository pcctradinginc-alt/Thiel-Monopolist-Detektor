"""
LLM analysis pipeline — three-step Thiel evaluation.

Step 1: market_hypothesis_generator
  → Reconstruct the narrow market the company might dominate
  → Never evaluate directly — hypothesize first

Step 2: substitute_gap_analyzer
  → Force explicit substitute analysis
  → What is the realistic alternative? How hard is switching?

Step 3: thiel_auditor
  → Evidence + Counter-Evidence for each of Thiel's 4 criteria
  → Three scores: monopoly, confidence, data_quality
  → Typed alert if threshold met

All outputs are strict JSON — no narrative prose that could mask hallucination.
"""

import json
import logging
import os
import time
from typing import Optional
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception

logger = logging.getLogger(__name__)

try:
    import anthropic
    ANTHROPIC_AVAILABLE = True
except ImportError:
    ANTHROPIC_AVAILABLE = False
    logger.error("anthropic package not installed")


# ─── Prompts ────────────────────────────────────────────────────────────────
#
# Each prompt is split into two parts:
#   _STATIC  — instructions, criteria, output schema (same for every company → cached)
#   _DYNAMIC — company-specific data (changes per call → not cached)
#
# Anthropic charges cache_write once (first call) and ~10% of normal input price
# for cache_read on all subsequent calls within the 5-minute TTL window.
# With ~500 companies per run the static parts are read 499× from cache.

# ── Step 1 ──────────────────────────────────────────────────────────────────

_HYPOTHESIS_STATIC = """\
You are analyzing a company's SEC filing to identify if it might be a hidden monopolist \
in the sense Peter Thiel describes in Zero to One.

Thiel's key insight: Real monopolists disguise themselves by claiming to operate in large, \
competitive markets. They actually dominate a narrow, specific problem space where substitutes are poor.

Your task (Step 1 of 3): Generate narrow market hypotheses ONLY. Do not score or evaluate yet.

Return ONLY valid JSON, no other text:

{
  "company_claimed_market": "The broad market the company claims to operate in",
  "narrow_market_hypotheses": [
    {
      "narrow_market": "The specific, narrow problem space where this company might actually dominate",
      "why_narrow": "Why this is a genuinely narrow definition, not the company's broad claim",
      "customer_pain_point": "The specific problem this solves better than alternatives",
      "wedge_type": "One of: workflow, dataset, standard, api_integration, compliance, marketplace, vertical_software, infrastructure",
      "possible_substitutes": ["list", "of", "realistic", "alternatives"],
      "expansion_path": "How this narrow position could expand to a larger market",
      "confidence": "high/medium/low"
    }
  ],
  "red_flags_against_hypothesis": ["reasons why this might NOT be a real narrow market"],
  "data_gaps": ["what information would confirm or deny these hypotheses"]
}"""

_HYPOTHESIS_DYNAMIC = """\

--- COMPANY DATA ---
Company: {ticker}
Business Description: {business_description}
Risk Factors (excerpt): {risk_factors}
MD&A (excerpt): {mda}
Financial Signals: {financial_signals}"""


# ── Step 2 ──────────────────────────────────────────────────────────────────

_SUBSTITUTE_STATIC = """\
You are performing Step 2 of a Thiel monopoly analysis.

Peter Thiel's test: A true monopolist has NO close substitute. Customers stay not because \
of loyalty but because switching is genuinely costly or impossible.

Analyze substitutes rigorously. Be SKEPTICAL — most companies have more competition than they admit.

Return ONLY valid JSON:

{
  "substitute_analysis": {
    "primary_substitute": "The single most realistic alternative for customers",
    "substitute_type": "One of: direct_competitor, internal_tool, manual_process, legacy_system, open_source, build_own",
    "switching_cost_estimate": "high/medium/low",
    "switching_barriers": ["specific barriers to switching: technical, contractual, organizational, data"],
    "switching_ease_factors": ["specific factors that make switching EASIER — be honest"],
    "customer_lock_in_evidence": ["concrete evidence from filings of actual lock-in"],
    "customer_lock_in_counterevidence": ["concrete evidence that lock-in might be weaker than claimed"],
    "substitute_gap_verdict": "strong/moderate/weak/unclear",
    "substitute_gap_reasoning": "2-3 sentence explanation"
  },
  "pricing_power_signals": {
    "revenue_per_customer_trend": "rising/stable/falling/unknown",
    "sm_efficiency_trend": "improving/stable/declining/unknown",
    "gross_margin_trend": "rising/stable/falling/unknown",
    "pricing_power_verdict": "strong/moderate/weak/unclear"
  }
}"""

_SUBSTITUTE_DYNAMIC = """\

--- COMPANY DATA ---
Company: {ticker}
Narrow Market Hypotheses: {hypotheses}
Business Description: {business_description}
Financial Signals: {financial_signals}"""


# ── Step 3 ──────────────────────────────────────────────────────────────────

_AUDIT_STATIC = """\
You are performing Step 3 (final audit) of a Thiel monopoly analysis.

Peter Thiel's 4 criteria (from Zero to One):
1. PROPRIETARY TECHNOLOGY: Must be at least 10x better than closest substitute in some important dimension
2. NETWORK EFFECTS: Product becomes more valuable as more people use it (very hard to start, but powerful)
3. ECONOMIES OF SCALE: Business gets stronger as it gets bigger (high fixed costs, near-zero marginal costs)
4. BRANDING: Real phenomenon, but no technology company can be built on branding alone

CRITICAL INSTRUCTION: You MUST provide both evidence AND counter-evidence for each criterion.
Do NOT construct a narrative. Find the strongest case FOR and the strongest case AGAINST.
A good counter-evidence section is as important as evidence.

SCORE CALIBRATION — use the full 0-100 range, do NOT cluster in the 55-68 middle:
- 0-25: commodity business, no moat | 26-45: typical public company (MOST belong here)
- 46-60: partial moat, substitutes exist | 61-75: dominant narrow market, rare
- 76-100: textbook Thiel monopoly — exceptional
If the evidence is generic ("leading provider"), score LOW, not middle.

Return ONLY valid JSON, no other text:

{
  "ticker": "<ticker>",
  "evaluation_summary": "2-3 sentence honest summary of what this company is and why it might or might not be a Thiel monopolist",
  "criteria": {
    "proprietary_technology": {
      "evidence": ["specific quotes or data points supporting 10x advantage"],
      "counter_evidence": ["specific reasons why technology advantage might be weaker than claimed"],
      "score": 0,
      "score_reasoning": "why this score (0-100)"
    },
    "network_effects": {
      "evidence": ["specific evidence of network effects"],
      "counter_evidence": ["reasons network effects might be weak or absent"],
      "score": 0,
      "score_reasoning": "why this score (0-100)"
    },
    "economies_of_scale": {
      "evidence": ["evidence of scale advantages: margin trends, cost structure"],
      "counter_evidence": ["reasons scale advantages might be limited"],
      "score": 0,
      "score_reasoning": "why this score (0-100)"
    },
    "branding": {
      "evidence": ["evidence of genuine brand moat"],
      "counter_evidence": ["reasons branding might not be a durable moat here"],
      "score": 0,
      "score_reasoning": "why this score (0-100)"
    }
  },
  "contradiction_analysis": {
    "risk_factors_claim": "What risk factors say about competition",
    "business_desc_reality": "What business description reveals about actual positioning",
    "financial_signal_verdict": "What the numbers suggest",
    "contradiction_strength": "strong/moderate/weak/none"
  },
  "scores": {
    "monopoly_score": 0,
    "monopoly_score_reasoning": "why this score (0-100): weighted average of criteria + substitute gap",
    "confidence_score": 0,
    "confidence_score_reasoning": "how certain the system is given available data (0-100)",
    "data_quality_score": 0,
    "data_quality_score_reasoning": "completeness of available data (0-100)"
  },
  "alert_type": null,
  "alert_reasoning": "why this alert type was chosen, or why no alert",
  "next_verification_steps": ["what a human analyst should check next to confirm or deny this thesis"],
  "status": "STRONG/PARTIAL/WEAK/NONE"
}

For alert_type, use ONE of these if monopoly_score > 65, otherwise null:
HIDDEN_WEDGE_DETECTED, SUBSTITUTE_GAP_DETECTED, LOCK_IN_STRENGTHENING,
SCALE_INFLECTION, CUSTOMER_EXPANSION_SIGNAL, IPO_WITH_NARROW_DOMINANCE,
MOAT_EVIDENCE_IMPROVED, MOAT_RISK_DETECTED"""

_AUDIT_DYNAMIC = """\

--- COMPANY DATA ---
Company: {ticker}
Filing date: {filing_date}
Market Hypotheses: {hypotheses}
Substitute Analysis: {substitute_analysis}
Business Description: {business_description}
Risk Factors: {risk_factors}
Financial Signals: {financial_signals}
Lock-in Keywords Found: {lock_in_keywords}
Contradiction Signal Detected: {has_contradiction}"""


# ─── Rate Limiter ────────────────────────────────────────────────────────────

class RateLimiter:
    """Simple token bucket rate limiter for API calls."""

    def __init__(self, max_rpm: int = 40, max_tpm: int = 400000):
        self.max_rpm = max_rpm
        self.max_tpm = max_tpm
        self.calls_this_minute = 0
        self.tokens_this_minute = 0
        self.minute_start = time.time()

    def wait_if_needed(self, estimated_tokens: int = 8000):
        now = time.time()
        if now - self.minute_start > 60:
            self.calls_this_minute = 0
            self.tokens_this_minute = 0
            self.minute_start = now

        if (self.calls_this_minute >= self.max_rpm or
                self.tokens_this_minute + estimated_tokens >= self.max_tpm):
            sleep_time = 60 - (now - self.minute_start) + 1
            logger.info(f"Rate limit: sleeping {sleep_time:.0f}s")
            time.sleep(max(sleep_time, 1))
            self.calls_this_minute = 0
            self.tokens_this_minute = 0
            self.minute_start = time.time()

        self.calls_this_minute += 1
        self.tokens_this_minute += estimated_tokens


_rate_limiter = RateLimiter()


# ─── LLM Calls ───────────────────────────────────────────────────────────────

def _is_transient_error(exc: BaseException) -> bool:
    """Nur transiente API-Fehler retrien — Auth-/Konfig-Fehler sofort durchreichen."""
    if isinstance(exc, json.JSONDecodeError):
        return True
    if ANTHROPIC_AVAILABLE:
        transient = (
            anthropic.APIConnectionError,
            anthropic.RateLimitError,
            anthropic.InternalServerError,
            anthropic.APITimeoutError,
        )
        if isinstance(exc, transient):
            return True
    return False


@retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=2, min=4, max=60),
    retry=retry_if_exception(_is_transient_error),
    reraise=True,
)
def _call_llm(client, model: str, static_part: str, dynamic_part: str,
              max_tokens: int = 2000) -> dict:
    """
    Single LLM call with retry, rate limiting, and prompt caching.
    static_part is marked ephemeral so Anthropic caches it across the run.
    dynamic_part contains company-specific data and is never cached.
    """
    _rate_limiter.wait_if_needed(estimated_tokens=(len(static_part) + len(dynamic_part)) // 4)

    response = client.messages.create(
        model=model,
        max_tokens=max_tokens,
        messages=[{
            "role": "user",
            "content": [
                {"type": "text", "text": static_part,
                 "cache_control": {"type": "ephemeral"}},
                {"type": "text", "text": dynamic_part},
            ],
        }]
    )

    raw = response.content[0].text.strip()
    if raw.startswith("```"):
        raw = raw.split("```")[1]
        if raw.startswith("json"):
            raw = raw[4:]
        raw = raw.strip()

    parsed = json.loads(raw)
    return {
        "result": parsed,
        "input_tokens": response.usage.input_tokens,
        "output_tokens": response.usage.output_tokens,
        "cache_read_tokens": getattr(response.usage, "cache_read_input_tokens", 0) or 0,
        "cache_created_tokens": getattr(response.usage, "cache_creation_input_tokens", 0) or 0,
    }


def analyze_company(
    ticker: str,
    filing_data: dict,
    config: dict,
    run_state_tracker=None
) -> dict:
    """
    Full three-step Thiel analysis for one company.
    Returns structured assessment dict.
    """
    if not ANTHROPIC_AVAILABLE:
        return {"error": "anthropic not available", "ticker": ticker}

    if not os.environ.get("ANTHROPIC_API_KEY"):
        logger.error("ANTHROPIC_API_KEY ist nicht gesetzt — LLM-Analyse nicht möglich")
        return {"error": "ANTHROPIC_API_KEY not set", "ticker": ticker}

    client = anthropic.Anthropic()
    model_screening = config.get("screening", {}).get("model_screening", "claude-haiku-4-5-20251001")
    model_final = config.get("screening", {}).get("model_final", "claude-sonnet-4-6")

    total_input_tokens = 0
    total_output_tokens = 0

    signals_str = json.dumps(filing_data.get("financial_signals", {}), indent=2)
    biz_desc = filing_data.get("business_description", "")[:3000]
    risk_factors = filing_data.get("risk_factors", "")[:1500]
    mda = filing_data.get("mda", "")[:1500]

    # ── Step 1: Market Hypothesis Generation ──
    logger.info(f"{ticker}: Step 1 — generating market hypotheses")
    try:
        h_result = _call_llm(
            client, model_screening,
            static_part=_HYPOTHESIS_STATIC,
            dynamic_part=_HYPOTHESIS_DYNAMIC.format(
                ticker=ticker, business_description=biz_desc,
                risk_factors=risk_factors, mda=mda, financial_signals=signals_str,
            ),
            max_tokens=1500,
        )
        hypotheses = h_result["result"]
        total_input_tokens += h_result["input_tokens"]
        total_output_tokens += h_result["output_tokens"]
        logger.debug(f"{ticker}: Step 1 cache_read={h_result['cache_read_tokens']}")
    except Exception as e:
        logger.error(f"{ticker}: Hypothesis step failed: {e}")
        return {"error": str(e), "ticker": ticker, "step_failed": "hypothesis"}

    # ── Step 2: Substitute Analysis ──
    logger.info(f"{ticker}: Step 2 — substitute analysis")
    try:
        s_result = _call_llm(
            client, model_screening,
            static_part=_SUBSTITUTE_STATIC,
            dynamic_part=_SUBSTITUTE_DYNAMIC.format(
                ticker=ticker,
                hypotheses=json.dumps(hypotheses, indent=2)[:2000],
                business_description=biz_desc,
                financial_signals=signals_str,
            ),
            max_tokens=1200,
        )
        substitute_analysis = s_result["result"]
        total_input_tokens += s_result["input_tokens"]
        total_output_tokens += s_result["output_tokens"]
        logger.debug(f"{ticker}: Step 2 cache_read={s_result['cache_read_tokens']}")
    except Exception as e:
        logger.error(f"{ticker}: Substitute step failed: {e}")
        substitute_analysis = {}

    # ── Step 3: Final Audit (stronger model only for strong/moderate substitute gap) ──
    logger.info(f"{ticker}: Step 3 — final audit")
    try:
        sub_verdict = substitute_analysis.get("substitute_analysis", {}).get("substitute_gap_verdict", "")
        model = model_final if sub_verdict in ("strong", "moderate") else model_screening

        a_result = _call_llm(
            client, model,
            static_part=_AUDIT_STATIC,
            dynamic_part=_AUDIT_DYNAMIC.format(
                ticker=ticker,
                filing_date=filing_data.get("filing_date", "unknown"),
                hypotheses=json.dumps(hypotheses.get("narrow_market_hypotheses", [])[:2], indent=2)[:1500],
                substitute_analysis=json.dumps(substitute_analysis, indent=2)[:1500],
                business_description=biz_desc,
                risk_factors=risk_factors,
                financial_signals=signals_str,
                lock_in_keywords=str(filing_data.get("lock_in_keyword_hits", [])[:15]),
                has_contradiction=filing_data.get("has_contradiction_signal", False),
            ),
            max_tokens=2500,
        )
        assessment = a_result["result"]
        total_input_tokens += a_result["input_tokens"]
        total_output_tokens += a_result["output_tokens"]
        logger.debug(f"{ticker}: Step 3 cache_read={a_result['cache_read_tokens']}")
    except Exception as e:
        logger.error(f"{ticker}: Audit step failed: {e}")
        assessment = {"error": str(e)}

    # ── Compile Result ──
    result = {
        "ticker": ticker,
        "market_hypotheses": hypotheses,
        "substitute_analysis": substitute_analysis,
        "assessment": assessment,
        "scores": assessment.get("scores", {}),
        "alert_type": assessment.get("alert_type"),
        "status": assessment.get("status", "NONE"),
        "tokens_used": {
            "input": total_input_tokens,
            "output": total_output_tokens
        }
    }

    if run_state_tracker:
        run_state_tracker.add_tokens(total_input_tokens, total_output_tokens)

    return result
