"""
LLM-Powered Fallback Product Extractor
=======================================
Uses LangChain's PydanticOutputParser + Google Gemini to extract product data
from any arbitrary e-commerce page when DOM-based extractors fail.

This module is the LAST RESORT — only invoked when site-specific CSS/JSON
extractors return zero products.

Supports pagination detection and handling:
  - URL-based pagination (?page=2, &p=3, etc.)
  - "Load More" button clicking via JS injection
  - Infinite scroll via JS injection
"""

import os
import re
import logging
import urllib.parse
from typing import List, Optional

from bs4 import BeautifulSoup
from pydantic import BaseModel, Field

# Load environment variables (such as GOOGLE_API_KEY)
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

# LangChain imports (langchain_core with backward-compatible fallback to langchain)
try:
    from langchain_core.output_parsers import PydanticOutputParser
    from langchain_core.prompts import PromptTemplate
except ImportError:
    try:
        from langchain.output_parsers import PydanticOutputParser
        from langchain.prompts import PromptTemplate
    except ImportError:
        PydanticOutputParser = None
        PromptTemplate = None

try:
    from langchain_google_genai import ChatGoogleGenerativeAI
except ImportError:
    ChatGoogleGenerativeAI = None

logger = logging.getLogger("master_scraper")


# ---------------------------------------------------------------------------
# Pydantic Schemas (defines both LLM output format & validation)
# ---------------------------------------------------------------------------
class Product(BaseModel):
    """Schema for a single e-commerce product."""
    title: str = Field(description="Product name/title")
    price: str = Field(description="Current selling price with currency (e.g., 'Rs. 45,000' or 'PKR 45,000')")
    original_price: Optional[str] = Field(default="", description="Original/strikethrough price before discount, empty string if not available")
    discount: Optional[str] = Field(default="", description="Discount percentage (e.g., '-15%'), empty string if not available")
    rating: Optional[str] = Field(default="", description="Product rating (e.g., '4.5'), empty string if not available")
    product_url: str = Field(default="", description="Direct URL to the product detail page")
    image_url: Optional[str] = Field(default="", description="URL of the product image")


class ProductList(BaseModel):
    """List of products extracted from an e-commerce page."""
    products: List[Product]


# ---------------------------------------------------------------------------
# HTML Cleaning Helpers
# ---------------------------------------------------------------------------
def _clean_html_for_llm(html: str, max_chars: int = 60000) -> tuple:
    """
    Strip noise from HTML and extract structured context for the LLM.
    
    Returns:
        (cleaned_text, links_text, images_text) — all truncated to fit context window.
    """
    soup = BeautifulSoup(html, "html.parser")

    # Remove non-product elements to reduce noise and token usage
    for tag in soup(["script", "style", "nav", "footer", "header",
                     "noscript", "svg", "iframe", "meta", "link"]):
        tag.decompose()

    # Extract links and images BEFORE converting to text (preserves URL context)
    links = []
    for a in soup.find_all("a", href=True):
        text = a.get_text(strip=True)
        href = a.get("href", "")
        if text and href and len(href) > 1:
            links.append(f"  {text[:80]} -> {href}")

    images = []
    for img in soup.find_all("img"):
        alt = img.get("alt", "").strip()
        src = img.get("src", "") or img.get("data-src", "") or img.get("data-lazy-src", "")
        if src and not src.startswith("data:"):
            images.append(f"  {alt[:60]} -> {src}")

    # Get visible text content
    cleaned_text = soup.get_text(separator="\n", strip=True)

    # Truncate everything to stay within token limits
    return (
        cleaned_text[:max_chars],
        "\n".join(links[:200]),
        "\n".join(images[:100]),
    )


# ---------------------------------------------------------------------------
# Pagination Detection & JavaScript Injection
# ---------------------------------------------------------------------------

LOAD_MORE_JS = """
(async () => {
    const selectors = [
        'button[class*="load-more"]', 'a[class*="load-more"]',
        '[class*="show-more"]', 'button[class*="view-more"]',
        '[data-action="load-more"]', '.load-more-btn', '.btn-loadmore',
    ];

    function findByText() {
        const buttons = document.querySelectorAll('button, a, [role="button"]');
        for (const btn of buttons) {
            const text = btn.textContent.trim().toLowerCase();
            if (text.match(/load\\s*more|show\\s*more|view\\s*more|see\\s*more/)) {
                return btn;
            }
        }
        return null;
    }

    let clicks = 0;
    const MAX_CLICKS = 50;

    while (clicks < MAX_CLICKS) {
        let btn = null;
        for (const sel of selectors) {
            btn = document.querySelector(sel);
            if (btn && btn.offsetParent !== null) break;
            btn = null;
        }
        if (!btn) btn = findByText();

        if (!btn || btn.disabled || btn.offsetParent === null) break;

        const prevHeight = document.body.scrollHeight;
        btn.click();
        clicks++;

        await new Promise(r => setTimeout(r, 2000));
        window.scrollTo(0, document.body.scrollHeight);
        await new Promise(r => setTimeout(r, 1000));

        if (document.body.scrollHeight === prevHeight) {
            await new Promise(r => setTimeout(r, 3000));
            if (document.body.scrollHeight === prevHeight) break;
        }
    }

    window.scrollTo(0, document.body.scrollHeight);
    await new Promise(r => setTimeout(r, 2000));
})();
"""

INFINITE_SCROLL_JS = """
(async () => {
    let previousHeight = 0;
    let unchangedCount = 0;
    const MAX_UNCHANGED = 5;
    const SCROLL_PAUSE = 2500;

    while (unchangedCount < MAX_UNCHANGED) {
        window.scrollTo(0, document.body.scrollHeight);
        await new Promise(r => setTimeout(r, SCROLL_PAUSE));

        const currentHeight = document.body.scrollHeight;
        if (currentHeight === previousHeight) {
            unchangedCount++;
        } else {
            unchangedCount = 0;
        }
        previousHeight = currentHeight;
    }

    window.scrollTo(0, 0);
    await new Promise(r => setTimeout(r, 500));
    window.scrollTo(0, document.body.scrollHeight);
    await new Promise(r => setTimeout(r, 2000));
})();
"""


def _detect_pagination_type(html: str) -> str:
    """
    Analyze HTML to determine what kind of pagination the page uses.

    Returns: "url" | "load_more" | "infinite_scroll" | "none"
    """
    soup = BeautifulSoup(html, "html.parser")
    html_lower = html.lower()

    # 1. Check for "Load More" buttons (by CSS class)
    load_more_selectors = [
        '[class*="load-more"]', '[class*="loadmore"]',
        '[class*="show-more"]', '[class*="view-more"]',
        '[data-action="load-more"]',
    ]
    for sel in load_more_selectors:
        if soup.select_one(sel):
            return "load_more"

    # Check button/link text content
    for tag in soup.find_all(["button", "a"]):
        text = tag.get_text(strip=True).lower()
        if re.search(r"load\s*more|show\s*more|view\s*more|see\s*all", text):
            return "load_more"

    # 2. Check for URL-based pagination
    pagination_selectors = [
        ".pagination", ".pager", "[class*='pagination']",
        "nav[aria-label*='pagination']", "ul.pages", ".page-numbers",
    ]
    for sel in pagination_selectors:
        el = soup.select_one(sel)
        if el and el.find_all("a", href=True):
            return "url"

    # Check for ?page= or &p= links anywhere
    for a in soup.find_all("a", href=True):
        href = a.get("href", "")
        if re.search(r"[?&](page|p|pg)=\d+", href):
            return "url"

    # 3. Check for infinite scroll indicators in class names / attributes
    infinite_indicators = [
        "infinite-scroll", "infinitescroll", "lazy-load",
        "data-infinite", "scroll-trigger", "waypoint",
    ]
    for indicator in infinite_indicators:
        if indicator in html_lower:
            return "infinite_scroll"

    return "none"


def _detect_url_pagination(html: str, url: str) -> list:
    """
    Scan HTML for pagination links and return a sorted list of page URLs.
    """
    soup = BeautifulSoup(html, "html.parser")
    parsed = urllib.parse.urlparse(url)
    base_url = f"{parsed.scheme}://{parsed.netloc}"

    page_urls = set()

    # Look in common pagination containers first
    pagination_selectors = [
        "nav[aria-label*='pagination']",
        ".pagination", ".pager", ".paginator",
        "[class*='pagination']", "[class*='paging']",
        "ul.pages", ".page-numbers",
    ]

    candidates = []
    for sel in pagination_selectors:
        candidates.extend(soup.select(f"{sel} a[href]"))

    # Fallback: any <a> whose visible text is a number > 1
    if not candidates:
        for a in soup.find_all("a", href=True):
            text = a.get_text(strip=True)
            if text.isdigit() and int(text) > 1:
                candidates.append(a)

    for a in candidates:
        href = a.get("href", "")
        if not href or href == "#":
            continue
        if href.startswith("/"):
            full_url = base_url + href
        elif href.startswith("http"):
            full_url = href
        elif href.startswith("?"):
            full_url = url.split("?")[0] + href
        else:
            full_url = url.rstrip("/") + "/" + href
        page_urls.add(full_url)

    # Remove the current page URL if it ended up in the set
    page_urls.discard(url)

    return sorted(page_urls)


def _deduplicate_products(products: list) -> list:
    """Remove duplicate products based on product_url."""
    seen_urls = set()
    unique = []
    for p in products:
        p_url = p.get("product_url", "")
        if p_url and p_url in seen_urls:
            continue
        if p_url:
            seen_urls.add(p_url)
        unique.append(p)
    return unique


# ---------------------------------------------------------------------------
# Single-Page LLM Extraction (core logic)
# ---------------------------------------------------------------------------
def _extract_single_page_llm(
    html: str,
    url: str,
    website: str,
    api_key: str,
    base_url: str,
    page_label: str = "",
) -> list:
    """
    Extract products from a single HTML page using Google Gemini.

    This is the core LLM call logic, separated so it can be invoked
    once per pagination page.
    """
    page_content, links_text, images_text = _clean_html_for_llm(html)

    if len(page_content.strip()) < 100:
        logger.warning(
            "%sCleaned page content too short (%d chars). Skipping LLM call.",
            page_label, len(page_content),
        )
        return []

    # Set up Pydantic output parser
    parser = PydanticOutputParser(pydantic_object=ProductList)

    prompt = PromptTemplate(
        template="""You are an expert e-commerce data extractor. Analyze the following webpage content 
from {url} and extract ALL product listings you can find.

For each product, extract:
- title: The product name
- price: Current price with currency symbol (e.g., "Rs. 45,000")
- original_price: Original price before discount (if shown), otherwise empty string
- discount: Discount percentage (e.g., "-15%"), otherwise empty string
- rating: Product rating (e.g., "4.5"), otherwise empty string
- product_url: Full URL to the product page (combine with base URL if relative)
- image_url: URL of the product image

IMPORTANT RULES:
1. Extract EVERY product visible on the page, do not skip any.
2. For product_url, if the href is relative (starts with /), prepend the base URL: {base_url}
3. For image_url, if src starts with //, prepend https:
4. If price is not visible, use "Check Website".
5. Do NOT invent or hallucinate products that are not on the page.

{format_instructions}

--- WEBPAGE TEXT CONTENT ---
{page_content}

--- LINKS FOUND ON PAGE (text -> href) ---
{links_text}

--- IMAGES FOUND ON PAGE (alt -> src) ---
{images_text}
""",
        input_variables=["url", "base_url", "page_content", "links_text", "images_text"],
        partial_variables={"format_instructions": parser.get_format_instructions()},
    )

    # Initialize LLM
    llm = ChatGoogleGenerativeAI(
        model="gemini-2.0-flash",
        temperature=0,
        google_api_key=api_key,
    )

    input_values = {
        "url": url,
        "base_url": base_url,
        "page_content": page_content,
        "links_text": links_text or "(no links extracted)",
        "images_text": images_text or "(no images extracted)",
    }

    # ── Debug: print the fully-formatted prompt ──────────────────────────────
    formatted_prompt = prompt.format(**input_values)
    print("\n" + "=" * 80)
    print(f"[ LLM DEBUG ] PROMPT SENT TO GEMINI {page_label}:")
    print("=" * 80)
    print(formatted_prompt)
    print("=" * 80 + "\n")
    # ─────────────────────────────────────────────────────────────────────────

    logger.info("%sInvoking Gemini LLM for structured product extraction...", page_label)

    # Run prompt → LLM only first so we can print the raw text response
    raw_chain = prompt | llm
    raw_response = raw_chain.invoke(input_values)
    raw_text = raw_response.content if hasattr(raw_response, "content") else str(raw_response)

    # ── Debug: print raw LLM response ────────────────────────────────────────
    print("\n" + "=" * 80)
    print(f"[ LLM DEBUG ] RAW RESPONSE FROM GEMINI {page_label}:")
    print("=" * 80)
    print(raw_text)
    print("=" * 80 + "\n")
    # ─────────────────────────────────────────────────────────────────────────

    # Now parse the raw response through the Pydantic parser
    result = parser.parse(raw_text)

    # Convert Pydantic models to dicts and add website tag
    products = []
    for p in result.products:
        product_dict = p.model_dump()
        product_dict["website"] = website

        # Ensure absolute product URLs
        p_url = product_dict.get("product_url", "")
        if p_url and not p_url.startswith("http"):
            product_dict["product_url"] = base_url + (p_url if p_url.startswith("/") else f"/{p_url}")

        # Ensure absolute image URLs
        img_url = product_dict.get("image_url", "")
        if img_url:
            if img_url.startswith("//"):
                product_dict["image_url"] = "https:" + img_url
            elif img_url.startswith("/"):
                product_dict["image_url"] = base_url + img_url

        products.append(product_dict)

    return products


# ---------------------------------------------------------------------------
# LLM Extractor with Pagination Support (Last Resort)
# ---------------------------------------------------------------------------
def extract_with_llm(html: str, url: str, website: str, fetch_page_fn=None) -> list:
    """
    Last-resort extractor: feed cleaned HTML to Google Gemini via LangChain
    with structured output parsing (Pydantic validation).

    Supports pagination:
      - URL-based (?page=2) — fetches each page URL and extracts separately
      - "Load More" button — re-fetches with JS that clicks the button in a loop
      - Infinite scroll — re-fetches with JS that scrolls repeatedly

    Args:
        html: Raw HTML string of the page.
        url: The original URL being scraped.
        website: Detected website name (used as the 'website' field in output).
        fetch_page_fn: Optional callback to fetch additional pages.
                       Signature: fetch_page_fn(url, js_code=None) -> str | None

    Returns:
        List of product dicts matching the standard schema, or empty list on failure.
    """
    if None in (PydanticOutputParser, PromptTemplate, ChatGoogleGenerativeAI):
        missing = []
        if PydanticOutputParser is None or PromptTemplate is None:
            missing.append("langchain-core (or langchain)")
        if ChatGoogleGenerativeAI is None:
            missing.append("langchain-google-genai")
        logger.error(
            "Missing required LangChain packages: %s. Install with: pip install %s",
            ", ".join(missing),
            " ".join(missing),
        )
        return []

    api_key = os.environ.get("GOOGLE_API_KEY") or os.environ.get("GEMINI_API_KEY")
    if not api_key:
        logger.error("GOOGLE_API_KEY not set. Cannot use LLM fallback extractor.")
        return []

    # Parse base URL for resolving relative paths
    parsed_url = urllib.parse.urlparse(url)
    base_url = f"{parsed_url.scheme}://{parsed_url.netloc}"

    # ── Pagination Detection & Content Gathering ─────────────────────────────
    pagination_type = _detect_pagination_type(html)
    all_htmls = [(html, url)]  # List of (html_content, page_url) tuples

    if pagination_type != "none":
        logger.info("Detected pagination type: '%s' on %s", pagination_type, url)

    if pagination_type == "url" and fetch_page_fn:
        page_urls = _detect_url_pagination(html, url)
        logger.info("Found %d additional pagination page URLs.", len(page_urls))
        for page_url in page_urls[:10]:  # Cap at 10 extra pages
            logger.info("  Fetching pagination page: %s", page_url)
            page_html = fetch_page_fn(page_url)
            if page_html and len(page_html) > 500:
                all_htmls.append((page_html, page_url))
            else:
                logger.warning("  Page returned empty/short HTML. Stopping pagination.")
                break

    elif pagination_type == "load_more" and fetch_page_fn:
        logger.info("Re-fetching page with 'Load More' JS click loop...")
        full_html = fetch_page_fn(url, js_code=LOAD_MORE_JS)
        if full_html and len(full_html) > len(html):
            logger.info(
                "Expanded HTML: %d -> %d bytes (+%d)",
                len(html), len(full_html), len(full_html) - len(html),
            )
            all_htmls = [(full_html, url)]  # Replace with expanded version
        else:
            logger.warning("Load More JS did not expand the page content.")

    elif pagination_type == "infinite_scroll" and fetch_page_fn:
        logger.info("Re-fetching page with infinite scroll JS loop...")
        full_html = fetch_page_fn(url, js_code=INFINITE_SCROLL_JS)
        if full_html and len(full_html) > len(html):
            logger.info(
                "Expanded HTML: %d -> %d bytes (+%d)",
                len(html), len(full_html), len(full_html) - len(html),
            )
            all_htmls = [(full_html, url)]
        else:
            logger.warning("Infinite scroll JS did not expand the page content.")

    elif pagination_type == "none":
        logger.info("No pagination detected. Extracting from single page.")

    # ── Per-Page LLM Extraction ──────────────────────────────────────────────
    logger.info(
        "Preparing %d page(s) for LLM extraction (cleaning noise, extracting links/images)...",
        len(all_htmls),
    )

    all_products = []
    for i, (page_html, page_url) in enumerate(all_htmls):
        page_label = f"[Page {i + 1}/{len(all_htmls)}] " if len(all_htmls) > 1 else ""
        try:
            page_products = _extract_single_page_llm(
                page_html, page_url, website, api_key, base_url, page_label
            )
            all_products.extend(page_products)
            logger.info(
                "%sExtracted %d products (running total: %d).",
                page_label, len(page_products), len(all_products),
            )
        except Exception as e:
            logger.error("%sLLM extraction failed for %s: %s", page_label, page_url, e)

    # ── Deduplication ────────────────────────────────────────────────────────
    products = _deduplicate_products(all_products)
    if len(products) < len(all_products):
        logger.info(
            "Deduplicated: %d -> %d products (removed %d duplicates).",
            len(all_products), len(products), len(all_products) - len(products),
        )

    logger.info("LLM extraction complete: %d products validated by Pydantic schema.", len(products))
    return products
