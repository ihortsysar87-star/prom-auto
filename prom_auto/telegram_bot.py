import logging

import requests
from telegram import Update
from telegram.ext import Application, ContextTypes, MessageHandler, filters

from . import config, openai_client, prom_client, product_mapper, xlsx_builder

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logger = logging.getLogger(__name__)


def _resolve_image_url(candidate, fallback: str) -> str:
    """Prefers the real product photo found by web search; falls back to
    the user's own Telegram upload if search found nothing or the
    candidate URL doesn't actually resolve to an image."""
    if not candidate:
        return fallback
    try:
        head = requests.head(candidate, timeout=5, allow_redirects=True)
        if head.ok and head.headers.get("Content-Type", "").startswith("image/"):
            return candidate
    except requests.RequestException:
        logger.warning("Web-searched image URL failed to resolve: %s", candidate)
    return fallback


async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Equivalent of the n8n chain: Telegram Trigger -> Get a file ->
    HTTP Request api.telegram -> Extract from File -> Aggregate ->
    Parse raw data2 -> HTTP Request api.openai -> parse to prom api ->
    Convert to File -> HTTP Request to prom -> Send a text message1."""
    if not update.message or not update.message.photo:
        return

    photo = update.message.photo[-1]
    photo_file = await photo.get_file()
    image_bytes = await photo_file.download_as_bytearray()
    telegram_image_url = photo_file.file_path

    try:
        response = openai_client.identify_product([bytes(image_bytes)])
        data = openai_client.extract_json(response)
        image_url = _resolve_image_url(
            openai_client.extract_image_url(response), telegram_image_url
        )

        product = product_mapper.build_prom_product(data, image_url)
        xlsx_bytes = xlsx_builder.build_xlsx([product])

        result = prom_client.import_file(xlsx_bytes)

        if result.get("status") == "success" or "id" in result:
            await update.message.reply_text("Дані успішно передані в Пром")
        else:
            await update.message.reply_text(
                f"Помилка передачі на Пром {result.get('error', '')}"
            )
    except Exception as exc:
        logger.exception("Failed to process product photo")
        await update.message.reply_text(f"Помилка передачі на Пром {exc}")


def main() -> None:
    if not config.TELEGRAM_TOKEN:
        print("Please set the TELEGRAM_TOKEN environment variable before running the bot.")
        return

    application = Application.builder().token(config.TELEGRAM_TOKEN).build()
    application.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    application.run_polling()


if __name__ == "__main__":
    main()
