"""
Samsung Mobile Scraper - API-based approach
Calls Samsung's internal product listing API directly (same endpoint their
website uses) to get product data, prices, and images without needing JS rendering.

Outputs:
  samsung_output/
    products.json
    products.csv
    images/
    scraper.log
"""

import os
import re
import csv
import json
import time
import logging
import urllib.parse
import requests
from datetime import datetime

# ---------------------------------------------------------------------------
# Directories & Logging
# ---------------------------------------------------------------------------
OUTPUT_DIR = "samsung_output"
IMAGES_DIR = os.path.join(OUTPUT_DIR, "images")
os.makedirs(IMAGES_DIR, exist_ok=True)

LOG_FILE = os.path.join(OUTPUT_DIR, "scraper.log")

logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s [%(levelname)-8s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(LOG_FILE, encoding="utf-8"),
    ],
)
logging.getLogger("urllib3").setLevel(logging.WARNING)
logging.getLogger("requests").setLevel(logging.WARNING)

logger = logging.getLogger("samsung_scraper")

# ---------------------------------------------------------------------------
# Samsung Product API Configuration
# Samsung PK uses this API to load product listings dynamically.
# ---------------------------------------------------------------------------
SAMSUNG_API_URL = (
    "https://www.samsung.com/pk/smartphones/all-smartphones/all-smartphones-pf-pagination/"
)
# These are the query parameters Samsung's frontend sends
API_PARAMS = {
    "prd_type": "01010100",   # smartphones category code
    "start": 0,
    "perpage": 100,           # request up to 100 per page
    "type": "pf",
    "sort": "latest",
}
# Samsung's CDN base for images
IMAGE_CDN = "https://images.samsung.com"

REQUEST_HEADERS = {
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://www.samsung.com/pk/smartphones/all-smartphones/",
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "X-Requested-With": "XMLHttpRequest",
}


# ---------------------------------------------------------------------------
# API Discovery: Try several known Samsung API patterns for PK region
# ---------------------------------------------------------------------------
CANDIDATE_APIS = [
    # Pattern 1: Samsung standard product finder JSON API
    "https://www.samsung.com/pk/smartphones/all-smartphones/all-smartphones-pf-pagination/",
    # Pattern 2: Samsung common search/filter API
    "https://www.samsung.com/common/searchProduct.do",
    # Pattern 3: Samsung product finder JSON endpoint
    "https://www.samsung.com/pk/common/productData.do",
    # Pattern 4: Samsung Galaxy Finder
    "https://www.samsung.com/pk/api/v1/productFinder/",
]

SEARCH_PARAMS_VARIANTS = [
    {"prd_type": "01010100", "start": 0, "perpage": 100, "type": "pf"},
    {"categoryId": "01010100", "start": 0, "perpage": 100},
    {"cat_code": "01010100", "start": 0, "perpage": 100},
]


def try_api_fetch(url, params):
    """Try fetching an API endpoint. Return parsed JSON or None."""
    try:
        resp = requests.get(url, params=params, headers=REQUEST_HEADERS, timeout=30)
        logger.debug("GET %s [%d] Content-Type: %s", resp.url[:100], resp.status_code, resp.headers.get("content-type", ""))
        if resp.status_code == 200:
            ct = resp.headers.get("content-type", "")
            if "json" in ct or "javascript" in ct:
                return resp.json()
            elif "html" not in ct:
                try:
                    return resp.json()
                except Exception:
                    pass
    except Exception as e:
        logger.debug("API probe failed for %s: %s", url, e)
    return None


def discover_api():
    """
    Auto-discover Samsung's product listing API by probing candidate endpoints.
    Returns (url, params, data) or None.
    """
    logger.info("Probing Samsung product API endpoints...")
    for url in CANDIDATE_APIS:
        for params in SEARCH_PARAMS_VARIANTS:
            data = try_api_fetch(url, params)
            if data and isinstance(data, dict):
                # Check for product-like data
                possible_keys = ["productList", "products", "items", "resultList", "data"]
                for k in possible_keys:
                    if k in data and data[k]:
                        logger.info("Found product data at %s (key: '%s')", url, k)
                        return url, params, data
            time.sleep(0.5)
    return None


# ---------------------------------------------------------------------------
# Crawl4AI fallback: scrape the raw API URLs embedded in the page source
# ---------------------------------------------------------------------------
CRAWL4AI_URL = "http://localhost:11235"
API_TOKEN = os.environ.get(
    "CRAWL4AI_API_TOKEN",
    "2fe90f64dbaa1f2167d7f62663d33db5db47c2a51ef791d2ecc10fae77e3019b"
)
HEADERS_C4AI = {"Authorization": f"Bearer {API_TOKEN}"}


def intercept_api_via_crawl4ai():
    """
    Use Crawl4AI to fetch the page source and extract Samsung API endpoints
    by searching for JSON data embedded in <script> tags.
    """
    logger.info("Fetching Samsung page source via Crawl4AI to find embedded API data...")
    payload = {
        "urls": ["https://www.samsung.com/pk/smartphones/all-smartphones/"],
        "browser_config": {
            "type": "BrowserConfig",
            "params": {
                "headless": True,
                "java_script_enabled": True,
                "user_agent": REQUEST_HEADERS["User-Agent"],
            },
        },
        "crawler_config": {
            "type": "CrawlerRunConfig",
            "params": {
                "cache_mode": "BYPASS",
                "word_count_threshold": 0,
                "delay_before_return": 8.0,
                "page_timeout": 60000,
            },
        },
    }
    try:
        resp = requests.post(
            f"{CRAWL4AI_URL}/crawl",
            json=payload,
            headers=HEADERS_C4AI,
            timeout=180,
        )
        if not resp.ok:
            logger.error("Crawl4AI request failed: %s", resp.status_code)
            return None, None

        data = resp.json()
        results = data.get("results") or data.get("result") or [data]
        if isinstance(results, dict):
            results = [results]
        r0 = results[0]
        html = r0.get("html") or ""
        logger.info("Received page source: %d bytes", len(html))

        # Save for reference
        with open(os.path.join(OUTPUT_DIR, "debug_raw.html"), "w", encoding="utf-8") as f:
            f.write(html)

        return extract_products_from_json_in_html(html), html

    except Exception as e:
        logger.error("Crawl4AI intercept failed: %s", e)
        return None, None


def extract_products_from_json_in_html(html):
    """
    Samsung embeds product data as a JSON object inside a <script> tag.
    Common patterns:
      window.productList = [...];
      var pfData = {...};
      digitalData.product = [...];
    """
    if not html:
        return []

    products = []

    # Try to find JSON product arrays embedded in script tags
    patterns = [
        r'window\.__PRELOADED_STATE__\s*=\s*({.+?});\s*</script>',
        r'window\.productList\s*=\s*(\[.+?\]);\s*(?:</script>|var )',
        r'"productList"\s*:\s*(\[.+?\])\s*[,}]',
        r'var pfData\s*=\s*({.+?});\s*(?:</script>|//)',
        r'"products"\s*:\s*(\[.+?\])',
        r'"items"\s*:\s*(\[.+?\])',
    ]

    for pattern in patterns:
        matches = re.findall(pattern, html, re.DOTALL)
        for match in matches:
            try:
                obj = json.loads(match)
                if isinstance(obj, list) and len(obj) > 0:
                    products.extend(obj)
                    logger.info("Found %d products via pattern '%s...'", len(obj), pattern[:30])
                    break
                elif isinstance(obj, dict):
                    for key in ["productList", "products", "items", "data"]:
                        if key in obj and isinstance(obj[key], list):
                            products.extend(obj[key])
                            logger.info("Found %d products in key '%s'", len(obj[key]), key)
                            break
            except Exception:
                pass
        if products:
            break

    return products


# ---------------------------------------------------------------------------
# Parse products from Samsung's API response or embedded JSON
# ---------------------------------------------------------------------------
def normalize_product(p):
    """
    Normalize a Samsung product object from either API or embedded JSON.
    Samsung's objects use various field naming conventions.
    """
    def get_field(*keys):
        for k in keys:
            val = p.get(k)
            if val and str(val).lower() not in ("null", "undefined", "none", ""):
                return str(val).strip()
        return ""

    title = get_field("displayName", "modelNm", "title", "name", "productName")
    model_code = get_field("modelCode", "modelCd", "model_code", "sku")
    price_raw = get_field("priceDisplay", "price_display", "price", "sellingPrice", "lowestPrice")
    product_url = get_field("linkUrl", "detailUrl", "url", "pdpUrl")
    image_url = get_field("thumbUrl", "thumbImgUrl", "imageUrl", "img_url", "image")
    rating = get_field("ratingAvg", "rating", "avgRating")

    # Clean price
    price = ""
    if price_raw and price_raw.lower() not in ("null", "undefined"):
        price = price_raw
    else:
        price = "Coming Soon"

    # Fix relative URLs
    if product_url and product_url.startswith("/"):
        product_url = "https://www.samsung.com" + product_url
    if image_url and image_url.startswith("//"):
        image_url = "https:" + image_url
    elif image_url and image_url.startswith("/"):
        image_url = IMAGE_CDN + image_url

    return {
        "title": title,
        "model_code": model_code,
        "price": price,
        "rating": rating,
        "product_url": product_url,
        "image_url": image_url,
    }


# ---------------------------------------------------------------------------
# Image downloader
# ---------------------------------------------------------------------------
def sanitize(name):
    return re.sub(r"[^a-zA-Z0-9_\-.]", "_", name)[:80]


def download_image(img_url, title, idx):
    """Download image and return local path."""
    if not img_url or img_url.startswith("data:"):
        return None
    if img_url.startswith("//"):
        img_url = "https:" + img_url
    elif img_url.startswith("/"):
        img_url = "https://www.samsung.com" + img_url

    ext = os.path.splitext(urllib.parse.urlparse(img_url).path)[-1] or ".png"
    if ext not in (".jpg", ".jpeg", ".png", ".webp", ".gif"):
        ext = ".png"

    fname = f"{idx:02d}_{sanitize(title or 'product')}{ext}"
    fpath = os.path.join(IMAGES_DIR, fname)

    if os.path.exists(fpath) and os.path.getsize(fpath) > 0:
        return fpath

    try:
        r = requests.get(img_url, timeout=25, headers={
            "Referer": "https://www.samsung.com/",
            "User-Agent": "Mozilla/5.0",
        })
        r.raise_for_status()
        with open(fpath, "wb") as f:
            f.write(r.content)
        return fpath
    except Exception as e:
        logger.warning("[%02d] Image download error: %s", idx, e)
        return None


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    start_time = datetime.now()
    logger.info("=" * 65)
    logger.info("Samsung Pakistan Smartphones Scraper")
    logger.info("=" * 65)

    raw_products = []

    # Strategy 1: Direct API probe
    logger.info("[Strategy 1] Probing Samsung product API endpoints...")
    api_result = discover_api()
    if api_result:
        url, params, data = api_result
        for key in ["productList", "products", "items", "resultList", "data"]:
            if key in data and data[key]:
                raw_products = data[key]
                logger.info("API returned %d raw product entries.", len(raw_products))
                break

    # Strategy 2: Crawl4AI page source + embedded JSON
    if not raw_products:
        logger.info("[Strategy 2] Fetching via Crawl4AI and searching for embedded product JSON...")
        embedded, raw_html = intercept_api_via_crawl4ai()
        if embedded:
            raw_products = embedded
            logger.info("Found %d products from embedded JSON.", len(raw_products))

    # Strategy 3: Load cached debug_raw.html if available
    if not raw_products:
        cache_path = os.path.join(OUTPUT_DIR, "debug_raw.html")
        if os.path.exists(cache_path):
            logger.info("[Strategy 3] Searching cached HTML for embedded product JSON...")
            with open(cache_path, "r", encoding="utf-8") as f:
                html = f.read()
            embedded = extract_products_from_json_in_html(html)
            if embedded:
                raw_products = embedded
                logger.info("Found %d products from cached HTML.", len(raw_products))

    if not raw_products:
        logger.warning("No products found from any strategy.")
        logger.warning("Samsung may require session cookies or a geo-specific proxy.")
        return

    # Normalize products
    products = [normalize_product(p) for p in raw_products]
    products = [p for p in products if p.get("title")]
    logger.info("Normalized %d valid product records.", len(products))

    if not products:
        logger.warning("No valid products after normalization. Raw sample:")
        logger.warning("%s", json.dumps(raw_products[0] if raw_products else {}, indent=2)[:500])
        return

    # Download images & log
    logger.info("-" * 65)
    for idx, p in enumerate(products, start=1):
        local_img = download_image(p["image_url"], p["title"], idx)
        p["local_image"] = local_img
        logger.info(
            "[%02d] %-30s | %-18s | %s",
            idx, p["title"][:30], p["price"][:18],
            os.path.basename(local_img) if local_img else "(no image)"
        )
    logger.info("-" * 65)

    # Save JSON
    json_path = os.path.join(OUTPUT_DIR, "products.json")
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(products, f, indent=2, ensure_ascii=False)
    logger.info("Saved JSON -> %s", json_path)

    # Save CSV
    csv_path = os.path.join(OUTPUT_DIR, "products.csv")
    fieldnames = ["title", "model_code", "price", "rating", "product_url", "image_url", "local_image"]
    with open(csv_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(products)
    logger.info("Saved CSV  -> %s", csv_path)

    # Summary
    downloaded = sum(1 for p in products if p.get("local_image"))
    duration = (datetime.now() - start_time).seconds
    logger.info("=" * 65)
    logger.info("Total Products  : %d", len(products))
    logger.info("Images Saved    : %d -> %s/", downloaded, IMAGES_DIR)
    logger.info("JSON            : %s", json_path)
    logger.info("CSV             : %s", csv_path)
    logger.info("Time Elapsed    : %ds", duration)
    logger.info("=" * 65)


if __name__ == "__main__":
    main()
