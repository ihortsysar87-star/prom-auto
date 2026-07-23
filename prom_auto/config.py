import os

from dotenv import load_dotenv

load_dotenv()

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "")
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "")
OPENAI_MODEL = os.environ.get("OPENAI_MODEL", "gpt-5.4")

PROM_API_TOKEN = os.environ.get("PROM_API_TOKEN", "")
PROM_API_BASE_URL = "https://my.prom.ua/api/v1"
PROM_REGION = os.environ.get("PROM_REGION", "Київ")

IMGBB_API_KEY = os.environ.get("IMGBB_API_KEY", "")

GOOGLE_VISION_API_KEY = os.environ.get("GOOGLE_VISION_API_KEY", "")

# Chat ID of the separate Telegram group products also get posted to, with a
# manually-set price (unlike Prom.ua, which gets an AI-estimated price).
# Negative for groups/supergroups. Get it by adding the bot to the group as
# admin and sending any message there - the bot logs every chat ID it sees.
SALES_GROUP_CHAT_ID = os.environ.get("SALES_GROUP_CHAT_ID", "")
MANAGER_CONTACT_URL = os.environ.get("MANAGER_CONTACT_URL", "")
