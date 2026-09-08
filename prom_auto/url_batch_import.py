"""Runs telegram_bot.py's link-mode pipeline (fetch -> OpenAI extract ->
price/characteristics/description -> Prom.ua import -> native discount) for
a batch of source URLs given directly on the command line/JSON, instead of
through an interactive Telegram chat - for bulk one-off listing jobs where
typing each URL into the bot one at a time isn't practical.

Behavior deliberately mirrors telegram_bot._finalize_url_batch /
_apply_link_mode_discounts exactly: one xlsx, one Prom.ua import call, then
Prom.ua's own discount API for every item except those given an explicit
--price override (an override is a final store price, not a source price to
discount further, same as a manually-typed price in the chat flow).

Usage:
    python -m prom_auto.url_batch_import urls.json
    python -m prom_auto.url_batch_import urls.json --dry-run

urls.json: a JSON array of either a plain URL string, or an object:
{"url": ..., "price": 430} to override that item's price (UAH) instead of
using the source page's price, {"url": ..., "description": "..."} to
override the listing's description instead of the one scraped/generated from
the source page (given verbatim in one language - translated into the
matched UA/RU pair the same way a scraped raw description is, see
product_data_extractor.identify_product_from_html), and/or {"url": ...,
"prefetched": {"page_text": ..., "json_ld": {...} | null, "image_urls":
[...]}} for a page fetched by some other means (e.g. a real browser session,
for a site whose Cloudflare challenge blocks both this tool's direct fetch
and its reader-proxy fallback) instead of this tool fetching the URL itself.
"""
from __future__ import annotations

import argparse
import calendar
import datetime
import json
import logging
import time

from . import config, openai_client, product_data_extractor, product_mapper, prom_client, xlsx_builder

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logger = logging.getLogger(__name__)


def _one_month_from(d: datetime.date) -> datetime.date:
    month = d.month + 1
    year = d.year + (month - 1) // 12
    month = (month - 1) % 12 + 1
    last_day = calendar.monthrange(year, month)[1]
    return d.replace(year=year, month=month, day=min(d.day, last_day))


def load_entries(path: str) -> list[tuple[str, float | None, dict | None, str | None]]:
    with open(path, encoding="utf-8") as f:
        raw = json.load(f)
    entries = []
    for item in raw:
        if isinstance(item, str):
            entries.append((item, None, None, None))
        else:
            entries.append(
                (item["url"], item.get("price"), item.get("prefetched"), item.get("description"))
            )
    return entries


def build_products(
    entries: list[tuple[str, float | None, dict | None, str | None]]
) -> tuple[list[dict], list[tuple[list[str], dict]]]:
    products: list[dict] = []
    data_list: list[tuple[list[str], dict]] = []

    for i, (url, manual_price, prefetched, manual_description) in enumerate(entries, start=1):
        tag = f"[{i}/{len(entries)}]"
        logger.info("%s Fetching %s", tag, url)
        try:
            if prefetched:
                data, image_urls = product_data_extractor.identify_product_from_html(
                    url,
                    prefetched["page_text"],
                    is_html=False,
                    image_urls=prefetched.get("image_urls"),
                    json_ld=prefetched.get("json_ld"),
                )
            else:
                data, image_urls = product_data_extractor.identify_product_from_url(url)
        except product_data_extractor.ProductNotFoundError as exc:
            logger.error("%s Not recognized as a product: %s", tag, exc)
            continue
        except Exception:
            logger.exception("%s Failed to process URL", tag)
            continue

        if manual_price is not None:
            data["priceUAH"] = manual_price
            data["price_found"] = True
            data["manual_price"] = True
        elif not data.get("price_found"):
            logger.error("%s No price found on page and no --price override given, skipping: %s", tag, url)
            continue

        if manual_description:
            try:
                translated = openai_client.translate_description(manual_description)
                data["description"] = translated.get("description") or manual_description
                data["description_ru"] = translated.get("description_ru")
            except Exception:
                logger.exception("%s Description override translation failed, using verbatim text for both languages", tag)
                data["description"] = manual_description
                data["description_ru"] = manual_description

        if not image_urls:
            logger.warning("%s No images found - listing will have none", tag)

        try:
            prom_row = product_mapper.build_prom_product(data, ", ".join(image_urls))
        except ValueError as exc:
            logger.error("%s %s", tag, exc)
            continue

        logger.info(
            "%s %s -> %s (%s грн, %s)",
            tag,
            prom_row["Код_товару"],
            data.get("name"),
            prom_row["Ціна"],
            "manual price" if manual_price is not None else "source price, 5% Prom discount to follow",
        )
        products.append(prom_row)
        data_list.append((image_urls, data))

    return products, data_list


def apply_link_mode_discounts(products: list[dict], data_list: list[tuple[list[str], dict]]) -> None:
    manual_articles = {
        product["Ідентифікатор_товару"]
        for product, (_image_urls, data) in zip(products, data_list)
        if data.get("manual_price")
    }

    edits = []
    missing = []
    for product in products:
        article = product["Ідентифікатор_товару"]
        if article in manual_articles:
            continue
        try:
            prom_product = prom_client.get_product_by_external_id(article)
        except Exception:
            logger.exception("Failed to look up product %s for discount", article)
            continue
        if prom_product is None:
            missing.append(article)
            continue
        today = datetime.date.today()
        edits.append(
            {
                "id": prom_product["id"],
                "discount": {
                    "value": product_data_extractor.DISCOUNT_RATE * 100,
                    "type": "percent",
                    "date_start": today.strftime("%d.%m.%Y"),
                    "date_end": _one_month_from(today).strftime("%d.%m.%Y"),
                },
            }
        )

    if missing:
        logger.warning("Not found in Prom.ua yet for discount: %s", missing)
    if not edits:
        return

    result = prom_client.edit_products(edits)
    logger.info(
        "Discount applied to %d/%d products: %s",
        len(result.get("processed_ids") or []),
        len(edits),
        result.get("errors") or "no errors",
    )


def run(path: str, dry_run: bool) -> None:
    entries = load_entries(path)
    products, data_list = build_products(entries)
    logger.info("Built %d/%d product(s)", len(products), len(entries))
    if not products:
        return

    if dry_run:
        logger.info("[dry-run] skipping Prom.ua import")
        return

    xlsx_bytes = xlsx_builder.build_xlsx(products)
    status_payload = prom_client.import_and_wait(xlsx_bytes)
    logger.info("Prom.ua import status: %s", status_payload)

    time.sleep(5)  # let Prom.ua's async import settle before looking products up by external_id
    apply_link_mode_discounts(products, data_list)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("urls_file", help="Path to a JSON file: array of URL strings or {url, price} objects")
    parser.add_argument("--dry-run", action="store_true", help="Extract and print, but don't import to Prom.ua")
    args = parser.parse_args()

    if not args.dry_run and not config.PROM_API_TOKEN:
        parser.error("PROM_API_TOKEN is not set in .env")

    run(args.urls_file, args.dry_run)


if __name__ == "__main__":
    main()
