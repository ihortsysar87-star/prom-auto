import logging

from telegram import Update
from telegram.ext import Application, ContextTypes, MessageHandler, filters

from . import (
    config,
    image_host,
    openai_client,
    prom_client,
    product_mapper,
    reverse_image_search,
    xlsx_builder,
)

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logger = logging.getLogger(__name__)

ALBUM_FLUSH_DELAY = 1.5  # seconds to wait for the rest of a Telegram album

_pending_albums: dict[str, dict] = {}

_FIELD_LABELS = {
    "name": "назва",
    "brand": "бренд",
    "manufacturer": "виробник",
    "country": "країна",
    "material": "матеріал",
    "color": "колір",
}


def _format_data_sources(data_sources: dict) -> str:
    """Groups fields by whether they came from reading the photo, an
    actual web match, or the model's own estimate, so it's clear what's
    verified vs. just read off the packaging."""
    groups: dict[str, list[str]] = {"web": [], "photo": [], "estimated": []}
    for field, source in (data_sources or {}).items():
        label = _FIELD_LABELS.get(field, field)
        groups.setdefault(source, []).append(label)

    parts = []
    if groups["web"]:
        parts.append("з пошуку: " + ", ".join(groups["web"]))
    if groups["photo"]:
        parts.append("з фото: " + ", ".join(groups["photo"]))
    if groups["estimated"]:
        parts.append("орієнтовно: " + ", ".join(groups["estimated"]))
    return "ℹ️ Джерела даних — " + "; ".join(parts) if parts else ""


async def _process_photos(bot, chat_id: int, photos: list[bytes]) -> None:
    """Equivalent of the n8n chain: Telegram Trigger -> Get a file ->
    HTTP Request api.telegram -> Extract from File -> Aggregate ->
    Parse raw data2 -> HTTP Request api.openai -> parse to prom api ->
    Convert to File -> HTTP Request to prom -> Send a text message1."""
    photo_word = "фото" if len(photos) == 1 else f"{len(photos)} фото"
    await bot.send_message(chat_id=chat_id, text=f"📸 Отримано {photo_word}. Завантажую фото...")

    try:
        # Re-host on imgbb: Telegram's own file links can 404 by the time
        # Prom.ua's async import gets around to fetching them.
        hosted_urls = [image_host.upload_image(image_bytes) for image_bytes in photos]
        image_url = ";".join(hosted_urls)

        await bot.send_message(chat_id=chat_id, text="🌐 Шукаю схожі зображення в інтернеті...")
        try:
            image_search_hints = reverse_image_search.find_web_matches(photos[0])
        except Exception:
            logger.exception("Reverse image search failed, continuing without hints")
            image_search_hints = None
        logger.info("Reverse image search hints: %s", image_search_hints)

        await bot.send_message(chat_id=chat_id, text="🔍 Розпізнаю товар...")
        response = openai_client.identify_product(photos, image_search_hints=image_search_hints)
        data = openai_client.extract_json(response)
        logger.info("Full identification data: %s", data)
        await bot.send_message(
            chat_id=chat_id,
            text=f"🔍 Товар розпізнано: «{data.get('name', '')}». Формую картку для Prom.ua...",
        )
        sources_text = _format_data_sources(data.get("data_sources"))
        if sources_text:
            await bot.send_message(chat_id=chat_id, text=sources_text)
        if not data.get("price_found"):
            await bot.send_message(
                chat_id=chat_id,
                text=f"⚠️ Точну ціну не знайдено, встановлено орієнтовну: {data.get('priceUAH')} грн. Перевірте вручну.",
            )

        product = product_mapper.build_prom_product(data, image_url)
        xlsx_bytes = xlsx_builder.build_xlsx([product])

        await bot.send_message(chat_id=chat_id, text="⬆️ Завантажую до Prom.ua...")
        result = prom_client.import_file(xlsx_bytes)
        logger.info("Prom.ua import result: %s", result)

        if result.get("status") == "success" or "id" in result:
            await bot.send_message(chat_id=chat_id, text="✅ Дані успішно передані в Пром")
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
        logger.exception("Failed to process product photo(s)")
        await bot.send_message(chat_id=chat_id, text=f"❌ Помилка обробки фото: {exc}")


async def _flush_album(context: ContextTypes.DEFAULT_TYPE) -> None:
    album_id = context.job.data
    album = _pending_albums.pop(album_id, None)
    if not album:
        return
    logger.info("Flushing album %s (%d photo(s))", album_id, len(album["photos"]))
    await _process_photos(context.bot, album["chat_id"], album["photos"])


async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not update.message.photo:
        return

    logger.info("Photo received from chat %s", update.message.chat.id)

    photo = update.message.photo[-1]
    photo_file = await photo.get_file()
    image_bytes = bytes(await photo_file.download_as_bytearray())

    album_id = update.message.media_group_id
    if not album_id:
        await _process_photos(context.bot, update.message.chat.id, [image_bytes])
        return

    # Telegram sends each album photo as a separate update, so buffer them
    # and process the whole album once no new photo has arrived for a bit.
    album = _pending_albums.setdefault(
        album_id, {"photos": [], "chat_id": update.message.chat.id, "job": None}
    )
    album["photos"].append(image_bytes)

    if album["job"] is not None:
        album["job"].schedule_removal()
    album["job"] = context.job_queue.run_once(_flush_album, ALBUM_FLUSH_DELAY, data=album_id)


def main() -> None:
    if not config.TELEGRAM_TOKEN:
        print("Please set the TELEGRAM_TOKEN environment variable before running the bot.")
        return

    application = Application.builder().token(config.TELEGRAM_TOKEN).build()
    application.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    application.run_polling()


if __name__ == "__main__":
    main()
