import asyncio
import json
import logging
import os
from pathlib import Path
from urllib.parse import urljoin

import pandas as pd
import requests
try:
    from dotenv import load_dotenv
except ImportError as exc:
    raise RuntimeError(
        "Missing dependency 'python-dotenv'. Install it with 'pip install -r requirements.txt'."
    ) from exc
from playwright.async_api import TimeoutError as PlaywrightTimeoutError
from playwright.async_api import async_playwright

BASE_DIR = Path(__file__).resolve().parent
ENV_FILE = BASE_DIR / ".env"
CONFIG_FILE = BASE_DIR / "config.json"
LOG_FILE = BASE_DIR / "scraper.log"
PRODUCT_LIMIT = 10
MAX_NAVIGATION_ATTEMPTS = 3
NAVIGATION_TIMEOUT_MS = 60_000
REQUEST_TIMEOUT_SECONDS = 30
BOLD_GREEN = "\033[1;32m"
BOLD_CYAN = "\033[1;36m"
BOLD_MAGENTA = "\033[1;35m"
RESET_STYLE = "\033[0m"


def load_settings(config_path: Path) -> dict[str, object]:
    """Load required user-adjustable settings from config.json."""
    try:
        with config_path.open("r", encoding="utf-8") as config_file:
            settings = json.load(config_file)
    except FileNotFoundError as exc:
        raise RuntimeError(f"Missing configuration file: {config_path.name}") from exc
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Invalid JSON in configuration file: {config_path.name}") from exc

    required_keys = ("target_url", "scroll_count", "output_file_name", "webhook_url")
    missing_keys = [key for key in required_keys if key not in settings]
    if missing_keys:
        missing = ", ".join(missing_keys)
        raise RuntimeError(f"Missing required config value(s): {missing}")

    if not isinstance(settings["target_url"], str) or not settings["target_url"].strip():
        raise RuntimeError("Config value 'target_url' must be a non-empty string")
    if not isinstance(settings["webhook_url"], str) or not settings["webhook_url"].strip():
        raise RuntimeError("Config value 'webhook_url' must be a non-empty string")
    if not isinstance(settings["output_file_name"], str) or not settings["output_file_name"].strip():
        raise RuntimeError("Config value 'output_file_name' must be a non-empty string")
    if not isinstance(settings["scroll_count"], int) or settings["scroll_count"] < 0:
        raise RuntimeError("Config value 'scroll_count' must be a non-negative integer")

    return settings


def get_required_env(name: str) -> str:
    """Read a required environment variable after loading .env."""
    value = os.getenv(name)
    if not value:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value


load_dotenv(ENV_FILE)
SETTINGS = load_settings(CONFIG_FILE)
TARGET_URL = SETTINGS["target_url"]
SCROLL_COUNT = SETTINGS["scroll_count"]
WEBHOOK_URL = SETTINGS["webhook_url"]
OUTPUT_FILE = BASE_DIR / SETTINGS["output_file_name"]
MAKE_API_KEY = get_required_env("MAKE_API_KEY")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
PYTHONANYWHERE_SECRET_KEY = os.getenv("PYTHONANYWHERE_SECRET_KEY", "")


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


async def apply_scroll(page, scroll_count: int) -> None:
    """Scroll the page when a target site requires lazy-loaded content."""
    for scroll_index in range(scroll_count):
        await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        await page.wait_for_timeout(500)
        logger.info("Completed scroll %s/%s", scroll_index + 1, scroll_count)


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
                    "product_url": urljoin(TARGET_URL, relative_url),
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
                TARGET_URL,
                attempt,
                MAX_NAVIGATION_ATTEMPTS,
            )
            await page.goto(
                TARGET_URL, wait_until="domcontentloaded", timeout=NAVIGATION_TIMEOUT_MS
            )
            await page.wait_for_selector(
                "article.product_pod", timeout=NAVIGATION_TIMEOUT_MS
            )
            return
        except PlaywrightTimeoutError as exc:
            if attempt == MAX_NAVIGATION_ATTEMPTS:
                logger.exception(
                    "Timed out while loading %s after %s attempts: %s",
                    TARGET_URL,
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
                    TARGET_URL,
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
            await apply_scroll(page, SCROLL_COUNT)
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

        secure_url = f"{WEBHOOK_URL}?api_key={MAKE_API_KEY}"

        response = requests.post(
            secure_url, json=payload, timeout=REQUEST_TIMEOUT_SECONDS
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
