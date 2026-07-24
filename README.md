# Final Market Research Automation

Amazon-India product scraper that writes to a Google Sheet. Two ways to run it:

- **v2 — web tool** (`server.py`): type a **category** ("ragi atta"), pick which of
  the organic results to keep (≤ 20), scrape them, and make **one** batched Gemini
  call for all of them. Recommended.
- **v1 — CLI batch** (`batch_scraper.py`): scrape a `products.csv` of brand + name
  rows, one Gemini call per product.

Both use the **latest scraping and data-sending logic from the
`amazonScraperExtension` Chrome extension** (via `scrape_logic.js`).

## Quick start (v2 web tool)

```bash
cd finalMarketResearch_Automation
source venv/bin/activate
export GEMINI_API_KEY="your-key"   # optional — enables Gemini packaging analysis
python server.py                   # opens a Chrome window; visit http://127.0.0.1:8000
```

Type a category → uncheck off-topic products (max 20) → **Scrape & send to sheet**.
Solve any Amazon CAPTCHA in the Chrome window if it appears. See
[ARCHITECTURE.md](ARCHITECTURE.md) for the full design.

## How the v1 CLI works

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
| `server.py`          | **v2** web server (FastAPI): search → filter → scrape → batched Gemini → sheet |
| `frontend/index.html`| **v2** UI: search box, checkbox filter (≤ 20), results table |
| `batch_scraper.py`   | **v1** CLI: reads CSV, searches, clicks, injects JS, runs Gemini, sends to sheet |
| `scrape_logic.js`    | The extension's scraping logic (`scrapeProduct` + helpers), shared by v1 & v2 |
| `gemini_analysis.py` | AI stage: single (`analyze_packaging`) and batched (`analyze_packaging_batch`) |
| `products.csv`       | v1 input list of products to scrape |
| `results.jsonl`      | Local backup of scraped rows (git-ignored) |
| `requirements.txt`   | Python dependencies |
