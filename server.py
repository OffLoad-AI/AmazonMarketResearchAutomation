"""
Final Market Research Automation — v2 server
============================================

A small local web app around the same scraping + sheet logic as the v1 CLI.
Interactive flow instead of a products.csv:

    1. POST /api/search  {query}            -> all organic page-1 results
    2. (user filters in the browser, <= 20)
    3. POST /api/scrape  {query, products}  -> scrape each, ONE batched Gemini
                                               call for all front images, then
                                               push every row to the Google Sheet

It reuses v1's helpers (scrape_logic.js injection, image scraping, sheet POST)
by importing them from batch_scraper, so both tools scrape identically.

Run it (a real Chrome window opens — solve any CAPTCHA there):

    source venv/bin/activate
    export GEMINI_API_KEY="your-key"     # optional
    python server.py                     # then open http://127.0.0.1:8000
"""

import asyncio
import json
import os
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel
from playwright.async_api import async_playwright

# Reuse v1's config + helpers verbatim (single source of truth).
from batch_scraper import (
    USER_AGENT,
    SCRAPE_LOGIC_JS,
    RESULTS_FILE,
    handle_captcha,
    scroll_to_load_lazy_sections,
    extract_image_urls,
    send_to_sheet,
)
from gemini_analysis import get_client, analyze_packaging_batch

HERE = Path(__file__).parent
FRONTEND = HERE / "frontend"
MAX_PRODUCTS = 20  # hard cap on how many products go forward to scraping


# ================================================================
# SEARCH — collect ALL organic results on page 1
# ================================================================

# Runs in-page: gathers non-sponsored search cards as {title, url, asin}.
_COLLECT_JS = r"""
() => {
  const out = [];
  const seen = new Set();
  const cards = document.querySelectorAll('div[data-component-type="s-search-result"]');
  for (const card of cards) {
    if (card.classList.contains('AdHolder')) continue;
    if (card.querySelector('.puis-sponsored-label-text, .s-sponsored-label-text, [aria-label="View Sponsored information"]')) continue;

    const a = card.querySelector('div[data-cy="title-recipe"] a.a-link-normal, h2 a.a-link-normal, a.a-text-normal');
    if (!a) continue;

    let href = a.getAttribute('href') || '';
    const title = (card.querySelector('h2')?.innerText || a.innerText || '').trim();
    if (!href || !title) continue;
    if (!href.startsWith('http')) href = 'https://www.amazon.in' + href;

    const m = href.match(/\/dp\/([A-Z0-9]{10})/);
    const asin = m ? m[1] : href;
    if (seen.has(asin)) continue;
    seen.add(asin);

    const cleanUrl = m ? ('https://www.amazon.in/dp/' + asin) : href.split('?')[0];
    out.push({ title, url: cleanUrl, asin });
  }
  return out;
}
"""


async def collect_organic_results(page, query):
    """Search `query` on Amazon and return every organic page-1 result."""
    await page.goto("https://www.amazon.in/", wait_until="domcontentloaded")
    await handle_captcha(page)

    await page.fill("#twotabsearchtextbox", query)
    await page.click("#nav-search-submit-button")
    await page.wait_for_timeout(2000)
    await handle_captcha(page)

    try:
        await page.wait_for_selector('div[data-component-type="s-search-result"]', timeout=10000)
    except Exception:
        return []
    return await page.evaluate(_COLLECT_JS)


# ================================================================
# SCRAPE — one product page, via the extension's injected JS
# ================================================================

async def scrape_product_at_url(page, scrape_js, url):
    """Navigate to a product URL and run the extension's scrapeProduct()."""
    await page.goto(url, wait_until="domcontentloaded")
    try:
        await page.wait_for_selector("#productTitle", timeout=15000)
    except Exception:
        return None
    await handle_captcha(page)

    await scroll_to_load_lazy_sections(page)
    data = await page.evaluate(f"async () => {{ {scrape_js}\n return await scrapeProduct(); }}")
    if data is not None:
        data["image_urls"] = await extract_image_urls(page)
    return data


# ================================================================
# APP
# ================================================================

class SearchRequest(BaseModel):
    query: str


class ProductRef(BaseModel):
    title: str
    url: str


class ScrapeRequest(BaseModel):
    query: str
    products: list[ProductRef]


@asynccontextmanager
async def lifespan(app: FastAPI):
    # One long-lived headful browser + page, shared across the two phases.
    pw = await async_playwright().start()
    # Visible by default so you can solve Amazon CAPTCHAs. HEADLESS=1 hides it
    # (handy for testing, but you can't solve a CAPTCHA you can't see).
    browser = await pw.chromium.launch(headless=bool(os.environ.get("HEADLESS")))
    context = await browser.new_context(user_agent=USER_AGENT)
    app.state.page = await context.new_page()
    app.state.lock = asyncio.Lock()  # serialize browser use (single-user tool)
    app.state.http = httpx.AsyncClient(follow_redirects=True)
    app.state.scrape_js = SCRAPE_LOGIC_JS.read_text(encoding="utf-8")
    app.state.gemini = None if os.environ.get("NO_GEMINI") else get_client()
    print("✅ Server ready — open http://127.0.0.1:8000")
    try:
        yield
    finally:
        await app.state.http.aclose()
        await browser.close()
        await pw.stop()


app = FastAPI(title="Market Research Automation v2", lifespan=lifespan)


@app.get("/")
async def index():
    return FileResponse(FRONTEND / "index.html")


@app.post("/api/search")
async def api_search(req: SearchRequest, request: Request):
    query = req.query.strip()
    if not query:
        return {"products": [], "error": "Empty query."}
    st = request.app.state
    async with st.lock:
        print(f"\n🔍 Search: {query!r}")
        products = await collect_organic_results(st.page, query)
    print(f"   → {len(products)} organic result(s)")
    return {"products": products, "cap": MAX_PRODUCTS}


@app.post("/api/scrape")
async def api_scrape(req: ScrapeRequest, request: Request):
    st = request.app.state
    selected = req.products[:MAX_PRODUCTS]  # enforce the cap server-side too
    scraped = []

    async with st.lock:
        for i, p in enumerate(selected, 1):
            print(f"\n[{i}/{len(selected)}] {p.title[:70]}")
            try:
                data = await scrape_product_at_url(st.page, st.scrape_js, p.url)
            except Exception as e:
                print(f"   ❌ scrape crashed: {e}")
                data = None
            if not data or not data.get("title"):
                print("   ⚠️ no data")
                continue
            data["input_query"] = req.query
            data["input_brand"] = ""
            data["input_product_name"] = req.query
            scraped.append(data)

        # ONE Gemini call for every product's front image.
        if st.gemini is not None and scraped:
            analyses = await analyze_packaging_batch(st.gemini, st.http, scraped)
            for d, a in zip(scraped, analyses):
                if a:
                    d.update(a)

        # Backup locally, then push each row to the sheet.
        results = []
        for d in scraped:
            with RESULTS_FILE.open("a", encoding="utf-8") as f:
                f.write(json.dumps(d, ensure_ascii=False) + "\n")
            sheet = await send_to_sheet(st.http, d)
            results.append({
                "title": d.get("title"),
                "product_url": d.get("product_url"),
                "selling_price": d.get("selling_price"),
                "units_sold": d.get("units_sold"),
                "cumulative_revenue": d.get("cumulative_revenue"),
                "pack_sizes": d.get("pack_sizes"),
                "rating": d.get("rating"),
                "ai_packaging_label_style": d.get("ai_packaging_label_style"),
                "ai_marketing": d.get("ai_marketing"),
                "sheet_status": sheet.get("status"),
                "sheet_row": sheet.get("row"),
            })

    print(f"\n🏁 Scraped {len(results)} product(s).")
    return {"results": results}


if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=8000)
