# Final Market Research Automation

Batch Amazon-India product scraper that writes to a Google Sheet.

This is the **batch-processing model** of the original `atta-data-pipeline`
(open a real Chrome window, search a product, click the first organic result,
scrape it) — but wired to the **latest scraping and data-sending logic from the
`amazonScraperExtension` Chrome extension**.

## How it works

For each `brand, product_name` row in `products.csv`:

1. Search `amazon.in` for `"brand product_name"`.
2. Click the **first organic (non-sponsored)** search result.
3. Inject [`scrape_logic.js`](scrape_logic.js) — the extension's own
   `scrapeProduct()` — into the live product page and run it.
4. *(optional)* Send the product images to **Gemini** to read the packaging and
   add `ai_*` fields (ingredients, packaging style, label style, marketing) —
   see [`gemini_analysis.py`](gemini_analysis.py). Needs `GEMINI_API_KEY`.
5. POST the scraped row to the Google Apps Script web app (the same sheet the
   extension writes to).
6. Append the row to `results.jsonl` as a local backup.

### Why inject the extension's JS instead of re-porting to Python?

The old `atta-data-pipeline/scraper.py` was a **Python re-port** of the
extension's logic — and the two drifted apart (the extension later gained
multi-dimension twister handling, count-based variants, the "M" units suffix,
and a smarter top-variant baseline). To avoid that drift forever, this tool
runs the extension's actual JavaScript in the page via Playwright's
`page.evaluate`. Same code → same results. The only Python-side logic is the
batch driving (search + click first organic) and the sheet POST.

**Keep `scrape_logic.js` in sync with the extension's `content.js`** — it is a
copy of that file with the extension-only bits (webhook URL, `sendToSheet`,
the floating button) removed.

## Setup

```bash
cd finalMarketResearch_Automation
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
playwright install chromium
```

## Configure

- **Products to scrape** — edit `products.csv` (a `brand,product_name` header
  followed by one product per row).
- **Target sheet** — set `SHEET_WEBHOOK_URL` near the top of
  [`batch_scraper.py`](batch_scraper.py) to your Apps Script `/exec` URL. It is
  currently the same sheet the extension uses.
- **Gemini (optional)** — export your key to enable the packaging analysis:
  ```bash
  export GEMINI_API_KEY="your-key"
  ```
  Without it (or with `--no-gemini`), the AI stage is skipped cleanly. If you
  want the `ai_*` fields to land in the sheet, add matching columns/handling in
  your Apps Script.

## Run

```bash
python batch_scraper.py               # scrape products.csv (+ Gemini) -> sheet
python batch_scraper.py --input x.csv # use a different input file
python batch_scraper.py --no-sheet    # scrape + save locally, skip the sheet POST
python batch_scraper.py --no-gemini   # skip the Gemini packaging analysis
python batch_scraper.py --headless    # no visible browser window
```

A visible window (the default) is recommended — Amazon sometimes shows a
CAPTCHA, and the script pauses for you to solve it by hand before continuing.

## Output

- **Google Sheet** — one row per product via the Apps Script webhook.
- **`results.jsonl`** — a local backup, one JSON object per line, written before
  the sheet POST so a scrape is never lost if the sheet is unreachable.

## Files

| File | Purpose |
|------|---------|
| `batch_scraper.py`   | Batch driver: reads CSV, searches, clicks, injects JS, runs Gemini, sends to sheet |
| `scrape_logic.js`    | The extension's scraping logic (`scrapeProduct` + helpers) |
| `gemini_analysis.py` | Optional AI stage: images → Gemini → `ai_*` packaging fields |
| `products.csv`       | Input list of products to scrape |
| `results.jsonl`      | Local backup of scraped rows (git-ignored) |
| `requirements.txt`   | Python dependencies |
