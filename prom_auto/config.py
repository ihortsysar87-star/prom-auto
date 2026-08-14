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

# Chat ID of the separate Telegram group products also get posted to, with a
# manually-set price (unlike Prom.ua, which gets an AI-estimated price).
# Negative for groups/supergroups. Get it by adding the bot to the group as
# admin and sending any message there - the bot logs every chat ID it sees.
SALES_GROUP_CHAT_ID = os.environ.get("SALES_GROUP_CHAT_ID", "")
MANAGER_CONTACT_URL = os.environ.get("MANAGER_CONTACT_URL", "")

# OLX.ua API (OAuth2 - see olx_auth_setup.py for the one-time login that
# produces OLX_REFRESH_TOKEN). client_id/client_secret come from OLX's App
# Manager (https://developer.olx.ua/ua/profile/applications); posting adverts
# requires a user-context refresh_token, not just the static app credentials.
OLX_CLIENT_ID = os.environ.get("OLX_CLIENT_ID", "")
OLX_CLIENT_SECRET = os.environ.get("OLX_CLIENT_SECRET", "")
OLX_REFRESH_TOKEN = os.environ.get("OLX_REFRESH_TOKEN", "")
OLX_API_BASE_URL = "https://www.olx.ua/api/partner"
OLX_TOKEN_URL = "https://www.olx.ua/api/open/oauth/token"
OLX_AUTHORIZE_URL = "https://www.olx.ua/oauth/authorize"
OLX_ADVERTISER_TYPE = os.environ.get("OLX_ADVERTISER_TYPE", "private")
OLX_CONTACT_NAME = os.environ.get("OLX_CONTACT_NAME", "")
OLX_CONTACT_PHONE = os.environ.get("OLX_CONTACT_PHONE", "")
OLX_CITY_ID = os.environ.get("OLX_CITY_ID", "")
OLX_DISTRICT_ID = os.environ.get("OLX_DISTRICT_ID", "")
OLX_CONDITION = os.environ.get("OLX_CONDITION", "new")

# Rozetka's own feed-validation report (seller.rozetka.com.ua) - not a Prom.ua
# or Rozetka API, just the hash tied to your account's validation page, used
# by rozetka_backfill.py to find products it flagged as missing vendor/
# characteristics.
ROZETKA_VALIDATE_HASH = os.environ.get("ROZETKA_VALIDATE_HASH", "")

# Rozetka Marketplace's own direct seller API (api-seller.rozetka.com.ua) -
# a completely separate account/credentials from PROM_API_TOKEN above. Only
# needed for rozetka_client.py, which can fix vendor/characteristics
# directly on Rozetka instead of going through a Prom.ua re-import.
# ROZETKA_SELLER_ACCESS_TOKEN is a token generated directly in the seller
# panel; if set, rozetka_client.py uses it as-is and skips the
# username/password login below entirely.
ROZETKA_SELLER_ACCESS_TOKEN = os.environ.get("ROZETKA_SELLER_ACCESS_TOKEN", "")
ROZETKA_SELLER_USERNAME = os.environ.get("ROZETKA_SELLER_USERNAME", "")
ROZETKA_SELLER_PASSWORD = os.environ.get("ROZETKA_SELLER_PASSWORD", "")
ROZETKA_SELLER_API_BASE_URL = "https://api-seller.rozetka.com.ua"
