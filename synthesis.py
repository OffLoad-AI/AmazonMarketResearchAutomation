"""
Category synthesis (Stage 3)
============================

Turns a full run's scraped rows into a founder-facing competitive brief.

Two pieces:
  - build_records()    — compacts the raw scraped dicts into the minimal
                         record set the model sees (and does all arithmetic
                         in Python, so the model never fumbles a number).
  - synthesize_brief() — ONE Gemini call over the whole category, returning
                         the brief as structured JSON.

This reuses gemini_analysis._generate_json(), so it inherits the retry,
JSON-mode, and model auto-discovery behaviour already proven by the
packaging stage. No new API key, no new dependency.

One call per RUN (not per product), so it adds a single request to the
free-tier daily budget regardless of how many products were scraped.

Failure is never fatal: synthesize_brief() returns None on any error and
the caller carries on with the scrape + sheet write as normal.
"""

import math
import re

from gemini_analysis import _generate_json

# ================================================================
# RECORD PREP  (all arithmetic happens here, never in the prompt)
# ================================================================

# Brand = first segment of the Amazon title, before the first comma or pipe.
# Crude, but Amazon titles are consistently "<Brand> <desc>, <spec> | <spec>".
_BRAND_SPLIT = re.compile(r"[,|]")


def _num(x):
    """Clean float, or None. Never NaN — NaN is invalid JSON and breaks the call."""
    if x is None:
        return None
    if isinstance(x, str):
        m = re.search(r"[\d.]+", x.replace(",", ""))
        if not m:
            return None
        x = m.group()
    try:
        f = float(x)
        return None if math.isnan(f) or math.isinf(f) else f
    except (TypeError, ValueError):
        return None


def _brand(title):
    return _BRAND_SPLIT.split(str(title or ""))[0].strip()


def _aspects_to_text(aspects):
    """
    scrapeProduct() returns aspects as [{name, sentiment, mentions}, ...].
    Flatten to 'quality (220, positive), taste (116, mixed)' — compact for the
    model, and keeps the sentiment, which is the analytically useful part.
    """
    if not aspects:
        return None
    if isinstance(aspects, str):
        return aspects
    parts = []
    for a in aspects:
        if not isinstance(a, dict):
            continue
        name = a.get("name")
        if not name:
            continue
        mentions = a.get("mentions")
        sentiment = a.get("sentiment")
        bits = [str(mentions)] if mentions is not None else []
        if sentiment:
            bits.append(str(sentiment))
        parts.append(f"{name} ({', '.join(bits)})" if bits else str(name))
    return ", ".join(parts) or None


def build_records(rows):
    """
    Compact the raw scraped dicts into what the model sees.

    Deliberately DROPPED: _variants_debug (huge, no analytical value at
    category level), image_urls, listing_date, price_per_unit_raw. Dropping
    these is most of the token saving.

    Deliberately DERIVED here: discount_pct, price_per_kg,
    monthly_revenue_estimate — so the model interprets numbers instead of
    computing them.
    """
    records = []
    for r in rows:
        sp = _num(r.get("selling_price"))
        mrp = _num(r.get("mrp"))
        units = _num(r.get("units_sold"))
        # price_per_unit is normalized to price-per-gram (or per-ml) by
        # scrape_logic.js. It falls back to a raw string when it can't parse.
        ppg = _num(r.get("price_per_unit"))

        records.append({
            "brand": _brand(r.get("title")),
            "title": r.get("title"),
            "selling_price": sp,
            "mrp": mrp,
            "discount_pct": round((1 - sp / mrp) * 100, 1) if sp and mrp else None,
            "price_per_g": ppg,
            "price_per_kg": round(ppg * 1000, 1) if ppg else None,
            "units_sold_estimate": units,
            "monthly_revenue_estimate": round(sp * units) if (sp and units) else None,
            "cumulative_revenue_estimate": _num(r.get("cumulative_revenue")),
            "rating": _num(r.get("rating")),
            "review_count": _num(r.get("review_count")),
            "pack_sizes": r.get("pack_sizes"),
            "most_selling_pack": r.get("reference_size"),
            "units_sold_ratio": r.get("units_sold_ratio"),
            "ingredients": r.get("ingredients"),
            "packaging_design": r.get("ai_packaging_label_style"),
            "marketing_claims": r.get("ai_marketing"),
            "review_summary": r.get("customers_say_summary"),
            "review_tags": _aspects_to_text(r.get("aspects")),
        })
    return records


# ================================================================
# PROMPT
# ================================================================

_SYSTEM = """
You are a category analyst producing a one-page competitive brief for the founder
of a consumer brand. Your reader is a busy CEO. They want the decision, the
evidence, and the move — in that order. They do not want a data recap; they can
read the spreadsheet themselves.

You are given a single snapshot of one product category on Amazon India: every
tracked competitor as one record, with commercial fields (price, units sold,
revenue estimate, rating, review count), packaging analysis, and a summary of
customer reviews with tag counts.

Your job is to find the three or four things in this data that would change what
the founder does next, and say them plainly.

NON-NEGOTIABLE RULES — breaking any one of these destroys the brief's credibility:

1. EVERY claim must trace to the data you were given. When you assert something,
   the specific brand(s) and number(s) behind it must be present in the input. If
   you catch yourself writing a claim you cannot point to a row for, delete it.

2. THIS IS ONE SNAPSHOT IN TIME. You have no history. You therefore CANNOT say
   anything is "growing", "gaining", "rising", "trending", "losing share", or
   "declining". You may say who is currently large, small, cheap, expensive,
   well-rated, or poorly-rated. You may not narrate change over time. There is no
   time axis in this data.

3. NAME THE GAPS. Fields are missing for some products (revenue, review
   summaries, ratings). Where a conclusion is weakened by missing data, say so in
   one clause rather than pretending the picture is complete.

4. SEPARATE FACT FROM INFERENCE. "Aashirvaad shows the highest monthly revenue
   estimate" is a fact from the data. "This suggests brand trust outweighs price
   here" is your inference. Both are allowed; keep them grammatically distinct.

5. NO FILLER, NO MARKETING VOICE. Short declarative sentences. Numbers, not
   adjectives. If a section has only one real finding, give one — do not pad.

Revenue and units figures are Amazon estimates, not audited sales. Treat them as
directional; never present them as exact.

Return ONLY the JSON object specified. No preamble, no markdown, no code fences.
"""

_USER = """
CATEGORY: {category}
CLIENT BRAND: {client_brand}
PRODUCTS ANALYSED: {n}
DATA SNAPSHOT DATE: {run_date}

If CLIENT BRAND is a brand name, frame the brief around where THAT brand sits and
what it should do. If it is NONE, frame the brief for a founder considering
ENTERING this category — where the opportunity and the crowding are.

Here is the full dataset, one object per competitor:

{records_json}

Produce the brief as JSON with exactly this shape:

{{
  "executive_summary": {{
    "headline": "One sentence a CEO could repeat in a meeting. The single most important thing this data says.",
    "category_shape": "2-3 sentences on the structure of the category: price spread, how concentrated revenue is, how differentiated the field is.",
    "key_findings": ["3 to 5 findings, one sentence each, each tied to specific brands and numbers, ordered by how much it should influence a decision."],
    "recommended_actions": ["Exactly 3 concrete moves, each justified by a finding above. Specific, not generic: 'Price the 1kg SKU under Rs 110 to sit inside the mainstream cluster', not 'consider competitive pricing'."]
  }},
  "price_quality_map": {{
    "clusters": [
      {{"name": "Tier name, e.g. Value / Mainstream / Organic-premium / Ultra-premium",
        "price_per_kg_range": "e.g. Rs 90-130/kg",
        "rating_range": "e.g. 4.2-4.5",
        "members": ["brand names"],
        "read": "One sentence on what this cluster wins or loses on."}}
    ],
    "anomalies": ["Brands mispriced relative to rating or demand, with the numbers. Only real ones."]
  }},
  "revenue_concentration": {{
    "narrative": "2-3 sentences: is the money spread out or captured by a few? Name the top earners and roughly what share they hold.",
    "top_players": [{{"brand": "", "monthly_revenue_estimate": 0, "read": "Why this brand is large, based on its own row."}}],
    "long_tail_note": "One sentence on the smaller players and what separates them from the leaders."
  }},
  "voice_of_customer": {{
    "category_praise": [{{"theme": "", "read": "What buyers consistently reward, and which brands exemplify it."}}],
    "category_complaints": [{{"theme": "", "evidence": "Specific brands and the tag or phrasing showing it.", "read": "Why this is a recurring failure point and what it means for a new product."}}],
    "unmet_needs": ["Only where the review data actually shows it. If it does not, return an empty array — do not invent needs."],
    "coverage_note": "How many products carried usable review data, e.g. '13 of 20 products carried review summaries; conclusions here rest on those.'"
  }},
  "positioning_whitespace": {{
    "occupied_territories": [{{"aesthetic": "e.g. Rustic / village-heritage", "brands": ["..."], "price_tier": "which cluster this look concentrates in"}}],
    "gaps": ["Thinly occupied or empty territory, tied to what the packaging fields actually show."],
    "read": "One sentence: where a new entrant could look distinct on shelf without crowding an existing brand."
  }},
  "data_caveats": ["Plain-language flags a careful reader deserves: which fields were missing, that revenue is an Amazon estimate, that this is a single snapshot with no trend information."]
}}
"""


# ================================================================
# THE CALL
# ================================================================

async def synthesize_brief(client, rows, category, client_brand=None, run_date=""):
    """
    ONE Gemini call over the whole category. Returns the brief dict, or None
    if Gemini is disabled or the call fails (never raises — a synthesis
    failure must not cost the caller its scrape).

    rows: the raw scraped dicts, exactly as they go to results.jsonl.
    """
    if client is None:
        return None
    if not rows:
        print("   ℹ️ Nothing to synthesize — no rows.")
        return None

    import json as _json
    records = build_records(rows)

    # System + user concatenated into one text part. _generate_json() sets a
    # fixed config (JSON response mode), so passing the instructions inline
    # keeps that wrapper untouched.
    prompt = _SYSTEM + "\n" + _USER.format(
        category=category,
        client_brand=client_brand or "NONE",
        n=len(records),
        run_date=run_date,
        records_json=_json.dumps(records, ensure_ascii=False, indent=1),
    )

    print(f"   🧠 One Gemini call to synthesize the brief ({len(records)} products)...")
    try:
        brief = await _generate_json(client, [prompt])
    except Exception as e:
        print(f"   ⚠️ Synthesis failed — continuing without a brief. {str(e)[:150]}")
        return None

    if not isinstance(brief, dict) or "executive_summary" not in brief:
        print("   ⚠️ Synthesis returned an unexpected shape — skipping brief.")
        return None

    print("   ✅ Brief generated.")
    return brief
