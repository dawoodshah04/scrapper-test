"""
Master Multi-Site E-Commerce Scraper
====================================
Unified scraper for Samsung, PriceOye, Telex, and other mobile & tech websites.

Usage:
  python main_scraper.py --url <TARGET_URL>

Examples:
  python main_scraper.py --url https://www.samsung.com/pk/smartphones/all-smartphones/
  python main_scraper.py --url https://priceoye.pk/mobiles
  python main_scraper.py --url https://telex.pk/collections/mobiles-tablets

Features:
  1. Automatic website detection and tagging ('website' field in products.json).
  2. Central output directory: output/products.json & output/products.csv.
  3. Incremental image caching: skips downloading images if already saved locally.
  4. High-speed multi-threaded parallel image downloader.
"""

import os
import re
import csv
import json
import hashlib
import argparse
import logging
import urllib.parse
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor, as_completed
import requests
from bs4 import BeautifulSoup

# ---------------------------------------------------------------------------
# Directories & Logging Setup
# ---------------------------------------------------------------------------
OUTPUT_DIR = "output"
IMAGES_DIR = os.path.join(OUTPUT_DIR, "images")
os.makedirs(IMAGES_DIR, exist_ok=True)

MASTER_JSON = os.path.join(OUTPUT_DIR, "products.json")
MASTER_CSV = os.path.join(OUTPUT_DIR, "products.csv")
SITE_CACHE_FILE = os.path.join(OUTPUT_DIR, "site_cache.json")
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

logger = logging.getLogger("master_scraper")

# Crawl4AI Config
CRAWL4AI_URL = "http://localhost:11235"
API_TOKEN = os.environ.get(
    "CRAWL4AI_API_TOKEN",
    "2fe90f64dbaa1f2167d7f62663d33db5db47c2a51ef791d2ecc10fae77e3019b"
)
HEADERS_C4AI = {"Authorization": f"Bearer {API_TOKEN}"}


# ---------------------------------------------------------------------------
# Site Cache & Hash Helper Functions (Method 1: Content Hash Comparison)
# ---------------------------------------------------------------------------
def load_site_cache():
    """Load URL hash cache from site_cache.json."""
    if os.path.exists(SITE_CACHE_FILE):
        try:
            with open(SITE_CACHE_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            logger.warning("Error loading site cache (%s): %s", SITE_CACHE_FILE, e)
    return {}


def save_site_cache(cache):
    """Save URL hash cache to site_cache.json."""
    with open(SITE_CACHE_FILE, "w", encoding="utf-8") as f:
        json.dump(cache, f, indent=2, ensure_ascii=False)
    logger.info("Updated site cache -> %s", SITE_CACHE_FILE)


def compute_html_hash(html):
    """Compute MD5 hash digest of raw HTML content."""
    return hashlib.md5(html.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Page Fetcher (Crawl4AI container with HTTP fallback)
# ---------------------------------------------------------------------------
def fetch_page(url, js_code=None):
    """Fetch rendered HTML using Crawl4AI REST service, or fallback to requests."""
    logger.info("Fetching page via Crawl4AI: %s ...", url)

    crawler_params = {
        "cache_mode": "BYPASS",
        "word_count_threshold": 0,
        "wait_for": "css:body",
        "wait_for_timeout": 5000,
        "scan_full_page": True,
        "scroll_delay": 0.5,
        "delay_before_return_html": 3.0,
        "page_timeout": 60000,
        "remove_overlay_elements": True,
    }
    if js_code:
        crawler_params["js_code"] = js_code

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
        resp = requests.post(f"{CRAWL4AI_URL}/crawl", json=payload, headers=HEADERS_C4AI, timeout=(20, 300))
        if resp.status_code == 200:
            data = resp.json()
            results = data.get("results") or data.get("result") or [data]
            if isinstance(results, dict):
                results = [results]
            res_obj = results[0] if isinstance(results, list) and len(results) > 0 else {}
            html = res_obj.get("html") or ""
            if html and len(html) > 500:
                logger.info("Crawl4AI rendered HTML (%d bytes)", len(html))
                return html
            else:
                logger.warning("Crawl4AI returned empty or short HTML (%d bytes). Error: %s", len(html), res_obj.get("error"))
        else:
            logger.warning("Crawl4AI API returned HTTP %d: %s", resp.status_code, resp.text[:200])
    except Exception as e:
        logger.warning("Crawl4AI unavailable (%s). Falling back to direct HTTP...", e)

    # Fallback direct HTTP
    try:
        headers = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
            )
        }
        r = requests.get(url, headers=headers, timeout=30)
        if r.status_code == 200:
            logger.info("Direct HTTP fetch succeeded (%d bytes)", len(r.text))
            return r.text
    except Exception as e:
        logger.error("Direct HTTP fetch failed: %s", e)

    return None


# ---------------------------------------------------------------------------
# Website Extractor Router & Parsers
# ---------------------------------------------------------------------------
def detect_website(url):
    """Identify website name from URL hostname."""
    domain = urllib.parse.urlparse(url).netloc.lower()
    if "samsung.com" in domain:
        return "Samsung"
    elif "priceoye.pk" in domain:
        return "PriceOye"
    elif "telex.pk" in domain:
        return "Telex"
    elif "mega.pk" in domain:
        return "Mega"
    else:
        # Fallback to domain name root
        parts = domain.replace("www.", "").split(".")
        return parts[0].capitalize() if parts else "Unknown"


def extract_samsung(html, url):
    """Extract Samsung products from rendered HTML / JSON-LD."""
    soup = BeautifulSoup(html, "html.parser")
    products = []

    # Try DOM cards
    cards = soup.select("li.pd21-product-card__item:not(.pd21-product-card__banner)")
    for card in cards:
        title = ""
        for sel in [".pd21-product-card__name", "a[data-modelname]", "[data-modeldisplay]", "h3"]:
            el = card.select_one(sel)
            if el:
                t = el.get("data-modeldisplay") or el.get("data-modelname") or el.get_text(strip=True)
                if t and len(t) > 2:
                    title = t
                    break
        price = ""
        for sel in [".pd21-product-card__price-main", ".price-ux__price-current", "[data-pricetext]"]:
            el = card.select_one(sel)
            if el:
                p = el.get("data-pricetext") or el.get_text(" ", strip=True)
                if p and p not in ("null", "undefined"):
                    price = p
                    break
        link = card.select_one("a[href]")
        p_url = f"https://www.samsung.com{link.get('href')}" if link and link.get("href", "").startswith("/") else (link.get("href", "") if link else "")
        img = ""
        img_el = card.select_one("img")
        if img_el:
            src = img_el.get("data-desktop-src") or img_el.get("src") or ""
            if src and not src.startswith("data:"):
                img = f"https:{src}" if src.startswith("//") else (f"https://images.samsung.com{src}" if src.startswith("/") else src)

        if title or p_url:
            products.append({
                "website": "Samsung",
                "title": title,
                "price": price or "Coming Soon",
                "original_price": "",
                "discount": "",
                "rating": "",
                "product_url": p_url,
                "image_url": img,
            })

    # JSON-LD fallback
    if not products:
        for s in soup.find_all("script", type="application/ld+json"):
            if not s.string:
                continue
            try:
                data = json.loads(s.string)
                items = data.get("itemListElement", []) if data.get("@type") == "ItemList" else ([data] if data.get("@type") == "Product" else [])
                for pos in items:
                    item = pos.get("item", {}) if isinstance(pos, dict) and pos.get("item") else (pos if isinstance(pos, dict) and pos.get("@type") == "Product" else {})
                    if item:
                        t = item.get("name", "").strip()
                        u = item.get("url") or item.get("@id") or ""
                        img_val = item.get("image", "")
                        if isinstance(img_val, list) and img_val:
                            img_val = img_val[0]
                        if isinstance(img_val, str) and img_val.startswith("//"):
                            img_val = "https:" + img_val
                        offers = item.get("offers") or {}
                        p_val = offers.get("price", "")
                        curr = offers.get("priceCurrency", "PKR")
                        p_str = f"{curr} {int(p_val):,}" if str(p_val).isdigit() else (str(p_val) or "N/A")
                        
                        agg_rating = item.get("aggregateRating") or {}
                        r_val = str(agg_rating.get("ratingValue") or "").strip()

                        if t or u:
                            products.append({
                                "website": "Samsung",
                                "title": t,
                                "price": p_str,
                                "original_price": "",
                                "discount": "",
                                "rating": r_val,
                                "product_url": u,
                                "image_url": img_val,
                            })
            except Exception:
                continue

    return products


def extract_priceoye(html, url):
    """Extract PriceOye products from DOM cards and embedded script JSON."""
    soup = BeautifulSoup(html, "html.parser")
    products = []

    # DOM cards
    cards = soup.select(".productBox, .b-productBox, div[class*='productBox']")
    seen_urls = set()
    for card in cards:
        link = card.select_one("a[href]")
        if not link:
            continue
        href = link.get("href", "")
        p_url = f"https://priceoye.pk{href}" if href.startswith("/") else href
        if p_url in seen_urls:
            continue
        seen_urls.add(p_url)

        title_el = card.select_one(".p-title, .product-title, [class*='title']") or link
        title = title_el.get("data-vars-value") or title_el.get_text(strip=True) if title_el else ""
        title = re.sub(r"\s+", " ", title).strip()

        img_el = card.select_one("img.product-thumbnail-img, img.product-thumbnail, img")
        img = ""
        if img_el:
            src = img_el.get("src") or img_el.get("data-src") or ""
            if src and not src.startswith("data:"):
                img = f"https:{src}" if src.startswith("//") else src

        price_el = card.select_one(".price-box, .p-price, [class*='price']")
        price = price_el.get_text(" ", strip=True) if price_el else "Check Website"
        m = re.search(r"Rs\.?\s*[\d,]+", price, re.I)
        if m:
            price = m.group(0)

        retail_el = card.select_one(".retail-price, .old-price, strike, del")
        retail_price = retail_el.get_text(strip=True) if retail_el else ""

        rating_el = card.select_one(".rating, .p-rating, [class*='rating']")
        rating_raw = rating_el.get_text(" ", strip=True) if rating_el else ""
        m_rate = re.search(r"(\d+(?:\.\d+)?)", rating_raw)
        rating = m_rate.group(1) if m_rate else ""

        products.append({
            "website": "PriceOye",
            "title": title,
            "price": price,
            "original_price": retail_price,
            "discount": "",
            "rating": rating,
            "product_url": p_url,
            "image_url": img,
        })

    # Parse all embedded script JSON products (ensures complete 177+ product extraction including Samsung, etc.)
    matches = re.findall(
        r'\"product_id\":\s*(\d+).*?\"product_title\":\s*\"([^\"]+)\".*?\"product_image\":\s*\"([^\"]+)\".*?\"product_slug\":\s*\"([^\"]+)\".*?\"average_rating\":\s*([\d\.]+).*?\"lowest_price\":\s*(\d+)',
        html,
    )
    for pid, title, img_raw, slug, rating_val, price_val in matches:
        p_url = f"https://priceoye.pk/mobiles/{slug}"
        if p_url not in seen_urls:
            seen_urls.add(p_url)
            p_fmt = f"Rs {int(price_val):,}" if price_val.isdigit() else price_val
            
            img_match = re.search(r'\"0\":\s*\"([^\"]+)\"', img_raw)
            img_file = img_match.group(1) if img_match else ""
            img_url = f"https://images.priceoye.pk/{img_file}" if img_file else ""

            products.append({
                "website": "PriceOye",
                "title": title.strip(),
                "price": p_fmt,
                "original_price": "",
                "discount": "",
                "rating": str(rating_val or "").strip(),
                "product_url": p_url,
                "image_url": img_url,
            })

    return products


def extract_telex(html, url):
    """Extract Telex products from Shopify collection API across all pages."""
    products = []
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
        ),
        "Accept": "application/json",
    }
    page = 1
    seen_ids = set()

    while True:
        api_url = f"https://telex.pk/collections/mobiles-tablets/products.json?limit=250&page={page}"
        try:
            r = requests.get(api_url, headers=headers, timeout=30)
            if r.status_code != 200:
                break
            prods = r.json().get("products", [])
            if not prods:
                break

            for item in prods:
                pid = item.get("id")
                if pid in seen_ids:
                    continue
                seen_ids.add(pid)

                title = item.get("title", "").strip()
                handle = item.get("handle", "")
                p_url = f"https://telex.pk/products/{handle}" if handle else ""

                images = item.get("images", [])
                img_url = images[0].get("src", "") if images else ""

                variants = item.get("variants", [])
                v0 = variants[0] if variants else {}
                price_val = v0.get("price", "")
                compare_val = v0.get("compare_at_price", "")

                price_fmt = f"Rs.{float(price_val):,.2f}" if price_val and str(price_val).replace(".", "").isdigit() else (price_val or "N/A")
                compare_fmt = f"Rs.{float(compare_val):,.2f}" if compare_val and str(compare_val).replace(".", "").isdigit() else ""

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

                products.append({
                    "website": "Telex",
                    "title": title,
                    "price": price_fmt,
                    "original_price": compare_fmt,
                    "discount": discount,
                    "rating": "",
                    "product_url": p_url,
                    "image_url": img_url,
                })
            page += 1
        except Exception:
            break

    return products


def extract_mega_page(soup):
    """Extract product items from a single Mega.pk page BeautifulSoup object."""
    items = []
    boxes = soup.select(".lap_thu_box")
    for box in boxes:
        # Title & Link
        title_el = (
            box.select_one("#lap_name_div h3 a")
            or box.select_one("h3 a")
            or box.select_one("a[href*='mobiles_products']")
        )
        title = title_el.text.strip() if title_el else ""
        p_url = title_el.get("href", "") if title_el else ""
        if p_url and not p_url.startswith("http"):
            p_url = f"https://www.mega.pk{p_url}"

        # Image
        img_el = box.select_one(".image img") or box.select_one("img")
        img_url = ""
        if img_el:
            src = img_el.get("src") or img_el.get("data-src") or ""
            if src and not src.startswith("http"):
                img_url = f"https://www.mega.pk{src}" if src.startswith("/") else f"https://www.mega.pk/{src}"
            else:
                img_url = src

        # Discount
        disc_el = box.select_one(".discount")
        discount = disc_el.text.strip() if disc_el else ""
        if discount and "off" in discount.lower():
            discount = "-" + re.sub(r"[^\d%]", "", discount)

        # Price & Original Price
        price_box = box.select_one(".cat_price")
        price = ""
        was_price = ""
        if price_box:
            price_box_copy = BeautifulSoup(str(price_box), "html.parser")
            was_el = price_box_copy.select_one(".was")
            if was_el:
                was_price = was_el.text.strip().replace(" - PKR", "").replace("PKR", "").strip()
                was_el.decompose()
            raw_price = price_box_copy.text.strip().replace(" - PKR", "").replace("PKR", "").strip()
            price = raw_price

        if price:
            price = f"Rs. {price}"
        if was_price:
            was_price = f"Rs. {was_price}"

        # Rating
        rating = ""
        rating_el = box.select_one(".rating, [class*='star'], [class*='rating']")
        if rating_el:
            rating = rating_el.text.strip()

        if title or p_url:
            items.append({
                "website": "Mega",
                "title": title,
                "price": price or "Check Price",
                "original_price": was_price,
                "discount": discount,
                "rating": rating,
                "product_url": p_url,
                "image_url": img_url,
            })
    return items


def extract_mega(html, url):
    """Extract products from Mega.pk rendered HTML across all pagination pages."""
    products = []
    seen_urls = set()

    # Parse Page 1
    soup1 = BeautifulSoup(html, "html.parser")
    p1_items = extract_mega_page(soup1)
    for p in p1_items:
        if p["product_url"] not in seen_urls:
            seen_urls.add(p["product_url"])
            products.append(p)

    logger.info("Mega Page 1: extracted %d products.", len(p1_items))

    # Base URL for pagination
    clean_url = url.split("?")[0].rstrip("/")
    if re.search(r"/\d+$", clean_url):
        clean_url = re.sub(r"/\d+$", "", clean_url)

    # Handle pagination pages 2, 3, 4, ...
    page = 2
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
        )
    }

    while page <= 25:
        page_url = f"{clean_url}/{page}/"
        try:
            r = requests.get(page_url, headers=headers, timeout=15)
            if r.status_code != 200:
                break
            soup = BeautifulSoup(r.text, "html.parser")
            page_items = extract_mega_page(soup)
            if not page_items:
                break

            added_count = 0
            for p in page_items:
                if p["product_url"] not in seen_urls:
                    seen_urls.add(p["product_url"])
                    products.append(p)
                    added_count += 1

            logger.info("Mega Page %d: extracted %d products (total so far: %d).", page, len(page_items), len(products))
            if added_count == 0:
                break
            page += 1
        except Exception as e:
            logger.warning("Error fetching Mega page %d (%s): %s", page, page_url, e)
            break

    return products


def extract_products(website, html, url):
    """Route extraction logic by website name."""
    if website == "Samsung":
        return extract_samsung(html, url)
    elif website == "PriceOye":
        return extract_priceoye(html, url)
    elif website == "Telex":
        return extract_telex(html, url)
    elif website == "Mega":
        return extract_mega(html, url)
    else:
        # Generic fallback attempt using Mega or PriceOye extractors
        logger.info("Using generic extractor for website: %s", website)
        prods = extract_mega(html, url)
        return prods if prods else extract_priceoye(html, url)


# ---------------------------------------------------------------------------
# Incremental Image Downloader (Multi-Threaded & Cross-Site Cached)
# ---------------------------------------------------------------------------
def normalize_model_key(title):
    """Normalize product title into a clean model key for cross-site matching."""
    t = (title or "").lower()
    t = re.sub(r"\(.*?\)", "", t)     # Strip parenthetical specs like (8gb/256gb)
    t = re.sub(r"[^a-z0-9]", "_", t)  # Keep alphanumeric
    t = re.sub(r"_+", "_", t).strip("_")
    return t[:60] or "product"


def find_existing_model_image(model_key):
    """Check if an image for this product model already exists in IMAGES_DIR."""
    if not os.path.exists(IMAGES_DIR) or not model_key:
        return None
    for fname in os.listdir(IMAGES_DIR):
        base, ext = os.path.splitext(fname)
        if base == model_key and ext.lower() in (".jpg", ".jpeg", ".png", ".webp", ".gif", ".svg"):
            fpath = os.path.join(IMAGES_DIR, fname)
            if os.path.getsize(fpath) > 0:
                return fpath
    return None


def download_single_image(args):
    """Download a single image file if not already downloaded by any website."""
    img_url, title, website, idx = args
    if not img_url or img_url.startswith("data:"):
        return None

    if img_url.startswith("//"):
        img_url = "https:" + img_url

    model_key = normalize_model_key(title)
    existing_file = find_existing_model_image(model_key)
    if existing_file:
        return existing_file

    ext = os.path.splitext(urllib.parse.urlparse(img_url).path)[-1] or ".jpg"
    if ext.lower() not in (".jpg", ".jpeg", ".png", ".webp", ".gif", ".svg"):
        ext = ".jpg"

    fname = f"{model_key}{ext}"
    fpath = os.path.join(IMAGES_DIR, fname)

    if os.path.exists(fpath) and os.path.getsize(fpath) > 0:
        return fpath

    try:
        r = requests.get(
            img_url,
            timeout=20,
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
                )
            },
        )
        r.raise_for_status()
        with open(fpath, "wb") as f:
            f.write(r.content)
        return fpath
    except Exception as e:
        logger.warning("Image download failed (%s): %s", img_url[:60], e)
        return None


def download_images_parallel(products, website, max_workers=15):
    """Download images concurrently for items missing local images (cross-site cached)."""
    to_download = []

    for idx, p in enumerate(products, start=1):
        model_key = normalize_model_key(p.get("title", ""))
        existing_local = p.get("local_image", "") or find_existing_model_image(model_key)
        
        if existing_local and os.path.exists(existing_local) and os.path.getsize(existing_local) > 0:
            p["local_image"] = existing_local
            continue

        if p.get("image_url"):
            to_download.append((p, idx))

    if not to_download:
        logger.info("All images already cached on disk across websites! Skipping download step.")
        return

    logger.info("Downloading %d new/missing images using %d threads...", len(to_download), max_workers)
    
    download_args = [
        (p["image_url"], p["title"], website, idx)
        for p, idx in to_download
    ]

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {
            executor.submit(download_single_image, arg): p
            for arg, (p, _) in zip(download_args, to_download)
        }
        for future in as_completed(futures):
            p = futures[future]
            res = future.result()
            if res:
                p["local_image"] = res


# ---------------------------------------------------------------------------
# Master Data Storage (products.json & products.csv)
# ---------------------------------------------------------------------------
def load_master_database():
    """Load existing products.json into a dict keyed by product_url."""
    db = {}
    if os.path.exists(MASTER_JSON):
        try:
            with open(MASTER_JSON, "r", encoding="utf-8") as f:
                records = json.load(f)
                for item in records:
                    url = item.get("product_url")
                    if url:
                        db[url] = item
            logger.info("Loaded %d existing product records from %s", len(db), MASTER_JSON)
        except Exception as e:
            logger.warning("Error loading master database (%s): %s", MASTER_JSON, e)
    return db


def save_master_database(db):
    """Save combined products dict to products.json and products.csv."""
    records = list(db.values())

    # Write master JSON
    with open(MASTER_JSON, "w", encoding="utf-8") as f:
        json.dump(records, f, indent=2, ensure_ascii=False)
    logger.info("Updated master database -> %s (%d items)", MASTER_JSON, len(records))

    # Write master CSV
    fieldnames = ["website", "title", "price", "original_price", "discount", "rating", "product_url", "image_url", "local_image"]
    with open(MASTER_CSV, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(records)
    logger.info("Updated master CSV export -> %s", MASTER_CSV)


# ---------------------------------------------------------------------------
# Main Routine
# ---------------------------------------------------------------------------
def main():
    parser = argparse.ArgumentParser(description="Master Multi-Site E-Commerce Scraper")
    parser.add_argument("url", nargs="?", help="Target URL to scrape (e.g. https://priceoye.pk/mobiles)")
    parser.add_argument("--url", dest="url_opt", help="Target URL (alternative option)")
    parser.add_argument("--workers", type=int, default=15, help="Number of parallel image download threads")
    parser.add_argument("--force", action="store_true", help="Bypass content hash cache and force re-scraping")

    args = parser.parse_args()
    target_url = args.url or args.url_opt

    if not target_url:
        logger.error("No URL provided. Usage: python main_scraper.py <URL>")
        logger.error("Example: python main_scraper.py https://priceoye.pk/mobiles")
        return

    website = detect_website(target_url)
    logger.info("=" * 65)
    logger.info("Master E-Commerce Scraper")
    logger.info("Target URL : %s", target_url)
    logger.info("Website    : %s", website)
    logger.info("=" * 65)

    # 1. Load Master Database & Site Cache
    master_db = load_master_database()
    site_cache = load_site_cache()

    # 2. Fetch HTML
    html = fetch_page(target_url)
    if not html:
        logger.error("Failed to retrieve HTML. Aborting.")
        return

    # 3. Content Hash Check (Method 1: Content Hash Comparison)
    current_hash = compute_html_hash(html)
    cached_info = site_cache.get(target_url, {})
    previous_hash = cached_info.get("content_hash")

    if previous_hash == current_hash and not args.force:
        logger.info("=" * 65)
        logger.info("✅ NO CHANGES DETECTED on page for %s", target_url)
        logger.info("        Content Hash: %s", current_hash)
        logger.info("        Last Scraped: %s", cached_info.get("last_scraped", "N/A"))
        logger.info("Skipping full parsing, item updates, and image downloads.")
        logger.info("(Use --force flag to force re-parsing all products)")
        logger.info("=" * 65)
        return

    if args.force:
        logger.info("🔄 FORCED RE-SCRAPE (--force flag passed). Bypassing content hash cache.")
    else:
        logger.info("⚠️ PAGE MODIFIED or First Run (Hash: %s -> %s)", previous_hash or "None", current_hash)

    # 4. Extract Products
    scraped_products = extract_products(website, html, target_url)
    logger.info("Extracted %d products from %s", len(scraped_products), website)

    if not scraped_products:
        logger.warning("No products extracted. Aborting.")
        return

    # 5. Selective Product Replacement
    new_count = 0
    updated_count = 0
    unchanged_count = 0

    for p in scraped_products:
        url = p["product_url"]
        if url in master_db:
            existing = master_db[url]
            # Check if any field changed (price, title, original_price, discount, rating, image_url)
            fields_changed = False
            for field in ["title", "price", "original_price", "discount", "rating", "image_url"]:
                if p.get(field, "") != existing.get(field, ""):
                    fields_changed = True
                    break

            if fields_changed:
                # Preserve existing local_image if file exists
                if existing.get("local_image") and os.path.exists(existing["local_image"]):
                    p["local_image"] = existing["local_image"]
                master_db[url] = p
                updated_count += 1
                logger.info("  [UPDATED] %s | Price: %s -> %s", (p['title'] or '')[:30], existing.get('price'), p.get('price'))
            else:
                unchanged_count += 1
        else:
            master_db[url] = p
            new_count += 1
            logger.info("  [ADDED] %s | Price: %s", (p['title'] or '')[:30], p.get('price'))

    logger.info("Merge Summary: %d new products added, %d updated, %d unchanged.", new_count, updated_count, unchanged_count)

    # 6. Multi-Threaded Parallel Image Download for missing/new images
    download_images_parallel(list(master_db.values()), website, max_workers=args.workers)

    # 7. Save Master Database & Update Site Cache
    save_master_database(master_db)
    
    site_cache[target_url] = {
        "content_hash": current_hash,
        "last_scraped": datetime.now().isoformat(),
        "products_count": len(scraped_products),
        "website": website,
    }
    save_site_cache(site_cache)

    logger.info("=" * 65)
    logger.info("Scrape Completed Successfully!")
    logger.info("Website Tag        : %s", website)
    logger.info("Scraped This Run   : %d", len(scraped_products))
    logger.info("Master Database    : %d total products -> %s", len(master_db), MASTER_JSON)
    logger.info("Master CSV Export  : %s", MASTER_CSV)
    logger.info("Images Directory   : %s/", IMAGES_DIR)
    logger.info("=" * 65)


if __name__ == "__main__":
    main()
