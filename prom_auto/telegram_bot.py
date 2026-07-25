import logging

from telegram import InputMediaPhoto, Update
from telegram.ext import Application, ContextTypes, MessageHandler, filters

from . import (
    config,
    image_host,
    openai_client,
    page_fetch,
    product_image_scraper,
    prom_client,
    product_mapper,
    xlsx_builder,
)

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logger = logging.getLogger(__name__)

# Photos sharing a Telegram media_group_id (sent together as one album) are
# treated as multiple photos of the SAME product; a photo sent on its own is
# its own product. Telegram delivers each album photo as a separate update,
# so this is how long to wait after the last photo of a group before
# deciding the group is complete and asking whether to add another product.
ALBUM_COLLECT_DELAY = 2.5

# chat_id -> {
#   "groups": list[list[bytes]],           completed product photo-groups,
#                                           queued for _process_batch
#   "current_group": list[bytes] | None,   photos being collected for the
#                                           in-progress group
#   "current_media_group_id": str | None,  media_group_id of the in-progress
#                                           group, if any
#   "job": Job | None,                     debounce job that finalizes
#                                           current_group
#   "awaiting_continue": bool,             True once we've asked "add
#                                           another product?"
# }
_pending_batches: dict[int, dict] = {}

# chat_id -> {"queue": list[tuple[list[bytes], dict]], "awaiting": bool}. Drives the
# post-batch "what price for the sales group?" back-and-forth: one entry per
# successfully identified product, asked one at a time.
_pending_price_requests: dict[int, dict] = {}

# Ukrainian/English yes-no answers, used both for "add another product?" and
# for the per-product sales-group prompt.
_YES_ANSWERS = {"так", "да", "yes", "y"}
_NO_ANSWERS = {"ні", "ни", "нет", "no", "n"}

_FIELD_LABELS = {
    "name": "назва",
    "brand": "бренд",
    "manufacturer": "виробник",
    "country": "країна",
    "material": "матеріал",
    "color": "колір",
}


def _format_data_sources(data_sources: dict, source_urls: dict | None = None) -> str:
    """Groups fields by whether they came from reading the photo, an
    actual web match, or the model's own estimate, so it's clear what's
    verified vs. just read off the packaging. Web-sourced fields get their
    backing URL appended so the claim is checkable, not just self-reported."""
    groups: dict[str, list[str]] = {"web": [], "photo": [], "estimated": []}
    source_urls = source_urls or {}
    for field, source in (data_sources or {}).items():
        label = _FIELD_LABELS.get(field, field)
        if source == "web" and source_urls.get(field):
            label = f"{label} ({source_urls[field]})"
        groups.setdefault(source, []).append(label)

    parts = []
    if groups["web"]:
        parts.append("з пошуку: " + ", ".join(groups["web"]))
    if groups["photo"]:
        parts.append("з фото: " + ", ".join(groups["photo"]))
    if groups["estimated"]:
        parts.append("орієнтовно: " + ", ".join(groups["estimated"]))
    return "ℹ️ Джерела даних — " + "; ".join(parts) if parts else ""


def _identify_and_validate(photos: list[bytes]) -> tuple[dict, int]:
    """Runs identify_product once and enforces that any "web" data_source
    claim is backed by a real URL, downgrading it to "estimated" otherwise -
    a "web" self-report is only as trustworthy as the source_urls/
    price_source_url entry behind it, regardless of how many searches ran.

    Returns (data, search_count).
    """
    response, search_count = openai_client.identify_product(photos)
    data = openai_client.extract_json(response)
    logger.info("Full identification data (%d web search(es)): %s", search_count, data)

    data_sources = data.get("data_sources") or {}
    source_urls = data.get("source_urls") or {}
    for field, source in data_sources.items():
        if source == "web" and not (source_urls.get(field) or "").strip():
            logger.warning("Field %r marked 'web' with no source_urls entry - downgrading", field)
            data_sources[field] = "estimated"
    if data.get("price_found") and not (data.get("price_source_url") or "").strip():
        logger.warning("price_found=True with no price_source_url - downgrading")
        data["price_found"] = False

    # A real URL isn't proof the page is actually about THIS product -
    # confirmed empirically (two "web"-confirmed citations in a row were
    # both for the wrong item). Cheap sanity check: the brand/model anchor
    # must actually appear in the cited page's real text, or the citation
    # doesn't survive. No anchor at all (neither brand nor model) means
    # there's nothing to check against, so those claims can't be trusted
    # either.
    anchor = (data.get("model") or data.get("brand") or "").strip().lower()
    page_text_cache: dict[str, str | None] = {}

    def _page_has_anchor(url: str) -> bool:
        if not anchor:
            return False
        if url not in page_text_cache:
            try:
                page_text_cache[url] = page_fetch.fetch_page_text(url)
            except Exception:
                logger.warning("Could not fetch cited page %s for sanity check", url)
                page_text_cache[url] = None
        text = page_text_cache[url]
        return text is not None and anchor in text.lower()

    for field, source in list(data_sources.items()):
        if source != "web":
            continue
        url = (source_urls.get(field) or "").strip()
        if not url or not _page_has_anchor(url):
            logger.warning(
                "Field %r cites %s but anchor %r not confirmed on page - downgrading",
                field,
                url,
                anchor,
            )
            data_sources[field] = "estimated"

    if data.get("price_found"):
        price_url = (data.get("price_source_url") or "").strip()
        if not price_url or not _page_has_anchor(price_url):
            logger.warning(
                "price_source_url %r doesn't confirm anchor %r - downgrading price_found",
                price_url,
                anchor,
            )
            data["price_found"] = False

    return data, search_count


async def _identify_single_product(
    bot, chat_id: int, photos: list[bytes], index: int, total: int
) -> tuple[dict, dict] | None:
    """Runs the full identify -> validate -> (maybe swap in marketplace
    photos) pipeline for one product (one or more photos of it), sending
    progress messages prefixed with its position in the batch. Returns
    (prom_row, data), or None if this particular item failed - callers
    should skip it and keep processing the rest of the batch rather than
    aborting everything.
    """
    tag = f"[{index}/{total}]" if total > 1 else ""
    prefix = f"{tag} " if tag else ""

    try:
        # Re-host on imgbb: Telegram's own file links can 404 by the time
        # Prom.ua's async import gets around to fetching them. All of this
        # product's photos are uploaded and passed to Prom.ua as one
        # comma-separated field (its import format's convention for
        # multiple images per listing).
        image_urls = [image_host.upload_image(photo) for photo in photos]
        image_url = ", ".join(image_urls)

        await bot.send_message(chat_id=chat_id, text=f"{prefix}🔍 Розпізнаю товар...")
        data, _search_count = _identify_and_validate(photos)

        await bot.send_message(
            chat_id=chat_id,
            text=f"{prefix}🔍 Товар розпізнано: «{data.get('name', '')}». Формую картку для Prom.ua...",
        )
        sources_text = _format_data_sources(data.get("data_sources"), data.get("source_urls"))
        if sources_text:
            await bot.send_message(chat_id=chat_id, text=f"{prefix}{sources_text}")
        if data.get("price_found") and data.get("price_source_url"):
            await bot.send_message(
                chat_id=chat_id,
                text=f"{prefix}💰 Ціну підтверджено тут: {data['price_source_url']}",
            )

            # Real confirmed match (price_found gated on a real price_source_url
            # above) - swap the user's own snapshot for the seller's own listing
            # photos, which are almost always better quality/lighting. Every
            # fallback-to-own-photo path below tells the user why, instead of
            # silently keeping the original with no explanation.
            try:
                marketplace_image_urls = product_image_scraper.find_product_image_urls(
                    data["price_source_url"]
                )
            except Exception:
                logger.exception(
                    "Failed to scrape marketplace photos from %s, keeping own photo(s)",
                    data["price_source_url"],
                )
                await bot.send_message(
                    chat_id=chat_id,
                    text=(
                        f"{prefix}ℹ️ Не вдалося завантажити сторінку джерела для фото — "
                        "залишаю ваше власне фото."
                    ),
                )
                marketplace_image_urls = []

            if data.get("price_found") and not marketplace_image_urls:
                await bot.send_message(
                    chat_id=chat_id,
                    text=(
                        f"{prefix}ℹ️ На підтвердженій сторінці не знайдено фото товару — "
                        "залишаю ваше власне фото."
                    ),
                )

            good_image_bytes = []
            for url in marketplace_image_urls:
                try:
                    img_bytes = product_image_scraper.fetch_image_bytes(url)
                except Exception:
                    logger.exception("Failed to download scraped image %s, skipping", url)
                    continue
                if product_image_scraper.is_acceptable_quality(img_bytes):
                    good_image_bytes.append(img_bytes)

            if good_image_bytes:
                try:
                    rehosted_urls = [image_host.upload_image(b) for b in good_image_bytes]
                    image_url = ", ".join(rehosted_urls)
                    await bot.send_message(
                        chat_id=chat_id,
                        text=(
                            f"{prefix}🖼️ Використовую {len(rehosted_urls)} фото товару з "
                            "підтвердженого джерела замість власного знімку."
                        ),
                    )
                except Exception:
                    logger.exception(
                        "Failed to re-host scraped marketplace photos, keeping own photo(s)"
                    )
                    await bot.send_message(
                        chat_id=chat_id,
                        text=(
                            f"{prefix}ℹ️ Знайдені фото не вдалося завантажити на хостинг — "
                            "залишаю ваше власне фото."
                        ),
                    )
            elif marketplace_image_urls:
                logger.info(
                    "All %d scraped image(s) failed the quality check, keeping own photo(s)",
                    len(marketplace_image_urls),
                )
                await bot.send_message(
                    chat_id=chat_id,
                    text=(
                        f"{prefix}ℹ️ Знайдено {len(marketplace_image_urls)} фото товару на джерелі, "
                        "але вони замалі за розміром (нижче 400px) — залишаю ваше власне фото."
                    ),
                )
        if not data.get("price_found"):
            await bot.send_message(
                chat_id=chat_id,
                text=(
                    f"{prefix}⚠️ Пошук не підтвердив саме цей товар (ціну поставлено як заглушку "
                    f"{data.get('priceUAH')} грн). Перевірте назву, характеристики та ціну вручну."
                ),
            )

        return product_mapper.build_prom_product(data, image_url), data
    except Exception as exc:
        logger.exception("Failed to process product %d/%d", index, total)
        await bot.send_message(chat_id=chat_id, text=f"{prefix}❌ Помилка обробки товару: {exc}")
        return None


async def _process_batch(bot, chat_id: int, groups: list[list[bytes]]) -> None:
    """Identifies every photo group in the batch as its own separate
    product, then combines all of them into a single Prom.ua import - one
    API call for the whole batch instead of one per product, since Prom.ua's
    import endpoint is flaky/rate-limited under repeated back-to-back
    calls."""
    total = len(groups)
    photo_count = sum(len(group) for group in groups)
    photo_word = "фото" if photo_count == 1 else f"{photo_count} фото"
    product_word = "товар" if total == 1 else f"{total} товар(и/ів)"
    await bot.send_message(
        chat_id=chat_id,
        text=f"📸 Отримано {photo_word} ({product_word}). Обробляю по черзі...",
    )

    products = []
    identified = []  # (photos, data) pairs, for the sales-group price queue below
    for index, photos in enumerate(groups, start=1):
        result = await _identify_single_product(bot, chat_id, photos, index, total)
        if result is not None:
            product, data = result
            products.append(product)
            identified.append((photos, data))

    if not products:
        await bot.send_message(chat_id=chat_id, text="❌ Жоден товар не вдалося розпізнати, нічого завантажувати.")
        return

    xlsx_bytes = xlsx_builder.build_xlsx(products)

    await bot.send_message(
        chat_id=chat_id,
        text=f"⬆️ Завантажую {len(products)} товар(и/ів) до Prom.ua одним запитом...",
    )
    try:
        result = prom_client.import_file(xlsx_bytes)
        logger.info("Prom.ua import result: %s", result)
        if result.get("status") == "success" or "id" in result:
            await bot.send_message(
                chat_id=chat_id, text=f"✅ {len(products)} товар(и/ів) успішно передано в Пром"
            )
        else:
            await bot.send_message(
                chat_id=chat_id,
                text=f"❌ Помилка передачі на Пром: {result.get('error', '')}",
            )
    except prom_client.PromImportBusyError:
        logger.warning("Prom.ua import busy after retries (likely nightly restriction)")
        await bot.send_message(
            chat_id=chat_id,
            text=(
                "⏳ Prom.ua зараз не приймає імпорт (схоже на їхнє нічне обмеження). "
                "Це не помилка бота — спробуйте надіслати фото ще раз пізніше."
            ),
        )
    except Exception as exc:
        logger.exception("Prom.ua import failed")
        await bot.send_message(chat_id=chat_id, text=f"❌ Помилка передачі на Пром: {exc}")

    # Prom.ua's own outcome doesn't gate the sales-group post - that's a
    # separate customer-facing channel with its own manually-set price, not
    # tied to whether the Prom.ua import itself succeeded.
    if identified and config.SALES_GROUP_CHAT_ID:
        _pending_price_requests[chat_id] = {"queue": identified, "awaiting": False}
        await bot.send_message(
            chat_id=chat_id,
            text="💬 Тепер оберіть, які товари додати в групу продажів (по одному, у відповідь на запит):",
        )
        await _ask_next_price(bot, chat_id)


async def _ask_next_price(bot, chat_id: int) -> None:
    state = _pending_price_requests.get(chat_id)
    if not state or not state["queue"]:
        _pending_price_requests.pop(chat_id, None)
        return
    _photos, data = state["queue"][0]
    state["awaiting"] = True
    await bot.send_message(
        chat_id=chat_id,
        text=(
            f"💬 Додати «{data.get('name', '')}» в групу продажів? "
            "Якщо так - напишіть ціну, якщо ні - напишіть «ні»."
        ),
    )


async def _post_to_sales_group(bot, chat_id: int, photos: list[bytes], data: dict, price: float) -> None:
    caption = (
        f"{data.get('name', '')}\n\n"
        f"Ціна: {price:.0f} грн\n\n"
        f"Щоб купити, напишіть нашому менеджеру: {config.MANAGER_CONTACT_URL}"
    )
    try:
        if len(photos) > 1:
            # sendMediaGroup only shows the caption of the first item as the
            # album's caption - the rest are left uncaptioned on purpose.
            media = [
                InputMediaPhoto(photo, caption=caption if i == 0 else None)
                for i, photo in enumerate(photos)
            ]
            await bot.send_media_group(chat_id=config.SALES_GROUP_CHAT_ID, media=media)
        else:
            await bot.send_photo(chat_id=config.SALES_GROUP_CHAT_ID, photo=photos[0], caption=caption)
        await bot.send_message(chat_id=chat_id, text="✅ Додано в групу продажів")
    except Exception as exc:
        logger.exception("Failed to post to sales group")
        await bot.send_message(chat_id=chat_id, text=f"❌ Не вдалося додати в групу продажів: {exc}")


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not update.message.text:
        return

    chat_id = update.message.chat.id
    # Logged for every text message (not just ones we act on) so the sales
    # group's chat_id can be read straight from the logs once the bot is
    # added there and someone sends any message - negative IDs are groups.
    logger.info(
        "Text message from chat %s (%r, type=%s): %r",
        chat_id,
        update.message.chat.title,
        update.message.chat.type,
        update.message.text[:200],
    )

    answer = update.message.text.strip()
    answer_lower = answer.lower()

    batch = _pending_batches.get(chat_id)
    if batch and batch.get("awaiting_continue"):
        if answer_lower in _YES_ANSWERS:
            batch["awaiting_continue"] = False
            await context.bot.send_message(chat_id=chat_id, text="👍 Надсилайте фото наступного товару.")
        elif answer_lower in _NO_ANSWERS:
            groups = batch["groups"]
            _pending_batches.pop(chat_id, None)
            await _process_batch(context.bot, chat_id, groups)
        else:
            await context.bot.send_message(
                chat_id=chat_id, text="⚠️ Не зрозумів відповідь. Напишіть «так» або «ні»."
            )
        return

    state = _pending_price_requests.get(chat_id)
    if not state or not state.get("awaiting"):
        return

    if answer_lower in _NO_ANSWERS:
        _photos, data = state["queue"].pop(0)
        state["awaiting"] = False
        await context.bot.send_message(
            chat_id=chat_id, text=f"⏭️ Пропущено «{data.get('name', '')}», не додано в групу продажів"
        )
        await _ask_next_price(context.bot, chat_id)
        return

    try:
        price = float(answer.replace(",", "."))
    except ValueError:
        await context.bot.send_message(
            chat_id=chat_id,
            text="⚠️ Не розпізнав відповідь. Напишіть ціну числом, або «ні», щоб не додавати товар.",
        )
        return

    photos, data = state["queue"].pop(0)
    state["awaiting"] = False
    await _post_to_sales_group(context.bot, chat_id, photos, data, price)
    await _ask_next_price(context.bot, chat_id)


async def _finalize_current_group(context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = context.job.data
    batch = _pending_batches.get(chat_id)
    if not batch or not batch["current_group"]:
        return

    batch["groups"].append(batch["current_group"])
    photo_count = len(batch["current_group"])
    batch["current_group"] = None
    batch["current_media_group_id"] = None
    batch["job"] = None
    batch["awaiting_continue"] = True

    photo_word = "фото" if photo_count == 1 else f"{photo_count} фото"
    await context.bot.send_message(
        chat_id=chat_id,
        text=(
            f"📦 Товар #{len(batch['groups'])} додано до черги ({photo_word}). "
            "Додати ще один товар? (так/ні)"
        ),
    )


async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not update.message.photo:
        return

    chat_id = update.message.chat.id
    media_group_id = update.message.media_group_id
    logger.info("Photo received from chat %s (media_group_id=%s)", chat_id, media_group_id)

    photo = update.message.photo[-1]
    photo_file = await photo.get_file()
    image_bytes = bytes(await photo_file.download_as_bytearray())

    # Photos sharing a media_group_id (sent together as one Telegram album)
    # are collected as multiple photos of the SAME product; anything else
    # (a lone photo, or one whose media_group_id differs from the group
    # currently being collected) starts a new product.
    batch = _pending_batches.setdefault(
        chat_id,
        {
            "groups": [],
            "current_group": None,
            "current_media_group_id": None,
            "job": None,
            "awaiting_continue": False,
        },
    )

    # A photo arriving while we're waiting on "add another product?" starts
    # the next product implicitly - no need to make the user type "так" first.
    batch["awaiting_continue"] = False

    same_group = (
        batch["current_group"] is not None
        and media_group_id is not None
        and media_group_id == batch["current_media_group_id"]
    )
    if same_group:
        batch["current_group"].append(image_bytes)
    else:
        if batch["current_group"]:
            batch["groups"].append(batch["current_group"])
        batch["current_group"] = [image_bytes]
        batch["current_media_group_id"] = media_group_id

    if batch["job"] is not None:
        batch["job"].schedule_removal()
    batch["job"] = context.job_queue.run_once(_finalize_current_group, ALBUM_COLLECT_DELAY, data=chat_id)


def main() -> None:
    if not config.TELEGRAM_TOKEN:
        print("Please set the TELEGRAM_TOKEN environment variable before running the bot.")
        return

    application = Application.builder().token(config.TELEGRAM_TOKEN).build()
    application.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    application.run_polling()


if __name__ == "__main__":
    main()
