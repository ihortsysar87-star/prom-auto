import logging

from telegram import Update
from telegram.ext import Application, ContextTypes, MessageHandler, filters

from . import (
    config,
    image_host,
    openai_client,
    product_image_scraper,
    prom_client,
    product_mapper,
    reverse_image_search,
    xlsx_builder,
)

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logger = logging.getLogger(__name__)

# Every photo sent is treated as its own separate product (even ones sent
# together as one Telegram album) - this is how many products a single
# message can identify at once. Waits this long after the last photo before
# assuming the user is done sending and starts processing the whole batch.
BATCH_FLUSH_DELAY = 8.0

_pending_batches: dict[int, dict] = {}

# chat_id -> {"queue": list[tuple[bytes, dict]], "awaiting": bool}. Drives the
# post-batch "what price for the sales group?" back-and-forth: one entry per
# successfully identified product, asked one at a time.
_pending_price_requests: dict[int, dict] = {}

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


def _identify_and_validate(photos: list[bytes], image_search_hints: dict | None) -> tuple[dict, int]:
    """Runs identify_product once and enforces that any "web" data_source
    claim is backed by a real URL, downgrading it to "estimated" otherwise -
    a "web" self-report is only as trustworthy as the source_urls/
    price_source_url entry behind it, regardless of how many searches ran.

    Returns (data, search_count).
    """
    response, search_count = openai_client.identify_product(photos, image_search_hints=image_search_hints)
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

    return data, search_count


async def _identify_single_product(
    bot, chat_id: int, photo: bytes, index: int, total: int
) -> tuple[dict, dict] | None:
    """Runs the full identify -> validate -> (maybe swap in marketplace
    photos) pipeline for one photo/product, sending progress messages
    prefixed with its position in the batch. Returns (prom_row, data), or
    None if this particular item failed - callers should skip it and keep
    processing the rest of the batch rather than aborting everything.
    """
    tag = f"[{index}/{total}]" if total > 1 else ""
    prefix = f"{tag} " if tag else ""

    try:
        # Re-host on imgbb: Telegram's own file links can 404 by the time
        # Prom.ua's async import gets around to fetching them.
        image_url = image_host.upload_image(photo)

        await bot.send_message(chat_id=chat_id, text=f"{prefix}🌐 Шукаю схожі зображення в інтернеті...")
        try:
            image_search_hints = reverse_image_search.find_web_matches(photo)
        except Exception:
            logger.exception("Reverse image search failed, continuing without hints")
            image_search_hints = None
        logger.info(
            "Reverse image search: %d page match(es), %d with fetched content",
            len((image_search_hints or {}).get("page_urls", [])),
            len((image_search_hints or {}).get("page_contents", {})),
        )

        await bot.send_message(chat_id=chat_id, text=f"{prefix}🔍 Розпізнаю товар...")
        data, _search_count = _identify_and_validate([photo], image_search_hints)

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
            # photos, which are almost always better quality/lighting.
            try:
                marketplace_image_urls = product_image_scraper.find_product_image_urls(
                    data["price_source_url"]
                )
            except Exception:
                logger.exception(
                    "Failed to scrape marketplace photos from %s, keeping own photo(s)",
                    data["price_source_url"],
                )
                marketplace_image_urls = []

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
            elif marketplace_image_urls:
                logger.info(
                    "All %d scraped image(s) failed the quality check, keeping own photo(s)",
                    len(marketplace_image_urls),
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


async def _process_batch(bot, chat_id: int, photos: list[bytes]) -> None:
    """Identifies every photo in the batch as its own separate product, then
    combines all of them into a single Prom.ua import - one API call for
    the whole batch instead of one per product, since Prom.ua's import
    endpoint is flaky/rate-limited under repeated back-to-back calls."""
    total = len(photos)
    photo_word = "фото" if total == 1 else f"{total} фото"
    await bot.send_message(
        chat_id=chat_id,
        text=f"📸 Отримано {photo_word}. Кожне фото — окремий товар. Обробляю по черзі...",
    )

    products = []
    identified = []  # (photo, data) pairs, for the sales-group price queue below
    for index, photo in enumerate(photos, start=1):
        result = await _identify_single_product(bot, chat_id, photo, index, total)
        if result is not None:
            product, data = result
            products.append(product)
            identified.append((photo, data))

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
            text="💬 Тепер вкажіть ціни для групи продажів (по одній, у відповідь на запит):",
        )
        await _ask_next_price(bot, chat_id)


async def _ask_next_price(bot, chat_id: int) -> None:
    state = _pending_price_requests.get(chat_id)
    if not state or not state["queue"]:
        _pending_price_requests.pop(chat_id, None)
        return
    _photo, data = state["queue"][0]
    state["awaiting"] = True
    await bot.send_message(
        chat_id=chat_id,
        text=f"💬 Яка ціна для «{data.get('name', '')}» у групі продажів? Напишіть число.",
    )


async def _post_to_sales_group(bot, chat_id: int, photo: bytes, data: dict, price: float) -> None:
    caption = (
        f"{data.get('name', '')}\n\n"
        f"Ціна: {price:.0f} грн\n\n"
        f"Щоб купити, напишіть нашому менеджеру: {config.MANAGER_CONTACT_URL}"
    )
    try:
        await bot.send_photo(chat_id=config.SALES_GROUP_CHAT_ID, photo=photo, caption=caption)
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

    state = _pending_price_requests.get(chat_id)
    if not state or not state.get("awaiting"):
        return

    try:
        price = float(update.message.text.strip().replace(",", "."))
    except ValueError:
        await context.bot.send_message(
            chat_id=chat_id, text="⚠️ Не розпізнав число. Введіть ціну ще раз, наприклад: 450"
        )
        return

    photo, data = state["queue"].pop(0)
    state["awaiting"] = False
    await _post_to_sales_group(context.bot, chat_id, photo, data, price)
    await _ask_next_price(context.bot, chat_id)


async def _flush_batch(context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = context.job.data
    batch = _pending_batches.pop(chat_id, None)
    if not batch:
        return
    logger.info("Flushing batch for chat %s (%d photo(s))", chat_id, len(batch["photos"]))
    await _process_batch(context.bot, chat_id, batch["photos"])


async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not update.message.photo:
        return

    chat_id = update.message.chat.id
    logger.info("Photo received from chat %s", chat_id)

    photo = update.message.photo[-1]
    photo_file = await photo.get_file()
    image_bytes = bytes(await photo_file.download_as_bytearray())

    # Every photo - whether sent alone, as a Telegram album, or as several
    # separate messages - is buffered here as its own product and the batch
    # is processed once no new photo has arrived for BATCH_FLUSH_DELAY.
    batch = _pending_batches.setdefault(chat_id, {"photos": [], "job": None})
    batch["photos"].append(image_bytes)

    if batch["job"] is not None:
        batch["job"].schedule_removal()
    batch["job"] = context.job_queue.run_once(_flush_batch, BATCH_FLUSH_DELAY, data=chat_id)


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
