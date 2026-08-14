"""Backfills "Виробник" (manufacturer) on Prom.ua products Rozetka's feed
validation flags with a missing vendor tag - Rozetka's own feed-validation
report is the only place that says which existing products still need it,
since Prom.ua's product API doesn't expose this at all.

Vendor is set to the fixed catch-all value _NO_BRAND_VALUE ("No Name")
rather than each product's actual brand, since there's no API to register a
new manufacturer or check whether one already exists, and "Виробник" is a
constrained picker against Prom.ua's own manufacturer directory.

KNOWN BROKEN as of 2026-08-03: the bulk XLS import's "Виробник" column
appears to only take effect when a product is first CREATED, not when an
already-existing product is UPDATED through this script's re-import path -
confirmed by testing several values ("Без бренду", "No Name", "Noname",
including one already used successfully by other live products in the
account) that all reported a clean `updated: N, errors: []` import status,
yet none of them showed up in the actual exported rozetka_feed.xml <vendor>
tag afterward. Also tried setting it via the "characteristics" triple
columns (Назва_Характеристики/Значення_Характеристики) instead of the
plain column - same silent no-op. POST /products/edit has no equivalent
field at all ("Unknown field." for manufacturer/vendor/producer/brand/etc).
So --execute currently produces a false sense of success: Prom.ua reports
the import as fine, but the vendor tag genuinely does not change on
already-live products. Real brand names set at product-creation time (via
telegram_bot.py's normal listing flow) DO stick, which is consistent with
this being a create-only field on Prom.ua's side. Until a working API path
is found, use rozetka_vendor_manual_checklist.csv-style output and fix
Виробник by hand in Prom.ua's product-edit UI instead.

Rozetka's other big warning category - missing structured "параметри"
(characteristics) - is NOT handled here. Characteristics turned out to be
strict, per-category typed fields (numeric ranges, dropdowns) defined in
Prom.ua's own category schema, which its public API doesn't expose - an
earlier version of this script tried writing generic characteristic names
and Prom.ua's import silently discarded all of them. Fixing that reliably
needs the category-specific field names, which for now means the Prom.ua
product-edit UI, by hand.

Products in a Rozetka-banned category/brand aren't touched at all either -
no field fix helps those, they need a human decision (recategorize on
Prom.ua, or drop the Rozetka label) and are just listed for manual
follow-up.

By default this only prints what it *would* change - nothing is written to
Prom.ua until --execute is passed, since this mutates already-live listings
(unlike olx_sync.py, which only ever creates new, separately-tracked
adverts).

Usage:
    python -m prom_auto.rozetka_backfill [--limit N] [--execute]

Requires ROZETKA_VALIDATE_HASH in .env - the hash= query param from your
own https://seller.rozetka.com.ua/gomer/pricevalidate/check/items?hash=...
report page.
"""
from __future__ import annotations

import argparse
import logging
import re
import time

import requests

from . import config, prom_client, xlsx_builder

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logger = logging.getLogger(__name__)

_VALIDATE_URL = "https://seller.rozetka.com.ua/gomer/pricevalidate/check/items"
_ROW_PATTERN = re.compile(r'<tr data-key="\d+">(.*?)</tr>\n', re.S)
_OFFER_ID_PATTERN = re.compile(r"Offer ID:\s*(\d+)")
_ERROR_PATTERN = re.compile(r'<li class="item-\d+"><span[^>]*>(.*?)</span></li>')
# "<div class="summary">Показаны записи <b>1-20</b> из <b>384</b>.</div>" -
# the page's own reported total (last <b> number in that div, robust to
# language - uk/ru phrasing differs but the "N-M of/из/з TOTAL" shape
# doesn't). Used to bound pagination: Rozetka's server doesn't return an
# empty page for a page number past the end, it just keeps re-serving
# content, so relying on "rows became empty" to stop looping never
# terminates (confirmed - an earlier run without this stopped at 18101
# "flagged products" against a real total of ~209).
_SUMMARY_DIV_PATTERN = re.compile(r'<div class="summary">(.*?)</div>')
_SUMMARY_NUMBER_PATTERN = re.compile(r"<b>(\d+)</b>")

# The only Rozetka message this script fixes.
_VENDOR_MARKER = "vendor"
# Rozetka messages no field fix helps - the category/brand is outright
# refused, so these are only ever listed for a human to recategorize or
# drop from the Rozetka feed.
_MANUAL_MARKERS = ("стоп-категор", "стоп-бренд")

# Guaranteed-present catch-all entry in Prom.ua's manufacturer directory -
# see module docstring for why the real brand name isn't used instead.
# NOTE: "No Name" was tried instead and DID show up correctly in the
# exported rozetka_feed.xml (unlike "Без бренду", never observed there for
# any product) - but the user confirmed (2026-08-03, outside this codebase)
# that "No Name" should not be used, so reverted back to "Без бренду"
# despite that not being observed to land. See module docstring's
# "known-broken" section either way.
_NO_BRAND_VALUE = "Без бренду"

# Products per single xlsx import call - matches Prom.ua's import being one
# HTTP call regardless of row count, batched mainly to keep any single
# import job (and its retry-on-failure blast radius) a manageable size.
_BATCH_SIZE = 20


def _page_total(html: str) -> int | None:
    div_match = _SUMMARY_DIV_PATTERN.search(html)
    if not div_match:
        return None
    numbers = _SUMMARY_NUMBER_PATTERN.findall(div_match.group(1))
    return int(numbers[-1]) if numbers else None


def _fetch_flagged_items(hash_: str) -> list[dict]:
    """Pages through Rozetka's warning_mark=1 report, returning
    {offer_id, errors} for every flagged product. offer_id is Prom.ua's own
    numeric product id.

    Stops once it's seen at least as many distinct offer_ids as the first
    page's own reported total (or a page contributes nothing new) - see
    _SUMMARY_DIV_PATTERN above for why "the page came back empty" alone
    can't be the stop condition here.
    """
    items = []
    seen_offer_ids: set[str] = set()
    total = None
    page = 1
    while True:
        response = requests.get(
            _VALIDATE_URL, params={"hash": hash_, "warning_mark": 1, "page": page}, timeout=30
        )
        response.raise_for_status()
        if total is None:
            total = _page_total(response.text)
        rows = _ROW_PATTERN.findall(response.text)
        if not rows:
            break

        new_on_this_page = 0
        for row in rows:
            offer_id_match = _OFFER_ID_PATTERN.search(row)
            if not offer_id_match:
                continue
            offer_id = offer_id_match.group(1)
            if offer_id in seen_offer_ids:
                continue
            seen_offer_ids.add(offer_id)
            new_on_this_page += 1
            items.append({"offer_id": offer_id, "errors": _ERROR_PATTERN.findall(row)})

        if new_on_this_page == 0:
            break
        if total is not None and len(items) >= total:
            break
        page += 1
        time.sleep(0.3)
    return items


def _classify(errors: list[str]) -> str:
    if any(marker in error for error in errors for marker in _MANUAL_MARKERS):
        return "manual"
    if any(_VENDOR_MARKER in error.lower() for error in errors):
        return "backfill"
    return "skip"


def _presence_ok(product: dict) -> bool:
    """Same safety gate olx_sync.py uses before touching a product: only
    ever act on something that's actually live, so a stale/edited/removed
    listing can't get a confusing re-import."""
    return product.get("presence") == "available" and product.get("status") == "on_display"


def _merge_key(product: dict) -> str | None:
    """Prom.ua's XLS import matches Ідентифікатор_товару/Код_товару against
    whichever of external_id/sku actually holds this bot's "vNNNN" article
    on a given product - confirmed via a live lookup that external_id comes
    back null while sku carries it (article_counter.find_max_article_number
    and olx_sync.py already check both for the same reason)."""
    return product.get("external_id") or product.get("sku") or None


def _build_backfill_row(product: dict) -> dict:
    """Only Виробник carries new data (always _NO_BRAND_VALUE - see module
    docstring); every other field here is copied straight back from
    Prom.ua's own current value for that product - Prom.ua's XLS import
    requires Назва_позиції/Опис/Ідентифікатор_товару to be non-blank on
    every row (including updates), and leaving Ціна/Наявність/Кількість out
    risks Prom.ua reinterpreting the blank as an actual change (its own
    docs warn a blank Наявність imports as "not available").
    Назва_позиції_укр/Опис_укр are deliberately left out - Prom.ua only
    updates a translation when *both* its fields are filled, so leaving
    them blank preserves the existing Ukrainian listing untouched instead
    of overwriting it with nothing.

    Note there's no "Знижка" column here at all - see _reapply_discounts()
    in main(), which restores any active native discount through the same
    dedicated /products/edit API telegram_bot.py already uses for it,
    rather than trusting the XLS import not to clear it.
    """
    merge_key = _merge_key(product)
    return {
        "Ідентифікатор_товару": merge_key,
        "Код_товару": merge_key,
        "Унікальний_ідентифікатор": product["id"],
        "Назва_позиції": product.get("name", ""),
        "Опис": product.get("description", ""),
        "Ціна": product.get("price", 0),
        "Валюта": product.get("currency") or "UAH",
        "Кількість": product.get("quantity_in_stock") or 1,
        "Наявність": "available",
        "Одиниця_виміру": "шт",
        "Виробник": _NO_BRAND_VALUE,
        "Де_знаходиться_товар": config.PROM_REGION,
    }


def _reapply_discounts(batch: list[tuple[dict, dict, dict | None]]) -> None:
    """Prom.ua's own XLS import docs don't say whether an update row with
    no "Знижка" column leaves an existing native discount alone or clears
    it - unwilling to find out on live listings, so any product in this
    batch that had one (product["discount"] from GET /products/{id},
    carried through from main()) gets it reapplied via POST /products/edit,
    the same dedicated discount API telegram_bot.py's link-mode flow
    already uses (see prom_client.edit_products)."""
    edits = [
        {"id": row["Унікальний_ідентифікатор"], "discount": discount}
        for _, row, discount in batch
        if discount
    ]
    if not edits:
        return
    result = prom_client.edit_products(edits)
    logger.info("Reapplied discount on %d product(s): %s", len(edits), result)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument(
        "--limit", type=int, default=None, help="Stop after this many products (for testing)"
    )
    parser.add_argument(
        "--execute", action="store_true", help="Actually import the changes (default: dry-run/print only)"
    )
    args = parser.parse_args()

    if not config.ROZETKA_VALIDATE_HASH:
        raise SystemExit("ROZETKA_VALIDATE_HASH is not set in .env")

    items = _fetch_flagged_items(config.ROZETKA_VALIDATE_HASH)
    logger.info("Rozetka reports %d flagged product(s)", len(items))

    rows: list[tuple[dict, dict, dict | None]] = []
    manual_review: list[dict] = []
    skipped: list[tuple[dict, str]] = []
    processed = 0

    for item in items:
        classification = _classify(item["errors"])
        if classification == "manual":
            manual_review.append(item)
            continue
        if classification == "skip":
            continue
        if args.limit and processed >= args.limit:
            break
        processed += 1

        offer_id = int(item["offer_id"])
        product = prom_client.get_product(offer_id)
        if product is None or not _presence_ok(product):
            skipped.append((item, "not found, or not available/on_display on Prom.ua"))
            continue
        if not _merge_key(product):
            skipped.append((item, "no external_id/sku on Prom.ua, can't safely merge-update"))
            continue

        row = _build_backfill_row(product)
        rows.append((item, row, product.get("discount")))
        logger.info("%s: Виробник -> %r", offer_id, row["Виробник"])

    print(
        f"\n{len(rows)} product(s) ready to backfill, {len(skipped)} skipped, "
        f"{len(manual_review)} need manual review (banned category/brand)."
    )

    if manual_review:
        print("\nNeeds manual review (Rozetka rejects the category/brand outright, no field fix helps):")
        for item in manual_review:
            print(f"  {item['offer_id']}  {item['errors']}")

    if skipped:
        print("\nSkipped:")
        for item, reason in skipped:
            print(f"  {item['offer_id']}  - {reason}")

    if not rows:
        return

    if not args.execute:
        print("\nDry-run (pass --execute to actually import). Planned changes:")
        for item, row, discount in rows:
            print(f"  {item['offer_id']} -> Виробник={row['Виробник']!r}", end="")
            print(f" (discount will be reapplied: {discount})" if discount else "")
        return

    for batch_start in range(0, len(rows), _BATCH_SIZE):
        batch = rows[batch_start : batch_start + _BATCH_SIZE]
        xlsx_bytes = xlsx_builder.build_xlsx([row for _, row, _ in batch])
        status_payload = prom_client.import_and_wait(xlsx_bytes)
        logger.info(
            "Batch %d-%d import status: %s", batch_start, batch_start + len(batch), status_payload
        )
        _reapply_discounts(batch)

    print(f"\nDone - {len(rows)} product(s) submitted for backfill.")


if __name__ == "__main__":
    main()
