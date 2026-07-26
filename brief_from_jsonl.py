"""
Brief generator — offline, from results.jsonl
=============================================

Generate (or re-render) a category brief from rows you have ALREADY scraped.
No browser, no Amazon, no CAPTCHA. This is the loop you'll actually live in
while tuning the report.

    # generate a brief for one category from the local backup
    python brief_from_jsonl.py --query "ragi atta"

    # re-render the SAME brief after editing render.py — costs nothing,
    # calls no API, reuses the saved brief JSON
    python brief_from_jsonl.py --query "ragi atta" --no-llm

    # frame it for a specific client instead of a generic entrant
    python brief_from_jsonl.py --query "ragi atta" --client "Anveshan"

Output lands in briefs/<slug>/ as brief.json + brief.html.

results.jsonl accumulates across every run, so --query filters to the rows
from one category (matched against input_query / input_product_name). Use
--all to take everything in the file.
"""

import argparse
import asyncio
import json
import re
import sys
from datetime import date
from pathlib import Path

from gemini_analysis import get_client
from render import render_brief
from synthesis import build_records, synthesize_brief

HERE = Path(__file__).parent
RESULTS_FILE = HERE / "results.jsonl"
BRIEFS_DIR = HERE / "briefs"


def slugify(s):
    return re.sub(r"[^a-z0-9]+", "-", str(s).lower()).strip("-") or "run"


def load_rows(path, query=None, take_all=False):
    """Read results.jsonl, optionally filtered to one category, newest-wins."""
    if not path.exists():
        sys.exit(f"❌ {path} not found — scrape something first.")

    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue

    if not take_all and query:
        q = query.lower().strip()
        rows = [r for r in rows
                if q in str(r.get("input_query") or "").lower()
                or q in str(r.get("input_product_name") or "").lower()]

    # results.jsonl appends, so the same product can appear from several runs.
    # Keep the LAST occurrence of each product_url — that's the freshest scrape.
    deduped = {}
    for r in rows:
        key = r.get("product_url") or r.get("title")
        if key:
            deduped[key] = r
    return list(deduped.values())


async def main():
    ap = argparse.ArgumentParser(description="Generate a category brief from results.jsonl")
    ap.add_argument("--query", help="Category to filter rows by (matches input_query)")
    ap.add_argument("--all", action="store_true", help="Use every row in results.jsonl")
    ap.add_argument("--client", help="Client brand to frame the brief around (default: generic entrant)")
    ap.add_argument("--input", help="Path to results.jsonl (default: ./results.jsonl)")
    ap.add_argument("--no-llm", action="store_true",
                    help="Skip the Gemini call and re-render the saved brief.json")
    args = ap.parse_args()

    if not args.query and not args.all:
        sys.exit("❌ Pass --query \"ragi atta\" (or --all).")

    path = Path(args.input) if args.input else RESULTS_FILE
    rows = load_rows(path, args.query, args.all)
    if not rows:
        sys.exit(f"❌ No rows matched. Check --query against input_query values in {path.name}.")

    category = args.query or "All products"
    out_dir = BRIEFS_DIR / slugify(category)
    out_dir.mkdir(parents=True, exist_ok=True)
    brief_json = out_dir / "brief.json"
    brief_html = out_dir / "brief.html"

    print(f"📋 {len(rows)} product(s) for {category!r}")

    # ---- the brief itself -------------------------------------------------
    if args.no_llm:
        if not brief_json.exists():
            sys.exit(f"❌ --no-llm needs an existing {brief_json}. Run once without it first.")
        brief = json.loads(brief_json.read_text(encoding="utf-8"))
        print("♻️  Reusing saved brief.json (no API call).")
    else:
        client = get_client()
        if client is None:
            sys.exit("❌ Gemini unavailable. Set GEMINI_API_KEY, or use --no-llm to re-render.")
        brief = await synthesize_brief(
            client, rows, category=category,
            client_brand=args.client, run_date=date.today().isoformat(),
        )
        if not brief:
            sys.exit("❌ Synthesis returned nothing — see the error above.")
        brief_json.write_text(json.dumps(brief, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"   💾 {brief_json}")

    # ---- render -----------------------------------------------------------
    html = render_brief(
        brief, build_records(rows), category=category,
        client_brand=args.client, run_date=date.today().isoformat(),
    )
    brief_html.write_text(html, encoding="utf-8")
    print(f"\n✅ {brief_html}\n   open {brief_html}")


if __name__ == "__main__":
    asyncio.run(main())
