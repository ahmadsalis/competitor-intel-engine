import asyncio
import csv
import json
import os
import re
from datetime import date
from pathlib import Path
from urllib.parse import urljoin

import aiohttp
from playwright.async_api import async_playwright

BASE_URL = "https://homestra.com/list/houses-for-sale/norway/?property-type=house"
CSV_PATH = Path("listings.csv")
API_CLEAN_URL = "http://127.0.0.1:5000/clean"
MAX_LISTINGS = 10


def regex_clean_price(price_text: str) -> int | None:
    if not price_text:
        return None

    cleaned = re.findall(r"\d[\d,]*", price_text)
    if not cleaned:
        return None

    normalized = cleaned[0].replace(",", "")
    try:
        return int(normalized)
    except ValueError:
        return None


async def clean_price(price_text: str) -> int | None:
    if not price_text:
        return None

    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                API_CLEAN_URL, json={"price": price_text}, timeout=5
            ) as response:
                if response.status == 200:
                    payload = await response.json()
                    if isinstance(payload, dict):
                        for key in ("clean_price", "price_clean", "cleaned", "value"):
                            if key in payload:
                                return int(payload[key])
                        if "price" in payload and isinstance(
                            payload["price"], (int, float, str)
                        ):
                            return int(payload["price"])
                    if isinstance(payload, (int, float, str)):
                        return int(payload)

    except Exception:
        pass

    return regex_clean_price(price_text)


def get_existing_scrape_date() -> str | None:
    if not CSV_PATH.exists():
        return None

    try:
        with CSV_PATH.open("r", newline="", encoding="utf-8") as csvfile:
            reader = csv.DictReader(csvfile)
            for row in reader:
                return row.get("scrape_date")
    except Exception:
        return None

    return None


def write_listings(rows: list[dict[str, str]]) -> None:
    today = date.today().isoformat()
    current_date = get_existing_scrape_date()
    write_header = current_date != today
    mode = "a" if not write_header and CSV_PATH.exists() else "w"

    with CSV_PATH.open(mode, newline="", encoding="utf-8") as csvfile:
        fieldnames = ["scrape_date", "title", "price_raw", "price_clean", "link"]
        writer = csv.DictWriter(csvfile, fieldnames=fieldnames)
        if mode == "w":
            writer.writeheader()
        for row in rows:
            writer.writerow(
                {
                    "scrape_date": today,
                    "title": row.get("title", ""),
                    "price_raw": row.get("price_raw", ""),
                    "price_clean": row.get("price_clean", ""),
                    "link": row.get("link", ""),
                }
            )

    if mode == "w":
        print(f"Wrote {len(rows)} listings to {CSV_PATH}.")
    else:
        print(f"Appended {len(rows)} listings to {CSV_PATH}.")


async def extract_text(locator) -> str | None:
    try:
        text = await locator.inner_text()
        if text:
            return text.strip()
    except Exception:
        pass
    return None


async def extract_listing_data(listing) -> dict[str, str | None]:
    selectors = [
        "h2",
        "h3",
        "a",
        ".title",
        ".property-title",
        ".listing-title",
        ".card-title",
        ".name",
        "span",
        "p",
    ]
    title = None
    for selector in selectors:
        candidate = listing.locator(selector)
        if await candidate.count() > 0:
            value = await extract_text(candidate.first)
            if value:
                title = value
                break

    price_selectors = [
        ".price",
        ".listing-price",
        ".property-price",
        "span.price",
        "strong",
        "p",
    ]
    price = None
    for selector in price_selectors:
        candidate = listing.locator(selector)
        if await candidate.count() > 0:
            value = await extract_text(candidate.first)
            if value and re.search(r"\d", value):
                price = value
                break

    link = None
    try:
        anchor = listing.locator("a[href]")
        if await anchor.count() > 0:
            href = await anchor.first.get_attribute("href")
            if href:
                link = urljoin(BASE_URL, href)
    except Exception:
        pass

    if not link:
        link = BASE_URL

    return {
        "title": title or "Unknown Title",
        "price_raw": price or "Unknown Price",
        "link": link,
    }


async def extract_listings_from_next_data(page) -> list[dict[str, str]]:
    try:
        script = page.locator("script#__NEXT_DATA__")
        if await script.count() == 0:
            return []

        payload = await script.first.inner_text()
        data = json.loads(payload)
        apollo = data.get("props", {}).get("pageProps", {}).get("__APOLLO_STATE__", {})
        root = apollo.get("ROOT_QUERY", {})
        search_key = next(
            (key for key in root if key.startswith("listSearch(")),
            None,
        )
        if not search_key:
            return []

        items = root.get(search_key, {}).get("items", [])
        listings: list[dict[str, str]] = []
        for item in items[:MAX_LISTINGS]:
            ref = item.get("__ref") if isinstance(item, dict) else None
            if not isinstance(ref, str):
                continue

            property_data = apollo.get(ref, {})
            if not isinstance(property_data, dict):
                continue

            title = (
                property_data.get("title")
                or property_data.get("address")
                or property_data.get("slug")
                or property_data.get("city")
                or "Unknown Title"
            )
            price = property_data.get("price")
            price_raw = str(int(price)) if isinstance(price, (int, float)) else "Unknown Price"
            slug = property_data.get("slug")
            link = (
                urljoin("https://homestra.com", f"/property/{slug}/")
                if isinstance(slug, str)
                else BASE_URL
            )
            listings.append(
                {
                    "title": title,
                    "price_raw": price_raw,
                    "price_clean": int(price) if isinstance(price, (int, float)) else "",
                    "link": link,
                }
            )

        return listings
    except Exception:
        return []


async def scrape_listings() -> list[dict[str, str]]:
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)
        page = await browser.new_page()
        print(f"Navigating to {BASE_URL}...")
        await page.goto(BASE_URL, wait_until="networkidle", timeout=45000)
        await page.wait_for_timeout(3000)

        rows = await extract_listings_from_next_data(page)
        if rows:
            print(f"Found {len(rows)} listings via JSON data.")
            await browser.close()
            return rows

        card_selector = ", ".join(
            [
                "article",
                ".listing-card",
                ".property-card",
                ".listing",
                ".property",
                ".home-card",
                ".result-card",
                "[data-testid='listing-card']",
            ]
        )
        locator = page.locator(card_selector)
        total = await locator.count()
        if total == 0:
            print(
                "No listing cards found with default selectors. Trying fallback selectors..."
            )
            locator = page.locator(
                "li, div[data-testid='property-card'], div[class*='listing']"
            )
            total = await locator.count()

        row_count = min(total, MAX_LISTINGS)
        print(f"Found {total} listing candidates, scraping first {row_count}.")

        rows = []
        for index in range(row_count):
            element = locator.nth(index)
            record = await extract_listing_data(element)
            record["price_clean"] = await clean_price(record["price_raw"])
            rows.append(
                {
                    "title": record["title"],
                    "price_raw": record["price_raw"],
                    "price_clean": (
                        record["price_clean"]
                        if record["price_clean"] is not None
                        else ""
                    ),
                    "link": record["link"],
                }
            )

        await browser.close()
        return rows


async def run_scraper() -> None:
    print("Starting scrape job...")
    try:
        rows = await scrape_listings()
        if rows:
            write_listings(rows)
        else:
            print("No data scraped.")
    except Exception as exc:
        print(f"Scraper error: {exc}")


async def main() -> None:
    await run_scraper()


if __name__ == "__main__":
    asyncio.run(main())
