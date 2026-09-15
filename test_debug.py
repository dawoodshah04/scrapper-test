"""
Diagnostic test script to inspect Crawl4AI raw output for Samsung Pakistan.
"""
import os
import json
import re
import requests

CRAWL4AI_URL = "http://localhost:11235"
API_TOKEN = "2fe90f64dbaa1f2167d7f62663d33db5db47c2a51ef791d2ecc10fae77e3019b"
TARGET_URL = "https://www.samsung.com/pk/smartphones/all-smartphones/"
OUTPUT_DIR = "samsung_output"
os.makedirs(OUTPUT_DIR, exist_ok=True)

headers = {"Authorization": f"Bearer {API_TOKEN}"}

payload = {
    "urls": [TARGET_URL],
    "browser_config": {
        "type": "BrowserConfig",
        "params": {
            "headless": True,
            "java_script_enabled": True,
            "user_agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
        }
    },
    "crawler_config": {
        "type": "CrawlerRunConfig",
        "params": {
            "cache_mode": "BYPASS",
            "word_count_threshold": 0,
            "wait_until": "networkidle",
            "wait_for": "css:li.pd21-product-card__item",
            "scan_full_page": True,
            "scroll_delay": 0.5,
            "delay_before_return_html": 5.0,
            "page_timeout": 90000,
            "remove_overlay_elements": True,
        }
    }
}

print("Sending crawl request to Crawl4AI (waiting for response)...")
resp = requests.post(f"{CRAWL4AI_URL}/crawl", json=payload, headers=headers, timeout=300)
print(f"Response status: {resp.status_code}")

if resp.status_code != 200:
    print(f"Error response: {resp.text}")
    exit(1)

data = resp.json()
results = data.get("results") or data.get("result") or [data]
if isinstance(results, dict):
    results = [results]

r0 = results[0]
print(f"Crawl success: {r0.get('success')}")
html = r0.get("html") or ""

print(f"HTML length: {len(html)} characters")

html_path = os.path.join(OUTPUT_DIR, "debug_raw.html")
with open(html_path, "w", encoding="utf-8") as f:
    f.write(html)
print(f"Saved: {html_path}")

# Quick search in HTML
keywords = ["Galaxy", "Galaxy S", "Galaxy Z", "Galaxy A", "Rs.", "PKR", "pd21", "product-card", "price"]
print("\n--- Keyword search in crawled HTML ---")
for kw in keywords:
    count = html.count(kw)
    print(f"'{kw}': {count} occurrences")

# Find class names containing 'product' or 'card' or 'price'
classes = set(re.findall(r'class="([^"]*(?:product|card|price|pd21|phone)[^"]*)"', html, re.I))
print(f"\n--- Found {len(classes)} matching CSS class strings ---")
for c in sorted(list(classes))[:30]:
    print(" ", c)

# Show markdown snippet
print("\n--- Markdown preview (first 1000 chars) ---")
print(md[:1000])
