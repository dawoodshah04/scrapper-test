"""
Diagnose why 40 cards were found but 0 products extracted.
Prints actual structure of the first 3 cards.
"""
from bs4 import BeautifulSoup

with open("samsung_output/debug_raw.html", "r", encoding="utf-8") as f:
    html = f.read()

print(f"HTML size: {len(html):,} bytes")

soup = BeautifulSoup(html, "html.parser")
cards = soup.select(".pd21-product-card__item")
print(f"Cards found: {len(cards)}\n")

for i, card in enumerate(cards[:3], 1):
    print(f"{'='*60}")
    print(f"CARD {i}")
    print(f"{'='*60}")

    # What text content exists in the card?
    all_text = card.get_text(separator="|", strip=True)
    print(f"All text: {all_text[:300]}")
    print()

    # Name/title candidates
    print("--- Name element candidates ---")
    for sel in [".pd21-product-card__name", "a[data-modelname]", "[data-modeldisplay]", "h3", "h4", "a[aria-label]"]:
        els = card.select(sel)
        for el in els[:2]:
            print(f"  {sel} -> tag={el.name} text='{el.get_text(strip=True)[:60]}' attrs={dict(list(el.attrs.items())[:5])}")

    print()
    print("--- Price element candidates ---")
    for sel in [".price-ux__price-current", "[data-pricetext]", ".price-ux__wrap", "[class*='price']"]:
        els = card.select(sel)
        for el in els[:2]:
            print(f"  {sel} -> tag={el.name} text='{el.get_text(strip=True)[:60]}' data-pricetext='{el.get('data-pricetext','')}'")

    print()
    print("--- Image candidates ---")
    imgs = card.select("img")
    for img in imgs[:2]:
        print(f"  img src='{img.get('src','')[:80]}' dsrc='{img.get('data-desktop-src','')[:80]}'")

    print()
    # Card data attributes
    print(f"Card attrs: {dict(list(card.attrs.items())[:8])}")
    print()
