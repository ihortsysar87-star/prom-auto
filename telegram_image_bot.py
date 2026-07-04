import os
import logging
import base64
from telegram import Update
from telegram.ext import Application, MessageHandler, filters, ContextTypes
from openai import OpenAI

# Initialize logging
logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)

# Load API keys from environment (safer than hardcoding)
TELEGRAM_TOKEN = os.environ.get('TELEGRAM_TOKEN', '')
OPENAI_API_KEY = os.environ.get('OPENAI_API_KEY', '')

client = OpenAI(api_key=OPENAI_API_KEY)


def detect_image_mime_type(file_path: str) -> str:
    with open(file_path, "rb") as image_file:
        header = image_file.read(12)

    if header.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if header.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if header.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if header.startswith(b"RIFF") and header[8:12] == b"WEBP":
        return "image/webp"

    return "image/jpeg"


async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not update.message.photo:
        return

    # Get the highest-resolution photo
    photo = update.message.photo[-1]
    photo_file = await photo.get_file()
    file_path = f"temp_{photo.file_id}"
    await photo_file.download_to_drive(file_path)

    await update.message.reply_text("Analyzing image... please wait.")

    try:
        # Read and base64-encode the image so it can be sent as a data URL
        with open(file_path, "rb") as image_file:
            image_bytes = image_file.read()
        image_b64 = base64.b64encode(image_bytes).decode("utf-8")
        image_mime_type = detect_image_mime_type(file_path)

        # Call the OpenAI Responses API with a supported vision-capable model.
        response = client.responses.create(
            model="gpt-4o-mini",
            input=[
                {
                    "role": "user",
                    "content": [
                        {"type": "input_text", "text": "Опиши це зображення детально українською мовою."},
                        {"type": "input_image", "image_url": f"data:{image_mime_type};base64,{image_b64}"},
                    ],
                }
            ],
        )

        # Extract text output from the response object (be tolerant of formats)
        description = ""
        if hasattr(response, "output_text") and response.output_text:
            description = response.output_text
        else:
            for item in getattr(response, "output", []):
                for c in item.get("content", []):
                    if c.get("type") == "output_text":
                        description += c.get("text", "")

        if not description:
            description = "No description returned by the model."

        await update.message.reply_text(description)
    except Exception as exc:
        logging.exception("Error while processing image")
        logging.error("OpenAI error details: %s", exc)
        await update.message.reply_text("Sorry, I couldn't process that image.")
    finally:
        # Clean up the local file
        if os.path.exists(file_path):
            os.remove(file_path)


def main() -> None:
    if TELEGRAM_TOKEN in (None, "", "YOUR_TELEGRAM_TOKEN"):
        print("Please set the TELEGRAM_TOKEN environment variable before running the bot.")
        return

    application = Application.builder().token(TELEGRAM_TOKEN).build()

    # Listen for photos
    application.add_handler(MessageHandler(filters.PHOTO, handle_photo))

    # Run the bot
    application.run_polling()


if __name__ == '__main__':
    main()
