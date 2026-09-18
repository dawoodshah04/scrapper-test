"""
Multi-Site Pakistan Smartphones Scraper
========================================
Scrapes smartphone listings from multiple Pakistani e-commerce sites:

  1. PriceOye   — Server-rendered HTML (direct HTTP, no Crawl4AI needed)
  2. Xiaomi PK  — React SPA (requires Crawl4AI for JS rendering)
  3. Telemart   — Shopify-based (requires Crawl4AI for JS rendering)

Outputs:
  output/
    products.json           (combined from all sites)
    products.csv
    priceoye/
      products.json
      images/
      debug_raw.html
    xiaomi/
      products.json
      images/
      debug_raw.html
    telemart/
      products.json
      images/
      debug_raw.html
    scraper.log
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
OUTPUT_DIR = "output"
os.makedirs(OUTPUT_DIR, exist_ok=True)

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

logger = logging.getLogger("multi_scraper")

# ---------------------------------------------------------------------------
# Crawl4AI Config (used only for JS-rendered sites)
# ---------------------------------------------------------------------------
CRAWL4AI_URL = "http://localhost:11235"
API_TOKEN = os.environ.get(
    "CRAWL4AI_API_TOKEN",
    "2fe90f64dbaa1f2167d7f62663d33db5db47c2a51ef791d2ecc10fae77e3019b",
)
HEADERS_C4AI = {"Authorization": f"Bearer {API_TOKEN}"}

# Common HTTP headers for direct requests
HTTP_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}

# ---------------------------------------------------------------------------
# Site Configurations
# ---------------------------------------------------------------------------
SITES = {
    "priceoye": {
        "name": "PriceOye",
        "url": "https://priceoye.pk/mobiles",
        "method": "direct",          # Server-rendered, no Crawl4AI needed
        "pages": 5,                   # Number of pages to scrape
    },
    "xiaomi": {
        "name": "Xiaomi Pakistan",
        "url": "https://www.mi.com/pk/product-list?categoryId=2",
        "method": "crawl4ai",         # React SPA, needs JS rendering
        "wait_for": "css:.product-list-item",
        "delay": 5.0,
    },
    "telemart": {
        "name": "Telemart",
        "url": "https://telemart.pk/collections/smartphones",
        "method": "crawl4ai",         # Shopify, needs JS rendering
        "wait_for": "css:.product-card",
        "delay": 3.0,
    },
}


# ===================================================================
# FETCH STRATEGIES
# ===================================================================

def fetch_direct(url, site_dir):
    """Fetch page via direct HTTP request (for server-rendered sites)."""
    logger.info("  Direct HTTP GET: %s", url)
    try:
        resp = requests.get(url, headers=HTTP_HEADERS, timeout=30)
        resp.raise_for_status()
        html = resp.text
        # Save debug snapshot
        debug_path = os.path.join(site_dir, "debug_raw.html")
        with open(debug_path, "w", encoding="utf-8") as f:
            f.write(html)
        logger.info("  Saved HTML snapshot (%d bytes) -> %s", len(html), debug_path)
        return html
    except Exception as e:
        logger.error("  Direct fetch failed: %s", e)
        return None


def fetch_via_crawl4ai(url, wait_for=None, delay=5.0, site_dir="."):
    """Fetch fully rendered HTML from Crawl4AI container."""
    logger.info("  Crawl4AI request: %s", url)
    crawler_params = {
        "cache_mode": "BYPASS",
        "word_count_threshold": 0,
        "scan_full_page": True,
        "scroll_delay": 0.5,
        "delay_before_return_html": delay,
        "page_timeout": 90000,
        "remove_overlay_elements": True,
    }
    if wait_for:
        crawler_params["wait_for"] = wait_for

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
            "params": crawler_params,
        },
    }

    try:
        resp = requests.post(
            f"{CRAWL4AI_URL}/crawl",
            json=payload,
            headers=HEADERS_C4AI,
            timeout=300,
        )
        if resp.status_code != 200:
            logger.error("  Crawl4AI HTTP %d: %s", resp.status_code, resp.text[:300])
            return None
        data = resp.json()
        results = data.get("results") or data.get("result") or [data]
        if isinstance(results, dict):
            results = [results]
        html = results[0].get("html") or ""
        if html:
            debug_path = os.path.join(site_dir, "debug_raw.html")
            with open(debug_path, "w", encoding="utf-8") as f:
                f.write(html)
            logger.info("  Saved HTML snapshot (%d bytes) -> %s", len(html), debug_path)
        return html
    except requests.ConnectionError:
        logger.warning("  Crawl4AI not available at %s — skipping JS-rendered site", CRAWL4AI_URL)
        return None
    except Exception as e:
        logger.error("  Crawl4AI request failed: %s", e)
        return None


# ===================================================================
# PARSERS — one per site
# ===================================================================

def parse_priceoye(html):
    """
    Parse PriceOye product cards.

    Structure:
      div.productBox > a[href]
        div.image-box > img.product-thumbnail  (src / srcset)
        h4.p-title                             (product name)
        div.price-box > span                   (current price)
        div.price-diff-retail > span           (original price)
        div.user-rating-content                (rating + reviews)
    """
    soup = BeautifulSoup(html, "html.parser")
    cards = soup.select("div.productBox")
    logger.info("  Found %d product cards.", len(cards))

    products = []
    for card in cards:
        try:
            # --- Title ---
            title_el = card.select_one("h4.p-title")
            title = title_el.get_text(strip=True) if title_el else ""

            # --- Product URL ---
            link = card.select_one("a[href]")
            product_url = ""
            if link:
                href = link.get("href", "")
                if href.startswith("/"):
                    product_url = f"https://priceoye.pk{href}"
                elif href.startswith("http"):
                    product_url = href

            # --- Price ---
            price = ""
            price_el = card.select_one("div.price-box span")
            if price_el:
                price = price_el.get_text(strip=True)

            # --- Original price ---
            original_price = ""
            orig_el = card.select_one("div.price-diff-retail span")
            if orig_el:
                original_price = orig_el.get_text(strip=True)

            # --- Discount ---
            discount = ""
            disc_el = card.select_one("div.price-diff-saving")
            if disc_el:
                discount = disc_el.get_text(strip=True)

            # --- Image ---
            image_url = ""
            img = card.select_one("img.product-thumbnail")
            if img:
                # Prefer the 270x270 srcset variant for better quality
                srcset = img.get("srcset", "")
                if srcset:
                    # Parse srcset — take the 2x image if available
                    parts = [s.strip() for s in srcset.split(",")]
                    for part in parts:
                        tokens = part.split()
                        if len(tokens) >= 2 and "2x" in tokens[1]:
                            image_url = tokens[0]
                            break
                    if not image_url and parts:
                        image_url = parts[0].split()[0]
                if not image_url:
                    image_url = img.get("src", "")

            # --- Rating ---
            rating = ""
            review_count = ""
            rating_box = card.select_one("div.user-rating-content")
            if rating_box:
                rating_span = rating_box.select_one("span.h6")
                if rating_span:
                    rating = rating_span.get_text(strip=True)
                review_spans = rating_box.select("span.rating-h7")
                if review_spans:
                    review_count = review_spans[0].get_text(strip=True)

            if not title:
                continue

            products.append({
                "title": title,
                "price": price,
                "original_price": original_price,
                "discount": discount,
                "rating": rating,
                "review_count": review_count,
                "product_url": product_url,
                "image_url": image_url,
                "source": "priceoye",
            })
        except Exception as e:
            logger.warning("  Error parsing PriceOye card: %s", e)
            continue

    logger.info("  Extracted %d products from PriceOye.", len(products))
    return products


def parse_xiaomi(html):
    """
    Parse Xiaomi PK product list page.

    The Xiaomi site is a React SPA. After JS renders, the product grid
    typically uses class names like .product-list-item, .product-card, etc.
    We try multiple selector strategies since Xiaomi frequently updates
    their class names.
    """
    soup = BeautifulSoup(html, "html.parser")

    # Try multiple possible selectors (Xiaomi changes these often)
    selectors = [
        ".product-list-item",
        ".product-card",
        "[class*='productItem']",
        "[class*='product-item']",
        "[class*='ProductCard']",
        ".product-catalogue__item",
    ]

    cards = []
    for sel in selectors:
        cards = soup.select(sel)
        if cards:
            logger.info("  Xiaomi: matched selector '%s' with %d cards", sel, len(cards))
            break

    if not cards:
        logger.warning("  Xiaomi: No product cards found with known selectors.")
        logger.warning("  Check debug_raw.html for current class names.")
        return []

    products = []
    for card in cards:
        try:
            # Title
            title_el = card.select_one("h2, h3, [class*='name'], [class*='title']")
            title = title_el.get_text(strip=True) if title_el else ""

            # Price
            price_el = card.select_one("[class*='price'], [class*='Price']")
            price = price_el.get_text(strip=True) if price_el else ""

            # Link
            link = card.select_one("a[href]")
            product_url = ""
            if link:
                href = link.get("href", "")
                if href.startswith("/"):
                    product_url = f"https://www.mi.com{href}"
                elif href.startswith("http"):
                    product_url = href

            # Image
            img = card.select_one("img")
            image_url = ""
            if img:
                image_url = img.get("data-src") or img.get("src") or ""
                if image_url.startswith("//"):
                    image_url = f"https:{image_url}"

            if not title:
                continue

            products.append({
                "title": title,
                "price": price,
                "original_price": "",
                "discount": "",
                "rating": "",
                "review_count": "",
                "product_url": product_url,
                "image_url": image_url,
                "source": "xiaomi",
            })
        except Exception as e:
            logger.warning("  Error parsing Xiaomi card: %s", e)
            continue

    logger.info("  Extracted %d products from Xiaomi.", len(products))
    return products


def parse_telemart(html):
    """
    Parse Telemart (Shopify-based) product cards.

    Shopify typically uses .product-card or .grid__item elements.
    We try multiple selectors for robustness.
    """
    soup = BeautifulSoup(html, "html.parser")

    selectors = [
        ".product-card",
        ".grid__item .card",
        ".product-grid-item",
        "[class*='product-card']",
        ".collection-product-card",
        ".grid__item",
    ]

    cards = []
    for sel in selectors:
        cards = soup.select(sel)
        if cards:
            logger.info("  Telemart: matched selector '%s' with %d cards", sel, len(cards))
            break

    if not cards:
        logger.warning("  Telemart: No product cards found with known selectors.")
        logger.warning("  Check debug_raw.html for current class names.")
        return []

    products = []
    for card in cards:
        try:
            # Title
            title_el = card.select_one(
                ".product-card__title, .card__heading, "
                "h3, h2, [class*='title'], [class*='name']"
            )
            title = title_el.get_text(strip=True) if title_el else ""

            # Price
            price_el = card.select_one(
                ".price__regular .price-item, .price-item--regular, "
                ".product-card__price, [class*='price'], .money"
            )
            price = price_el.get_text(strip=True) if price_el else ""

            # Link
            link = card.select_one("a[href]")
            product_url = ""
            if link:
                href = link.get("href", "")
                if href.startswith("/"):
                    product_url = f"https://telemart.pk{href}"
                elif href.startswith("http"):
                    product_url = href

            # Image
            img = card.select_one("img")
            image_url = ""
            if img:
                image_url = (
                    img.get("data-src")
                    or img.get("data-srcset", "").split(",")[0].split(" ")[0]
                    or img.get("src")
                    or ""
                )
                if image_url.startswith("//"):
                    image_url = f"https:{image_url}"

            if not title:
                continue

            products.append({
                "title": title,
                "price": price,
                "original_price": "",
                "discount": "",
                "rating": "",
                "review_count": "",
                "product_url": product_url,
                "image_url": image_url,
                "source": "telemart",
            })
        except Exception as e:
            logger.warning("  Error parsing Telemart card: %s", e)
            continue

    logger.info("  Extracted %d products from Telemart.", len(products))
    return products


# Mapping of site key -> parser function
PARSERS = {
    "priceoye": parse_priceoye,
    "xiaomi": parse_xiaomi,
    "telemart": parse_telemart,
}


# ===================================================================
# Image Downloader
# ===================================================================

def sanitize(name):
    return re.sub(r"[^a-zA-Z0-9_\-.]", "_", name)[:80]


def download_image(img_url, title, idx, images_dir, referer=""):
    """Download product image and return local path."""
    if not img_url or img_url.startswith("data:"):
        return None

    ext = os.path.splitext(urllib.parse.urlparse(img_url).path)[-1] or ".png"
    if ext.lower() not in (".jpg", ".jpeg", ".png", ".webp", ".gif"):
        ext = ".png"

    fname = f"{idx:03d}_{sanitize(title or 'product')}{ext}"
    fpath = os.path.join(images_dir, fname)

    if os.path.exists(fpath) and os.path.getsize(fpath) > 0:
        return fpath

    try:
        headers = dict(HTTP_HEADERS)
        if referer:
            headers["Referer"] = referer

        r = requests.get(img_url, timeout=30, headers=headers)
        r.raise_for_status()

        if len(r.content) < 500:
            logger.warning("  [%03d] Tiny image (%d bytes), skipping: %s", idx, len(r.content), img_url[:60])
            return None

        with open(fpath, "wb") as f:
            f.write(r.content)
        return fpath
    except Exception as e:
        logger.warning("  [%03d] Image download failed (%s): %s", idx, img_url[:60], e)
        return None


# ===================================================================
# Per-Site Scraper Orchestrator
# ===================================================================

def scrape_priceoye(config, site_dir):
    """
    Scrape PriceOye with pagination support.
    PriceOye URL pattern: /mobiles?page=N
    """
    all_products = []
    num_pages = config.get("pages", 3)

    for page_num in range(1, num_pages + 1):
        url = config["url"] if page_num == 1 else f"{config['url']}?page={page_num}"
        logger.info("  Page %d/%d: %s", page_num, num_pages, url)

        html = fetch_direct(url, site_dir)
        if not html:
            logger.warning("  Failed to fetch page %d, stopping pagination.", page_num)
            break

        products = parse_priceoye(html)
        if not products:
            logger.info("  No products on page %d, stopping pagination.", page_num)
            break

        all_products.extend(products)
        logger.info("  Running total: %d products", len(all_products))

    # Deduplicate by product_url
    seen = set()
    unique = []
    for p in all_products:
        key = p.get("product_url") or p.get("title")
        if key not in seen:
            seen.add(key)
            unique.append(p)

    logger.info("  After dedup: %d unique products", len(unique))
    return unique


def scrape_site(site_key):
    """Scrape a single site and return list of product dicts."""
    config = SITES[site_key]
    site_dir = os.path.join(OUTPUT_DIR, site_key)
    images_dir = os.path.join(site_dir, "images")
    os.makedirs(images_dir, exist_ok=True)

    logger.info("-" * 65)
    logger.info("Scraping: %s", config["name"])
    logger.info("-" * 65)

    # --- Fetch HTML ---
    if site_key == "priceoye":
        products = scrape_priceoye(config, site_dir)
    else:
        html = None
        method = config.get("method", "crawl4ai")

        if method == "direct":
            html = fetch_direct(config["url"], site_dir)
        elif method == "crawl4ai":
            html = fetch_via_crawl4ai(
                config["url"],
                wait_for=config.get("wait_for"),
                delay=config.get("delay", 5.0),
                site_dir=site_dir,
            )

        if not html:
            # Try cached snapshot fallback
            cache_path = os.path.join(site_dir, "debug_raw.html")
            if os.path.exists(cache_path):
                logger.info("  Loading cached HTML snapshot: %s", cache_path)
                with open(cache_path, "r", encoding="utf-8") as f:
                    html = f.read()
            else:
                logger.warning("  No HTML available for %s. Skipping.", config["name"])
                return []

        parser = PARSERS.get(site_key)
        if not parser:
            logger.error("  No parser registered for site: %s", site_key)
            return []

        products = parser(html)

    if not products:
        logger.warning("  No products extracted from %s.", config["name"])
        return []

    # --- Download Images ---
    logger.info("  Downloading images for %s ...", config["name"])
    referer_map = {
        "priceoye": "https://priceoye.pk/",
        "xiaomi": "https://www.mi.com/",
        "telemart": "https://telemart.pk/",
    }
    referer = referer_map.get(site_key, "")

    for idx, p in enumerate(products, start=1):
        local_img = download_image(
            p["image_url"], p["title"], idx, images_dir, referer=referer
        )
        p["local_image"] = local_img

    # --- Save per-site JSON ---
    json_path = os.path.join(site_dir, "products.json")
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(products, f, indent=2, ensure_ascii=False)
    logger.info("  Saved %s -> %s", config["name"], json_path)

    return products


# ===================================================================
# Main
# ===================================================================

def main():
    start_time = datetime.now()
    logger.info("=" * 65)
    logger.info("Multi-Site Pakistan Smartphones Scraper")
    logger.info("=" * 65)
    logger.info("Sites configured: %s", ", ".join(s["name"] for s in SITES.values()))
    logger.info("=" * 65)

    all_products = []

    for site_key in SITES:
        try:
            products = scrape_site(site_key)
            all_products.extend(products)
            logger.info("  => %s: %d products collected", SITES[site_key]["name"], len(products))
        except Exception as e:
            logger.error("  => %s FAILED: %s", SITES[site_key]["name"], e)
            continue

    logger.info("=" * 65)

    if not all_products:
        logger.error("No products extracted from any site!")
        return

    # --- Save combined JSON ---
    json_path = os.path.join(OUTPUT_DIR, "products.json")
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(all_products, f, indent=2, ensure_ascii=False)
    logger.info("Combined JSON -> %s  (%d products)", json_path, len(all_products))

    # --- Save combined CSV ---
    csv_path = os.path.join(OUTPUT_DIR, "products.csv")
    fieldnames = [
        "source", "title", "price", "original_price", "discount",
        "rating", "review_count", "product_url", "image_url", "local_image",
    ]
    with open(csv_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(all_products)
    logger.info("Combined CSV  -> %s", csv_path)

    # --- Summary ---
    downloaded = sum(1 for p in all_products if p.get("local_image"))
    duration = (datetime.now() - start_time).seconds

    by_source = {}
    for p in all_products:
        src = p.get("source", "unknown")
        by_source[src] = by_source.get(src, 0) + 1

    logger.info("=" * 65)
    logger.info("SUMMARY")
    logger.info("-" * 65)
    for src, count in sorted(by_source.items()):
        logger.info("  %-15s : %d products", src, count)
    logger.info("-" * 65)
    logger.info("Total Products  : %d", len(all_products))
    logger.info("Images Saved    : %d", downloaded)
    logger.info("JSON            : %s", json_path)
    logger.info("CSV             : %s", csv_path)
    logger.info("Time Elapsed    : %ds", duration)
    logger.info("=" * 65)


if __name__ == "__main__":
    main()
