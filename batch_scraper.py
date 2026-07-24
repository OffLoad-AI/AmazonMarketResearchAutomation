"""
Final Market Research Automation — Batch Scraper
================================================

Batch-processing driver (the old atta-data-pipeline model) wired to the
LATEST scraping + data-sending logic from amazonScraperExtension.

Flow, for every {brand, product_name} row in products.csv:
    1. Search amazon.in for "brand product_name"
    2. Click the first ORGANIC (non-ad) result
    3. Inject scrape_logic.js (the extension's scrapeProduct) into the
       live product page and run it  -> identical to the Chrome extension
    4. (optional) Gemini reads the product images and adds ai_* packaging
       fields (see gemini_analysis.py) — needs GEMINI_API_KEY in the env
    5. POST the scraped row to the Google Apps Script web app
       (same webhook the extension uses) -> lands in the Google Sheet
    6. Append the row to results.jsonl locally as a backup

Because the scraping runs the extension's own JS in-page, the batch tool
and the extension always produce the same data for the same page.

Usage:
    python batch_scraper.py                # scrape everything in products.csv
    python batch_scraper.py --input my.csv # a different input file
    python batch_scraper.py --no-sheet     # scrape + save locally, don't POST
    python batch_scraper.py --no-gemini    # skip the Gemini packaging analysis
    python batch_scraper.py --headless     # run without a visible window
"""

import argparse
import argparse
import asyncio
import csv
import json
import re
import sys
from pathlib import Path

import httpx
from playwright.async_api import async_playwright

from gemini_analysis import analyze_packaging, get_client

# ================================================================
# CONFIG
# ================================================================

# Google Apps Script web app URL (ends in /exec) — the SAME target sheet
# the Chrome extension writes to. Paste a new /exec URL here to retarget.
SHEET_WEBHOOK_URL = "https://script.google.com/macros/s/AKfycbx-erpAh3jR-qJ8NEoHfEBO2LBVVQ5s9XIWr6aTjRiQtV9OjYuLJjN-a8anoc7UN5Ka_Q/exec"

HERE = Path(__file__).parent
SCRAPE_LOGIC_JS = HERE / "scrape_logic.js"     # the extension's scraping code
DEFAULT_INPUT = HERE / "products.csv"          # brand,product_name rows
RESULTS_FILE = HERE / "results.jsonl"          # local backup, one JSON per line

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)

# Organic-only search-result link selectors, tried in order.
ORGANIC_SELECTORS = [
    'div[data-component-type="s-search-result"]:not(.AdHolder) div[data-cy="title-recipe"] a.a-link-normal',
    'div[data-component-type="s-search-result"]:not(.AdHolder) a:has(h2)',
    'div.s-result-item:not(.AdHolder) a.a-text-normal',
]


# ================================================================
# INPUT
# ================================================================

def read_products(path: Path):
    """Read [{brand, product_name}, ...] from a CSV with a brand,product_name header."""
    if not path.exists():
        sys.exit(f"❌ Input file not found: {path}\n   Create it with a 'brand,product_name' header.")

    products = []
    with path.open(newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        cols = {c.lower().strip(): c for c in (reader.fieldnames or [])}
        if "brand" not in cols or "product_name" not in cols:
            sys.exit(f"❌ {path} must have 'brand' and 'product_name' columns. Found: {reader.fieldnames}")
        for row in reader:
            brand = (row[cols["brand"]] or "").strip()
            name = (row[cols["product_name"]] or "").strip()
            if brand or name:
                products.append({"brand": brand, "product_name": name})
    return products


# ================================================================
# SHEET (mirrors sendToSheet() in content.js, sent from Python)
# ================================================================

async def send_to_sheet(client: httpx.AsyncClient, data: dict) -> dict:
    """POST one scraped row to the Apps Script web app, exactly like the extension."""
    if not SHEET_WEBHOOK_URL or "PASTE_YOUR" in SHEET_WEBHOOK_URL:
        print("   ⚠️ SHEET_WEBHOOK_URL not set — skipping sheet write.")
        return {"status": "skipped"}
    try:
        # text/plain avoids a CORS preflight; Apps Script reads the raw body regardless.
        res = await client.post(
            SHEET_WEBHOOK_URL,
            headers={"Content-Type": "text/plain;charset=utf-8"},
            content=json.dumps(data),
            timeout=30,
        )
        try:
            return res.json()
        except Exception:
            # 2xx but non-JSON body -> the row was still written; report success.
            if res.is_success:
                return {"status": "success", "row": "?"}
            return {"status": "error", "message": f"HTTP {res.status_code}: {res.text[:200]}"}
    except Exception as e:
        print(f"   ❌ Sheet write failed: {e}")
        return {"status": "error", "message": str(e)}


# ================================================================
# BROWSER HELPERS
# ================================================================

async def handle_captcha(page):
    """If Amazon shows its character CAPTCHA, wait for the user to solve it by hand."""
    if await page.locator("#captchacharacters").count() > 0:
        print("🛑 AMAZON CAPTCHA DETECTED! Please solve it in the browser window...")
        await page.locator("#captchacharacters").wait_for(state="hidden", timeout=120000)
        print("✅ Captcha solved, continuing...")


async def find_first_organic_url(page):
    """Return the href of the first non-sponsored search result, or None."""
    for selector in ORGANIC_SELECTORS:
        try:
            link = page.locator(selector).first
            await link.wait_for(state="attached", timeout=3000)
            href = await link.get_attribute("href")
            if href:
                return href if href.startswith("http") else f"https://www.amazon.in{href}"
        except Exception:
            continue
    return None


async def scroll_to_load_lazy_sections(page):
    """
    Amazon lazy-loads 'Customers say', aspect chips and the details table as you
    scroll. In the extension the user has already scrolled; in batch mode we do it
    ourselves so those fields aren't empty.
    """
    try:
        for frac in (0.25, 0.5, 0.75, 1.0):
            await page.evaluate("f => window.scrollTo(0, document.body.scrollHeight * f)", frac)
            await page.wait_for_timeout(600)
        await page.evaluate("() => window.scrollTo(0, 0)")
        await page.wait_for_timeout(300)
    except Exception:
        pass


# Hi-res product images live in the page source as "hiRes":"...jpg" (large as fallback).
# Scraped here in Python — rather than in scrape_logic.js — so that file stays a
# faithful mirror of the extension (which has no image/AI stage).
_IMG_HIRES = re.compile(r'"hiRes":"(https://m\.media-amazon\.com/images/I/[^"]+\.jpg)"')
_IMG_LARGE = re.compile(r'"large":"(https://m\.media-amazon\.com/images/I/[^"]+\.jpg)"')


async def extract_image_urls(page, limit=3):
    """Up to `limit` de-duped hi-res product image URLs from the page source."""
    try:
        content = await page.content()
    except Exception:
        return []
    matches = _IMG_HIRES.findall(content) or _IMG_LARGE.findall(content)
    urls = []
    for u in matches:
        if u not in urls:
            urls.append(u)
        if len(urls) >= limit:
            break
    return urls


async def scrape_one(page, scrape_js: str, brand: str, product_name: str):
    """Search -> open first organic result -> run the extension's scrapeProduct()."""
    search_query = f"{brand} {product_name}".strip()
    print(f"\n🔍 Searching Amazon for: {search_query}")

    await page.goto("https://www.amazon.in/", wait_until="domcontentloaded")
    await handle_captcha(page)

    # 1. Search
    await page.fill("#twotabsearchtextbox", search_query)
    await page.click("#nav-search-submit-button")
    await page.wait_for_timeout(2000)
    await handle_captcha(page)

    # 2. First organic link
    print("🔗 Looking for the first organic product link...")
    product_url = await find_first_organic_url(page)
    if not product_url:
        print("❌ Could not find any organic product link for this query.")
        return None

    # 3. Open the product page
    print("🚀 Navigating to product page...")
    await page.goto(product_url, wait_until="domcontentloaded")
    try:
        await page.wait_for_selector("#productTitle", timeout=15000)
    except Exception:
        print("❌ Product page did not load a title — skipping.")
        return None
    await handle_captcha(page)

    # 4. Trigger lazy sections, then run the extension's scraping logic in-page.
    await scroll_to_load_lazy_sections(page)
    print("📦 Scraping (extension logic + variant units)...")
    data = await page.evaluate(f"async () => {{ {scrape_js}\n return await scrapeProduct(); }}")

    # Image URLs (for the Gemini packaging analysis) — scraped Python-side.
    if data is not None:
        data["image_urls"] = await extract_image_urls(page)
    return data


# ================================================================
# MAIN
# ================================================================

async def run(args):
    input_path = Path(args.input) if args.input else DEFAULT_INPUT
    products = read_products(input_path)
    if not products:
        sys.exit(f"❌ No products found in {input_path}")

    scrape_js = SCRAPE_LOGIC_JS.read_text(encoding="utf-8")
    print(f"📋 Loaded {len(products)} product(s) from {input_path.name}")

    # Gemini packaging analysis — enabled only if not disabled AND a key is set.
    gemini_client = None if args.no_gemini else get_client()
    if gemini_client:
        print("🧠 Gemini packaging analysis: ON")

    ok = failed = 0
    # follow_redirects: Apps Script /exec answers a POST with a 302 to
    # script.googleusercontent.com that serves the real JSON body. Without
    # this, httpx returns the empty 302 and res.json() fails (the row still
    # gets written, since doPost already ran).
    async with async_playwright() as p, httpx.AsyncClient(follow_redirects=True) as http:
        browser = await p.chromium.launch(headless=args.headless)
        context = await browser.new_context(user_agent=USER_AGENT)
        page = await context.new_page()

        for i, prod in enumerate(products, 1):
            print(f"\n{'='*60}\n[{i}/{len(products)}] {prod['brand']} — {prod['product_name']}\n{'='*60}")
            try:
                data = await scrape_one(page, scrape_js, prod["brand"], prod["product_name"])
            except Exception as e:
                print(f"❌ Scrape crashed: {e}")
                data = None

            if not data or not data.get("title"):
                failed += 1
                print("⚠️ No data scraped for this product.")
                continue

            # Carry the input identity alongside the scraped fields.
            data["input_brand"] = prod["brand"]
            data["input_product_name"] = prod["product_name"]

            print(f"   ✅ {data.get('title')[:70]}")
            print(f"      price={data.get('selling_price')}  units={data.get('units_sold')}  "
                  f"revenue={data.get('cumulative_revenue')}  sizes={data.get('pack_sizes')}")

            # Gemini packaging analysis (adds ai_* fields), if enabled.
            if gemini_client is not None:
                print("   🧠 Analyzing packaging with Gemini...")
                ai = await analyze_packaging(
                    gemini_client, http, prod["brand"], prod["product_name"],
                    data.get("image_urls", []),
                )
                if ai:
                    data.update(ai)
                    print(f"      ai_marketing={str(ai.get('ai_marketing'))[:60]}")

            # Local backup first (never lose a scrape if the sheet is down).
            with RESULTS_FILE.open("a", encoding="utf-8") as f:
                f.write(json.dumps(data, ensure_ascii=False) + "\n")

            # Then push to the sheet.
            if not args.no_sheet:
                print("   📤 Sending to sheet...")
                result = await send_to_sheet(http, data)
                status = result.get("status")
                if status == "success":
                    print(f"   ✅ Added to sheet (row {result.get('row')})")
                elif status == "skipped":
                    print("   ✅ Saved locally (sheet URL not set)")
                else:
                    print(f"   ⚠️ Sheet write did not succeed: {result}")

            ok += 1
            await page.wait_for_timeout(1500)  # be gentle between products

        await browser.close()

    print(f"\n{'='*60}\n🏁 Done. {ok} scraped, {failed} failed. Backup: {RESULTS_FILE.name}\n{'='*60}")


def main():
    parser = argparse.ArgumentParser(description="Batch Amazon scraper (extension logic).")
    parser.add_argument("--input", help="CSV of products (default: products.csv)")
    parser.add_argument("--headless", action="store_true", help="Run without a visible browser window")
    parser.add_argument("--no-sheet", action="store_true", help="Scrape + save locally, skip the sheet POST")
    parser.add_argument("--no-gemini", action="store_true", help="Skip the Gemini packaging analysis")
    args = parser.parse_args()
    asyncio.run(run(args))


if __name__ == "__main__":
    main()
