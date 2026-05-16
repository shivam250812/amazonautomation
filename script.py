"""
script.py — Amazon product scraper with Helium 10 revenue extraction.

Usage:
  # Standalone with default keywords
  python script.py

  # With custom keywords (comma-separated)
  python script.py --keywords "copper bottle,standing desk"

  # From a JSON file (output of suggest_amazon_categories.py)
  python script.py --keywords-file suggestions.json

  # Limit search pages (for testing)
  SEARCH_PAGES=1 python script.py --keywords "copper bottle"
"""

import argparse
import asyncio
import json
import re
import csv
import os
import sys
from pathlib import Path
from urllib.parse import urljoin, urlparse

from playwright.async_api import async_playwright

# Shared Chrome profile setup
from chrome_profile import (
    CHROME_USER_DATA_DIR,
    PROFILE_DIR,
    create_browser,
    purge_helium10_storage,
)

# ─── Config ────────────────────────────────────────────────────────────────────

DEFAULT_KEYWORDS = ["copper water dispenser", "standing desk"]
BASE_URL = "https://www.amazon.com/s?k="
_OUTPUT_DIR = Path(__file__).resolve().parent
OUTPUT_FILE = str(_OUTPUT_DIR / "output.csv")

AMAZON_ORIGIN = f"{urlparse(BASE_URL).scheme}://{urlparse(BASE_URL).netloc}"
SETUP_ONLY = os.getenv("SETUP_ONLY", "0") == "1"
AUTO_CLEAN_HELIUM10 = os.getenv("AUTO_CLEAN_HELIUM10", "0") == "1"
HELIUM_LOGIN_FIRST = os.getenv("HELIUM_LOGIN_FIRST", "0") == "1"
HELIUM_LOGIN_ONLY = os.getenv("HELIUM_LOGIN_ONLY", "0") == "1"


# ─── Utility Functions ────────────────────────────────────────────────────────

def clean_price(text):
    if not text:
        return None
    text = text.replace(",", "")
    match = re.search(r"\d+\.?\d*", text)
    return float(match.group()) if match else None


def clean_money(text):
    if not text:
        return None
    text = text.replace(",", "")
    match = re.search(r"[$₹€£]?\s*(\d+\.?\d*)", text)
    return float(match.group(1)) if match else None


def extract_asin(url):
    match = re.search(r"/dp/([A-Z0-9]{10})", url)
    return match.group(1) if match else "N/A"


# ─── Helium 10 Revenue Extraction ─────────────────────────────────────────────

async def extract_helium10_revenue(page):
    """
    Scrape the '30-Day Revenue' value injected by the Helium 10 extension.

    Strategy:
      1. Wait up to 25s for the Helium 'Product Summary' panel to appear.
      2. Once the panel is visible, poll for the $ value.
      3. If we see N/A or '-', keep polling for 10 more seconds in case
         Helium is still loading.
      4. Only accept N/A as final after the extra wait.

    Returns: float (real revenue), "NA" (confirmed no data), or None (panel never loaded).
    """
    # Regex to find dollar value right after "30-Day Revenue"
    DOLLAR_RE = re.compile(
        r"30[\u2011-]Day Revenue[\s\S]{0,50}?([$\u20b9\u20ac\u00a3]\s*[\d,]+(?:\.\d+)?)",
        re.IGNORECASE,
    )
    # Regex to detect N/A or dash placeholder
    NA_RE = re.compile(
        r"30[\u2011-]Day Revenue[\s\S]{0,30}?(N/?A|[-\u2011])\s",
        re.IGNORECASE,
    )

    try:
        panel_locator = page.locator(
            "xpath=//*[contains(normalize-space(.), '30-Day Revenue') "
            "or contains(normalize-space(.), '30\u2011Day Revenue') "
            "or contains(normalize-space(.), 'Product Summary')]"
        ).first

        try:
            await panel_locator.wait_for(state="visible", timeout=25_000)
        except Exception:
            print("        Helium panel not detected — skipping revenue for this product.")
            return "NA"

        saw_na_at = None  # Track which attempt we first saw N/A

        for attempt in range(50):  # 50 × 500 ms = 25 s max polling
            try:
                container = panel_locator.locator(
                    "xpath=ancestor-or-self::*[contains(@class,'summary') "
                    "or contains(@class,'helium') "
                    "or contains(@class,'xray') "
                    "or self::section or self::aside][1]"
                )
                if await container.count():
                    txt = await container.inner_text()
                else:
                    txt = await panel_locator.inner_text()
            except Exception:
                txt = ""

            # First, always check for a real dollar value
            m = DOLLAR_RE.search(txt)
            if m:
                val = clean_money(m.group(1))
                if val is not None:
                    return val

            # Check if we see N/A or dash
            if NA_RE.search(txt):
                if saw_na_at is None:
                    saw_na_at = attempt
                    print("        Helium showing N/A — waiting 10s to see if it updates...")
                # If we've been seeing N/A for 20 attempts (10 seconds), accept it
                if attempt - saw_na_at >= 20:
                    print("        Helium confirmed N/A after waiting.")
                    return "NA"

            await page.wait_for_timeout(500)

        # Fallback: body text scan
        try:
            body = await page.locator("body").inner_text()
            m = DOLLAR_RE.search(body)
            if m:
                val = clean_money(m.group(1))
                if val is not None:
                    return val
        except Exception:
            pass

        print("        Helium panel visible but revenue value not found.")

    except Exception as e:
        print(f"        extract_helium10_revenue error: {e}")

    return "NA"


# ─── Rating, Reviews, Sellers, Shipper/Seller ──────────────────────────────────

async def extract_rating_and_reviews(page):
    rating = None
    reviews = None
    try:
        alt = await page.locator("span.a-icon-alt").first.inner_text()
        m = re.search(r"(\d+(?:\.\d+)?)\s+out of 5", alt)
        if m:
            rating = float(m.group(1))
    except:
        pass

    try:
        txt = await page.locator("#acrCustomerReviewText").first.inner_text()
        m = re.search(r"([\d,]+)", txt)
        if m:
            reviews = int(m.group(1).replace(",", ""))
    except:
        pass

    return rating, reviews


async def extract_seller_count(page):
    """Best-effort seller count from Amazon offer listings."""
    for selector in [
        "#olp_feature_div",
        "#olpLinkWidget_feature_div",
        "div[id*='secondaryUsedAndNew']",
    ]:
        try:
            el = page.locator(selector).first
            if await el.count():
                txt = (await el.inner_text()).strip()
                m = re.search(r"\((\d+)\)", txt)
                if m:
                    return int(m.group(1))
        except:
            pass

    try:
        offers = page.locator("a[href*='offer-listing'], a[href*='offerlisting']")
        count = await offers.count()
        for i in range(min(count, 10)):
            txt = (await offers.nth(i).inner_text()) or ""
            m = re.search(r"New\s*\(\s*(\d+)\s*\)", txt, flags=re.IGNORECASE)
            if m:
                return int(m.group(1))
    except:
        pass

    try:
        buybox = page.locator("#buybox, #buyBoxAccordion, #moreBuyingChoices_feature_div")
        if await buybox.count():
            txt = (await buybox.first.inner_text()) or ""
            m = re.search(r"(\d+)\s+new", txt, flags=re.IGNORECASE)
            if m:
                return int(m.group(1))
    except:
        pass

    try:
        body = (await page.locator("body").inner_text()) or ""
        m = re.search(r"New\s*\(\s*(\d+)\s*\)\s*from", body, flags=re.IGNORECASE)
        if m:
            return int(m.group(1))
        m = re.search(r"(\d+)\s+(?:new\s+)?offers?", body, flags=re.IGNORECASE)
        if m:
            return int(m.group(1))
    except:
        pass

    return None


async def extract_shipper_and_seller(page):
    """
    Best-effort extraction of Shipper and Seller from an Amazon product page.
    Returns (shipper, seller) strings, defaulting to "N/A".
    """
    shipper = "N/A"
    seller = "N/A"

    try:
        # Method 1: tabular buybox
        labels = page.locator(".tabular-buybox-text")
        values = page.locator(".tabular-buybox-text-message")
        label_count = await labels.count()
        val_count = await values.count()
        for i in range(label_count):
            try:
                label_txt = (await labels.nth(i).inner_text()).strip().lower()
                value_txt = (await values.nth(i).inner_text()).strip() if i < val_count else ""
                if not value_txt:
                    continue
                # Combined "Shipper / Seller" label — assign same value to both
                if "shipper" in label_txt and "seller" in label_txt:
                    shipper = value_txt
                    seller = value_txt
                else:
                    if "ship" in label_txt:
                        shipper = value_txt
                    if "sold" in label_txt or "seller" in label_txt:
                        seller = value_txt
            except:
                pass

        if shipper != "N/A" or seller != "N/A":
            return shipper, seller

        # Method 2: #merchant-info
        for sel in ["#merchant-info", "#soldByThirdParty", "#sellerProfileTriggerId"]:
            try:
                el = page.locator(sel).first
                if await el.count():
                    txt = (await el.inner_text()).strip()
                    if not txt:
                        continue
                    m = re.search(r"sold by\s+(.+?)(?:\.|$)", txt, re.IGNORECASE)
                    if m:
                        seller = m.group(1).strip()
                    m = re.search(r"ships from\s+(.+?)(?:\s+and|\.\band\b|$)", txt, re.IGNORECASE)
                    if m:
                        shipper = m.group(1).strip()
                    if seller != "N/A" or shipper != "N/A":
                        break
            except:
                pass

        if shipper != "N/A" or seller != "N/A":
            return shipper, seller

        # Method 3: buybox feature div
        for buybox_sel in ["#buybox", "#desktop_buybox", "#buyBoxAccordion", "#apex_desktop"]:
            try:
                box = page.locator(buybox_sel).first
                if not await box.count():
                    continue
                txt = (await box.inner_text()) or ""
                # Check for combined "Shipper / Seller" label first
                m = re.search(r"Shipper\s*/\s*Seller[:\s]+([^\n]+)", txt, re.IGNORECASE)
                if m:
                    val = m.group(1).strip().split("\n")[0].strip()
                    shipper = val
                    seller = val
                else:
                    m = re.search(r"Ships\s+from[:\s]+([^\n]+)", txt, re.IGNORECASE)
                    if m:
                        shipper = m.group(1).strip().split("\n")[0].strip()
                    m = re.search(r"Sold\s+by[:\s]+([^\n]+)", txt, re.IGNORECASE)
                    if m:
                        seller = m.group(1).strip().split("\n")[0].strip()
                if shipper != "N/A" or seller != "N/A":
                    break
            except:
                pass

        if shipper != "N/A" or seller != "N/A":
            return shipper, seller

        # Method 4: table rows
        rows = page.locator("tr")
        row_count = await rows.count()
        for i in range(min(row_count, 40)):
            try:
                cells = rows.nth(i).locator("td")
                if await cells.count() >= 2:
                    label_txt = (await cells.nth(0).inner_text()).strip().lower()
                    value_txt = (await cells.nth(1).inner_text()).strip()
                    if "ship" in label_txt and value_txt:
                        shipper = value_txt
                    if ("sold" in label_txt or "seller" in label_txt) and value_txt:
                        seller = value_txt
            except:
                pass

        if shipper != "N/A" or seller != "N/A":
            return shipper, seller

        # Method 5: full body scan
        try:
            body = (await page.locator("body").inner_text()) or ""
            # Check for combined "Shipper / Seller" first
            m = re.search(r"Shipper\s*/\s*Seller[:\s]+([^\n]{2,60})", body, re.IGNORECASE)
            if m:
                val = m.group(1).strip()
                shipper = val
                seller = val
            else:
                m = re.search(r"Ships\s+from[:\s]+([^\n]{2,60})", body, re.IGNORECASE)
                if m:
                    shipper = m.group(1).strip()
                m = re.search(r"Sold\s+by[:\s]+([^\n]{2,60})", body, re.IGNORECASE)
                if m:
                    seller = m.group(1).strip()
        except:
            pass

    except Exception as e:
        print(f" extract_shipper_and_seller error: {e}")

    return shipper, seller


# ─── Product Scraper ───────────────────────────────────────────────────────────

async def scrape_product(context, url):
    page = await context.new_page()

    try:
        await page.goto(url, timeout=30000)
        await page.wait_for_load_state("domcontentloaded", timeout=30000)
        # Wait for the buybox/seller info to fully render via JS
        try:
            await page.wait_for_load_state("networkidle", timeout=10000)
        except Exception:
            pass
        await asyncio.sleep(3)
    except:
        await page.close()
        return None

    price = None
    revenue = None
    rating = None
    reviews = None
    sellers = None
    shipper = "N/A"
    seller = "N/A"

    try:
        price_text = await page.locator(".a-price .a-offscreen").first.inner_text()
        price = clean_price(price_text)
    except:
        pass

    rating, reviews = await extract_rating_and_reviews(page)
    sellers = await extract_seller_count(page)
    shipper, seller = await extract_shipper_and_seller(page)
    revenue = await extract_helium10_revenue(page)

    asin = extract_asin(url)

    await page.close()

    return {
        "asin": asin,
        "price": price,
        "revenue": revenue,
        "rating": rating,
        "reviews": reviews,
        "sellers": sellers,
        "shipper": shipper,
        "seller": seller,
        "url": url,
    }


# ─── Keyword Processing ───────────────────────────────────────────────────────

async def process_keyword(context, keyword, writer, out_fp, min_price=None, max_price=None):
    print(f"\n {keyword}")

    max_pages = int(os.getenv("SEARCH_PAGES", "20"))
    max_pages = max(1, min(max_pages, 20))

    urls_set = set()
    for page_num in range(1, max_pages + 1):
        before_count = len(urls_set)
        page = await context.new_page()
        try:
            q = keyword.replace(" ", "+")
            search_url = f"{BASE_URL}{q}"
            
            # Apply price filters if provided
            if min_price or max_price:
                joiner = "&" if "?" in search_url else "?"
                search_url = f"{search_url}{joiner}low-price={min_price or ''}&high-price={max_price or ''}"

            # Always sort by Best Sellers (popularity rank)
            joiner = "&" if "?" in search_url else "?"
            search_url = f"{search_url}{joiner}s=exact-aware-popularity-rank"

            if page_num > 1:
                joiner = "&" if "?" in search_url else "?"
                search_url = f"{search_url}{joiner}page={page_num}"

            print(f"   • search page {page_num}/{max_pages}")
            await page.goto(search_url, timeout=60000)
            await page.wait_for_load_state("domcontentloaded", timeout=60000)
            await asyncio.sleep(2)

            try:
                body_txt = (await page.locator("body").inner_text()) or ""
                if "did not match any products" in body_txt.lower():
                    break
            except Exception:
                pass

            try:
                hrefs = await page.locator("a.a-link-normal.s-no-outline").evaluate_all(
                    "elements => elements.map(e => e.getAttribute('href'))"
                )
                for href in hrefs:
                    if href and "/dp/" in href:
                        urls_set.add(urljoin(AMAZON_ORIGIN, href.split("?")[0]))
            except Exception as e:
                print(f"        Error extracting links on page {page_num}: {e}")

            if len(urls_set) == before_count:
                break

            try:
                next_enabled = await page.locator("a.s-pagination-next").count()
                if next_enabled == 0:
                    break
            except Exception:
                pass
        finally:
            try:
                await page.close()
            except Exception:
                pass

    urls = list(urls_set)

    for url in urls:
        product = await scrape_product(context, url)
        if not product:
            continue

        print(
            f" ASIN: {product['asin']} | ${product['price']} "
            f"| Shipper: {product['shipper']} | Seller: {product['seller']}"
        )

        writer.writerow([
            keyword,
            product["asin"],
            product["price"],
            product["revenue"],
            product["rating"],
            product["reviews"],
            product["sellers"],
            product["shipper"],
            product["seller"],
            product["url"],
        ])
        out_fp.flush()


# ─── Helium 10 Login & Warmup ─────────────────────────────────────────────────

async def helium10_login_window(context):
    """
    Open a real PDP and wait so you can log in to Helium 10 (extensions menu).
    """
    url = os.getenv("HELIUM_WARMUP_DP", "https://www.amazon.com/dp/B08LVBV9KX")
    page = await context.new_page()
    try:
        await page.goto(url, timeout=90_000, wait_until="domcontentloaded")
        print(
            "\n" + "=" * 62 + "\n"
            "  HELIUM 10 — LOG IN HERE\n\n"
            "  In the Chrome window that opened:\n"
            "    1. Click the puzzle icon (extensions) -> Helium 10 for Amazon Sellers.\n"
            "    2. Complete sign-in until it succeeds.\n"
            "    3. Wait until this tab shows the overlay (30-Day Revenue / Product Summary).\n\n"
            "  No need to press Enter here -- the script continues automatically when\n"
            "  that overlay appears.\n"
            + "=" * 62 + "\n"
        )
        overlay = page.locator(
            "xpath=//*[contains(., '30-Day Revenue') or contains(., '30‑Day Revenue') or contains(., 'Product Summary')]"
        ).first
        max_sec = int(os.getenv("HELIUM_LOGIN_MAX_WAIT_SEC", "900"))
        print(f" Waiting up to {max_sec}s for the Helium overlay on this page…\n")
        try:
            await overlay.wait_for(state="visible", timeout=max_sec * 1000)
            print(" Helium overlay detected — session will be saved when Chrome closes.\n")
        except Exception:
            print(
                f" Helium overlay did not appear within {max_sec}s. "
                "Try signing in again or increase HELIUM_LOGIN_MAX_WAIT_SEC.",
                file=sys.stderr,
            )
        print(" Login step finished.\n")
    except Exception as e:
        print(f" Helium login step failed: {e}", file=sys.stderr)
    finally:
        try:
            await page.close()
        except Exception:
            pass


async def prime_helium10_on_pdp(context):
    """
    Open one PDP so the Helium 10 extension loads; wait until the
    revenue widget appears or time out.
    """
    if os.getenv("SKIP_HELIUM_WARMUP", "0") == "1":
        return

    url = os.getenv("HELIUM_WARMUP_DP", "https://www.amazon.com/dp/B08LVBV9KX")
    page = await context.new_page()
    try:
        print(f"\n Loading a product page so Helium 10 can run: {url}")
        await page.goto(url, timeout=90_000, wait_until="domcontentloaded")
        panel = page.locator(
            "xpath=//*[contains(., '30-Day Revenue') or contains(., '30‑Day Revenue') or contains(., 'Product Summary')]"
        ).first

        try:
            await panel.wait_for(state="visible", timeout=60_000)
        except Exception:
            print(
                "\n Helium 10 did not show its panel in 60s.\n"
                "   • Click the extensions (puzzle) icon → Helium 10 → sign in if asked.\n"
                "   • Reload this tab or rerun after login.\n"
                "   • To skip this wait: SKIP_HELIUM_WARMUP=1\n",
                file=sys.stderr,
            )
            return

        revenue = await extract_helium10_revenue(page)
        if revenue is not None:
            print(" Helium 10 is active and returning revenue values.\n")
            return

        print(
            "\n Helium 10 panel is visible but revenue was not found.\n"
            "   This usually means Helium 10 is asking you to sign in.\n"
            "   Please sign in once in the Chrome window, then wait.\n",
            file=sys.stderr,
        )

        max_sec = int(os.getenv("HELIUM_LOGIN_MAX_WAIT_SEC", "900"))
        print(f" Waiting up to {max_sec}s for a revenue value to appear…\n")
        for _ in range(max_sec):
            revenue = await extract_helium10_revenue(page)
            if revenue is not None:
                print(" Helium 10 revenue detected after login.\n")
                return
            await page.wait_for_timeout(1000)
    except Exception as e:
        print(f" Helium warm-up navigation failed: {e}", file=sys.stderr)
    finally:
        try:
            await page.close()
        except Exception:
            pass


# ─── Public API (for run_pipeline.py) ──────────────────────────────────────────

async def run_scraper(keywords: list[str], min_price: str = None, max_price: str = None) -> str:
    """
    Run the full scraping pipeline for the given keywords.
    Returns the path to the output CSV file.
    """
    async with async_playwright() as p:
        if SETUP_ONLY and AUTO_CLEAN_HELIUM10:
            try:
                purge_helium10_storage(Path(CHROME_USER_DATA_DIR), PROFILE_DIR)
            except Exception as e:
                print(f" Auto-clean failed: {e}")

        context = await create_browser(p, require_helium=not SETUP_ONLY, is_setup_mode=SETUP_ONLY)

        if SETUP_ONLY:
            page = await context.new_page()
            await page.goto(AMAZON_ORIGIN, timeout=30000)
            print("\n SETUP MODE")
            print("1) Go to the Chrome Web Store and install the Helium 10 extension.")
            print("2) Log into Helium 10 and Amazon Seller Central.")
            print("3) You have ~5 minutes to complete this before the browser closes automatically.\n")
            await page.wait_for_timeout(300_000)
            await context.close()
            print(" Setup complete. Re-run without SETUP_ONLY=1 to scrape.")
            return OUTPUT_FILE

        skip_prime = False
        if HELIUM_LOGIN_FIRST or HELIUM_LOGIN_ONLY:
            await helium10_login_window(context)
            skip_prime = True
            if HELIUM_LOGIN_ONLY:
                await context.close()
                print(" Helium login window finished.\n")
                return OUTPUT_FILE

        if not skip_prime:
            await prime_helium10_on_pdp(context)
        else:
            print("  Skipping automatic Helium warm-up.\n")

        with open(OUTPUT_FILE, "w", newline="\n", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow([
                "Keyword", "ASIN", "Price", "Revenue", "Rating",
                "Reviews", "Sellers", "Shipper", "Seller", "URL",
            ])
            f.flush()
            print(f"\n Writing rows to: {OUTPUT_FILE}\n")

            for kw in keywords:
                try:
                    await process_keyword(context, kw, writer, f, min_price, max_price)
                except Exception as e:
                    if type(e).__name__ == "TargetClosedError":
                        print(
                            "\n Browser was closed before scraping finished.",
                            file=sys.stderr,
                        )
                        break
                    raise

        await context.close()

    print(f"\n Done — output file: {OUTPUT_FILE}")
    return OUTPUT_FILE


# ─── CLI Entry Point ───────────────────────────────────────────────────────────

def _parse_args():
    """Parse CLI arguments."""
    parser = argparse.ArgumentParser(description="Amazon product scraper")
    parser.add_argument(
        "--keywords",
        type=str,
        default=None,
        help="Comma-separated list of keywords to search",
    )
    parser.add_argument(
        "--keywords-file",
        type=str,
        default=None,
        help="Path to JSON file with suggestions (output of suggest_amazon_categories.py)",
    )
    parser.add_argument(
        "--min-price",
        type=str,
        default=None,
        help="Minimum price filter",
    )
    parser.add_argument(
        "--max-price",
        type=str,
        default=None,
        help="Maximum price filter",
    )
    args = parser.parse_args()
    return args

def _get_keywords(args) -> list[str]:
    if args.keywords:
        return [kw.strip() for kw in args.keywords.split(",") if kw.strip()]

    if args.keywords_file:
        with open(args.keywords_file, "r", encoding="utf-8") as f:
            data = json.load(f)
        suggestions = data.get("suggestions", [])
        return [s["phrase"] for s in suggestions if "phrase" in s]

    return DEFAULT_KEYWORDS


async def main():
    args = _parse_args()
    keywords = _get_keywords(args)
    print(f" Keywords: {keywords}\n")
    if args.min_price or args.max_price:
        print(f" Price Filter: ${args.min_price or '0'} - ${args.max_price or 'Any'}\n")
    await run_scraper(keywords, args.min_price, args.max_price)


if __name__ == "__main__":
    asyncio.run(main())