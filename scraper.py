import asyncio
import json
import logging
from pathlib import Path
from urllib.parse import urljoin

import pandas as pd
import requests
from playwright.async_api import TimeoutError as PlaywrightTimeoutError
from playwright.async_api import async_playwright

BASE_URL = "https://books.toscrape.com/"
WEBHOOK_URL = "https://hook.eu1.make.com/lct2apf0fzkpfhtsxpz28wc6433apian"
OUTPUT_FILE = Path("competitor_data.json")
LOG_FILE = Path("scraper.log")
PRODUCT_LIMIT = 10
MAX_NAVIGATION_ATTEMPTS = 3
NAVIGATION_TIMEOUT_MS = 60_000
REQUEST_TIMEOUT_SECONDS = 30
BOLD_GREEN = "\033[1;32m"
BOLD_CYAN = "\033[1;36m"
BOLD_MAGENTA = "\033[1;35m"
RESET_STYLE = "\033[0m"


def configure_logging() -> logging.Logger:
    """Configure structured console and file logging."""
    logger = logging.getLogger("books_scraper")
    logger.setLevel(logging.INFO)
    logger.handlers.clear()

    formatter = logging.Formatter(
        fmt="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    console_handler = logging.StreamHandler()
    console_handler.setFormatter(formatter)

    file_handler = logging.FileHandler(LOG_FILE, mode="a", encoding="utf-8")
    file_handler.setFormatter(formatter)

    logger.addHandler(console_handler)
    logger.addHandler(file_handler)
    return logger


logger = configure_logging()


def print_status(message: str, color: str = BOLD_CYAN) -> None:
    """Print high-visibility terminal feedback."""
    print(f"{color}{message}{RESET_STYLE}", flush=True)


async def extract_products(page) -> list[dict[str, str]]:
    """Extract raw product data from the current page."""
    products: list[dict[str, str]] = []
    product_cards = page.locator("article.product_pod")
    product_count = min(await product_cards.count(), PRODUCT_LIMIT)

    logger.info(
        "Found %s product cards; extracting first %s",
        await product_cards.count(),
        product_count,
    )

    for index in range(product_count):
        card = product_cards.nth(index)

        try:
            title_link = card.locator("h3 a")
            title = (await title_link.get_attribute("title") or "").strip()
            relative_url = await title_link.get_attribute("href")
            price = (await card.locator(".price_color").inner_text()).strip()
            availability_text = (
                await card.locator(".availability").inner_text()
            ).strip()

            if not title:
                raise ValueError("Missing product title")
            if not relative_url:
                raise ValueError(f"Missing product URL for {title}")

            products.append(
                {
                    "title": title,
                    "price": price,
                    "availability": normalize_availability(availability_text),
                    "product_url": urljoin(BASE_URL, relative_url),
                }
            )
            logger.info("Extracted product %s/%s: %s", index + 1, product_count, title)

        except Exception as exc:
            logger.exception("Failed to extract product at index %s: %s", index, exc)

    return products


def normalize_availability(value: str) -> str:
    """Normalize availability text to the requested output labels."""
    return "In Stock" if "in stock" in value.lower() else "Out of Stock"


def clean_with_pandas(products: list[dict[str, str]]) -> pd.DataFrame:
    """Clean raw scraped data with Pandas and return the final DataFrame."""
    df = pd.DataFrame(
        products, columns=["title", "price", "availability", "product_url"]
    )

    if df.empty:
        logger.warning(
            "No products were extracted; output JSON will contain an empty list"
        )
        return df

    df["price"] = (
        df["price"]
        .astype(str)
        .str.replace(r"[^\d.]+", "", regex=True)
        .str.strip()
        .pipe(pd.to_numeric, errors="coerce")
    )

    invalid_prices = df["price"].isna().sum()
    if invalid_prices:
        logger.warning(
            "Detected %s product(s) with invalid prices after cleaning", invalid_prices
        )

    return df


async def navigate_with_retries(page) -> None:
    """Load the target page, retrying transient navigation failures."""
    for attempt in range(1, MAX_NAVIGATION_ATTEMPTS + 1):
        try:
            logger.info(
                "Navigating to %s (attempt %s/%s)",
                BASE_URL,
                attempt,
                MAX_NAVIGATION_ATTEMPTS,
            )
            await page.goto(
                BASE_URL, wait_until="domcontentloaded", timeout=NAVIGATION_TIMEOUT_MS
            )
            await page.wait_for_selector(
                "article.product_pod", timeout=NAVIGATION_TIMEOUT_MS
            )
            return
        except PlaywrightTimeoutError as exc:
            if attempt == MAX_NAVIGATION_ATTEMPTS:
                logger.exception(
                    "Timed out while loading %s after %s attempts: %s",
                    BASE_URL,
                    attempt,
                    exc,
                )
                raise

            logger.warning(
                "Navigation attempt %s/%s timed out; retrying: %s",
                attempt,
                MAX_NAVIGATION_ATTEMPTS,
                exc,
            )
            await asyncio.sleep(2 * attempt)
        except Exception as exc:
            if attempt == MAX_NAVIGATION_ATTEMPTS:
                logger.exception(
                    "Navigation failed for %s after %s attempts: %s",
                    BASE_URL,
                    attempt,
                    exc,
                )
                raise

            logger.warning(
                "Navigation attempt %s/%s failed; retrying: %s",
                attempt,
                MAX_NAVIGATION_ATTEMPTS,
                exc,
            )
            await asyncio.sleep(2 * attempt)


async def scrape_books() -> pd.DataFrame:
    """Navigate to the target site, scrape products, and return cleaned data."""
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)
        page = await browser.new_page()

        try:
            await navigate_with_retries(page)
            raw_products = await extract_products(page)
            return clean_with_pandas(raw_products)
        finally:
            await browser.close()
            logger.info("Browser closed")


def send_to_webhook(json_path: Path) -> int:
    """Send the generated JSON payload to the configured Make.com webhook."""
    try:
        with json_path.open("r", encoding="utf-8") as input_file:
            payload = json.load(input_file)

        record_count = len(payload) if isinstance(payload, list) else 1
        logger.info("Posting %s record(s) to Make.com webhook", record_count)

        response = requests.post(
            WEBHOOK_URL,
            json=payload,
            timeout=REQUEST_TIMEOUT_SECONDS,
        )
        if response.status_code != 200:
            raise requests.HTTPError(
                f"Unexpected webhook status code: {response.status_code}",
                response=response,
            )

        logger.info("Webhook POST succeeded with status code %s", response.status_code)
        return response.status_code
    except requests.RequestException as exc:
        logger.exception("Webhook POST failed: %s", exc)
        raise
    except OSError as exc:
        logger.exception("Could not read JSON file for webhook delivery: %s", exc)
        raise
    except json.JSONDecodeError as exc:
        logger.exception("Generated JSON file is invalid and was not sent: %s", exc)
        raise


async def main() -> None:
    """Run the scraper, save cleaned data to JSON, and send it to the webhook."""
    try:
        df = await scrape_books()
        print_status(f"✅ Data extraction complete: {len(df)} items found.", BOLD_GREEN)

        with OUTPUT_FILE.open("w", encoding="utf-8") as output_file:
            json.dump(
                df.to_dict(orient="records"), output_file, ensure_ascii=False, indent=2
            )
        logger.info("Saved %s cleaned product records to %s", len(df), OUTPUT_FILE)

        status_code = send_to_webhook(OUTPUT_FILE)
        if status_code == 200:
            print_status("🚀 Data successfully synced", BOLD_CYAN)

        print_status("✨ Automation Job Finished Successfully.", BOLD_MAGENTA)
    except Exception:
        logger.exception("Scraper failed")
        raise


if __name__ == "__main__":
    asyncio.run(main())
