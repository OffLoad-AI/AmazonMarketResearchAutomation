// ============================================================
// Amazon Product Scraper — PURE SCRAPING LOGIC
// ------------------------------------------------------------
// This is the exact scraping logic from the Chrome extension
// (amazonScraperExtension/content.js), with the extension-only
// parts removed:
//   - SHEET_WEBHOOK_URL / sendToSheet()  (Python sends the row)
//   - injectButton() and the button UI   (batch mode has no UI)
//
// The batch driver (batch_scraper.py) injects this file into the
// live Amazon product page and calls scrapeProduct(). Keeping the
// logic here as JS — instead of re-porting it to Python — means the
// batch tool and the extension always scrape identically.
//
// KEEP THIS FILE IN SYNC with the extension's content.js scraping
// functions. If you improve scraping in the extension, copy the
// changed functions here (or vice-versa).
// ============================================================


// ----------------------------------------------------------------
// SMALL HELPERS
// ----------------------------------------------------------------

function fmt(n) {
  return parseFloat(n.toFixed(4)).toString();
}

function getText(selector) {
  return document.querySelector(selector)?.innerText?.trim() || null;
}

function gcd(a, b) { return b === 0 ? a : gcd(b, a % b); }

// Normalize a swatch/label string to a stable join key (collapse whitespace, lowercase)
function normalizeLabel(s) {
  return s ? s.replace(/\s+/g, " ").trim().toLowerCase() : null;
}


// ----------------------------------------------------------------
// SIZE / QUANTITY PARSING
// ----------------------------------------------------------------

function parseReferenceSize(text) {
  if (!text) return null;

  const match = text.toLowerCase().match(
    /(\d+(?:\.\d+)?)\s*(kilograms?|kgs?|grams?|gms?|g|milliliters?|millilitres?|ml|liters?|litres?|l)\b/
  );
  if (!match) return null;

  const value = parseFloat(match[1]);
  const unit = match[2];

  if (/^(kilograms?|kgs?)$/.test(unit)) return `${fmt(value)} kg`;
  if (/^(grams?|gms?|g)$/.test(unit)) return `${fmt(value / 1000)} kg`;
  if (/^(liters?|litres?|l)$/.test(unit)) return `${fmt(value)} l`;
  if (/^(milliliters?|millilitres?|ml)$/.test(unit)) return `${fmt(value / 1000)} l`;

  return null;
}

function getNetQuantityRawFrom(doc) {
  const bullet = [...doc.querySelectorAll('#detailBullets_feature_div li')]
    .map(li => li.textContent.trim())
    .find(t => t.toLowerCase().includes("net quantity"));
  if (bullet) {
    return bullet.split(":")[1]?.replace(/[‎‏]/g, "").trim() || null;
  }

  const row = [...doc.querySelectorAll(
    '#productDetails_techSpec_section_1 tr, #productDetails_detailBullets_sections1 tr'
  )]
    .map(tr => tr.textContent.trim())
    .find(t => t.toLowerCase().includes("net quantity"));
  if (row) {
    return row.replace(/net quantity/i, "").replace(/[‎‏:]/g, "").trim() || null;
  }

  return null;
}


// ----------------------------------------------------------------
// PRICE PARSING (shared)
// ----------------------------------------------------------------

function parsePriceString(text) {
  if (!text) return null;
  const clean = text.replace(/[^\d.]/g, "").replace(/\.$/, "");
  return clean ? parseFloat(clean) : null;
}


// ----------------------------------------------------------------
// LANDING-PAGE PRICES
// ----------------------------------------------------------------

function getSellingPrice() {
  const container = document.querySelector(
    '#corePriceDisplay_desktop_feature_div .a-price:not(.a-text-price)'
  );
  if (!container) return null;

  let text = container.querySelector('.a-offscreen')?.innerText?.trim();
  if (!text) text = container.querySelector('.a-price-whole')?.innerText?.trim();
  return parsePriceString(text);
}

function getMrp(fallback) {
  let text = document.querySelector(
    '#corePriceDisplay_desktop_feature_div .a-text-price[data-a-strike="true"] .a-offscreen'
  )?.innerText?.trim();

  if (!text) {
    text = document.querySelector(
      '#corePriceDisplay_desktop_feature_div .a-text-price[data-a-strike="true"]'
    )?.innerText?.trim();
  }

  const parsed = parsePriceString(text);
  return parsed ?? (fallback ?? null);
}

function getPerUnitPriceRaw() {
  const el = document.querySelector(
    '#corePriceDisplay_desktop_feature_div .a-price[data-a-size="mini"]'
  );
  if (!el) return null;

  const parentText = el.parentElement?.innerText || "";
  const segment = parentText
    .split("\n")
    .map(s => s.trim())
    .find(s => s.includes("/"));

  return segment ? segment.replace(/[()]/g, "").trim() : null;
}

function normalizePerUnitPrice(raw) {
  if (!raw) return null;

  const match = raw.toLowerCase().match(
    /₹?\s*([\d,]+(?:\.\d+)?)\s*\/\s*(\d*)\s*(kilograms?|kgs?|grams?|gms?|g|milliliters?|millilitres?|ml|liters?|litres?|l)\b/
  );
  if (!match) return raw;

  const value = parseFloat(match[1].replace(/,/g, ""));
  const multiplier = match[2] ? parseFloat(match[2]) : 1;
  const unit = match[3];

  if (/^(kilograms?|kgs?)$/.test(unit)) return +(value / (1000 * multiplier)).toFixed(4);
  if (/^(grams?|gms?|g)$/.test(unit)) return +(value / (1 * multiplier)).toFixed(4);
  if (/^(liters?|litres?|l)$/.test(unit)) return +(value / (1000 * multiplier)).toFixed(4);
  if (/^(milliliters?|millilitres?|ml)$/.test(unit)) return +(value / (1 * multiplier)).toFixed(4);

  return raw;
}


// ----------------------------------------------------------------
// RATING / REVIEWS / UNITS SOLD
// ----------------------------------------------------------------

function getRating() {
  let ratingText = document.querySelector(
    "i[data-hook='average-star-rating'] .a-icon-alt"
  )?.innerText;
  if (!ratingText) ratingText = document.querySelector("#acrPopover")?.getAttribute("title");
  if (!ratingText) return null;

  const match = ratingText.match(/(\d+(?:[.,]\d+)?)/);
  return match ? parseFloat(match[1].replace(",", ".")) : null;
}

function getReviewCount() {
  const text = document.querySelector("#acrCustomerReviewText")?.innerText;
  if (!text) return 0;
  const clean = text.replace(/[^\d]/g, "");
  return clean ? parseInt(clean, 10) : 0;
}

function getUnitsSoldFrom(doc) {
  // The "X bought in past month" badge is often split across child nodes
  // (e.g. <span>700+ bought</span> in past month), so looking for a single
  // span containing the whole phrase misses it. Read the container text instead.
  const scope = doc.querySelector("#centerCol") || doc.body;
  if (!scope) return null;

  const text = (scope.textContent || "").replace(/\s+/g, " ");
  const match = text.match(/(\d+(?:[.,]\d+)?)\s*([KkMm]?)\s*\+?\s*bought/i);
  if (!match) return null;

  let val = parseFloat(match[1].replace(/,/g, ""));
  const suffix = match[2].toLowerCase();
  if (suffix === "k") val *= 1000;
  else if (suffix === "m") val *= 1000000;
  return Math.round(val);
}


// ----------------------------------------------------------------
// CUSTOMERS SAY / ASPECTS / INGREDIENTS / DATE
// ----------------------------------------------------------------

function getCustomersSaySummary() {
  return document.querySelector("div[data-testid='overall-summary'] span")
    ?.innerText?.trim() || null;
}

function getAspects() {
  const tabs = [...document.querySelectorAll('div[role="tab"][aria-label*="aspect"]')];
  const aspects = [];
  for (const tab of tabs) {
    const aria = tab.getAttribute("aria-label") || "";
    const parts = aria.split(",").map(p => p.trim());
    if (parts.length >= 3) {
      const sentiment = parts[0].replace("aspect", "").trim();
      const name = parts[1].trim();
      const mentions = parseInt(parts[2].replace(/[^\d]/g, ""), 10);
      if (name && !isNaN(mentions)) aspects.push({ name, sentiment, mentions });
    }
  }
  return aspects;
}

function getIngredients() {
  const boxes = [...document.querySelectorAll("#important-information .content")];
  const box = boxes.find(b =>
    b.querySelector("h4")?.innerText?.toLowerCase().includes("ingredient")
  );
  if (!box) return null;

  const paras = [...box.querySelectorAll("p")].map(p => p.innerText.trim()).filter(Boolean);
  let text = paras.join(" ");
  if (!text) text = box.innerText.replace(/ingredients:?/i, "").trim();
  return text || null;
}

function getListingDate() {
  const el = [...document.querySelectorAll(
    "#detailBullets_feature_div li, #productDetails_detailBullets_sections1 tr"
  )].find(e => e.innerText.toLowerCase().includes("date first available"));
  if (!el) return null;

  const raw = el.innerText.split("Available").pop();
  const clean = raw.replace(/[:\n‎‏]/g, "").trim();
  return clean || null;
}


// ----------------------------------------------------------------
// VARIANTS
// ----------------------------------------------------------------

// Read price/MRP/size/label for each variant from the twister buttons.
// Keyed by button INDEX so same-size multipacks stay distinct; the
// same index appearing twice (desktop/mobile double-render) is de-duped.
function getVariantsFromTwister() {
  const buttons = [...document.querySelectorAll(
    '[id^="size_name_"]:not([id$="-announce"])'
  )];

  const variants = {}; // keyed by button index

  for (const btn of buttons) {
    const idxMatch = btn.id.match(/size_name_(\d+)$/);
    if (!idxMatch) continue;
    const idx = idxMatch[1];

    const sizeLabel = btn.querySelector('.swatch-title-text-display')?.innerText?.trim();
    if (!sizeLabel) continue;

    // Weight/volume -> normalized size (e.g. "0.1 kg"). Otherwise (count-based
    // variants like "60 Count") keep a readable size from the label so the
    // variant still counts toward cumulative revenue / units instead of vanishing.
    let refSize = parseReferenceSize(sizeLabel);
    if (!refSize) refSize = sizeLabel.replace(/\(.*?\)/g, "").trim() || sizeLabel;

    const priceText = btn.querySelector('.apex-pricetopay-value .a-offscreen')?.innerText
      || btn.querySelector('.apex-pricetopay-value [aria-hidden="true"]')?.innerText;
    const mrpText = btn.querySelector('.apex-basisprice-value .a-offscreen')?.innerText;

    const price = parsePriceString(priceText);
    const mrp = parsePriceString(mrpText) ?? price;

    if (!variants[idx]) {
      variants[idx] = {
        size: refSize,
        label: normalizeLabel(sizeLabel), // opaque join key, e.g. "100 g (pack of 3)"
        price,
        mrp,
        units: null
      };
    }
  }

  return variants;
}

// Map each variant ASIN to its FULL array of twister dimension labels, e.g.
// ["butterfly pea flower", "60 count (pack of 1)"] for a flavour×size twister,
// or just ["100 g (pack of 3)"] for a single-dimension size twister.
function getVariantAsinArrays() {
  const html = document.body.innerHTML;
  const matches = [...html.matchAll(/"(B0[A-Z0-9]{8})"\s*:\s*\[([^\]]*)\]/g)];
  const map = {};
  for (const m of matches) {
    const labels = [...m[2].matchAll(/"([^"]*)"/g)]
      .map(x => normalizeLabel(x[1]))
      .filter(Boolean);
    if (labels.length && !map[m[1]]) map[m[1]] = labels;
  }
  return map; // { asin: [normalizedLabel, ...] }
}

// The ASIN of the page currently being viewed (from the URL)
function getCurrentAsin() {
  const m = window.location.href.match(/\/(?:dp|gp\/product)\/([A-Z0-9]{10})/);
  return m ? m[1] : null;
}

// Fetch one variant page and return only its units sold.
// Runs same-origin inside the live amazon.in page, so the browser's
// cookies apply and Amazon serves the real "bought in past month" badge.
async function fetchVariantData(asin) {
  try {
    const res = await fetch(`https://www.amazon.in/dp/${asin}`, { credentials: "include" });
    if (!res.ok) return null;

    const html = await res.text();
    const doc = new DOMParser().parseFromString(html, "text/html");

    return { asin, units: getUnitsSoldFrom(doc) };
  } catch (e) {
    console.warn(`Variant fetch failed for ${asin}:`, e);
    return null;
  }
}


// ----------------------------------------------------------------
// DERIVED FIELDS
// ----------------------------------------------------------------

// Ratio across ALL variants in ascending size order (same-size variants
// keep their button order). e.g. 0.1kg x3 -> "1 : 3 : 8"
function buildUnitsSoldRatio(variantList) {
  const sorted = [...variantList].sort((a, b) => parseFloat(a.size) - parseFloat(b.size));
  const units = sorted.map(v => v.units || 0);
  const nonZero = units.filter(u => u > 0);
  if (nonZero.length === 0) return null;

  const divisor = nonZero.reduce((acc, u) => gcd(acc, u));
  return units.map(u => (u > 0 ? String(u / divisor) : "0")).join(" : ");
}


// ----------------------------------------------------------------
// MAIN SCRAPE
// ----------------------------------------------------------------

async function scrapeProduct() {
  const title = getText("#productTitle");

  const netQtyRaw = getNetQuantityRawFrom(document);
  const landingReferenceSize = parseReferenceSize(netQtyRaw) || parseReferenceSize(title);

  const sellingPrice = getSellingPrice();
  const mrp = getMrp(sellingPrice);
  const unitsSold = getUnitsSoldFrom(document);

  const perUnitRaw = getPerUnitPriceRaw();
  const perUnitNormalized = normalizePerUnitPrice(perUnitRaw);

  // --- Variants ---
  const variantMap = getVariantsFromTwister();   // keyed by button index
  const variantList = Object.values(variantMap);

  let cumulativeRevenue = null;
  let unitsSoldRatio = null;
  let topReferenceSize = landingReferenceSize;
  let topPrice = sellingPrice;
  let topMrp = mrp;
  let topUnits = unitsSold;

  if (variantList.length === 0) {
    // Single-size product
    cumulativeRevenue = (sellingPrice || 0) * (unitsSold || 0);
  } else {
    // This is (potentially) a multi-dimension twister, e.g. Flavour × Size.
    // Find, among all twister ASINs, only the SIZE variants of the CURRENTLY
    // selected flavour: match by size label, disambiguate flavour using the
    // landing ASIN's own labels.
    const asinArrays = getVariantAsinArrays();              // { asin: [labels...] }
    const sizeLabelSet = new Set(variantList.map(v => v.label));
    const landingArr = asinArrays[getCurrentAsin()] || [];
    const flavourLabels = landingArr.filter(l => !sizeLabelSet.has(l)); // [] if single-dimension

    const sizeToAsin = {};
    for (const [asin, arr] of Object.entries(asinArrays)) {
      if (!flavourLabels.every(fl => arr.includes(fl))) continue; // current flavour only
      const sizeLabel = arr.find(l => sizeLabelSet.has(l));
      if (sizeLabel && !sizeToAsin[sizeLabel]) sizeToAsin[sizeLabel] = asin;
    }

    // Fetch only the current flavour's size variants, keyed by size label
    const targets = Object.entries(sizeToAsin);             // [ [sizeLabel, asin], ... ]
    const fetched = await Promise.all(
      targets.map(([label, asin]) =>
        fetchVariantData(asin).then(r => ({ label, units: r ? r.units : null })))
    );

    const unitsByLabel = {};
    for (const f of fetched) {
      if (f && f.units != null) unitsByLabel[f.label] = f.units;
    }

    // Assign units to each twister variant by its label
    for (const v of variantList) {
      if (v.label && unitsByLabel[v.label] !== undefined) {
        v.units = unitsByLabel[v.label];
      }
    }

    // Landing variant: prefer the live on-page units (more reliable than fetched)
    const selectedBtn = [...document.querySelectorAll('[id^="size_name_"]:not([id$="-announce"])')]
      .find(btn => btn.classList.contains('a-button-selected')
        || btn.querySelector('input')?.getAttribute('aria-checked') === 'true');
    const selectedLabel = normalizeLabel(selectedBtn?.querySelector('.swatch-title-text-display')?.innerText);
    if (selectedLabel && unitsSold != null) {
      const lv = variantList.find(v => v.label === selectedLabel);
      if (lv) lv.units = unitsSold;
    }

    // Cumulative revenue — sum across EVERY variant
    let total = 0;
    for (const v of variantList) {
      if (v.price && v.units) total += v.price * v.units;
    }
    cumulativeRevenue = total;

    // Top-selling variant -> overwrite headline fields.
    // Baseline = the landing variant's units, so a variant only takes over when
    // it genuinely sells more — a 0/unknown-units variant can't clobber the landing.
    let max = unitsSold || 0;

    for (const v of variantList) {
      if ((v.units || 0) > max) {
        max = v.units || 0;
        topReferenceSize = v.size;
        topPrice = v.price;
        topMrp = v.mrp;
        topUnits = v.units;
      }
    }

    unitsSoldRatio = buildUnitsSoldRatio(variantList);
  }

  return {
    title,
    net_quantity_raw: netQtyRaw,
    reference_size: topReferenceSize,
    selling_price: topPrice,
    mrp: topMrp,
    units_sold: topUnits,
    price_per_unit: perUnitNormalized, // from LANDING variant (per your spec)
    price_per_unit_raw: perUnitRaw,
    rating: getRating(),
    review_count: getReviewCount(),
    cumulative_revenue: cumulativeRevenue,
    units_sold_ratio: unitsSoldRatio,
    pack_sizes: variantList.length
      ? [...new Set(
          [...variantList]
            .sort((a, b) => parseFloat(a.size) - parseFloat(b.size))
            .map(v => v.size)
        )].join(", ")
      : (landingReferenceSize || null),
    customers_say_summary: getCustomersSaySummary(),
    aspects: getAspects(),
    ingredients: getIngredients(),
    listing_date: getListingDate(),
    product_url: window.location.href.split("?")[0],
    _variants_debug: variantList
  };
}
