"""Posts products from a Prom.ua "products export" CSV (the file you get from
my.prom.ua's Товари → Экспорт/Импорт → CSV export) to the Telegram sales
group (config.SALES_GROUP_CHAT_ID) - same selection/caption/discount rules
as telegram_feed_post.py, just sourced from this CSV instead of the
Rozetka XML feed.

Selection is by article ("vNNNN", the same code article_counter.py hands
out, found in Код_товару) - every product whose numeric part is
>= --from-article's is posted, file order otherwise ignored. Out-of-stock
products (Наявність != "+") are skipped - the article threshold only picks
a starting point among what's actually sellable.

Posted price is the CSV's Ціна reduced by 15%, mirroring
_post_to_sales_group's caption format in telegram_bot.py / the XML-feed
version in telegram_feed_post.py so posts look identical either way.

Usage:
    python -m prom_auto.telegram_csv_post "<csv_path>" --from-article v0658
    python -m prom_auto.telegram_csv_post "<csv_path>" --from-article v0658 --dry-run
"""
from __future__ import annotations

import argparse
import asyncio
import csv
import logging
import re

from telegram import Bot, InputMediaPhoto
from telegram.error import RetryAfter

from . import config

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logger = logging.getLogger(__name__)

_ARTICLE_PATTERN = re.compile(r"^v(\d+)$", re.IGNORECASE)
_DISCOUNT_RATE = 0.15
# Telegram's own sendMediaGroup cap.
_MAX_MEDIA_GROUP_PHOTOS = 10


def fetch_rows(csv_path: str) -> list[dict]:
    with open(csv_path, newline="", encoding="utf-8-sig") as f:
        return list(csv.DictReader(f))


def _in_stock(row: dict) -> bool:
    return row.get("Наявність", "").strip() == "+"


def select_from_article(rows: list[dict], start_article: str) -> list[tuple[str, dict]]:
    match = _ARTICLE_PATTERN.match(start_article)
    if not match:
        raise ValueError(f"--from-article must look like 'v0541', got {start_article!r}")
    threshold = int(match.group(1))

    numbered = []
    for row in rows:
        if not _in_stock(row):
            continue
        article = row.get("Код_товару", "")
        m = _ARTICLE_PATTERN.match(article)
        if m and int(m.group(1)) >= threshold:
            numbered.append((int(m.group(1)), article, row))
    numbered.sort(key=lambda t: t[0])
    return [(article, row) for _, article, row in numbered]


def _caption(name: str, price: float) -> str:
    return (
        f"{name}\n\n"
        f"Ціна: {price:.0f} грн\n\n"
        f"Щоб купити, напишіть нашому менеджеру: {config.MANAGER_CONTACT_URL}"
    )


async def _post_row(bot: Bot | None, article: str, row: dict, dry_run: bool) -> None:
    name = row.get("Назва_позиції_укр") or row.get("Назва_позиції") or ""
    price = float((row.get("Ціна") or "0").replace(",", "."))
    discounted = price * (1 - _DISCOUNT_RATE)
    pictures = [
        p.strip() for p in (row.get("Посилання_зображення") or "").split(",") if p.strip()
    ][:_MAX_MEDIA_GROUP_PHOTOS]
    caption = _caption(name, discounted)

    if dry_run:
        logger.info("[dry-run] %s (%d photos): %.0f грн\n%s", article, len(pictures), discounted, caption)
        return

    async def _send() -> None:
        if not pictures:
            await bot.send_message(chat_id=config.SALES_GROUP_CHAT_ID, text=caption, disable_notification=True)
        elif len(pictures) > 1:
            # sendMediaGroup only shows the caption of the first item as the
            # album's caption - the rest are left uncaptioned on purpose.
            media = [InputMediaPhoto(p, caption=caption if i == 0 else None) for i, p in enumerate(pictures)]
            await bot.send_media_group(chat_id=config.SALES_GROUP_CHAT_ID, media=media, disable_notification=True)
        else:
            await bot.send_photo(
                chat_id=config.SALES_GROUP_CHAT_ID,
                photo=pictures[0],
                caption=caption,
                disable_notification=True,
            )

    # Telegram's flood control on a single group is easy to trip when
    # posting dozens of products back-to-back - honor its own requested
    # cooldown and retry once rather than aborting the whole batch.
    try:
        await _send()
    except RetryAfter as exc:
        logger.warning("Flood control on %s, waiting %ss", article, exc.retry_after)
        await asyncio.sleep(exc.retry_after + 1)
        await _send()
    logger.info("Posted %s (%.0f грн)", article, discounted)


async def run(csv_path: str, start_article: str, dry_run: bool, delay: float) -> None:
    rows = fetch_rows(csv_path)
    selected = select_from_article(rows, start_article)
    logger.info("Selected %d product(s) from %s onward", len(selected), start_article)

    bot = None if dry_run else Bot(token=config.TELEGRAM_TOKEN)
    for i, (article, row) in enumerate(selected):
        await _post_row(bot, article, row, dry_run)
        if not dry_run and i < len(selected) - 1:
            await asyncio.sleep(delay)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("csv_path", help="Prom.ua products export CSV path")
    parser.add_argument("--from-article", default="v0000", help="Post products from this article onward, e.g. v0658")
    parser.add_argument("--dry-run", action="store_true", help="Print what would be posted instead of posting")
    parser.add_argument("--delay", type=float, default=6.0, help="Seconds to wait between posts (default: 6)")
    args = parser.parse_args()

    if not args.dry_run and not config.SALES_GROUP_CHAT_ID:
        parser.error("SALES_GROUP_CHAT_ID is not set in .env")
    if not args.dry_run and not config.TELEGRAM_TOKEN:
        parser.error("TELEGRAM_TOKEN is not set in .env")

    asyncio.run(run(args.csv_path, args.from_article, args.dry_run, args.delay))


if __name__ == "__main__":
    main()
