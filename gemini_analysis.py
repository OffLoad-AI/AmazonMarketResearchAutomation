"""
Gemini packaging analysis
=========================

Fetch a product's front image and ask Gemini to read the packaging,
returning fields that get merged into the scraped row before it goes to
the sheet:

    ai_packaging_label_style — design language of the front label
    ai_marketing             — top 3 marketing claims on the front

Two entry points:
  - analyze_packaging()       — one product per call  (used by the v1 CLI)
  - analyze_packaging_batch() — ALL products in ONE call (used by the v2
                                server) so a run stays within the free-tier
                                daily request limit regardless of count.

The Gemini API key is read from the GEMINI_API_KEY environment variable —
never hardcoded. If the key (or the SDK) is missing, get_client() returns
None and the whole stage is skipped cleanly.

    export GEMINI_API_KEY="your-key"
"""

import asyncio
import io
import json
import os

try:
    from google import genai
    from google.genai import types
    _GENAI_AVAILABLE = True
except ImportError:
    _GENAI_AVAILABLE = False

try:
    from PIL import Image
    _PIL_AVAILABLE = True
except ImportError:
    _PIL_AVAILABLE = False

# Model can be pinned via the GEMINI_MODEL env var. The default is the
# "-latest" alias, which tracks the current Flash model so it doesn't break
# when a specific dated version (e.g. gemini-2.5-flash) is retired. If even
# this 404s, analyze_packaging() auto-discovers a valid model from the API.
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-flash-latest")
# Only the front image (Amazon's first/main image) is sent to Gemini.
# image_urls is already in page order, so image_urls[:MAX_IMAGES] takes the front.
MAX_IMAGES = 1
MAX_RETRIES = 3

# Remembers whatever model actually worked (or was auto-picked) so the batch
# doesn't re-resolve the model on every product.
_resolved_model = None

# Keys always present on the row so the sheet columns line up even on failure.
_FALLBACK = {
    "ai_packaging_label_style": "Not Available (AI Error)",
    "ai_marketing": "Not Available (AI Error)",
}

_PROMPT = """
Analyze the product packaging for {brand} {product_name}.
Return a valid JSON object with EXACTLY these keys:
- "packaging_label_style": (String) Describe the design language/style/philosophy of the front label.
- "marketing": (String) Comma-separated list of the top 3 marketing claims ranked according to their impact/boldness/uniqueness printed on the front.
"""


def get_client():
    """Return a configured Gemini client, or None if unavailable (skip the stage)."""
    if not _GENAI_AVAILABLE:
        print("ℹ️ google-genai not installed — Gemini analysis disabled.")
        return None
    key = os.environ.get("GEMINI_API_KEY")
    if not key:
        print("ℹ️ GEMINI_API_KEY not set — Gemini analysis disabled. "
              "Run: export GEMINI_API_KEY=\"your-key\"")
        return None
    try:
        return genai.Client(api_key=key)
    except Exception as e:
        print(f"⚠️ Could not init Gemini client — analysis disabled. {e}")
        return None


async def _fetch_images(http, image_urls):
    """Download up to MAX_IMAGES product images as raw bytes."""
    images = []
    for url in image_urls[:MAX_IMAGES]:
        try:
            r = await http.get(url, timeout=20)
            r.raise_for_status()
            images.append(r.content)
        except Exception as e:
            print(f"   ⚠️ image fetch failed ({url}): {e}")
    return images


def _list_generate_models(client):
    """Names (no 'models/' prefix) of models that support generateContent."""
    names = []
    try:
        for m in client.models.list():
            actions = getattr(m, "supported_actions", None) or []
            if not actions or "generateContent" in actions:
                name = (m.name or "").replace("models/", "")
                if name:
                    names.append(name)
    except Exception as e:
        print(f"   ⚠️ Could not list Gemini models: {e}")
    return names


def _pick_flash_model(names):
    """Prefer a '-latest' flash alias, then any flash, then any model."""
    for n in names:
        if "flash" in n and "latest" in n:
            return n
    for n in names:
        if "flash" in n:
            return n
    return names[0] if names else None


def _downscale_jpeg(raw, max_side=768, quality=85):
    """Shrink an image so its longest side <= max_side, re-encoded as JPEG.
    Keeps the batched call under Gemini's ~20 MB inline cap and cuts tokens.
    Returns the original bytes unchanged if Pillow is missing or decode fails."""
    if not _PIL_AVAILABLE:
        return raw
    try:
        im = Image.open(io.BytesIO(raw)).convert("RGB")
        im.thumbnail((max_side, max_side))
        out = io.BytesIO()
        im.save(out, format="JPEG", quality=quality)
        return out.getvalue()
    except Exception:
        return raw


async def _generate_json(client, contents):
    """One generate_content call returning parsed JSON.

    Handles transient 503s (exponential backoff) and a retired model (404 ->
    auto-discover a valid model via the API, retry, and cache it). Raises on
    unrecoverable failure so the caller can apply its own fallback.
    """
    global _resolved_model
    model = _resolved_model or GEMINI_MODEL

    for attempt in range(MAX_RETRIES):
        try:
            # generate_content is blocking; keep the event loop free.
            result = await asyncio.to_thread(
                client.models.generate_content,
                model=model,
                contents=contents,
                config=types.GenerateContentConfig(response_mime_type="application/json"),
            )
            _resolved_model = model  # remember what worked for the rest of the run
            return json.loads(result.text)
        except Exception as e:
            msg = str(e)
            low = msg.lower()
            print(f"   🔥 Gemini error ({model}): {msg[:150]}")

            # Model missing/retired -> discover a valid one and retry with it.
            if "404" in msg or "not found" in low or "not available" in low:
                available = _list_generate_models(client)
                new_model = _pick_flash_model(available)
                if new_model and new_model != model:
                    print(f"   🔁 Switching model to '{new_model}' "
                          f"(pin it via GEMINI_MODEL to skip this).")
                    if available:
                        print(f"      Available: {', '.join(available[:12])}")
                    model = new_model
                    continue
                raise

            # Transient overload -> exponential backoff, then retry.
            if "503" in msg and attempt < MAX_RETRIES - 1:
                wait = 2 ** attempt
                print(f"   ⏳ Server busy, retrying in {wait}s "
                      f"(attempt {attempt + 1}/{MAX_RETRIES})...")
                await asyncio.sleep(wait)
                continue
            raise

    raise RuntimeError("Gemini call exhausted retries")


async def analyze_packaging(client, http, brand, product_name, image_urls):
    """
    Single-product analysis (used by the CLI batch tool). Fetches the front
    image and asks Gemini to read the packaging. Returns the ai_* fields
    (fallback values on failure), or None if Gemini is disabled.
    """
    if client is None:
        return None
    if not image_urls:
        print("   ⚠️ No image URLs to analyze — skipping Gemini.")
        return dict(_FALLBACK)

    images = await _fetch_images(http, image_urls)
    if not images:
        print("   ⚠️ Could not download any images — skipping Gemini.")
        return dict(_FALLBACK)

    prompt = _PROMPT.format(brand=brand, product_name=product_name)
    contents = [prompt] + [
        types.Part.from_bytes(data=b, mime_type="image/jpeg") for b in images
    ]
    try:
        parsed = await _generate_json(client, contents)
        return {
            "ai_packaging_label_style": parsed.get("packaging_label_style"),
            "ai_marketing": parsed.get("marketing"),
        }
    except Exception:
        print("   ⚠️ Gemini failed — writing fallback AI fields.")
        return dict(_FALLBACK)


_BATCH_PROMPT = """
You are given {n} products. For each, there is a line "PRODUCT <number>: <title>"
immediately followed by that product's front-of-pack image.

Return ONLY a valid JSON object whose keys are the product numbers as strings.
Each value must be an object with EXACTLY these keys:
- "packaging_label_style": (String) Describe the design language/style/philosophy of the front label.
- "marketing": (String) Comma-separated list of the top 3 marketing claims ranked by impact/boldness/uniqueness printed on the front.

Example shape: {{"1": {{"packaging_label_style": "...", "marketing": "..."}}, "2": {{...}}}}
Include an entry for every product number from 1 to {n}.
"""


async def analyze_packaging_batch(client, http, products):
    """
    Analyze ALL products' front images in a SINGLE Gemini call.

    products: list of dicts, each with at least "title" and "image_urls".
    Returns a list aligned 1:1 with `products`; each item is the ai_* dict
    (or fallback). Returns a list of None if Gemini is disabled.

    One call regardless of product count -> stays within the free-tier
    daily request limit no matter how many products are selected.
    """
    if client is None:
        return [None] * len(products)

    # Download + downscale each product's front image. Track which products
    # actually got an image so we can map Gemini's answers back by number.
    sent = []  # list of (original_index, title, jpeg_bytes)
    for idx, prod in enumerate(products):
        urls = prod.get("image_urls") or []
        if not urls:
            continue
        imgs = await _fetch_images(http, urls[:1])  # front image only
        if not imgs:
            continue
        sent.append((idx, prod.get("title") or f"Product {idx + 1}", _downscale_jpeg(imgs[0])))

    results = [dict(_FALLBACK) for _ in products]
    if not sent:
        print("   ⚠️ No downloadable images across products — skipping Gemini.")
        return results

    # Build one contents payload: prompt, then (label, image) per product.
    contents = [_BATCH_PROMPT.format(n=len(sent))]
    for number, (_, title, jpeg) in enumerate(sent, start=1):
        contents.append(f"PRODUCT {number}: {title}")
        contents.append(types.Part.from_bytes(data=jpeg, mime_type="image/jpeg"))

    print(f"   🧠 One Gemini call for {len(sent)} product image(s)...")
    try:
        parsed = await _generate_json(client, contents)
    except Exception:
        print("   ⚠️ Gemini batch failed — writing fallback AI fields for all.")
        return results

    # Map "1".."M" answers back to the original product indices.
    for number, (orig_idx, _, _) in enumerate(sent, start=1):
        entry = parsed.get(str(number)) or parsed.get(number) or {}
        results[orig_idx] = {
            "ai_packaging_label_style": entry.get("packaging_label_style") if entry else _FALLBACK["ai_packaging_label_style"],
            "ai_marketing": entry.get("marketing") if entry else _FALLBACK["ai_marketing"],
        }
    return results
