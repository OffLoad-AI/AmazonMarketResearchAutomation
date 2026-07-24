"""
Gemini packaging analysis
=========================

Restores the AI stage from the old atta-data-pipeline: fetch a product's
images and ask Gemini to read the packaging, returning four structured
fields that get merged into the scraped row before it goes to the sheet:

    ai_packaging_label_style — design language of the front label
    ai_marketing             — top 3 marketing claims on the front

The Gemini API key is read from the GEMINI_API_KEY environment variable —
never hardcoded. If the key (or the SDK) is missing, get_client() returns
None and the whole stage is skipped cleanly.

    export GEMINI_API_KEY="your-key"
"""

import asyncio
import json
import os

try:
    from google import genai
    from google.genai import types
    _GENAI_AVAILABLE = True
except ImportError:
    _GENAI_AVAILABLE = False

GEMINI_MODEL = "gemini-2.5-flash"
MAX_IMAGES = 3
MAX_RETRIES = 3

# Keys always present on the row so the sheet columns line up even on failure.
_FALLBACK = {
    "ai_packaging_label_style": "Not Available (AI Error)",
    "ai_marketing": "Not Available (AI Error)",
}

_PROMPT = """
Analyze the product packaging for {brand} {product_name}.
Return a valid JSON object with EXACTLY these keys:
- "packaging_style": (String) Describe the physical material and closure type (e.g., 'Matte Plastic Pouch', 'Cardboard Box').
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


async def analyze_packaging(client, http, brand, product_name, image_urls):
    """
    Fetch the product's images and ask Gemini to read the packaging.
    Returns the four ai_* fields (fallback values on any failure), or None
    if Gemini is disabled (client is None) so the caller can skip merging.
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

    for attempt in range(MAX_RETRIES):
        try:
            # generate_content is blocking; keep the event loop free.
            result = await asyncio.to_thread(
                client.models.generate_content,
                model=GEMINI_MODEL,
                contents=contents,
                config=types.GenerateContentConfig(response_mime_type="application/json"),
            )
            parsed = json.loads(result.text)
            return {
                "ai_packaging_label_style": parsed.get("packaging_label_style"),
                "ai_marketing": parsed.get("marketing"),
            }
        except Exception as e:
            msg = str(e)
            print(f"   🔥 Gemini error: {msg[:150]}")
            # Transient overload -> exponential backoff, then retry.
            if "503" in msg and attempt < MAX_RETRIES - 1:
                wait = 2 ** attempt
                print(f"   ⏳ Server busy, retrying in {wait}s "
                      f"(attempt {attempt + 1}/{MAX_RETRIES})...")
                await asyncio.sleep(wait)
                continue
            print("   ⚠️ Gemini failed — writing fallback AI fields.")
            return dict(_FALLBACK)
