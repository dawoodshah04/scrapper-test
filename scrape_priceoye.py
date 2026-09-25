"""
PriceOye Smartphones & Products Scraper
========================================
Uses Crawl4AI (via Docker container at localhost:11235) to fetch fully rendered
pages from PriceOye.pk with dynamic JavaScript execution and auto-scrolling.

Outputs:
  priceoye_output/
    products.json
    products.csv
    images/
    scraper.log
    debug_raw.html
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
OUTPUT_DIR = "priceoye_output"
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

logger = logging.getLogger("priceoye_scraper")

# ---------------------------------------------------------------------------
# Crawl4AI Config
# ---------------------------------------------------------------------------
CRAWL4AI_URL = "http://localhost:11235"
API_TOKEN = os.environ.get(
    "CRAWL4AI_API_TOKEN",
    "2fe90f64dbaa1f2167d7f62663d33db5db47c2a51ef791d2ecc10fae77e3019b"
)
HEADERS_C4AI = {"Authorization": f"Bearer {API_TOKEN}"}

TARGET_URL = "https://priceoye.pk/mobiles"


# ---------------------------------------------------------------------------
# Fetching (Crawl4AI with direct HTTP fallback)
# ---------------------------------------------------------------------------
def fetch_via_crawl4ai(url=TARGET_URL):
    """Fetch rendered HTML from Crawl4AI container, or fallback to direct HTTP fetch."""
    logger.info("Sending crawl request to Crawl4AI at %s ...", CRAWL4AI_URL)
    payload = {
        "urls": [url],
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
                "wait_for": "css:body",
                "wait_for_timeout": 5000,
                "scan_full_page": True,
                "scroll_delay": 0.5,
                "delay_before_return_html": 3.0,
                "page_timeout": 30000,
                "remove_overlay_elements": True,
            },
        },
    }

    html = None
    try:
        resp = requests.post(f"{CRAWL4AI_URL}/crawl", json=payload, headers=HEADERS_C4AI, timeout=(5, 300))
        if resp.status_code == 200:
            data = resp.json()
            results = data.get("results") or data.get("result") or [data]
            if isinstance(results, dict):
                results = [results]
            html = results[0].get("html") or ""
            if html and len(html) > 500:
                debug_path = os.path.join(OUTPUT_DIR, "debug_raw.html")
                with open(debug_path, "w", encoding="utf-8") as f:
                    f.write(html)
                logger.info("Saved Crawl4AI HTML snapshot (%d bytes) -> %s", len(html), debug_path)
                return html
        else:
            logger.warning("Crawl4AI HTTP %d: %s", resp.status_code, resp.text[:200])
    except Exception as e:
        logger.warning("Crawl4AI service unavailable on %s (%s).", CRAWL4AI_URL, e)
        logger.info("Falling back to direct HTTP fetch...")

    # Fallback: Direct HTTP fetch
    try:
        headers = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
            "Accept-Language": "en-US,en;q=0.9",
        }
        resp = requests.get(url, headers=headers, timeout=30)
        if resp.status_code == 200 and resp.text:
            html = resp.text
            debug_path = os.path.join(OUTPUT_DIR, "debug_raw.html")
            with open(debug_path, "w", encoding="utf-8") as f:
                f.write(html)
            logger.info("Direct HTTP fetch succeeded (%d bytes) -> %s", len(html), debug_path)
            return html
        else:
            logger.error("Direct HTTP request failed with status code %d", resp.status_code)
    except Exception as e:
        logger.error("Direct HTTP request failed: %s", e)

    return None


# ---------------------------------------------------------------------------
# Parsing Strategy
# ---------------------------------------------------------------------------
def parse_products(html):
    """Parse PriceOye products from HTML (DOM elements & Script JSON fallback)."""
    soup = BeautifulSoup(html, "html.parser")
    products = []

    # Strategy 1: DOM product card boxes (.productBox)
    cards = soup.select(".productBox, .b-productBox, div[class*='productBox']")
    if cards:
        logger.info("Found %d DOM product card elements.", len(cards))
        seen_urls = set()
        for c in cards:
            p = _extract_dom_card(c)
            if p and p["product_url"] not in seen_urls:
                seen_urls.add(p["product_url"])
                products.append(p)
        logger.info("Extracted %d unique product records from DOM cards.", len(products))

    # Strategy 2: Fallback to embedded script JSON data if DOM cards are missing or sparse
    if len(products) < 5:
        logger.info("Parsing embedded JSON script tags fallback...")
        script_products = _parse_embedded_json(html)
        if script_products:
            logger.info("Extracted %d products from embedded JSON data.", len(script_products))
            # Merge if needed
            existing_urls = {p["product_url"] for p in products}
            for sp in script_products:
                if sp["product_url"] not in existing_urls:
                    existing_urls.add(sp["product_url"])
                    products.append(sp)

    logger.info("Total unique product records extracted: %d", len(products))
    return products


def _extract_dom_card(card):
    """Extract details from a single DOM product box element."""
    link = card.select_one("a[href]")
    if not link:
        return None

    href = link.get("href", "")
    product_url = f"https://priceoye.pk{href}" if href.startswith("/") else href

    # Title
    title_el = card.select_one(".p-title, .product-title, [class*='title']") or link
    title = (
        title_el.get("data-vars-value")
        or title_el.get_text(strip=True)
        or ""
    )

    # Clean title
    title = re.sub(r"\s+", " ", title).strip()

    # Image
    image_url = ""
    img_el = card.select_one("img.product-thumbnail-img, img.product-thumbnail, img")
    if img_el:
        src = img_el.get("src") or img_el.get("data-src") or img_el.get("data-lazy-src") or ""
        if src and not src.startswith("data:"):
            image_url = f"https:{src}" if src.startswith("//") else src

    # Price
    price = ""
    price_el = card.select_one(".price-box, .p-price, [class*='price']")
    if price_el:
        price_text = price_el.get_text(" ", strip=True)
        m = re.search(r"Rs\.?\s*[\d,]+", price_text, re.I)
        if m:
            price = m.group(0)
        else:
            price = price_text

    if not price:
        price = "Check Website"

    # Retail / Original Price
    retail_price = ""
    retail_el = card.select_one(".retail-price, .old-price, strike, del")
    if retail_el:
        retail_price = retail_el.get_text(strip=True)

    # Rating
    rating = ""
    rating_el = card.select_one(".rating, .p-rating, [class*='rating']")
    if rating_el:
        rating = rating_el.get_text(strip=True)

    if not title and not product_url:
        return None

    return {
        "title": title,
        "price": price,
        "retail_price": retail_price,
        "product_url": product_url,
        "image_url": image_url,
        "rating": rating,
    }


def _parse_embedded_json(html):
    """Extract products from embedded JSON data in script tags."""
    products = []
    matches = re.findall(
        r'\"product_id\":\s*(\d+).*?\"product_title\":\s*\"([^\"]+)\".*?\"product_slug\":\s*\"([^\"]+)\".*?\"lowest_price\":\s*(\d+)',
        html,
    )
    seen = set()
    for pid, title, slug, price_val in matches:
        url = f"https://priceoye.pk/mobiles/{slug}"
        if url in seen:
            continue
        seen.add(url)
        price_formatted = f"Rs {int(price_val):,}" if price_val.isdigit() else price_val
        products.append({
            "title": title.strip(),
            "price": price_formatted,
            "retail_price": "",
            "product_url": url,
            "image_url": "",
            "rating": "",
        })
    return products


# ---------------------------------------------------------------------------
# Image Downloader
# ---------------------------------------------------------------------------
def sanitize(name):
    return re.sub(r"[^a-zA-Z0-9_\-.]", "_", name)[:80]


def download_image(img_url, title, idx):
    """Download product image from PriceOye CDN."""
    if not img_url or img_url.startswith("data:"):
        return None

    if img_url.startswith("//"):
        img_url = "https:" + img_url

    ext = os.path.splitext(urllib.parse.urlparse(img_url).path)[-1] or ".webp"
    if ext.lower() not in (".jpg", ".jpeg", ".png", ".webp", ".gif", ".svg"):
        ext = ".webp"

    fname = f"{idx:02d}_{sanitize(title or 'product')}{ext}"
    fpath = os.path.join(IMAGES_DIR, fname)

    if os.path.exists(fpath) and os.path.getsize(fpath) > 0:
        return fpath

    try:
        r = requests.get(
            img_url,
            timeout=30,
            headers={
                "Referer": "https://priceoye.pk/",
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
# Main Execution
# ---------------------------------------------------------------------------
def main():
    start_time = datetime.now()
    logger.info("=" * 65)
    logger.info("PriceOye Smartphones Scraper (via Crawl4AI)")
    logger.info("=" * 65)

    # Step 1: Fetch via Crawl4AI
    logger.info("[Step 1] Fetching rendered page from PriceOye via Crawl4AI...")
    html = fetch_via_crawl4ai(TARGET_URL)

    if not html:
        logger.error("Failed to retrieve HTML for PriceOye. Aborting.")
        return

    # Step 2: Parse Product Cards
    logger.info("[Step 2] Parsing product cards from HTML...")
    products = parse_products(html)

    if not products:
        logger.warning("No products extracted from PriceOye page.")
        return

    # Step 3: Download Images
    logger.info("[Step 3] Downloading product images...")
    logger.info("-" * 65)
    for idx, p in enumerate(products, start=1):
        local_img = download_image(p["image_url"], p["title"], idx)
        p["local_image"] = local_img or ""
        logger.info(
            "[%02d] %-35s | %-18s | %s",
            idx,
            (p["title"] or "(no name)")[:35],
            (p["price"] or "N/A")[:18],
            os.path.basename(local_img) if local_img else "(no image)",
        )
    logger.info("-" * 65)

    # Step 4: Export JSON
    json_path = os.path.join(OUTPUT_DIR, "products.json")
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(products, f, indent=2, ensure_ascii=False)
    logger.info("Saved JSON -> %s", json_path)

    # Step 5: Export CSV
    csv_path = os.path.join(OUTPUT_DIR, "products.csv")
    fieldnames = ["title", "price", "retail_price", "product_url", "image_url", "rating", "local_image"]
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
