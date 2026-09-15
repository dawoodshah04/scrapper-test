"""
Samsung Pakistan Smartphones Scraper
=====================================
Uses Crawl4AI (via Docker container at localhost:11235) to:
  1. Load the Samsung PK smartphones page with full JS rendering
  2. Wait until product cards are actually visible in the DOM
  3. Execute JS to scroll the page so lazy-loaded items appear
  4. Parse the rendered HTML for product name, price, image
  5. Download images (using official Samsung CDN URLs)
  6. Save results as products.json and products.csv

Outputs:
  samsung_output/
    products.json
    products.csv
    images/
    scraper.log
    debug_raw.html   (last raw HTML snapshot for debugging)
"""

import os
import re
import csv
import json
import logging
import urllib.parse
import requests
from datetime import datetime
from bs4 import BeautifulSoup

# ---------------------------------------------------------------------------
# Directories & Logging
# ---------------------------------------------------------------------------
OUTPUT_DIR = "samsung_output"
IMAGES_DIR = os.path.join(OUTPUT_DIR, "images")
os.makedirs(IMAGES_DIR, exist_ok=True)

LOG_FILE = os.path.join(OUTPUT_DIR, "scraper.log")

logging.basicConfig(
    level=logging.INFO,
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
# Crawl4AI Config
# ---------------------------------------------------------------------------
CRAWL4AI_URL = "http://localhost:11235"
API_TOKEN = os.environ.get(
    "CRAWL4AI_API_TOKEN",
    "2fe90f64dbaa1f2167d7f62663d33db5db47c2a51ef791d2ecc10fae77e3019b"
)
HEADERS_C4AI = {"Authorization": f"Bearer {API_TOKEN}"}

TARGET_URL = "https://www.samsung.com/pk/smartphones/all-smartphones/"
IMAGE_CDN = "https://images.samsung.com"

PRODUCT_CARD_SEL = "li.pd21-product-card__item:not(.pd21-product-card__banner)"


# ---------------------------------------------------------------------------
# Crawl4AI fetch with wait_for + JS scroll
# ---------------------------------------------------------------------------
def fetch_via_crawl4ai():
    """Fetch fully rendered HTML from Crawl4AI container without custom JS."""
    logger.info("Sending crawl request to Crawl4AI at %s ...", CRAWL4AI_URL)
    payload = {
        "urls": [TARGET_URL],
        "browser_config": {
            "type": "BrowserConfig",
            "params": {
                "headless": True,
                "java_script_enabled": True,
                "viewport_width": 1440,
                "viewport_height": 900,
            },
        },
        "crawler_config": {
            "type": "CrawlerRunConfig",
            "params": {
                "cache_mode": "BYPASS",
                "word_count_threshold": 0,
                "wait_for": "css:li.pd21-product-card__item",
                "scan_full_page": True,
                "scroll_delay": 0.5,
                "delay_before_return_html": 5.0,
                "page_timeout": 90000,
                "remove_overlay_elements": True,
            },
        },
    }

    try:
        resp = requests.post(f"{CRAWL4AI_URL}/crawl", json=payload, headers=HEADERS_C4AI, timeout=300)
        if resp.status_code != 200:
            logger.error("Crawl4AI HTTP %d: %s", resp.status_code, resp.text)
            return None
        data = resp.json()
        results = data.get("results") or data.get("result") or [data]
        if isinstance(results, dict):
            results = [results]
        html = results[0].get("html") or ""
        if html:
            debug_path = os.path.join(OUTPUT_DIR, "debug_raw.html")
            with open(debug_path, "w", encoding="utf-8") as f:
                f.write(html)
            logger.info("Saved HTML snapshot (%d bytes) -> %s", len(html), debug_path)
        return html
    except Exception as e:
        logger.error("Crawl4AI request failed: %s", e)
        return None


def parse_products(html):
    """Parse Samsung product cards from the fully-rendered page HTML."""
    soup = BeautifulSoup(html, "html.parser")
    cards = soup.select(PRODUCT_CARD_SEL) or [
        c for c in soup.select("li.pd21-product-card__item")
        if "pd21-product-card__banner" not in c.get("class", [])
    ]
    logger.info("Found %d product cards.", len(cards))
    products = [p for p in (_extract_card(c) for c in cards) if p]
    logger.info("Extracted %d product records.", len(products))
    return products


def _extract_card(card):
    """Extract title, price, image, model, url from a single product card."""
    title = ""
    for sel in [".pd21-product-card__name", "a[data-modelname]", "[data-modeldisplay]", "a[aria-label]", "h3"]:
        el = card.select_one(sel)
        if el:
            t = el.get("data-modeldisplay") or el.get("data-modelname") or el.get("aria-label") or el.get_text(strip=True)
            if t and len(t) > 2:
                title = t
                break

    price = ""
    for sel in [
        ".pd21-product-card__price-main",
        ".pd21-product-card__price",
        ".js-pfv2-price",
        ".option-selector-v2__price",
        ".price-ux__price-current",
        "[data-pricetext]",
        "[class*='price']",
    ]:
        el = card.select_one(sel)
        if el:
            p = el.get("data-pricetext") or el.get_text(" ", strip=True)
            if p and p not in ("null", "undefined"):
                price = p
                break
    if not price:
        price = "Coming Soon"

    model_code = card.get("data-modelcode") or card.get("data-model-code") or ""

    product_url = ""
    link = card.select_one("a[href]")
    if link:
        href = link.get("href", "")
        product_url = f"https://www.samsung.com{href}" if href.startswith("/") else href

    image_url = ""
    for img in card.select("img"):
        src = img.get("data-desktop-src") or img.get("data-src") or img.get("src") or ""
        if src and not src.startswith("data:") and len(src) > 15:
            image_url = (IMAGE_CDN + src) if src.startswith("/") else src
            break

    if not title and not product_url:
        return None

    return {
        "title": title,
        "model_code": model_code,
        "price": price,
        "product_url": product_url,
        "image_url": image_url,
    }


# ---------------------------------------------------------------------------
# Image downloader
# ---------------------------------------------------------------------------
def sanitize(name):
    return re.sub(r"[^a-zA-Z0-9_\-.]", "_", name)[:80]


def download_image(img_url, title, idx):
    """Download image from Samsung CDN and return local path."""
    if not img_url or img_url.startswith("data:"):
        return None

    ext = os.path.splitext(urllib.parse.urlparse(img_url).path)[-1] or ".png"
    if ext.lower() not in (".jpg", ".jpeg", ".png", ".webp", ".gif"):
        ext = ".png"

    fname = f"{idx:02d}_{sanitize(title or 'product')}{ext}"
    fpath = os.path.join(IMAGES_DIR, fname)

    if os.path.exists(fpath) and os.path.getsize(fpath) > 0:
        return fpath

    try:
        r = requests.get(
            img_url,
            timeout=30,
            headers={
                "Referer": "https://www.samsung.com/",
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/124.0.0.0 Safari/537.36"
                ),
            },
        )
        r.raise_for_status()
        with open(fpath, "wb") as f:
            f.write(r.content)
        return fpath
    except Exception as e:
        logger.warning("[%02d] Image download failed (%s): %s", idx, img_url[:60], e)
        return None


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    start_time = datetime.now()
    logger.info("=" * 65)
    logger.info("Samsung Pakistan Smartphones Scraper")
    logger.info("=" * 65)

    html = None

    # Step 1: Fetch via Crawl4AI (with JS wait_for + scroll)
    logger.info("[Step 1] Fetching rendered page via Crawl4AI...")
    html = fetch_via_crawl4ai()

    # Step 2: Fallback to cached snapshot
    if not html:
        cache_path = os.path.join(OUTPUT_DIR, "debug_raw.html")
        if os.path.exists(cache_path):
            logger.info("[Step 2] Loading cached HTML snapshot: %s", cache_path)
            with open(cache_path, "r", encoding="utf-8") as f:
                html = f.read()
        else:
            logger.error("No HTML available and no cached snapshot found. Aborting.")
            return

    # Step 3: Parse product cards from rendered HTML
    logger.info("[Step 3] Parsing product cards from HTML...")
    products = parse_products(html)

    if not products:
        logger.warning("No products extracted.")
        logger.warning("Possible causes:")
        logger.warning("  - Samsung changed CSS class names — check debug_raw.html")
        logger.warning("  - Page JS did not finish rendering (increase delay_before_return)")
        logger.warning("  - Geo-blocking or bot detection triggered")
        return

    # Step 4: Download images
    logger.info("[Step 4] Downloading product images...")
    logger.info("-" * 65)
    for idx, p in enumerate(products, start=1):
        local_img = download_image(p["image_url"], p["title"], idx)
        p["local_image"] = local_img
        logger.info(
            "[%02d] %-35s | %-20s | %s",
            idx,
            (p["title"] or "(no name)")[:35],
            p["price"][:20],
            os.path.basename(local_img) if local_img else "(no image)",
        )
    logger.info("-" * 65)

    # Step 5: Save JSON
    json_path = os.path.join(OUTPUT_DIR, "products.json")
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(products, f, indent=2, ensure_ascii=False)
    logger.info("Saved JSON -> %s", json_path)

    # Step 6: Save CSV
    csv_path = os.path.join(OUTPUT_DIR, "products.csv")
    fieldnames = ["title", "model_code", "price", "product_url", "image_url", "local_image"]
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

