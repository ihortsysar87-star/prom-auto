"""Posts products straight from vixi.prom.ua's own Rozetka export feed to
the Telegram sales group (config.SALES_GROUP_CHAT_ID) - bypassing the usual
photo/link identify flow entirely, since the feed already carries everything
a sales-group post needs (name, price, photos) for products already live on
Prom.ua.

Selection is by article ("vNNNN", the same code article_counter.py hands
out) - every offer whose numeric part is >= --from-article's is posted, feed
order otherwise ignored. The feed can list the same article twice (seen in
practice - two distinct offers sharing one article, a data issue on the
source side); both are posted rather than silently dropped. Out-of-stock
offers (available="false" or quantity_in_stock <= 0) are skipped - the
article threshold only picks a starting point among what's actually
sellable.

The article code lives in <vendorCode> on this feed (products_feed.xml);
some older exports used a plain <article> tag instead, so both are checked.

Posted price is the feed's already-discounted <price> reduced by another
15% (not layered on <price_old>, the pre-discount price), per how this was
requested - mirrors _post_to_sales_group's caption format in telegram_bot.py
so posts look identical either way.

Usage:
    python -m prom_auto.telegram_feed_post "<feed_url>" --from-article v0541
    python -m prom_auto.telegram_feed_post "<feed_url>" --from-article v0541 --dry-run
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import re
import xml.etree.ElementTree as ET

import requests
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


def fetch_offers(feed_url: str) -> list[ET.Element]:
    resp = requests.get(feed_url, timeout=30)
    resp.raise_for_status()
    root = ET.fromstring(resp.content)
    return list(root.find("shop").find("offers"))


def _in_stock(offer: ET.Element) -> bool:
    if offer.get("available") == "false":
        return False
    qty = offer.findtext("quantity_in_stock")
    if qty is not None and int(qty) <= 0:
        return False
    return True


def select_from_article(offers: list[ET.Element], start_article: str) -> list[tuple[str, ET.Element]]:
    match = _ARTICLE_PATTERN.match(start_article)
    if not match:
        raise ValueError(f"--from-article must look like 'v0541', got {start_article!r}")
    threshold = int(match.group(1))

    numbered = []
    for offer in offers:
        if not _in_stock(offer):
            continue
        article = offer.findtext("vendorCode") or offer.findtext("article") or ""
        m = _ARTICLE_PATTERN.match(article)
        if m and int(m.group(1)) >= threshold:
            numbered.append((int(m.group(1)), article, offer))
    numbered.sort(key=lambda t: t[0])
    return [(article, offer) for _, article, offer in numbered]


def _caption(name: str, price: float) -> str:
    return (
        f"{name}\n\n"
        f"Ціна: {price:.0f} грн\n\n"
        f"Щоб купити, напишіть нашому менеджеру: {config.MANAGER_CONTACT_URL}"
    )


async def _post_offer(bot: Bot | None, article: str, offer: ET.Element, dry_run: bool) -> None:
    name = offer.findtext("name_ua") or offer.findtext("name") or ""
    price = float(offer.findtext("price"))
    discounted = price * (1 - _DISCOUNT_RATE)
    pictures = [p.text for p in offer.findall("picture") if p.text][:_MAX_MEDIA_GROUP_PHOTOS]
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


async def run(feed_url: str, start_article: str, dry_run: bool, delay: float) -> None:
    offers = fetch_offers(feed_url)
    selected = select_from_article(offers, start_article)
    logger.info("Selected %d offer(s) from %s onward", len(selected), start_article)

    bot = None if dry_run else Bot(token=config.TELEGRAM_TOKEN)
    for i, (article, offer) in enumerate(selected):
        await _post_offer(bot, article, offer, dry_run)
        if not dry_run and i < len(selected) - 1:
            await asyncio.sleep(delay)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("feed_url", help="Rozetka export feed URL (yml_catalog XML)")
    parser.add_argument("--from-article", default="v0000", help="Post offers from this article onward, e.g. v0541")
    parser.add_argument("--dry-run", action="store_true", help="Print what would be posted instead of posting")
    parser.add_argument("--delay", type=float, default=6.0, help="Seconds to wait between posts (default: 6)")
    args = parser.parse_args()

    if not args.dry_run and not config.SALES_GROUP_CHAT_ID:
        parser.error("SALES_GROUP_CHAT_ID is not set in .env")
    if not args.dry_run and not config.TELEGRAM_TOKEN:
        parser.error("TELEGRAM_TOKEN is not set in .env")

    asyncio.run(run(args.feed_url, args.from_article, args.dry_run, args.delay))


if __name__ == "__main__":
    main()
