import os

from dotenv import load_dotenv

load_dotenv()

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "")
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "")
OPENAI_MODEL = os.environ.get("OPENAI_MODEL", "gpt-5.4-mini")

PROM_API_TOKEN = os.environ.get("PROM_API_TOKEN", "")
PROM_API_BASE_URL = "https://my.prom.ua/api/v1"
