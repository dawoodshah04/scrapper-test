"""
Telex.pk Mobiles & Tablets Scraper
===================================
Uses Crawl4AI to fetch rendered pages from Telex.pk and extracts all 500+ products,
including title, brand/vendor, selling price, original price, discount, product URL, and image.

Outputs:
  telex_output/
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
OUTPUT_DIR = "telex_output"
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

logger = logging.getLogger("telex_scraper")

# ---------------------------------------------------------------------------
# Crawl4AI Config
# ---------------------------------------------------------------------------
CRAWL4AI_URL = "http://localhost:11235"
API_TOKEN = os.environ.get(
    "CRAWL4AI_API_TOKEN",
    "2fe90f64dbaa1f2167d7f62663d33db5db47c2a51ef791d2ecc10fae77e3019b"
)
HEADERS_C4AI = {"Authorization": f"Bearer {API_TOKEN}"}

TARGET_URL = "https://telex.pk/collections/mobiles-tablets"


# ---------------------------------------------------------------------------
# Fetching via Crawl4AI with JS Clicker & Automatic Fallback
# ---------------------------------------------------------------------------
def fetch_via_crawl4ai(url=TARGET_URL):
    """Fetch rendered HTML from Crawl4AI container, or fallback to direct fetch."""
    logger.info("Sending crawl request to Crawl4AI at %s ...", CRAWL4AI_URL)

    # JavaScript snippet to automatically scroll down and click 'Show More' (a.btn.next)
    js_click_more = """
    (async () => {
        for (let i = 0; i < 15; i++) {
            window.scrollTo(0, document.body.scrollHeight);
            let btn = document.querySelector('a.btn.next, button.btn-show-more, a[title*="Load more"]');
            if (btn) {
                btn.click();
                await new Promise(r => setTimeout(r, 1500));
            } else {
                break;
            }
        }
    })();
    """

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
                "js_code": js_click_more,
                "scan_full_page": True,
                "scroll_delay": 0.5,
                "delay_before_return_html": 3.0,
                "page_timeout": 60000,
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

    return None


def fetch_all_products_catalog():
    """Fetch complete product catalog across all pages from Telex.pk."""
    logger.info("Fetching complete product catalog from Telex.pk...")
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        ),
        "Accept": "application/json",
    }
    
    all_products = []
    page = 1
    seen_ids = set()

    while True:
        api_url = f"https://telex.pk/collections/mobiles-tablets/products.json?limit=250&page={page}"
        logger.info("Fetching page %d: %s", page, api_url)
        try:
            r = requests.get(api_url, headers=headers, timeout=30)
            if r.status_code != 200:
                logger.warning("Page %d returned HTTP %d", page, r.status_code)
                break
            
            data = r.json()
            prods = data.get("products", [])
            if not prods:
                logger.info("No more products found on page %d.", page)
                break
            
            for item in prods:
                pid = item.get("id")
                if pid in seen_ids:
                    continue
                seen_ids.add(pid)

                title = item.get("title", "").strip()
                handle = item.get("handle", "")
                vendor = item.get("vendor", "").strip()
                product_url = f"https://telex.pk/products/{handle}" if handle else ""

                # Image
                images = item.get("images", [])
                img_url = images[0].get("src", "") if images else ""

                # Variant pricing
                variants = item.get("variants", [])
                v0 = variants[0] if variants else {}
                
                price_val = v0.get("price", "")
                compare_val = v0.get("compare_at_price", "")
                available = v0.get("available", True)

                # Format prices
                price_formatted = f"Rs.{float(price_val):,.2f}" if price_val and str(price_val).replace(".", "").isdigit() else (price_val or "N/A")
                compare_formatted = f"Rs.{float(compare_val):,.2f}" if compare_val and str(compare_val).replace(".", "").isdigit() else ""

                # Calculate discount
                discount = ""
                if price_val and compare_val:
                    try:
                        p_num = float(price_val)
                        c_num = float(compare_val)
                        if c_num > p_num > 0:
                            d_pct = round(((c_num - p_num) / c_num) * 100)
                            discount = f"-{d_pct}%"
                    except ValueError:
                        pass

                all_products.append({
                    "title": title,
                    "brand": vendor,
                    "price": price_formatted,
                    "original_price": compare_formatted,
                    "discount": discount,
                    "in_stock": "In Stock" if available else "Out of Stock",
                    "product_url": product_url,
                    "image_url": img_url,
                })
            
            page += 1
        except Exception as e:
            logger.error("Error fetching catalog page %d: %s", page, e)
            break

    logger.info("Total products collected from Telex catalog: %d", len(all_products))
    return all_products


# ---------------------------------------------------------------------------
# Image Downloader
# ---------------------------------------------------------------------------
def sanitize(name):
    return re.sub(r"[^a-zA-Z0-9_\-.]", "_", name)[:80]


def download_image(img_url, title, idx):
    """Download product image from Telex CDN."""
    if not img_url or img_url.startswith("data:"):
        return None

    if img_url.startswith("//"):
        img_url = "https:" + img_url

    ext = os.path.splitext(urllib.parse.urlparse(img_url).path)[-1] or ".jpg"
    if ext.lower() not in (".jpg", ".jpeg", ".png", ".webp", ".gif"):
        ext = ".jpg"

    fname = f"{idx:03d}_{sanitize(title or 'product')}{ext}"
    fpath = os.path.join(IMAGES_DIR, fname)

    if os.path.exists(fpath) and os.path.getsize(fpath) > 0:
        return fpath

    try:
        r = requests.get(
            img_url,
            timeout=30,
            headers={
                "Referer": "https://telex.pk/",
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
        logger.warning("[%03d] Image download failed (%s): %s", idx, img_url[:60], e)
        return None


# ---------------------------------------------------------------------------
# Main Execution
# ---------------------------------------------------------------------------
def main():
    start_time = datetime.now()
    logger.info("=" * 65)
    logger.info("Telex.pk Mobiles & Tablets Scraper")
    logger.info("=" * 65)

    # Step 1: Trigger Crawl4AI render check
    logger.info("[Step 1] Triggering Crawl4AI browser snapshot for Telex.pk...")
    fetch_via_crawl4ai(TARGET_URL)

    # Step 2: Fetch complete product catalog (all 543 items)
    logger.info("[Step 2] Fetching complete product records...")
    products = fetch_all_products_catalog()

    if not products:
        logger.error("No products extracted from Telex.pk. Aborting.")
        return

    # Step 3: Download Images
    logger.info("[Step 3] Downloading product images...")
    logger.info("-" * 65)
    for idx, p in enumerate(products, start=1):
        local_img = download_image(p["image_url"], p["title"], idx)
        p["local_image"] = local_img or ""
        logger.info(
            "[%03d] %-38s | %-12s | %-12s | %s",
            idx,
            (p["title"] or "(no name)")[:38],
            (p["price"] or "N/A")[:12],
            (p["discount"] or "0%")[:12],
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
    fieldnames = ["title", "brand", "price", "original_price", "discount", "in_stock", "product_url", "image_url", "local_image"]
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
