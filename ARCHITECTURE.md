# Final Market Research Automation — Codebase Explainer

## What this tool is

It automates Amazon-India market research. You give it a list of products (brand + name); it opens a real Chrome window, finds each product on Amazon, scrapes a rich data record (prices, sizes, sales estimates, ratings, reviews, ingredients, and more), and writes one row per product into your Google Sheet.

It's the **batch-processing** descendant of the original `atta-data-pipeline`, but rebuilt to use the **exact scraping and sheet-sending logic of the `amazonScraperExtension` Chrome extension** — which had become the more advanced of the two.

## The core idea (and why it matters)

There were two earlier tools that scraped the same Amazon pages two different ways:

- **`atta-data-pipeline`** — Python + Playwright, batch model: search a product, click the first organic result, scrape. Its scraping logic was a Python *re-port* of the extension.
- **`amazonScraperExtension`** — a Chrome extension that adds a "Scrape this product" button to any Amazon page you're already on, then pushes the result to the sheet. This is the newer, smarter logic.

The problem: those two scraping implementations **drifted apart**. The extension gained handling for multi-dimension variant selectors (e.g. Flavour × Size), count-based variants ("60 Count"), the "M" units suffix, and a smarter "top-selling variant" rule — none of which the Python port had.

**This tool's key design decision:** instead of re-porting the extension's JavaScript to Python *again* (which would just recreate the drift), it **injects the extension's actual JavaScript into the live Amazon page and runs it** via Playwright. Same code → identical results, permanently. The only Python-side logic is the part the extension doesn't have: driving the batch (search + click first organic) and sending to the sheet.

## Architecture / data flow

For each `brand, product_name` row in `products.csv`:

1. **Search** — Python/Playwright opens `amazon.in` and searches `"brand product_name"`.
2. **Click first organic result** — skips sponsored/ad listings, opens the real product page.
3. **Scroll** — nudges the page so Amazon lazy-loads its "Customers say", aspect chips, and details table (the extension never had to do this because a human had already scrolled).
4. **Scrape** — injects `scrape_logic.js` and calls `scrapeProduct()` *inside the page*. This is byte-for-byte the extension's logic, including the trick of `fetch()`-ing each variant's own page (same-origin, with your cookies) to read its individual "bought in past month" number.
5. **Gemini packaging analysis (optional)** — Python scrapes the hi-res product image URLs, downloads them, and sends them to Gemini, which reads the packaging and returns four structured `ai_*` fields (ingredients, packaging style, label style, marketing claims). Skipped cleanly if `GEMINI_API_KEY` isn't set or `--no-gemini` is passed. See `gemini_analysis.py`.
6. **Back up locally** — appends the row to `results.jsonl` *before* the sheet write, so a scrape is never lost.
7. **Send to sheet** — Python POSTs the row to the Google Apps Script web app (the same webhook the extension uses), which appends it to your Google Sheet.

## The files

| File | What it does |
|------|--------------|
| **`batch_scraper.py`** | The driver / entry point. Reads the CSV, runs the browser, does search + click-first-organic, injects the JS, scrapes image URLs, calls the Gemini stage, sends rows to the sheet, writes the local backup. All config lives at the top. |
| **`scrape_logic.js`** | The extension's scraping brain — `scrapeProduct()` plus all its helpers — copied from `content.js` with the extension-only bits removed (the floating button, the webhook URL, `sendToSheet`). This is what actually reads the page. |
| **`gemini_analysis.py`** | The optional AI stage. Downloads the product images and asks Gemini to read the packaging, returning the four `ai_*` fields. Reads the API key from `GEMINI_API_KEY`; returns `None`/fallbacks if unavailable. |
| **`products.csv`** | Your input. A `brand,product_name` header, then one product per row. |
| **`results.jsonl`** | Local backup, one JSON object per scraped product per line. Git-ignored. |
| **`requirements.txt`** | Python deps: `playwright`, `httpx`, `google-genai`. |
| **`README.md`** | Setup + usage. |
| **`.gitignore`** | Ignores `venv/`, `results.jsonl`, `__pycache__/`. |

### Inside `batch_scraper.py`

- **Config block** — `SHEET_WEBHOOK_URL` (your Apps Script `/exec` URL, currently pointing at the same sheet as the extension), file paths, the browser user-agent, and the ordered list of "organic result" CSS selectors.
- **`read_products()`** — parses the CSV, validates it has `brand` and `product_name` columns.
- **`send_to_sheet()`** — POSTs the row as `text/plain` (avoids a CORS preflight; Apps Script reads the raw body). It follows the Apps Script `302` redirect to get the JSON reply, and treats any 2xx response as success even if the body isn't JSON.
- **`handle_captcha()`** — if Amazon throws its character CAPTCHA, the script pauses (up to 2 min) so you can solve it by hand in the window, then continues.
- **`find_first_organic_url()`** — tries the selectors in order to grab the first non-sponsored product link.
- **`scroll_to_load_lazy_sections()`** — scrolls top-to-bottom to trigger lazy content.
- **`extract_image_urls()`** — regexes the hi-res product image URLs out of the page source (Python-side, so `scrape_logic.js` stays a faithful extension mirror).
- **`scrape_one()`** — ties steps 1–4 (+ image URLs) together for a single product.
- **`run()` / `main()`** — the loop over all products, one shared browser + HTTP client + Gemini client, progress printing, and the final summary.

### `gemini_analysis.py`

- **`get_client()`** — builds a Gemini client from `GEMINI_API_KEY`; returns `None` (stage skipped) if the key or the SDK is missing.
- **`analyze_packaging()`** — downloads up to 3 product images, sends them + a prompt to `gemini-2.5-flash` (JSON response mode), and returns the four `ai_*` fields. Retries on transient `503`s with exponential backoff; on any hard failure returns fallback `"Not Available (AI Error)"` values so the sheet columns still line up.

### What each row contains (the fields per product)

From `scrapeProduct()`: `title`, `net_quantity_raw`, `reference_size`, `selling_price`, `mrp`, `units_sold`, `price_per_unit` (+ raw), `rating`, `review_count`, `cumulative_revenue`, `units_sold_ratio`, `pack_sizes`, `customers_say_summary`, `aspects` (sentiment tags with mention counts), `ingredients`, `listing_date`, `product_url`, plus a `_variants_debug` breakdown.

The Python driver adds: `image_urls`, `input_brand` / `input_product_name` (ties the row back to your input), and — when Gemini is on — `ai_ingredients`, `ai_packaging_style`, `ai_packaging_label_style`, `ai_marketing`.

Three of these are the "smart"/derived fields worth knowing:

- **`cumulative_revenue`** — sums `price × units_sold` across every variant (pack size / flavour), giving a rough total revenue estimate rather than just the one you landed on.
- **`units_sold_ratio`** — the relative sales across sizes in ascending order (e.g. `1 : 3 : 8`), GCD-reduced.
- **`ai_*`** — the four Gemini packaging fields, read from the product images rather than the DOM (so `ai_ingredients` can differ from the DOM-scraped `ingredients`).

## How to run it

```bash
cd finalMarketResearch_Automation
source venv/bin/activate          # venv already created, deps installed
export GEMINI_API_KEY="your-key"  # optional — enables the Gemini stage
python batch_scraper.py           # scrape products.csv (+ Gemini) -> sheet
```

Flags: `--input other.csv` (different list), `--no-sheet` (scrape + local backup only, don't POST), `--no-gemini` (skip the AI stage), `--headless` (no visible window — but keep it visible if CAPTCHAs are likely, since solving them needs the window).

The Gemini stage runs only when `GEMINI_API_KEY` is set **and** `--no-gemini` isn't passed; otherwise it's skipped cleanly.

## Things to keep in mind

- **Keep `scrape_logic.js` in sync with the extension's `content.js`.** They're the same logic in two places; improve one, copy the change to the other. (A future cleanup could have both load a single shared file.)
- **New `ai_*` columns need Apps Script support.** Your Google Sheet's Apps Script maps known keys to columns; add handling for `ai_ingredients` / `ai_packaging_style` / `ai_packaging_label_style` / `ai_marketing` there if you want them in the sheet.
- **The Gemini API key is read from `GEMINI_API_KEY`, never hardcoded** (unlike the old pipeline). Rotate the old key that was committed in `atta-data-pipeline` if it's still live.
- **Re-running duplicates rows.** There's no dedupe — running the same `products.csv` twice appends the same products again. Edit the CSV, or dedupe in the sheet.
- **Fragile by nature.** It depends on Amazon's exact DOM structure and CSS. When Amazon changes their layout, individual fields quietly come back empty (they return `null` rather than crashing), so spot-check output periodically.
- **The webhook URL is hardcoded** (your personal sheet) and committed to git — fine for a personal Apps Script, just be aware it's in the history.
- **CAPTCHAs need a human.** Scraping at volume increases the odds Amazon challenges you; the script handles this by pausing for a manual solve.
